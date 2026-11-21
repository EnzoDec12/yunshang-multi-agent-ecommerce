"""MCP Client 适配层（五层架构第 4 层：Tool Runtime -> MCPClientManager -> 独立域 MCP Server）。

核心职责与安全防线：
1. 通过官方 `mcp` SDK 的 `Streamable HTTP` 客户端连接各独立域 MCP Server；
2. 动态执行能力发现（`tools/list`、`resources/list`、`resources/read`），强制按 `<domain>__<tool>`
   添加域前缀限定名注册至 `ToolRegistry`；
3. 从远端 MCP JSON Schema 动态构建严格的 Pydantic v2 `input_schema`（强制 `extra="forbid"` 并拦截
   `user_id`/`tenant_id`/`role`/`is_admin`/`approved`/`risk_level` 等禁止字段）与 `output_schema`；
4. 通过 HTTP Header（`X-MCP-Execution-Context`）与 MCP 请求 `_meta` 双通道带外注入不可变
   `ExecutionContext`，严禁污染模型可见工具参数；
5. 将远程 Streamable HTTP 调用包装为受控异步 `handler` 接入 `ToolRuntime`，完成响应归一化、
   精细四分类错误还原（`transport_error` / `permission_error` / `validation_error` / `business_error`）
   及面向编排层的结构化自愈诊断提示。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Union

from urllib.parse import urlsplit, urlunsplit

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import StreamableHTTPError, streamable_http_client
from mcp_types import (
    INVALID_PARAMS,
    BlobResourceContents,
    CallToolResult,
    TextContent,
    TextResourceContents,
)
from pydantic import BaseModel, ConfigDict, Field, create_model

from config.settings import AppSettings, get_settings
from mcp_servers.protocol import (
    EXECUTION_CONTEXT_HEADER,
    EXECUTION_CONTEXT_META_KEY,
    ROLE_HEADER,
    TENANT_ID_HEADER,
    TOOL_CALL_ID_HEADER,
    TRACE_ID_HEADER,
    USER_ID_HEADER,
    encode_execution_context_header,
    parse_mcp_domain_error,
    serialize_execution_context,
)
from runtime.executor import ToolLifecycleHook
from runtime.registry import (
    FORBIDDEN_INPUT_FIELDS,
    StrictToolInput,
    StrictToolOutput,
    ToolDefinition,
    ToolRegistrationError,
    ToolRegistry,
)
from schemas.context import ExecutionContext, RoleType
from schemas.contracts import (
    ErrorPayload,
    RiskLevel,
    ToolBusinessError,
    ToolCall,
    ToolPermissionError,
    ToolResultMessage,
    ToolTransportError,
    ToolValidationError,
)

_DOMAIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RISK_ORDER: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}

_OUTBOUND_HEADERS_VAR: ContextVar[dict[str, str] | None] = ContextVar(
    "_mcp_outbound_headers",
    default=None,
)
_ACTIVE_TOOL_CALL_ID_VAR: ContextVar[str | None] = ContextVar(
    "_mcp_active_tool_call_id",
    default=None,
)


@dataclass(frozen=True, kw_only=True)
class ToolSecurityPolicy:
    """客户端本地安全治理基线策略（防远端服务漏配或降级高风险等级）。"""

    permission: str
    risk: RiskLevel
    allowed_roles: frozenset[RoleType]


# 内置五域工具最小权限与最低风险基线（与动态发现的 `_meta` 取更严格交集）
DEFAULT_TOOL_SECURITY_POLICIES: dict[str, ToolSecurityPolicy] = {
    # PIM 商品与库存域
    "pim__hybrid_search": ToolSecurityPolicy(
        permission="pim:catalog:read",
        risk="low",
        allowed_roles=frozenset({"customer", "merchant"}),
    ),
    "pim__compare_skus": ToolSecurityPolicy(
        permission="pim:catalog:read",
        risk="low",
        allowed_roles=frozenset({"customer", "merchant"}),
    ),
    "pim__check_stock": ToolSecurityPolicy(
        permission="pim:stock:read",
        risk="low",
        allowed_roles=frozenset({"customer", "merchant"}),
    ),
    "pim__draft_sku_proposal": ToolSecurityPolicy(
        permission="pim:sku:propose",
        risk="high",
        allowed_roles=frozenset({"merchant"}),
    ),
    # OMS 订单与售后域
    "oms__search_orders": ToolSecurityPolicy(
        permission="oms:orders:read",
        risk="low",
        allowed_roles=frozenset({"customer", "merchant"}),
    ),
    "oms__update_address": ToolSecurityPolicy(
        permission="oms:orders:update_address",
        risk="medium",
        allowed_roles=frozenset({"customer", "merchant"}),
    ),
    "oms__create_refund": ToolSecurityPolicy(
        permission="oms:refund:create",
        risk="high",
        allowed_roles=frozenset({"customer", "merchant"}),
    ),
    # Logistics 物流域
    "logistics__track_pkg": ToolSecurityPolicy(
        permission="logistics:package:read",
        risk="low",
        allowed_roles=frozenset({"customer", "merchant"}),
    ),
    # BI 经营分析域（B 端专属）
    "bi__get_table_schema": ToolSecurityPolicy(
        permission="bi:schema:read",
        risk="low",
        allowed_roles=frozenset({"merchant"}),
    ),
    "bi__run_readonly_sql": ToolSecurityPolicy(
        permission="bi:sql:query",
        risk="medium",
        allowed_roles=frozenset({"merchant"}),
    ),
    "bi__render_chart": ToolSecurityPolicy(
        permission="bi:chart:render",
        risk="low",
        allowed_roles=frozenset({"merchant"}),
    ),
    # Market & ERP 选品与供应链域（B 端专属）
    "market_erp__search_trends": ToolSecurityPolicy(
        permission="market_erp:trends:read",
        risk="low",
        allowed_roles=frozenset({"merchant"}),
    ),
    "market_erp__calc_landed_cost": ToolSecurityPolicy(
        permission="market_erp:cost:calculate",
        risk="low",
        allowed_roles=frozenset({"merchant"}),
    ),
}

# B 端专属业务域兜底约束
MERCHANT_EXCLUSIVE_DOMAINS: frozenset[str] = frozenset({"bi", "market_erp"})


@dataclass(frozen=True, kw_only=True)
class MCPServerConfig:
    """单个独立域 Streamable HTTP MCP Server 连接配置。"""

    domain: str
    url: str
    timeout_seconds: float = field(
        default_factory=lambda: get_settings().MCP_CLIENT_TIMEOUT_SECONDS
    )
    connect_timeout_seconds: float = field(
        default_factory=lambda: get_settings().MCP_CLIENT_CONNECT_TIMEOUT_SECONDS
    )

    def __post_init__(self) -> None:
        cleaned_domain = self.domain.strip()
        if not _DOMAIN_NAME_RE.match(cleaned_domain):
            raise ValueError(
                f"无效业务域标识 {self.domain!r}（仅允许小写字母、数字与下划线且以字母开头）"
            )
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(
                f"域 '{cleaned_domain}' 的 Streamable HTTP 地址无效: {self.url!r}"
            )
        if self.timeout_seconds <= 0 or self.connect_timeout_seconds <= 0:
            raise ValueError("MCP 客户端超时配置必须大于 0 秒")


def build_mcp_server_configs(
    app_settings: AppSettings | None = None,
    *,
    endpoint_overrides: Mapping[str, str] | None = None,
) -> list[MCPServerConfig]:
    """根据 `config.settings` 组装五域 `MCPServerConfig` 列表。"""
    cfg = app_settings or get_settings()
    endpoints = (
        dict(endpoint_overrides)
        if endpoint_overrides is not None
        else cfg.mcp_domain_urls()
    )
    return [
        MCPServerConfig(
            domain=domain,
            url=url,
            timeout_seconds=cfg.MCP_CLIENT_TIMEOUT_SECONDS,
            connect_timeout_seconds=cfg.MCP_CLIENT_CONNECT_TIMEOUT_SECONDS,
        )
        for domain, url in endpoints.items()
    ]


@dataclass(frozen=True, kw_only=True)
class DiscoveredMCPResource:
    """动态发现的远端域 MCP Resource 元数据。"""

    domain: str
    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True, kw_only=True)
class MCPResourcePayload:
    """读取远端 MCP Resource 返回的规范化内容载荷。"""

    domain: str
    uri: str
    mime_type: str
    text: str


class NormalizedUnstructuredOutput(StrictToolOutput):
    """当远端 MCP Tool 未声明结构化 `outputSchema` 时使用的统一出参包装模型。"""

    result: Any


class MCPCallIdPropagationHook(ToolLifecycleHook):
    """可选 `ToolRuntime` Hook：将当前 `tool_call_id` 写入协程局部 `ContextVar` 供带外请求头透传。"""

    def __init__(self) -> None:
        self._tokens: dict[tuple[int, str], Any] = {}

    async def before_tool_call(
        self, tool_call: ToolCall, context: ExecutionContext
    ) -> None:
        task_key = (id(asyncio.current_task()), tool_call.tool_call_id)
        self._tokens[task_key] = _ACTIVE_TOOL_CALL_ID_VAR.set(tool_call.tool_call_id)

    async def after_tool_call(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        result: ToolResultMessage,
        duration_ms: float,
    ) -> None:
        task_key = (id(asyncio.current_task()), tool_call.tool_call_id)
        token = self._tokens.pop(task_key, None)
        if token is not None:
            _ACTIVE_TOOL_CALL_ID_VAR.reset(token)


async def _inject_context_headers_hook(request: httpx2.Request) -> None:
    """在 `httpx2.AsyncClient` 发送每个底层 HTTP 请求前注入当前协程的带外身份追踪头。"""
    headers = _OUTBOUND_HEADERS_VAR.get()
    if headers:
        for key, value in headers.items():
            request.headers[key] = value


def _sanitize_model_identifier(raw: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", raw)
    camel = "".join(part.capitalize() for part in parts if part)
    return camel or "DynamicModel"


def _resolve_json_schema_ref(
    node: Mapping[str, Any],
    defs: Mapping[str, Any],
) -> dict[str, Any]:
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return dict(node)
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            ref_name = ref[len(prefix) :]
            target = defs.get(ref_name)
            if isinstance(target, Mapping):
                merged = dict(target)
                for k, v in node.items():
                    if k != "$ref":
                        merged[k] = v
                return merged
    return dict(node)


def _schema_node_to_type(
    node: Mapping[str, Any],
    *,
    defs: Mapping[str, Any],
    model_prefix: str,
    is_input: bool,
    tool_name: str,
) -> Any:
    resolved = _resolve_json_schema_ref(node, defs)

    for union_key in ("anyOf", "oneOf"):
        variants = resolved.get(union_key)
        if isinstance(variants, list) and variants:
            arm_types: list[Any] = []
            has_null = False
            for idx, arm in enumerate(variants):
                if not isinstance(arm, Mapping):
                    continue
                if arm.get("type") == "null":
                    has_null = True
                    continue
                arm_types.append(
                    _schema_node_to_type(
                        arm,
                        defs=defs,
                        model_prefix=f"{model_prefix}Var{idx}",
                        is_input=is_input,
                        tool_name=tool_name,
                    )
                )
            if not arm_types:
                return type(None) if has_null else Any
            combined = arm_types[0]
            for extra_arm in arm_types[1:]:
                combined = Union[combined, extra_arm]
            if has_null:
                combined = Union[combined, None]
            return combined

    enum_values = resolved.get("enum")
    if isinstance(enum_values, list) and enum_values:
        non_null_enums = tuple(val for val in enum_values if val is not None)
        if non_null_enums:
            lit_type: Any = Literal[non_null_enums]  # type: ignore[valid-type]
            if any(val is None for val in enum_values):
                return Union[lit_type, None]
            return lit_type

    schema_type = resolved.get("type")
    if isinstance(schema_type, list):
        has_null = "null" in schema_type
        non_null = [t for t in schema_type if t != "null"]
        schema_type = non_null[0] if len(non_null) == 1 else None
        if schema_type is None:
            return Any
        inner = _schema_node_to_type(
            {**resolved, "type": schema_type},
            defs=defs,
            model_prefix=model_prefix,
            is_input=is_input,
            tool_name=tool_name,
        )
        return Union[inner, None] if has_null else inner

    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "null":
        return type(None)
    if schema_type == "array":
        items_schema = resolved.get("items")
        if isinstance(items_schema, Mapping):
            item_type = _schema_node_to_type(
                items_schema,
                defs=defs,
                model_prefix=f"{model_prefix}Item",
                is_input=is_input,
                tool_name=tool_name,
            )
            return list[item_type]  # type: ignore[valid-type]
        return list[Any]

    if schema_type == "object" or "properties" in resolved:
        props = resolved.get("properties")
        if isinstance(props, Mapping):
            return json_schema_to_pydantic_model(
                resolved,
                model_name=model_prefix,
                is_input=is_input,
                tool_name=tool_name,
                root_defs=defs,
            )
        additional = resolved.get("additionalProperties")
        if isinstance(additional, Mapping):
            val_type = _schema_node_to_type(
                additional,
                defs=defs,
                model_prefix=f"{model_prefix}Val",
                is_input=is_input,
                tool_name=tool_name,
            )
            return dict[str, val_type]  # type: ignore[valid-type]
        return dict[str, Any]

    return Any


def _extract_field_constraints(
    prop_schema: Mapping[str, Any],
    *,
    defs: Mapping[str, Any],
) -> dict[str, Any]:
    """提取 JSON Schema 属性级数值/长度/正则约束并映射为 Pydantic v2 `Field` 参数。"""
    resolved = _resolve_json_schema_ref(prop_schema, defs)

    # 若为 Optional 包装（anyOf: [{...}, {"type": "null"}]），穿透提取非 null 分支上的约束
    variants = resolved.get("anyOf") or resolved.get("oneOf")
    if isinstance(variants, list):
        non_null_arms = [
            _resolve_json_schema_ref(v, defs)
            for v in variants
            if isinstance(v, Mapping) and v.get("type") != "null"
        ]
        if len(non_null_arms) == 1:
            merged = dict(non_null_arms[0])
            for k in ("default", "description", "title"):
                if k in resolved:
                    merged[k] = resolved[k]
            resolved = merged

    kwargs: dict[str, Any] = {}
    if isinstance(resolved.get("description"), str):
        kwargs["description"] = resolved["description"]

    prop_type = resolved.get("type")
    if prop_type in ("integer", "number"):
        if "minimum" in resolved:
            kwargs["ge"] = resolved["minimum"]
        if "exclusiveMinimum" in resolved:
            kwargs["gt"] = resolved["exclusiveMinimum"]
        if "maximum" in resolved:
            kwargs["le"] = resolved["maximum"]
        if "exclusiveMaximum" in resolved:
            kwargs["lt"] = resolved["exclusiveMaximum"]
    elif prop_type == "string" and "enum" not in resolved:
        if "minLength" in resolved:
            kwargs["min_length"] = resolved["minLength"]
        if "maxLength" in resolved:
            kwargs["max_length"] = resolved["maxLength"]
        if "pattern" in resolved:
            kwargs["pattern"] = resolved["pattern"]
    elif prop_type == "array":
        if "minItems" in resolved:
            kwargs["min_length"] = resolved["minItems"]
        if "maxItems" in resolved:
            kwargs["max_length"] = resolved["maxItems"]

    return kwargs


def json_schema_to_pydantic_model(
    schema: Mapping[str, Any],
    *,
    model_name: str,
    is_input: bool,
    tool_name: str,
    root_defs: Mapping[str, Any] | None = None,
) -> type[BaseModel]:
    """从 MCP JSON Schema 动态构建严格的 Pydantic v2 模型。

    - 当 `is_input=True` 时，继承 `StrictToolInput`（强制 `extra="forbid", frozen=True`），
      并递归校验阻断任何可信上下文字段进入 Schema；
    - 当 `is_input=False` 时，继承 `StrictToolOutput`（`extra="ignore", frozen=True`），
      用于 `ToolRuntime` 后置字段白名单裁剪。
    """
    defs: dict[str, Any] = {}
    if root_defs:
        defs.update(root_defs)
    for defs_key in ("$defs", "definitions"):
        sub_defs = schema.get(defs_key)
        if isinstance(sub_defs, Mapping):
            defs.update(sub_defs)

    resolved_root = _resolve_json_schema_ref(schema, defs)
    properties = resolved_root.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}

    required_raw = resolved_root.get("required")
    required_set = (
        {str(item) for item in required_raw}
        if isinstance(required_raw, list)
        else set()
    )

    field_definitions: dict[str, tuple[Any, Any]] = {}
    for prop_name_raw, prop_schema_raw in properties.items():
        prop_name = str(prop_name_raw)
        if is_input and prop_name.strip().lower() in FORBIDDEN_INPUT_FIELDS:
            raise ToolRegistrationError(
                f"远端 MCP 工具 '{tool_name}' 的 inputSchema 违规暴露禁止的可信上下文字段 '{prop_name}'"
            )

        prop_schema = (
            prop_schema_raw if isinstance(prop_schema_raw, Mapping) else {}
        )
        nested_prefix = f"{model_name}{_sanitize_model_identifier(prop_name)}"
        annotation = _schema_node_to_type(
            prop_schema,
            defs=defs,
            model_prefix=nested_prefix,
            is_input=is_input,
            tool_name=tool_name,
        )
        constraints = _extract_field_constraints(prop_schema, defs=defs)

        if prop_name in required_set and "default" not in prop_schema:
            default_val: Any = ...
        else:
            default_val = copy.deepcopy(prop_schema.get("default", None))
            if default_val is None and annotation is not Any:
                annotation = Union[annotation, None]

        field_definitions[prop_name] = (
            annotation,
            Field(default=default_val, **constraints),
        )

    base_cls = StrictToolInput if is_input else StrictToolOutput
    cfg = (
        ConfigDict(extra="forbid", frozen=True)
        if is_input
        else ConfigDict(extra="ignore", frozen=True)
    )
    return create_model(
        _sanitize_model_identifier(model_name),
        __base__=base_cls,
        __config__=cfg,
        **field_definitions,  # type: ignore[call-overload]
    )


@dataclass
class _DomainConnectionState:
    config: MCPServerConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    http_client: httpx2.AsyncClient | None = None
    cached_tools_listing: Any = None


class MCPClientManager:
    """多域 Streamable HTTP MCP 客户端管理器与 `ToolRuntime` 适配器。"""

    def __init__(
        self,
        servers: Sequence[MCPServerConfig] | Mapping[str, str | MCPServerConfig] | None = None,
        *,
        default_timeout_seconds: float | None = None,
        security_policies: Mapping[str, ToolSecurityPolicy] | None = None,
    ) -> None:
        cfg_settings = get_settings()
        resolved_default_timeout = (
            default_timeout_seconds
            if default_timeout_seconds is not None
            else cfg_settings.MCP_CLIENT_TIMEOUT_SECONDS
        )
        self._default_timeout_seconds = resolved_default_timeout
        self._policies: dict[str, ToolSecurityPolicy] = dict(
            DEFAULT_TOOL_SECURITY_POLICIES
        )
        if security_policies:
            self._policies.update(security_policies)

        resolved_servers = (
            servers if servers is not None else build_mcp_server_configs(cfg_settings)
        )

        self._domains: dict[str, _DomainConnectionState] = {}
        if isinstance(resolved_servers, Mapping):
            for domain, target in resolved_servers.items():
                cfg = (
                    target
                    if isinstance(target, MCPServerConfig)
                    else MCPServerConfig(
                        domain=domain,
                        url=target,
                        timeout_seconds=resolved_default_timeout,
                        connect_timeout_seconds=cfg_settings.MCP_CLIENT_CONNECT_TIMEOUT_SECONDS,
                    )
                )
                self._domains[cfg.domain] = _DomainConnectionState(config=cfg)
        else:
            for cfg in resolved_servers:
                if cfg.domain in self._domains:
                    raise ValueError(f"重复注册的 MCP 业务域配置: '{cfg.domain}'")
                self._domains[cfg.domain] = _DomainConnectionState(config=cfg)

        self._discovered_resources: dict[str, list[DiscoveredMCPResource]] = {}
        self._resource_uri_index: dict[str, str] = {}
        self._closed = False

    @classmethod
    def from_settings(
        cls,
        app_settings: AppSettings | None = None,
        *,
        endpoint_overrides: Mapping[str, str] | None = None,
        security_policies: Mapping[str, ToolSecurityPolicy] | None = None,
    ) -> MCPClientManager:
        """从统一配置中心实例化 `MCPClientManager`。"""
        cfg = app_settings or get_settings()
        configs = build_mcp_server_configs(cfg, endpoint_overrides=endpoint_overrides)
        return cls(
            configs,
            default_timeout_seconds=cfg.MCP_CLIENT_TIMEOUT_SECONDS,
            security_policies=security_policies,
        )

    @property
    def domains(self) -> tuple[str, ...]:
        """已纳管的独立业务域名称元组。"""
        return tuple(self._domains.keys())

    @property
    def discovered_resources(self) -> dict[str, tuple[DiscoveredMCPResource, ...]]:
        """各域已发现的 MCP Resource 只读快照。"""
        return {
            domain: tuple(items)
            for domain, items in self._discovered_resources.items()
        }

    def create_runtime_hook(self) -> MCPCallIdPropagationHook:
        """创建用于向底层带外请求头同步 `tool_call_id` 的生命周期 Hook。"""
        return MCPCallIdPropagationHook()

    async def __aenter__(self) -> MCPClientManager:
        self._closed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.aclose()

    async def _ensure_http_pool(self, domain: str) -> httpx2.AsyncClient:
        if self._closed:
            raise ToolTransportError(
                f"MCPClientManager 已关闭，无法连接业务域 '{domain}'",
                code="MCP_CLIENT_MANAGER_CLOSED",
                retryable=False,
            )

        state = self._domains.get(domain)
        if state is None:
            raise ToolTransportError(
                f"未配置业务域 '{domain}' 的 MCP Server 端点",
                code="UNKNOWN_MCP_DOMAIN",
                retryable=False,
            )

        async with state.lock:
            if state.http_client is not None:
                return state.http_client

            cfg = state.config
            timeout = httpx2.Timeout(
                cfg.timeout_seconds,
                connect=cfg.connect_timeout_seconds,
                read=max(cfg.timeout_seconds * 4.0, 15.0),
            )
            http_client = httpx2.AsyncClient(
                timeout=timeout,
                event_hooks={"request": [_inject_context_headers_hook]},
            )
            await http_client.__aenter__()
            state.http_client = http_client
            return http_client

    @asynccontextmanager
    async def _open_domain_session(self, domain: str) -> AsyncIterator[Client]:
        """在当前协程结构化作用域内打开独立 Streamable HTTP MCP 会话（复用域级 HTTP 连接池，杜绝跨 Task CancelScope 串扰）。"""
        http_client = await self._ensure_http_pool(domain)
        state = self._domains[domain]
        cfg = state.config
        transport = streamable_http_client(
            cfg.url,
            http_client=http_client,
            terminate_on_close=True,
        )
        async with Client(
            transport,
            read_timeout_seconds=cfg.timeout_seconds,
            cache=None,
        ) as mcp_client:
            if state.cached_tools_listing is not None:
                mcp_client.session._absorb_tool_listing(  # pyright: ignore[reportPrivateUsage]
                    state.cached_tools_listing,
                    complete=True,
                )
            yield mcp_client

    async def _reset_domain_client(self, domain: str) -> None:
        """在底层连接池异常后安全回收单个受损域 HTTP 连接池，不影响其余健康域。"""
        state = self._domains.get(domain)
        if state is None:
            return
        async with state.lock:
            http_client = state.http_client
            state.http_client = None
            if http_client is not None:
                try:
                    await http_client.aclose()
                except BaseException:
                    pass

    async def aclose(self) -> None:
        """优雅关闭全部已建立的独立域 Streamable HTTP 会话与底层连接池。"""
        self._closed = True
        await asyncio.gather(
            *(self._reset_domain_client(domain) for domain in list(self._domains)),
            return_exceptions=True,
        )

    @staticmethod
    def _derive_health_url(mcp_url: str) -> str:
        parts = urlsplit(mcp_url)
        return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))

    async def check_domain_health(self, domain: str) -> dict[str, Any]:
        """探测指定远程独立域 MCP Server 的 `/health` 健康检查端点。"""
        http_client = await self._ensure_http_pool(domain)
        cfg = self._domains[domain].config
        health_url = self._derive_health_url(cfg.url)
        try:
            resp = await http_client.get(health_url, timeout=cfg.connect_timeout_seconds)
            body = resp.json() if resp.content else {}
            return {
                "domain": domain,
                "healthy": resp.status_code == 200 and body.get("status") == "ok",
                "status_code": resp.status_code,
                "endpoint": health_url,
                "payload": body if isinstance(body, dict) else {},
            }
        except Exception as exc:
            return {
                "domain": domain,
                "healthy": False,
                "status_code": None,
                "endpoint": health_url,
                "error": str(exc),
            }

    async def check_all_health(self) -> dict[str, dict[str, Any]]:
        """并发探测全部已配置远程 MCP Server 的 `/health` 状态。"""
        domain_list = list(self._domains)
        results = await asyncio.gather(
            *(self.check_domain_health(d) for d in domain_list)
        )
        return dict(zip(domain_list, results, strict=True))

    def _resolve_tool_security(
        self,
        *,
        domain: str,
        qualified_name: str,
        remote_meta: Mapping[str, Any] | None,
    ) -> ToolSecurityPolicy:
        meta = remote_meta if isinstance(remote_meta, Mapping) else {}
        baseline = self._policies.get(qualified_name)

        meta_perm = meta.get("permission")
        if isinstance(meta_perm, str) and meta_perm.strip():
            permission = meta_perm.strip()
        elif baseline is not None:
            permission = baseline.permission
        else:
            action_part = qualified_name.split("__", 1)[1]
            permission = f"{domain}:{action_part}:execute"

        meta_risk = meta.get("risk")
        discovered_risk: RiskLevel = (
            meta_risk if meta_risk in ("low", "medium", "high") else "low"
        )
        if baseline is not None:
            risk = (
                baseline.risk
                if _RISK_ORDER[baseline.risk] >= _RISK_ORDER[discovered_risk]
                else discovered_risk
            )
        else:
            risk = discovered_risk

        meta_roles = meta.get("allowed_roles")
        if isinstance(meta_roles, list) and meta_roles:
            valid_roles = frozenset(
                r for r in meta_roles if r in ("customer", "merchant")
            )
        else:
            valid_roles = frozenset({"customer", "merchant"})

        if domain in MERCHANT_EXCLUSIVE_DOMAINS:
            valid_roles = valid_roles & frozenset({"merchant"})

        if baseline is not None:
            valid_roles = valid_roles & baseline.allowed_roles

        if not valid_roles:
            valid_roles = (
                baseline.allowed_roles
                if baseline is not None
                else frozenset({"merchant"})
            )

        return ToolSecurityPolicy(
            permission=permission,
            risk=risk,
            allowed_roles=valid_roles,
        )

    async def discover_and_register(
        self,
        registry: ToolRegistry,
        *,
        domains: Sequence[str] | None = None,
        discover_resources: bool = True,
    ) -> list[ToolDefinition]:
        """并发或按序探测各独立域 MCP Server，动态构建强类型契约并注册入 `ToolRegistry`。"""
        target_domains = list(domains) if domains is not None else list(self._domains)
        registered_defs: list[ToolDefinition] = []

        for domain in target_domains:
            cfg = self._domains[domain].config

            try:
                async with self._open_domain_session(domain) as client:
                    tools_result = await asyncio.wait_for(
                        client.list_tools(cache_mode="refresh"),
                        timeout=cfg.timeout_seconds,
                    )
                self._domains[domain].cached_tools_listing = tools_result
            except Exception as exc:
                await self._reset_domain_client(domain)
                raise self._classify_transport_exception(
                    exc,
                    domain=domain,
                    qualified_tool_name=f"{domain}__list_tools",
                    endpoint=cfg.url,
                ) from exc

            for mcp_tool in tools_result.tools:
                remote_name = mcp_tool.name.strip()
                qualified_name = f"{domain}__{remote_name}"

                input_model = json_schema_to_pydantic_model(
                    mcp_tool.input_schema,
                    model_name=f"{qualified_name}_input",
                    is_input=True,
                    tool_name=qualified_name,
                )

                has_structured_output = bool(
                    isinstance(mcp_tool.output_schema, Mapping)
                    and isinstance(mcp_tool.output_schema.get("properties"), Mapping)
                    and mcp_tool.output_schema["properties"]
                )
                if has_structured_output:
                    assert mcp_tool.output_schema is not None
                    output_model: type[BaseModel] = json_schema_to_pydantic_model(
                        mcp_tool.output_schema,
                        model_name=f"{qualified_name}_output",
                        is_input=False,
                        tool_name=qualified_name,
                    )
                else:
                    output_model = NormalizedUnstructuredOutput

                policy = self._resolve_tool_security(
                    domain=domain,
                    qualified_name=qualified_name,
                    remote_meta=mcp_tool.meta,
                )

                handler = self._build_remote_handler(
                    domain=domain,
                    remote_tool_name=remote_name,
                    qualified_name=qualified_name,
                    expects_structured_output=has_structured_output,
                )

                tool_def = ToolDefinition(
                    name=qualified_name,
                    description=mcp_tool.description or "",
                    input_schema=input_model,
                    output_schema=output_model,
                    error_schema=ErrorPayload,
                    permission=policy.permission,
                    risk=policy.risk,
                    handler=handler,
                    allowed_roles=policy.allowed_roles,
                )
                registry.register(tool_def)
                registered_defs.append(tool_def)

            if discover_resources:
                await self.discover_domain_resources(domain)

        return registered_defs

    async def discover_domain_resources(
        self,
        domain: str,
    ) -> list[DiscoveredMCPResource]:
        """动态发现指定独立域 MCP Server 暴露的 `resources` 列表。"""
        cfg = self._domains[domain].config
        try:
            async with self._open_domain_session(domain) as client:
                res_list = await asyncio.wait_for(
                    client.list_resources(cache_mode="refresh"),
                    timeout=cfg.timeout_seconds,
                )
        except Exception as exc:
            await self._reset_domain_client(domain)
            raise self._classify_transport_exception(
                exc,
                domain=domain,
                qualified_tool_name=f"{domain}__list_resources",
                endpoint=cfg.url,
            ) from exc

        discovered: list[DiscoveredMCPResource] = []
        for item in res_list.resources:
            uri_str = str(item.uri)
            entry = DiscoveredMCPResource(
                domain=domain,
                uri=uri_str,
                name=item.name,
                title=item.title,
                description=item.description,
                mime_type=item.mime_type,
            )
            discovered.append(entry)
            self._resource_uri_index[uri_str] = domain

        self._discovered_resources[domain] = discovered
        return discovered

    async def read_resource(
        self,
        uri: str,
        *,
        domain: str | None = None,
    ) -> MCPResourcePayload:
        """读取远端域 MCP Server 的知识库或规则文档 Resource。"""
        resolved_domain = domain or self._resource_uri_index.get(uri)
        if resolved_domain is None and "://" in uri:
            scheme = uri.split("://", 1)[0].strip().lower()
            if scheme in self._domains:
                resolved_domain = scheme
        if resolved_domain is None:
            raise ValueError(f"无法为 Resource URI '{uri}' 匹配所属 MCP 业务域")

        cfg = self._domains[resolved_domain].config
        try:
            async with self._open_domain_session(resolved_domain) as client:
                read_res = await asyncio.wait_for(
                    client.read_resource(uri, cache_mode="refresh"),
                    timeout=cfg.timeout_seconds,
                )
        except Exception as exc:
            await self._reset_domain_client(resolved_domain)
            raise self._classify_transport_exception(
                exc,
                domain=resolved_domain,
                qualified_tool_name=f"{resolved_domain}__read_resource",
                endpoint=cfg.url,
            ) from exc

        texts: list[str] = []
        mime_type = "text/plain"
        for block in read_res.contents:
            if isinstance(block, TextResourceContents):
                texts.append(block.text)
                if block.mime_type:
                    mime_type = block.mime_type
            elif isinstance(block, BlobResourceContents):
                texts.append(block.blob)
                if block.mime_type:
                    mime_type = block.mime_type

        return MCPResourcePayload(
            domain=resolved_domain,
            uri=uri,
            mime_type=mime_type,
            text="\n".join(texts),
        )

    def _build_remote_handler(
        self,
        *,
        domain: str,
        remote_tool_name: str,
        qualified_name: str,
        expects_structured_output: bool,
    ):
        async def _remote_mcp_handler(
            validated_input: BaseModel,
            context: ExecutionContext,
        ) -> dict[str, Any]:
            return await self._invoke_remote_tool(
                domain=domain,
                remote_tool_name=remote_tool_name,
                qualified_name=qualified_name,
                arguments=validated_input.model_dump(mode="json", exclude_none=True),
                context=context,
                expects_structured_output=expects_structured_output,
            )

        return _remote_mcp_handler

    async def _invoke_remote_tool(
        self,
        *,
        domain: str,
        remote_tool_name: str,
        qualified_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        expects_structured_output: bool,
    ) -> dict[str, Any]:
        cfg = self._domains[domain].config
        active_call_id = _ACTIVE_TOOL_CALL_ID_VAR.get()

        # 组装双通道带外上下文（HTTP Header + MCP RequestParams._meta）
        outbound_headers: dict[str, str] = {
            EXECUTION_CONTEXT_HEADER: encode_execution_context_header(
                context, tool_call_id=active_call_id
            ),
            TRACE_ID_HEADER: context.trace_id,
            TENANT_ID_HEADER: context.tenant_id,
            USER_ID_HEADER: context.user_id,
            ROLE_HEADER: context.role,
        }
        if active_call_id:
            outbound_headers[TOOL_CALL_ID_HEADER] = active_call_id

        request_meta: dict[str, Any] = {
            EXECUTION_CONTEXT_META_KEY: serialize_execution_context(
                context, tool_call_id=active_call_id
            )
        }

        header_token = _OUTBOUND_HEADERS_VAR.set(outbound_headers)
        try:
            async def _execute_rpc() -> CallToolResult:
                async with self._open_domain_session(domain) as client:
                    return await client.call_tool(
                        remote_tool_name,
                        arguments,
                        read_timeout_seconds=cfg.timeout_seconds,
                        meta=request_meta,  # type: ignore[arg-type]
                    )

            call_result = await asyncio.wait_for(
                _execute_rpc(),
                timeout=cfg.timeout_seconds + 0.5,
            )
        except (
            ToolBusinessError,
            ToolPermissionError,
            ToolValidationError,
            ToolTransportError,
        ):
            raise
        except MCPError as mcp_exc:
            if mcp_exc.code == INVALID_PARAMS:
                raise ToolValidationError(
                    f"远端 MCP 工具 '{qualified_name}' 参数校验未通过: {mcp_exc}",
                    code="REMOTE_MCP_INVALID_PARAMS",
                    details={"domain": domain, "tool_name": qualified_name},
                ) from mcp_exc
            await self._reset_domain_client(domain)
            raise self._classify_transport_exception(
                mcp_exc,
                domain=domain,
                qualified_tool_name=qualified_name,
                endpoint=cfg.url,
            ) from mcp_exc
        except Exception as exc:
            await self._reset_domain_client(domain)
            raise self._classify_transport_exception(
                exc,
                domain=domain,
                qualified_tool_name=qualified_name,
                endpoint=cfg.url,
            ) from exc
        finally:
            _OUTBOUND_HEADERS_VAR.reset(header_token)

        return self._normalize_tool_result(
            call_result,
            domain=domain,
            qualified_name=qualified_name,
            expects_structured_output=expects_structured_output,
        )

    def _normalize_tool_result(
        self,
        result: CallToolResult,
        *,
        domain: str,
        qualified_name: str,
        expects_structured_output: bool,
    ) -> dict[str, Any]:
        text_fragments = [
            block.text
            for block in result.content
            if isinstance(block, TextContent) and block.text
        ]
        joined_text = "\n".join(text_fragments).strip()

        if result.is_error:
            self._raise_classified_tool_error(
                joined_text,
                domain=domain,
                qualified_name=qualified_name,
            )

        # 优先提取原始 JSON 文本载荷（保留后端内部字段供 ToolRuntime Output Schema 做标准白名单剔除验证），
        # 若文本非 JSON 对象则回退使用 structured_content。
        parsed_json: Any = None
        if joined_text:
            try:
                parsed_json = json.loads(joined_text)
            except json.JSONDecodeError:
                parsed_json = None

        if expects_structured_output:
            if isinstance(parsed_json, dict):
                return parsed_json
            if isinstance(result.structured_content, dict):
                return dict(result.structured_content)
            raise ToolTransportError(
                f"远端 MCP 工具 '{qualified_name}' 未返回约定的结构化 JSON 对象",
                code="MCP_MALFORMED_STRUCTURED_OUTPUT",
                retryable=False,
                details={"domain": domain, "tool_name": qualified_name},
            )

        if parsed_json is not None:
            return {"result": parsed_json}
        if result.structured_content is not None:
            return {"result": result.structured_content}
        return {"result": joined_text}

    def _raise_classified_tool_error(
        self,
        error_text: str,
        *,
        domain: str,
        qualified_name: str,
    ) -> None:
        structured = parse_mcp_domain_error(error_text)
        if structured is not None:
            err_type = structured.get("error_type")
            code = str(structured.get("code") or "REMOTE_DOMAIN_ERROR")
            msg = str(structured.get("message") or error_text)
            raw_details = structured.get("details")
            details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
            details.setdefault("domain", domain)
            details.setdefault("tool_name", qualified_name)

            if err_type == "permission_error":
                details.setdefault(
                    "self_healing_hint",
                    "当前身份或租户边界无权访问该资源，禁止换参绕过鉴权，请向用户说明权限限制。",
                )
                raise ToolPermissionError(msg, code=code, details=details)

            if err_type == "validation_error":
                details.setdefault(
                    "self_healing_hint",
                    "请根据错误描述修正工具入参格式或补充必填过滤条件后再重新调用。",
                )
                raise ToolValidationError(msg, code=code, details=details)

            if err_type == "transport_error":
                details.setdefault(
                    "self_healing_hint",
                    f"远端域 '{domain}' 底层资源暂时异常，可短暂退避重试一次或降级处理。",
                )
                raise ToolTransportError(
                    msg,
                    code=code,
                    retryable=bool(structured.get("retryable", True)),
                    details=details,
                )

            details.setdefault(
                "self_healing_hint",
                "业务规则校验未通过，请依据错误原因调整查询范围（如补充 dt 分区、更换有货 SKU 或引导人工客服），切勿盲目原样重试。",
            )
            raise ToolBusinessError(msg, code=code, details=details)

        lowered = error_text.lower()
        if "validation error" in lowered or "field required" in lowered:
            raise ToolValidationError(
                f"远端工具 '{qualified_name}' 入参未通过协议校验: {error_text}",
                code="REMOTE_ARGUMENT_VALIDATION_FAILED",
                details={
                    "domain": domain,
                    "tool_name": qualified_name,
                    "self_healing_hint": "请检查字段类型与取值范围，修正参数后重新调用。",
                },
            )

        raise ToolBusinessError(
            error_text or f"远端工具 '{qualified_name}' 返回未分类执行拒绝",
            code="REMOTE_TOOL_EXECUTION_ERROR",
            details={
                "domain": domain,
                "tool_name": qualified_name,
                "self_healing_hint": "请根据返回错误提示调整业务请求或切换替代方案。",
            },
        )

    @staticmethod
    def _flatten_exceptions(root: BaseException) -> list[BaseException]:
        collected: list[BaseException] = []
        seen: set[int] = set()
        stack: list[BaseException] = [root]
        while stack and len(collected) < 32:
            current = stack.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            collected.append(current)
            if isinstance(current, BaseExceptionGroup):
                stack.extend(current.exceptions)
            if current.__cause__ is not None:
                stack.append(current.__cause__)
            if current.__context__ is not None:
                stack.append(current.__context__)
        return collected

    def _classify_transport_exception(
        self,
        exc: BaseException,
        *,
        domain: str,
        qualified_tool_name: str,
        endpoint: str,
    ) -> ToolTransportError:
        """将超时、连接拒绝、服务宕机与流中断统一归类为带自愈提示的 `ToolTransportError`。"""
        causes = self._flatten_exceptions(exc)

        is_timeout = any(
            isinstance(item, (TimeoutError, httpx2.TimeoutException))
            for item in causes
        )
        is_conn_refused = any(
            isinstance(
                item,
                (
                    ConnectionRefusedError,
                    ConnectionError,
                    httpx2.ConnectError,
                    httpx2.NetworkError,
                    OSError,
                ),
            )
            for item in causes
        ) and not is_timeout

        if is_timeout:
            return ToolTransportError(
                message=(
                    f"独立域 '{domain}' MCP 服务调用超时（工具: '{qualified_tool_name}'）；"
                    "请勿高频重复发起同一慢请求，可缩小查询范围退避重试 1 次或先基于其它可用域结果降级回复。"
                ),
                code="MCP_RPC_TIMEOUT",
                retryable=True,
                details={
                    "domain": domain,
                    "tool_name": qualified_tool_name,
                    "endpoint": endpoint,
                    "failure_kind": "timeout",
                    "suggested_action": "retry_once_or_degrade",
                    "self_healing_hint": (
                        f"业务域 '{domain}' 响应超时。建议：1) 缩小时间窗口或返回条数后最多重试 1 次；"
                        f"2) 若仍超时，保留其它健康业务域数据并明确提示用户 '{domain}' 服务暂时繁忙。"
                    ),
                },
            )

        if is_conn_refused or isinstance(exc, StreamableHTTPError):
            return ToolTransportError(
                message=(
                    f"独立域 '{domain}' MCP 服务当前不可达或已宕机（工具: '{qualified_tool_name}'）；"
                    "请立即停止对该域工具的重复调用并执行优雅降级，其它健康业务域不受影响。"
                ),
                code="MCP_SERVER_UNREACHABLE",
                retryable=True,
                details={
                    "domain": domain,
                    "tool_name": qualified_tool_name,
                    "endpoint": endpoint,
                    "failure_kind": "connection_refused_or_server_down",
                    "suggested_action": "circuit_break_and_degrade",
                    "self_healing_hint": (
                        f"业务域 '{domain}' 节点端口不可达或进程宕机。建议：本轮对话跳过 '{domain}__*' 工具，"
                        "继续完成其它健康域调用，并向用户说明该子服务正在恢复中。"
                    ),
                },
            )

        return ToolTransportError(
            message=(
                f"独立域 '{domain}' MCP 传输协议异常（工具: '{qualified_tool_name}', "
                f"异常类型: {type(exc).__name__}）；建议有限重试或优雅降级。"
            ),
            code="MCP_TRANSPORT_PROTOCOL_ERROR",
            retryable=True,
            details={
                "domain": domain,
                "tool_name": qualified_tool_name,
                "endpoint": endpoint,
                "failure_kind": "protocol_or_stream_error",
                "exception_type": type(exc).__name__,
                "error_summary": str(exc)[:300],
                "suggested_action": "retry_or_degrade",
                "self_healing_hint": (
                    f"业务域 '{domain}' 通信流中断，系统已自动重置连接池；可尝试重试 1 次或降级告知用户。"
                ),
            },
        )
