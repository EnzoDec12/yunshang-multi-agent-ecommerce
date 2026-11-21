"""Tool Runtime 核心执行器（前后置必检闭环、生命周期 Hook、隐私脱敏与并发保序）。

前置五步必检闭环（严格按序）：
1. 工具是否存在于当前 `ToolSnapshot`（不存在 -> `permission_error`）；
2. `Input Schema` Pydantic v2 强校验（含 `extra='forbid'` 拦截伪造参数 -> `validation_error`）；
3. RBAC 权限与双域角色边界校验（无权限或跨 C/B 域越权 -> `permission_error`）；
4. 风险分级与 HITL 审批阻断（`risk == 'high'` 且 `ExecutionContext` 无有效审批 -> `HITLRequired` 挂起，不执行 handler）；
5. 下游依赖可用性检查（依赖不可用 -> `transport_error`）。

后置三步必检闭环（严格按序）：
1. `Output Schema` 强校验裁剪（剔除多余内部字段）与敏感隐私字段脱敏过滤；
2. 结构化审计日志记录（含 `trace_id`、`tool_call_id`、`tenant_id`、`user_id`、工具名、耗时与状态）；
3. 强制保留并透传原始 `tool_call_id`（`execute_batch()` 异步乱序完成仍严格对齐不串台）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from infra.audit_store import AuditStore, InMemoryAuditStore
from infra.observability import (
    bind_observability_context,
    get_structured_logger,
    log_structured_event,
)
from runtime.registry import ToolDefinition, ToolSnapshot
from schemas.context import ExecutionContext
from schemas.contracts import (
    AuditLogEntry,
    ErrorPayload,
    HITLInterruptError,
    HITLRequired,
    RiskLevel,
    ToolBusinessError,
    ToolCall,
    ToolExecutionStatus,
    ToolPermissionError,
    ToolResultMessage,
    ToolTransportError,
    ToolValidationError,
)

audit_logger = get_structured_logger("runtime.audit")

DependencyChecker = Callable[[str, str], Awaitable[bool] | bool]
AuditSink = Callable[[AuditLogEntry], None]

_CN_MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_CN_ID_CARD_RE = re.compile(r"(?<!\d)(\d{4})\d{10}(\d{3}[\dXx])(?!\d)")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

_PHONE_KEYS: frozenset[str] = frozenset(
    {
        "phone",
        "mobile",
        "tel",
        "telephone",
        "buyer_phone",
        "receiver_phone",
        "contact_phone",
        "customer_phone",
    }
)
_ID_CARD_KEYS: frozenset[str] = frozenset(
    {"id_card", "id_number", "identity_no", "cert_no"}
)
_EMAIL_KEYS: frozenset[str] = frozenset(
    {"email", "buyer_email", "customer_email", "contact_email"}
)
_NAME_KEYS: frozenset[str] = frozenset(
    {"buyer_name", "receiver_name", "consignee", "recipient_name", "real_name"}
)
_ADDRESS_KEYS: frozenset[str] = frozenset(
    {"address", "shipping_address", "receiver_address", "detail_address"}
)
_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "client_secret",
        "api_key",
        "access_token",
        "refresh_token",
        "bank_card",
        "card_no",
        "cvv",
    }
)


def compute_arguments_digest(canonical_args: Mapping[str, Any]) -> str:
    """计算规范化入参字典的确定性 SHA-256 摘要，用于高风险 HITL 审批防篡改绑定。"""
    serialized = json.dumps(
        canonical_args,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _mask_phone_value(text: str) -> str:
    masked = _CN_MOBILE_RE.sub(r"\1****\2", text)
    if masked != text:
        return masked
    digits = text.strip()
    if len(digits) >= 7:
        return f"{digits[:3]}****{digits[-4:]}"
    return "****"


def _mask_id_card_value(text: str) -> str:
    masked = _CN_ID_CARD_RE.sub(r"\1**********\2", text)
    if masked != text:
        return masked
    stripped = text.strip()
    if len(stripped) > 6:
        return f"{stripped[:3]}******{stripped[-3:]}"
    return "******"


def _mask_email_value(text: str) -> str:
    if "@" not in text:
        return "***"
    local, domain = text.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def _mask_person_name(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    if len(stripped) == 1:
        return "*"
    return stripped[0] + ("*" * (len(stripped) - 1))


def _mask_address(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= 6:
        return "****"
    return f"{stripped[:6]}****"


def _mask_free_text_pii(text: str) -> str:
    result = _CN_MOBILE_RE.sub(r"\1****\2", text)
    result = _CN_ID_CARD_RE.sub(r"\1**********\2", result)
    result = _EMAIL_RE.sub(r"\1***\2", result)
    return result


def sanitize_sensitive_data(value: Any, *, field_name: str | None = None) -> Any:
    """递归脱敏手机号、身份证、邮箱、收件人姓名/地址及密钥字段。"""
    normalized_key = field_name.strip().lower() if field_name else None

    if normalized_key in _SECRET_KEYS:
        return "[REDACTED]" if value is not None else None

    if isinstance(value, Mapping):
        return {
            str(k): sanitize_sensitive_data(v, field_name=str(k))
            for k, v in value.items()
        }

    if isinstance(value, list):
        return [sanitize_sensitive_data(item, field_name=field_name) for item in value]

    if isinstance(value, tuple):
        return [sanitize_sensitive_data(item, field_name=field_name) for item in value]

    if isinstance(value, str):
        if normalized_key in _PHONE_KEYS:
            return _mask_phone_value(value)
        if normalized_key in _ID_CARD_KEYS:
            return _mask_id_card_value(value)
        if normalized_key in _EMAIL_KEYS:
            return _mask_email_value(value)
        if normalized_key in _NAME_KEYS:
            return _mask_person_name(value)
        if normalized_key in _ADDRESS_KEYS:
            return _mask_address(value)
        return _mask_free_text_pii(value)

    return value


def _prune_value_by_annotation(raw_val: Any, annotation: Any) -> Any:
    if raw_val is None or annotation is None:
        return raw_val
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _prune_to_declared_fields(raw_val, annotation)

    origin = get_origin(annotation)
    args = get_args(annotation)
    if (
        origin in (list, tuple, set, frozenset)
        and args
        and isinstance(raw_val, (list, tuple))
    ):
        return [_prune_value_by_annotation(item, args[0]) for item in raw_val]
    if origin is dict and len(args) == 2 and isinstance(raw_val, Mapping):
        return {k: _prune_value_by_annotation(v, args[1]) for k, v in raw_val.items()}
    if origin in (Union, UnionType) and args:
        model_args = [
            a for a in args if isinstance(a, type) and issubclass(a, BaseModel)
        ]
        if len(model_args) == 1 and isinstance(raw_val, (Mapping, BaseModel)):
            return _prune_to_declared_fields(raw_val, model_args[0])
    return raw_val


def _prune_to_declared_fields(raw_data: Any, schema_cls: type[BaseModel]) -> Any:
    """按 `output_schema` 递归剔除未声明的多余内部字段（兼容嵌套列表、可选子模型及任意 `extra` 配置）。"""
    if isinstance(raw_data, BaseModel):
        raw_data = raw_data.model_dump()

    if not isinstance(raw_data, Mapping):
        return raw_data

    declared_fields = schema_cls.model_fields
    pruned: dict[str, Any] = {}
    for key, val in raw_data.items():
        if key not in declared_fields:
            continue
        annotation = declared_fields[key].annotation
        pruned[key] = _prune_value_by_annotation(val, annotation)
    return pruned


class ToolLifecycleHook:
    """ToolRuntime 异步生命周期扩展点基类。"""

    async def before_tool_call(
        self, tool_call: ToolCall, context: ExecutionContext
    ) -> None:
        """在进入前置校验闭环前触发。"""

    async def after_tool_call(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        result: ToolResultMessage,
        duration_ms: float,
    ) -> None:
        """在工具调用闭环结束（含 success / error / hitl_required）后触发。"""

    async def on_tool_error(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        error: ErrorPayload,
        duration_ms: float,
    ) -> None:
        """当校验阻断或底层执行发生错误（`status == 'error'`）时触发。"""


class ToolRuntime:
    """受控工具执行底座（Harness），强制串联前后置安全守卫闭环。"""

    def __init__(
        self,
        snapshot: ToolSnapshot | None = None,
        *,
        hooks: Sequence[ToolLifecycleHook] | None = None,
        dependency_checker: DependencyChecker | None = None,
        audit_sink: AuditSink | None = None,
        audit_store: AuditStore | None = None,
        timeout_seconds: float | None = 30.0,
        raise_on_hitl: bool = False,
    ) -> None:
        self._default_snapshot = snapshot
        self._hooks: list[ToolLifecycleHook] = list(hooks or [])
        self._dependency_checker = dependency_checker
        self._audit_sink = audit_sink
        self._audit_store: AuditStore = (
            audit_store if audit_store is not None else InMemoryAuditStore()
        )
        self._timeout_seconds = timeout_seconds
        self._raise_on_hitl = raise_on_hitl
        self._audit_logs: list[AuditLogEntry] = []

    def add_hook(self, hook: ToolLifecycleHook) -> None:
        """注册异步生命周期 Hook。"""
        self._hooks.append(hook)

    @property
    def audit_store(self) -> AuditStore:
        """返回当前挂载的 `AuditStore` 实例。"""
        return self._audit_store

    @property
    def audit_logs(self) -> tuple[AuditLogEntry, ...]:
        """返回已记录的结构化审计日志只读快照。"""
        return tuple(self._audit_logs)

    def _resolve_snapshot(self, snapshot: ToolSnapshot | None) -> ToolSnapshot:
        active = snapshot if snapshot is not None else self._default_snapshot
        if active is None:
            raise ValueError(
                "ToolRuntime 执行时必须提供受控 ToolSnapshot（构造注入或单次调用传入）"
            )
        return active

    async def _run_before_hooks(
        self, tool_call: ToolCall, context: ExecutionContext
    ) -> None:
        for hook in self._hooks:
            await hook.before_tool_call(tool_call, context)

    async def _run_error_hooks(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        error: ErrorPayload,
        duration_ms: float,
    ) -> None:
        for hook in self._hooks:
            await hook.on_tool_error(tool_call, context, error, duration_ms)

    async def _run_after_hooks(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        result: ToolResultMessage,
        duration_ms: float,
    ) -> None:
        for hook in self._hooks:
            await hook.after_tool_call(tool_call, context, result, duration_ms)

    async def _record_audit(
        self,
        *,
        context: ExecutionContext,
        tool_call: ToolCall,
        risk: RiskLevel | str,
        status: ToolExecutionStatus,
        duration_ms: float,
        error_type: str | None = None,
        error_code: str | None = None,
        guard_reason: str | None = None,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            timestamp=datetime.now(timezone.utc),
            trace_id=context.trace_id,
            tool_call_id=tool_call.tool_call_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            role=context.role,
            tool_name=tool_call.name,
            risk=risk,  # type: ignore[arg-type]
            status=status,
            error_type=error_type,  # type: ignore[arg-type]
            duration_ms=round(duration_ms, 3),
        )
        self._audit_logs.append(entry)
        record = await self._audit_store.append(
            entry,
            error_code=error_code,
            guard_reason=guard_reason,
        )
        if self._audit_sink is not None:
            self._audit_sink(entry)
        log_structured_event(
            audit_logger,
            "tool_execution_audit",
            message=f"tool={entry.tool_name} status={entry.status} duration_ms={entry.duration_ms}",
            context=context,
            tool_call_id=entry.tool_call_id,
            tool_name=entry.tool_name,
            domain=record.domain,
            risk=entry.risk,
            status=entry.status,
            intercepted=record.intercepted,
            error_type=entry.error_type,
            error_code=error_code,
            guard_reason=guard_reason,
            duration_ms=entry.duration_ms,
        )
        return entry

    async def _finalize_error(
        self,
        *,
        tool_call: ToolCall,
        context: ExecutionContext,
        tool_def: ToolDefinition | None,
        error_payload: ErrorPayload,
        start_ns: int,
    ) -> ToolResultMessage:
        if tool_def is not None and tool_def.error_schema is not ErrorPayload:
            tool_def.error_schema.model_validate(error_payload.model_dump())

        duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        await self._run_error_hooks(tool_call, context, error_payload, duration_ms)
        await self._record_audit(
            context=context,
            tool_call=tool_call,
            risk=tool_def.risk if tool_def is not None else "unknown",
            status="error",
            error_type=error_payload.error_type,
            error_code=error_payload.code,
            guard_reason=error_payload.message,
            duration_ms=duration_ms,
        )
        result = ToolResultMessage(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.name,
            status="error",
            data=None,
            error=error_payload,
            hitl=None,
        )
        await self._run_after_hooks(tool_call, context, result, duration_ms)
        return result

    async def _check_dependencies(self, tool_def: ToolDefinition) -> tuple[bool, str]:
        try:
            if self._dependency_checker is not None:
                outcome = self._dependency_checker(tool_def.domain, tool_def.name)
                available = await outcome if inspect.isawaitable(outcome) else outcome
                if not available:
                    return (
                        False,
                        f"业务域 '{tool_def.domain}' 下游 MCP 服务或依赖当前不可用",
                    )

            if tool_def.dependency_check is not None:
                tool_outcome = tool_def.dependency_check()
                tool_available = (
                    await tool_outcome
                    if inspect.isawaitable(tool_outcome)
                    else tool_outcome
                )
                if not tool_available:
                    return (False, f"工具 '{tool_def.name}' 专属依赖健康检查未通过")
        except Exception as exc:
            return (False, f"依赖健康探测异常: {exc}")

        return (True, "")

    async def execute(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        *,
        snapshot: ToolSnapshot | None = None,
        raise_on_hitl: bool | None = None,
    ) -> ToolResultMessage:
        """执行单次工具调用，严格遵循前置 5 步与后置 3 步安全守卫闭环。"""
        active_snapshot = self._resolve_snapshot(snapshot)
        should_raise_hitl = (
            self._raise_on_hitl if raise_on_hitl is None else raise_on_hitl
        )
        start_ns = time.perf_counter_ns()

        with bind_observability_context(context):
            return await self._execute_with_bound_context(
                tool_call,
                context,
                active_snapshot=active_snapshot,
                should_raise_hitl=should_raise_hitl,
                start_ns=start_ns,
            )

    async def _execute_with_bound_context(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        *,
        active_snapshot: ToolSnapshot,
        should_raise_hitl: bool,
        start_ns: int,
    ) -> ToolResultMessage:
        await self._run_before_hooks(tool_call, context)

        # ------------------------------------------------------------------
        # 前置必检 1：工具是否存在于当前受控 ToolSnapshot 白名单
        # ------------------------------------------------------------------
        tool_def = active_snapshot.resolve(tool_call.name)
        if tool_def is None:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=None,
                error_payload=ErrorPayload(
                    error_type="permission_error",
                    code="TOOL_NOT_IN_SNAPSHOT",
                    message=(
                        f"工具 '{tool_call.name}' 不在当前 Agent 的受控 ToolSnapshot 白名单内"
                    ),
                    retryable=False,
                ),
                start_ns=start_ns,
            )

        # ------------------------------------------------------------------
        # 前置必检 2：Input Schema Pydantic v2 强校验（含伪造额外字段拦截）
        # ------------------------------------------------------------------
        try:
            validated_input = tool_def.input_schema.model_validate(tool_call.arguments)
        except ValidationError as exc:
            sanitized_issues = [
                {
                    "loc": [str(part) for part in item.get("loc", ())],
                    "msg": item.get("msg", "validation failed"),
                    "type": item.get("type", "value_error"),
                }
                for item in exc.errors()
            ]
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="validation_error",
                    code="INPUT_SCHEMA_VALIDATION_FAILED",
                    message=f"工具 '{tool_def.name}' 入参未通过契约校验: {sanitized_issues[0]['msg']}",
                    retryable=False,
                    details={"errors": sanitized_issues},
                ),
                start_ns=start_ns,
            )

        # ------------------------------------------------------------------
        # 前置必检 3：RBAC 权限点与 C/B 双域角色边界校验
        # ------------------------------------------------------------------
        if (
            tool_def.allowed_roles is not None
            and context.role not in tool_def.allowed_roles
        ):
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="permission_error",
                    code="ROLE_BOUNDARY_DENIED",
                    message=(
                        f"当前角色 '{context.role}' 禁止跨双域调用工具 '{tool_def.name}'"
                    ),
                    retryable=False,
                ),
                start_ns=start_ns,
            )

        if tool_def.permission not in context.permissions:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="permission_error",
                    code="RBAC_PERMISSION_DENIED",
                    message=(
                        f"当前主体缺少执行工具 '{tool_def.name}' 所需的权限 '{tool_def.permission}'"
                    ),
                    retryable=False,
                ),
                start_ns=start_ns,
            )

        # ------------------------------------------------------------------
        # 前置必检 4：高风险分级与受信任 HITL 审批阻断（严禁读取模型入参审批态）
        # ------------------------------------------------------------------
        canonical_args = validated_input.model_dump(mode="json")
        args_digest = compute_arguments_digest(canonical_args)

        if tool_def.risk == "high" and not context.has_valid_approval(
            tool_name=tool_def.name,
            tool_call_id=tool_call.tool_call_id,
            canonical_args_digest=args_digest,
        ):
            hitl_signal = HITLRequired(
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_def.name,
                risk="high",
                permission=tool_def.permission,
                reason=(
                    f"高风险工具 '{tool_def.name}' 需经人工审批签发受信任凭证后方可执行"
                ),
                sanitized_arguments=sanitize_sensitive_data(canonical_args),
                arguments_digest=args_digest,
                trace_id=context.trace_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
            )
            duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            await self._record_audit(
                context=context,
                tool_call=tool_call,
                risk=tool_def.risk,
                status="hitl_required",
                error_code="HITL_APPROVAL_REQUIRED",
                guard_reason=hitl_signal.reason,
                duration_ms=duration_ms,
            )
            hitl_result = ToolResultMessage(
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_def.name,
                status="hitl_required",
                data=None,
                error=None,
                hitl=hitl_signal,
            )
            await self._run_after_hooks(tool_call, context, hitl_result, duration_ms)
            if should_raise_hitl:
                raise HITLInterruptError(hitl_signal)
            return hitl_result

        # ------------------------------------------------------------------
        # 前置必检 5：下游依赖可用性检查
        # ------------------------------------------------------------------
        dep_ok, dep_reason = await self._check_dependencies(tool_def)
        if not dep_ok:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="transport_error",
                    code="DEPENDENCY_UNAVAILABLE",
                    message=dep_reason,
                    retryable=True,
                ),
                start_ns=start_ns,
            )

        # ------------------------------------------------------------------
        # 执行受控异步 Handler（强制注入不可变 ExecutionContext）
        # ------------------------------------------------------------------
        try:
            coro = tool_def.handler(validated_input, context)
            if self._timeout_seconds is not None:
                raw_output = await asyncio.wait_for(coro, timeout=self._timeout_seconds)
            else:
                raw_output = await coro
        except ToolBusinessError as exc:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="business_error",
                    code=exc.code,
                    message=exc.message,
                    retryable=False,
                    details=exc.details,
                ),
                start_ns=start_ns,
            )
        except ToolValidationError as exc:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="validation_error",
                    code=exc.code,
                    message=exc.message,
                    retryable=False,
                    details=exc.details,
                ),
                start_ns=start_ns,
            )
        except ToolPermissionError as exc:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="permission_error",
                    code=exc.code,
                    message=exc.message,
                    retryable=False,
                    details=exc.details,
                ),
                start_ns=start_ns,
            )
        except PermissionError as exc:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="permission_error",
                    code="HANDLER_PERMISSION_DENIED",
                    message=str(exc) or "底层资源访问被权限策略拒绝",
                    retryable=False,
                ),
                start_ns=start_ns,
            )
        except ToolTransportError as exc:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="transport_error",
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                    details=exc.details,
                ),
                start_ns=start_ns,
            )
        except (TimeoutError, ConnectionError, OSError) as exc:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="transport_error",
                    code="UPSTREAM_TIMEOUT_OR_NETWORK_ERROR",
                    message=f"工具 '{tool_def.name}' 底层通信超时或网络异常: {type(exc).__name__}",
                    retryable=True,
                    details={
                        "domain": tool_def.domain,
                        "failure_kind": "timeout" if isinstance(exc, TimeoutError) else "network",
                        "self_healing_hint": (
                            f"业务域 '{tool_def.domain}' 下游通信暂不可达或超时，"
                            "可缩小查询范围后有限重试一次，若仍失败请降级告知用户稍后重试并继续处理其它可用业务域。"
                        ),
                    },
                ),
                start_ns=start_ns,
            )
        except Exception:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="transport_error",
                    code="INTERNAL_RUNTIME_ERROR",
                    message=f"工具 '{tool_def.name}' 执行期间发生未预期内部错误",
                    retryable=False,
                ),
                start_ns=start_ns,
            )

        # ------------------------------------------------------------------
        # 后置闭环 1：Output Schema 强校验裁剪与敏感隐私字段脱敏
        # ------------------------------------------------------------------
        try:
            pruned_candidate = _prune_to_declared_fields(
                raw_output, tool_def.output_schema
            )
            validated_output = tool_def.output_schema.model_validate(pruned_candidate)
            serialized_output = validated_output.model_dump(mode="json")
            sanitized_output = sanitize_sensitive_data(serialized_output)
        except ValidationError as exc:
            return await self._finalize_error(
                tool_call=tool_call,
                context=context,
                tool_def=tool_def,
                error_payload=ErrorPayload(
                    error_type="transport_error",
                    code="OUTPUT_SCHEMA_VIOLATION",
                    message=f"工具 '{tool_def.name}' 返回结果不符合 Output Schema 契约",
                    retryable=False,
                    details={"error_count": len(exc.errors())},
                ),
                start_ns=start_ns,
            )

        # ------------------------------------------------------------------
        # 后置闭环 2 & 3：结构化审计记录 + 强制透传原始 tool_call_id
        # ------------------------------------------------------------------
        duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        await self._record_audit(
            context=context,
            tool_call=tool_call,
            risk=tool_def.risk,
            status="success",
            duration_ms=duration_ms,
        )
        result = ToolResultMessage(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_def.name,
            status="success",
            data=sanitized_output,
            error=None,
            hitl=None,
        )
        await self._run_after_hooks(tool_call, context, result, duration_ms)
        return result

    async def execute_batch(
        self,
        tool_calls: Sequence[ToolCall],
        context: ExecutionContext,
        *,
        snapshot: ToolSnapshot | None = None,
    ) -> list[ToolResultMessage]:
        """异步并发执行多个工具调用，严格保证返回结果与原始 `tool_call_id` 顺序一一精准对齐。"""
        if not tool_calls:
            return []

        active_snapshot = self._resolve_snapshot(snapshot)
        results = list(
            await asyncio.gather(
                *(
                    self.execute(
                        call,
                        context,
                        snapshot=active_snapshot,
                        raise_on_hitl=False,
                    )
                    for call in tool_calls
                )
            )
        )

        # 防御性后置不变量断言：确保乱序并发完成后 tool_call_id 100% 精准对齐不串台
        for expected_call, actual_result in zip(tool_calls, results, strict=True):
            if actual_result.tool_call_id != expected_call.tool_call_id:
                raise RuntimeError(
                    f"ToolRuntime 并发对齐不变量被破坏：期望 {expected_call.tool_call_id}，"
                    f"实际收到 {actual_result.tool_call_id}"
                )

        return results
