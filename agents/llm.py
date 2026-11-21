"""统一异步大模型抽象、带超时/Fallback 降级的 LiteLLM 客户端与纯路由工厂。

职责边界：
1. 定义编排层与大模型交互的标准不可变消息/响应契约（`LLMMessage` / `LLMResponse`）与 `AsyncAgentLLM` 抽象基类；
2. 提供基于 `litellm.acompletion` 的异步客户端 `LiteLLMModel`，内置请求超时、瞬态重试、备选模型链式 Fallback
   及可选本地兜底推理器降级；
3. 提供纯配置驱动的路由工厂 `build_configured_llm` / `create_llm_client`：严格依据 `config.settings` 的
   `LLM_MODE` 返回确定性 Mock 实例或生产 LiteLLM 异步客户端，不包含任何具体业务门店话术或规则硬编码。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from config.settings import AppSettings, get_settings
from schemas.contracts import ToolCall

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMMessage(BaseModel):
    """编排层与大模型交互的标准不可变消息契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai_dict(self) -> dict[str, Any]:
        """转换为 OpenAI / LiteLLM `acompletion` 标准消息字典。"""
        payload: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.name:
            payload["name"] = self.name
        if self.role == "assistant" and self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.role == "tool" and self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload


class LLMResponse(BaseModel):
    """大模型单轮异步推理标准返回契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    require_user_confirmation: bool = False
    confirmation_prompt: str | None = None
    pending_confirmation_call: ToolCall | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True, kw_only=True)
class RecordedLLMCall:
    """单次模型调用的只读审计快照（用于测试验证上下文隔离与最小工具集暴露）。"""

    agent_name: str
    messages: tuple[LLMMessage, ...]
    exposed_tool_names: tuple[str, ...]
    response_schema_name: str | None
    response: LLMResponse


def snapshot_schemas_to_openai_tools(
    tool_schemas: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """将 `ToolSnapshot.to_llm_schemas()` 转换为 LiteLLM / OpenAI 标准 `tools` 列表。"""
    if not tool_schemas:
        return []
    formatted: list[dict[str, Any]] = []
    for schema in tool_schemas:
        formatted.append(
            {
                "type": "function",
                "function": {
                    "name": str(schema["name"]),
                    "description": str(schema.get("description", "")),
                    "parameters": dict(schema.get("input_schema", {"type": "object"})),
                },
            }
        )
    return formatted


class AsyncAgentLLM(ABC):
    """编排层异步大模型统一抽象基类。"""

    def __init__(self) -> None:
        self._recorded_calls: list[RecordedLLMCall] = []

    @property
    def recorded_calls(self) -> tuple[RecordedLLMCall, ...]:
        """返回按时间顺序记录的全部模型调用快照。"""
        return tuple(self._recorded_calls)

    def get_calls_for(self, agent_name: str) -> tuple[RecordedLLMCall, ...]:
        """按子 Agent 名称过滤模型调用记录。"""
        return tuple(c for c in self._recorded_calls if c.agent_name == agent_name)

    def clear_recorded_calls(self) -> None:
        """清空历史调用记录。"""
        self._recorded_calls.clear()

    async def ainvoke(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        agent_name: str = "agent",
    ) -> LLMResponse:
        """执行单轮异步模型生成并记录暴露的工具集与输入消息快照。"""
        msg_tuple = tuple(messages)
        tool_list = list(tools) if tools is not None else None
        exposed_names = tuple(str(t["name"]) for t in (tool_list or []))

        response = await self._agenerate(
            msg_tuple,
            tools=tool_list,
            response_schema=response_schema,
            agent_name=agent_name,
        )
        self._recorded_calls.append(
            RecordedLLMCall(
                agent_name=agent_name,
                messages=msg_tuple,
                exposed_tool_names=exposed_names,
                response_schema_name=(
                    response_schema.__name__ if response_schema is not None else None
                ),
                response=response,
            )
        )
        return response

    async def ainvoke_structured(
        self,
        messages: Sequence[LLMMessage],
        schema: type[ModelT],
        *,
        agent_name: str = "agent",
    ) -> ModelT:
        """执行异步结构化生成并通过 Pydantic v2 强校验返回目标契约实例。"""
        resp = await self.ainvoke(
            messages,
            tools=None,
            response_schema=schema,
            agent_name=agent_name,
        )
        if resp.structured_output is not None:
            return schema.model_validate(resp.structured_output)
        cleaned = resp.content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        parsed = json.loads(cleaned)
        return schema.model_validate(parsed)

    @abstractmethod
    async def _agenerate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None,
        response_schema: type[BaseModel] | None,
        agent_name: str,
    ) -> LLMResponse:
        """子类实现的底层异步生成逻辑。"""


class LiteLLMModel(AsyncAgentLLM):
    """基于 `litellm.acompletion` 的异步多模型客户端（支持超时控制与多级 Fallback 降级）。"""

    def __init__(
        self,
        model: str | None = None,
        *,
        temperature: float = 0.0,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        fallback_models: Sequence[str] | None = None,
        fallback_llm: AsyncAgentLLM | None = None,
        extra_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        cfg = get_settings()
        self.model = model or cfg.LLM_MODEL
        self.temperature = temperature
        self.api_key = api_key if api_key is not None else cfg.LLM_API_KEY
        self.api_base = api_base if api_base is not None else cfg.LLM_API_BASE
        self.timeout = timeout if timeout is not None else cfg.LLM_TIMEOUT_SECONDS
        self.max_retries = (
            max_retries if max_retries is not None else cfg.LLM_MAX_RETRIES
        )
        self.fallback_models: tuple[str, ...] = tuple(
            m.strip()
            for m in (
                fallback_models
                if fallback_models is not None
                else cfg.LLM_FALLBACK_MODELS
            )
            if m and m.strip() and m.strip() != self.model
        )
        self.fallback_llm = fallback_llm
        self.extra_kwargs = dict(extra_kwargs or {})

    async def _agenerate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None,
        response_schema: type[BaseModel] | None,
        agent_name: str,
    ) -> LLMResponse:
        import litellm

        openai_messages = [msg.to_openai_dict() for msg in messages]
        base_kwargs: dict[str, Any] = {
            "messages": openai_messages,
            "temperature": self.temperature,
            "timeout": self.timeout,
            **self.extra_kwargs,
        }
        if self.max_retries > 0 and "num_retries" not in base_kwargs:
            base_kwargs["num_retries"] = self.max_retries
        if self.api_key is not None:
            base_kwargs["api_key"] = self.api_key
        if self.api_base is not None:
            base_kwargs["api_base"] = self.api_base

        formatted_tools = snapshot_schemas_to_openai_tools(tools)
        if formatted_tools:
            base_kwargs["tools"] = formatted_tools
            base_kwargs["tool_choice"] = "auto"

        if response_schema is not None:
            base_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "schema": response_schema.model_json_schema(),
                },
            }

        candidate_models = (self.model, *self.fallback_models)
        last_error: BaseException | None = None
        raw_resp: Any = None

        for idx, target_model in enumerate(candidate_models):
            try:
                raw_resp = await litellm.acompletion(
                    model=target_model,
                    **base_kwargs,
                )
                break
            except Exception as exc:
                last_error = exc
                if idx + 1 < len(candidate_models):
                    logger.warning(
                        "LiteLLM 模型 '%s' 调用失败 (%s)，自动降级尝试备选模型 '%s'",
                        target_model,
                        exc,
                        candidate_models[idx + 1],
                    )

        if raw_resp is None:
            if self.fallback_llm is not None:
                logger.warning(
                    "LiteLLM 远程模型链 %s 全部不可用 (%s)，降级切换至本地兜底推理器 %s",
                    candidate_models,
                    last_error,
                    type(self.fallback_llm).__name__,
                )
                return await self.fallback_llm._agenerate(
                    messages,
                    tools=tools,
                    response_schema=response_schema,
                    agent_name=agent_name,
                )
            assert last_error is not None
            raise last_error

        choice_msg = raw_resp.choices[0].message
        content = str(getattr(choice_msg, "content", None) or "")

        parsed_tool_calls: list[ToolCall] = []
        raw_tcs = getattr(choice_msg, "tool_calls", None) or []
        for tc in raw_tcs:
            fn = getattr(tc, "function", None)
            raw_args = getattr(fn, "arguments", "{}") if fn is not None else "{}"
            args_dict = (
                json.loads(raw_args)
                if isinstance(raw_args, str) and raw_args.strip()
                else (dict(raw_args) if isinstance(raw_args, Mapping) else {})
            )
            parsed_tool_calls.append(
                ToolCall(
                    tool_call_id=str(
                        getattr(tc, "id", None) or f"tc_{uuid.uuid4().hex[:10]}"
                    ),
                    name=str(getattr(fn, "name", "")),
                    arguments=args_dict,
                )
            )

        structured_dict: dict[str, Any] | None = None
        if response_schema is not None and content.strip():
            try:
                candidate = json.loads(content)
                if isinstance(candidate, dict):
                    structured_dict = candidate
            except json.JSONDecodeError:
                structured_dict = None

        usage_obj = getattr(raw_resp, "usage", None)
        usage_dict: dict[str, int] | None = None
        if usage_obj is not None:
            usage_dict = {
                "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                "completion_tokens": int(
                    getattr(usage_obj, "completion_tokens", 0) or 0
                ),
                "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
            }

        return LLMResponse(
            content=content,
            tool_calls=parsed_tool_calls,
            structured_output=structured_dict,
            usage=usage_dict,
        )


def build_configured_llm(
    app_settings: AppSettings | None = None,
) -> tuple[AsyncAgentLLM, str]:
    """纯粹的 LLM 路由工厂：根据 `settings.LLM_MODE` 返回确定性 Mock 实例或带超时与 Fallback 的 LiteLLM 客户端。"""
    cfg = app_settings or get_settings()

    if cfg.LLM_MODE == "litellm":
        fallback_llm: AsyncAgentLLM | None = None
        if cfg.LLM_FALLBACK_TO_MOCK:
            from agents.mock_llm import DeterministicEcommerceMockLLM

            fallback_llm = DeterministicEcommerceMockLLM(
                require_address_confirmation=cfg.REQUIRE_ADDRESS_HITL,
            )
        client = LiteLLMModel(
            model=cfg.LLM_MODEL,
            temperature=0.0,
            api_key=cfg.LLM_API_KEY,
            api_base=cfg.LLM_API_BASE,
            timeout=cfg.LLM_TIMEOUT_SECONDS,
            max_retries=cfg.LLM_MAX_RETRIES,
            fallback_models=cfg.LLM_FALLBACK_MODELS,
            fallback_llm=fallback_llm,
        )
        return client, f"litellm:{cfg.LLM_MODEL}"

    from agents.mock_llm import DeterministicEcommerceMockLLM

    mock_client = DeterministicEcommerceMockLLM(
        require_address_confirmation=cfg.REQUIRE_ADDRESS_HITL,
    )
    return mock_client, "mock:DeterministicEcommerceMockLLM"


def create_llm_client(app_settings: AppSettings | None = None) -> AsyncAgentLLM:
    """按统一配置中心 `LLM_MODE` 创建异步大模型实例。"""
    llm_client, _ = build_configured_llm(app_settings)
    return llm_client


__all__ = [
    "AsyncAgentLLM",
    "LLMMessage",
    "LLMResponse",
    "LiteLLMModel",
    "RecordedLLMCall",
    "build_configured_llm",
    "create_llm_client",
    "snapshot_schemas_to_openai_tools",
]
