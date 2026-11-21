"""统一结构化 JSON 日志与可选 Langfuse 可观测钩子模块。

核心特性：
1. 基于协程安全 `ContextVar` 自动在日志记录中绑定 `trace_id`、`user_id`、`role`、`tenant_id`；
2. 提供单行 UTF-8 结构化 `StructuredJsonFormatter` 与幂等配置入口 `configure_structured_logging`；
3. 提供按需启用的 Langfuse 回调构造器（仅当同时配置了 `LANGFUSE_PUBLIC_KEY` 与 `LANGFUSE_SECRET_KEY`
   时启用；未配置时零报错、零依赖导入静默跳过）。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from config.settings import AppSettings, get_settings
from schemas.context import ExecutionContext
from schemas.contracts import ErrorPayload, ToolCall, ToolResultMessage

_OBSERVABILITY_CTX_VAR: ContextVar[dict[str, str]] = ContextVar(
    "_ecommerce_observability_ctx",
    default={},
)

_STANDARD_LOG_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        "trace_id",
        "user_id",
        "role",
        "tenant_id",
        "event",
        "extra_fields",
    }
)


def get_bound_observability_context() -> dict[str, str]:
    """获取当前异步协程上下文绑定的可观测身份标签副本。"""
    return dict(_OBSERVABILITY_CTX_VAR.get())


@contextmanager
def bind_observability_context(
    context: ExecutionContext | None = None,
    *,
    trace_id: str | None = None,
    user_id: str | None = None,
    role: str | None = None,
    tenant_id: str | None = None,
) -> Iterator[dict[str, str]]:
    """在当前请求或工具调用作用域内绑定 `trace_id`、`user_id`、`role`、`tenant_id`。"""
    merged = dict(_OBSERVABILITY_CTX_VAR.get())
    if context is not None:
        merged["trace_id"] = context.trace_id
        merged["user_id"] = context.user_id
        merged["role"] = context.role
        merged["tenant_id"] = context.tenant_id
    if trace_id is not None:
        merged["trace_id"] = trace_id
    if user_id is not None:
        merged["user_id"] = user_id
    if role is not None:
        merged["role"] = role
    if tenant_id is not None:
        merged["tenant_id"] = tenant_id

    token = _OBSERVABILITY_CTX_VAR.set(merged)
    try:
        yield merged
    finally:
        _OBSERVABILITY_CTX_VAR.reset(token)


class ObservabilityContextFilter(logging.Filter):
    """自动将当前协程绑定的 `trace_id`、`user_id`、`role`、`tenant_id` 注入 `LogRecord`。"""

    def filter(self, record: logging.LogRecord) -> bool:
        bound = _OBSERVABILITY_CTX_VAR.get()
        for key in ("trace_id", "user_id", "role", "tenant_id"):
            existing = getattr(record, key, None)
            if existing is None:
                setattr(record, key, bound.get(key))
        return True


class StructuredJsonFormatter(logging.Formatter):
    """统一单行 UTF-8 JSON 日志格式化器。"""

    def format(self, record: logging.LogRecord) -> str:
        bound = _OBSERVABILITY_CTX_VAR.get()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or bound.get("trace_id"),
            "user_id": getattr(record, "user_id", None) or bound.get("user_id"),
            "role": getattr(record, "role", None) or bound.get("role"),
            "tenant_id": getattr(record, "tenant_id", None) or bound.get("tenant_id"),
        }

        event_name = getattr(record, "event", None)
        if event_name is not None:
            payload["event"] = event_name

        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, Mapping):
            for k, v in extra_fields.items():
                if k not in payload:
                    payload[str(k)] = v

        for attr_key, attr_val in record.__dict__.items():
            if attr_key not in _STANDARD_LOG_ATTRS and not attr_key.startswith("_"):
                payload.setdefault(attr_key, attr_val)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_structured_logging(
    level: str | int | None = None,
    *,
    logger_name: str = "ecommerce_agent",
    stream: Any = None,
    force_replace_handlers: bool = False,
) -> logging.Logger:
    """为指定命名空间配置统一结构化 JSON 日志处理器（支持幂等重复调用）。"""
    resolved_level = level if level is not None else get_settings().LOG_LEVEL
    if isinstance(resolved_level, str):
        numeric_level = getattr(logging, resolved_level.strip().upper(), logging.INFO)
    else:
        numeric_level = int(resolved_level)

    target_logger = logging.getLogger(logger_name)
    target_logger.setLevel(numeric_level)
    target_logger.propagate = False

    if force_replace_handlers:
        target_logger.handlers.clear()

    has_structured_handler = any(
        isinstance(getattr(h, "formatter", None), StructuredJsonFormatter)
        for h in target_logger.handlers
    )
    if not has_structured_handler or stream is not None:
        if stream is not None:
            target_logger.handlers = [
                h
                for h in target_logger.handlers
                if not isinstance(getattr(h, "formatter", None), StructuredJsonFormatter)
            ]
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setLevel(numeric_level)
        handler.addFilter(ObservabilityContextFilter())
        handler.setFormatter(StructuredJsonFormatter())
        target_logger.addHandler(handler)

    return target_logger


def get_structured_logger(name: str = "ecommerce_agent") -> logging.Logger:
    """获取挂载在 `ecommerce_agent` 结构化 JSON 命名空间下的子 Logger。"""
    configure_structured_logging()
    if name == "ecommerce_agent" or name.startswith("ecommerce_agent."):
        return logging.getLogger(name)
    return logging.getLogger(f"ecommerce_agent.{name}")


def log_structured_event(
    logger: logging.Logger,
    event: str,
    message: str | None = None,
    *,
    context: ExecutionContext | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """输出自动携带可信身份标签的结构化业务/审计事件日志。"""
    extra: dict[str, Any] = {
        "event": event,
        "extra_fields": fields,
    }
    if context is not None:
        extra["trace_id"] = context.trace_id
        extra["user_id"] = context.user_id
        extra["role"] = context.role
        extra["tenant_id"] = context.tenant_id

    logger.log(level, message or event, extra=extra)


def is_langfuse_enabled(app_settings: AppSettings | None = None) -> bool:
    """检查当前环境是否已配置完整 Langfuse 公私钥。"""
    cfg = app_settings or get_settings()
    return cfg.is_langfuse_configured


def build_langfuse_callback_handler(
    app_settings: AppSettings | None = None,
    *,
    context: ExecutionContext | None = None,
    session_id: str | None = None,
) -> Any | None:
    """按需构造 Langfuse 回调处理器；未配置密钥或未安装 SDK 时零报错静默返回 `None`。"""
    cfg = app_settings or get_settings()
    if not cfg.is_langfuse_configured:
        return None

    bound = _OBSERVABILITY_CTX_VAR.get()
    trace_id = context.trace_id if context is not None else bound.get("trace_id")
    user_id = context.user_id if context is not None else bound.get("user_id")
    role = context.role if context is not None else bound.get("role")
    tenant_id = context.tenant_id if context is not None else bound.get("tenant_id")

    handler_cls: Any = None
    for module_path in ("langfuse.langchain", "langfuse.callback"):
        try:
            mod = __import__(module_path, fromlist=["CallbackHandler"])
            handler_cls = getattr(mod, "CallbackHandler", None)
            if handler_cls is not None:
                break
        except ImportError:
            continue

    if handler_cls is None:
        get_structured_logger("observability").warning(
            "检测到 LANGFUSE 密钥配置但当前环境未安装 langfuse SDK，已静默跳过远程追踪",
            extra={"event": "langfuse_sdk_missing"},
        )
        return None

    init_kwargs: dict[str, Any] = {
        "public_key": cfg.LANGFUSE_PUBLIC_KEY,
        "secret_key": cfg.LANGFUSE_SECRET_KEY,
        "host": cfg.LANGFUSE_HOST,
    }
    if user_id:
        init_kwargs["user_id"] = user_id
    if session_id:
        init_kwargs["session_id"] = session_id
    metadata = {
        k: v
        for k, v in {
            "trace_id": trace_id,
            "role": role,
            "tenant_id": tenant_id,
        }.items()
        if v is not None
    }
    if metadata:
        init_kwargs["metadata"] = metadata

    try:
        return handler_cls(**init_kwargs)
    except TypeError:
        try:
            return handler_cls(
                public_key=cfg.LANGFUSE_PUBLIC_KEY,
                secret_key=cfg.LANGFUSE_SECRET_KEY,
                host=cfg.LANGFUSE_HOST,
            )
        except Exception as exc:
            get_structured_logger("observability").warning(
                f"Langfuse CallbackHandler 初始化失败，已静默降级跳过: {exc}",
                extra={"event": "langfuse_init_failed"},
            )
            return None
    except Exception as exc:
        get_structured_logger("observability").warning(
            f"Langfuse CallbackHandler 初始化失败，已静默降级跳过: {exc}",
            extra={"event": "langfuse_init_failed"},
        )
        return None


class LangfuseToolObservabilityHook:
    """面向 `ToolRuntime` 的可选 Langfuse 追踪钩子（未启用时绝不抛错）。"""

    def __init__(self, langfuse_client: Any) -> None:
        self._client = langfuse_client
        self._active_spans: dict[tuple[str, str], Any] = {}

    async def before_tool_call(
        self, tool_call: ToolCall, context: ExecutionContext
    ) -> None:
        try:
            span_fn = getattr(self._client, "span", None)
            if callable(span_fn):
                span = span_fn(
                    name=f"tool:{tool_call.name}",
                    trace_id=context.trace_id,
                    metadata={
                        "tool_call_id": tool_call.tool_call_id,
                        "tenant_id": context.tenant_id,
                        "user_id": context.user_id,
                        "role": context.role,
                    },
                )
                self._active_spans[(context.trace_id, tool_call.tool_call_id)] = span
        except Exception:
            return

    async def on_tool_error(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        error: ErrorPayload,
        duration_ms: float,
    ) -> None:
        return

    async def after_tool_call(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        result: ToolResultMessage,
        duration_ms: float,
    ) -> None:
        span = self._active_spans.pop((context.trace_id, tool_call.tool_call_id), None)
        if span is None:
            return
        try:
            end_fn = getattr(span, "end", None)
            if callable(end_fn):
                end_fn(
                    status_message=result.status,
                    metadata={
                        "status": result.status,
                        "duration_ms": round(duration_ms, 3),
                        "error_code": result.error.code if result.error else None,
                    },
                )
        except Exception:
            return


def create_langfuse_tool_hook(
    app_settings: AppSettings | None = None,
) -> LangfuseToolObservabilityHook | None:
    """当配置了 Langfuse Key 且 SDK 可用时创建工具级追踪 Hook；未配置时静默返回 `None`。"""
    cfg = app_settings or get_settings()
    if not cfg.is_langfuse_configured:
        return None
    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]

        client = Langfuse(
            public_key=cfg.LANGFUSE_PUBLIC_KEY,
            secret_key=cfg.LANGFUSE_SECRET_KEY,
            host=cfg.LANGFUSE_HOST,
        )
        return LangfuseToolObservabilityHook(client)
    except Exception:
        return None


__all__ = [
    "LangfuseToolObservabilityHook",
    "ObservabilityContextFilter",
    "StructuredJsonFormatter",
    "bind_observability_context",
    "build_langfuse_callback_handler",
    "configure_structured_logging",
    "create_langfuse_tool_hook",
    "get_bound_observability_context",
    "get_structured_logger",
    "is_langfuse_enabled",
    "log_structured_event",
]
