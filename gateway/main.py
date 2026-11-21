"""电商多 Agent 系统 FastAPI 网关入口（五层架构第 1 层：API Gateway）。

调用链路严格遵循：
`API Gateway` -> `Agent Host (LangGraph)` -> `Tool Runtime (Harness)` -> `MCP Client` -> `独立域 MCP Server`。

核心能力：
1. `lifespan` 自动管理 5 个独立域 Streamable HTTP MCP Server 子进程集群（或连接外部配置的端点），
   退出时优雅回收零残留；完成动态能力发现、`ToolRuntime` 挂载及带 Checkpointer 的双域隔离图编译；
2. 认证与不可变 `ExecutionContext` 签发（`POST /api/auth/login`、`GET /api/auth/accounts`、`GET /api/auth/me`），
   严禁信任前端请求 JSON Body 中的任何身份或审批字段；
3. `POST /api/chat/stream`：基于 LangGraph `astream` 与 `ToolRuntime` 生命周期 Hook，实时推送
   `thinking`、`tool_start`、`tool_result`、`hitl_interrupt`、`final_answer` 五类 SSE 事件；
4. `POST /api/hitl/resume`：严格校验当前调用者与挂起 `thread_id` 的租户、用户、角色及权限一致性（越权一律返回 403），
   批准时通过 `ExecutionContext.with_hitl_approval(...)` 签发绑定入参摘要的受信任凭证并流式恢复执行；
5. `GET /api/audit/logs`：按租户与角色可见性隔离返回结构化工具调用与风控拦截审计日志。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from agents.graph import (
    MultiAgentOrchestrator,
    _dump_context_for_checkpoint,
)
from agents.llm import AsyncAgentLLM, build_configured_llm
from agents.state import PendingHITLGatePayload, parse_execution_context
from config.settings import AppSettings, get_settings
from gateway.auth import (
    JWTExecutionContextMiddleware,
    PRESET_ACCOUNTS,
    issue_access_token,
    require_execution_context,
)
from infra.audit_store import AuditStore, audit_store_lifespan
from infra.checkpointer import checkpointer_lifespan
from infra.local_mcp_cluster import DOMAIN_SERVER_MODULES, ManagedMCPCluster
from infra.observability import (
    bind_observability_context,
    build_langfuse_callback_handler,
    configure_structured_logging,
    create_langfuse_tool_hook,
)
from runtime.executor import ToolLifecycleHook, ToolRuntime, sanitize_sensitive_data
from runtime.mcp_adapter import (
    DEFAULT_TOOL_SECURITY_POLICIES,
    MCPClientManager,
    build_mcp_server_configs,
)
from runtime.registry import ToolRegistry
from schemas.context import ExecutionContext
from schemas.contracts import (
    DiagnosisArtifact,
    ErrorPayload,
    SelectionProposal,
    ToolCall,
    ToolResultMessage,
)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

NODE_DISPLAY_METADATA: dict[str, tuple[str, str]] = {
    "c_supervisor": ("C-Supervisor", "正在理解您的需求并为您安排专属顾问…"),
    "shopping_guide_agent": (
        "ShoppingGuideAgent",
        "穿搭顾问正在为您比对款式、尺码颜色、面料成分与现货库存…",
    ),
    "order_agent": (
        "OrderAgent",
        "售后顾问正在为您核对订单状态与包裹最新物流进度…",
    ),
    "c_hitl_gate": (
        "OrderAgent",
        "正在等待您确认本次订单服务申请…",
    ),
    "analytics_agent": (
        "AnalyticsAgent",
        "正在分析近 3 日女装各品类退货率与消费者退货原因…",
    ),
    "b_contract_handoff": (
        "B-Orchestrator",
        "已完成退货问题诊断，正在衔接新款选品与成本测算…",
    ),
    "sourcing_agent": (
        "SourcingAgent",
        "正在结合价格带空白机会测算成衣单件成本与预估毛利率…",
    ),
    "b_hitl_gate": (
        "SourcingAgent",
        "正在处理新款建档确认并同步更新门店导购卖点库…",
    ),
    "b_finalize": (
        "B-Orchestrator",
        "正在为您整理本次退货诊断与新款开发建议总结…",
    ),
}


# ============================================================================
# 1. 请求与响应数据契约（严格忽略任何客户端伪造的身份与审批字段）
# ============================================================================


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    account_id: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("account_id", "username"),
        description="预设登录账号标识（如 customer_01 / merchant_01）",
    )


class ChatStreamRequest(BaseModel):
    """聊天流式请求体（显式 `extra='ignore'`，绝不读取 user_id/tenant_id/role/approved）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        validation_alias=AliasChoices("message", "query", "user_input"),
        description="用户输入文本",
    )
    thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="可选会话线程 ID；省略时由网关自动分配并绑定当前登录者身份",
    )


class HITLResumeRequest(BaseModel):
    """人工审批恢复请求体（身份与权限一律从 JWT 解析的 ExecutionContext 校验）。"""

    model_config = ConfigDict(extra="ignore")

    thread_id: str = Field(..., min_length=1, max_length=128, description="待恢复的挂起线程 ID")
    decision: Literal["approve", "reject"] = Field(
        ..., description="人工审批动作：approve（批准放行）或 reject（驳回终止）"
    )
    comment: str = Field(
        default="",
        max_length=500,
        description="审批人备注说明",
    )


# ============================================================================
# 2. 实时 SSE 流式事件收集器与 ToolRuntime 生命周期 Hook
# ============================================================================


@dataclass
class StreamSessionCollector:
    """单次流式 HTTP 请求专属的异步事件队列与工具结果收集器。"""

    thread_id: str
    trace_id: str
    domain: Literal["customer", "merchant"]
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = field(
        default_factory=asyncio.Queue
    )
    current_agent: str = "Router"
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    errors_by_call_id: dict[str, ErrorPayload] = field(default_factory=dict)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.queue.put_nowait((event_type, payload))

    def close(self) -> None:
        self.queue.put_nowait(None)


_ACTIVE_STREAM_COLLECTOR_VAR: ContextVar[StreamSessionCollector | None] = ContextVar(
    "_gateway_active_stream_collector",
    default=None,
)


def _infer_agent_for_tool(tool_name: str, fallback_agent: str) -> str:
    if fallback_agent and fallback_agent not in ("Router", "B-Orchestrator"):
        return fallback_agent
    prefix = tool_name.split("__", 1)[0] if "__" in tool_name else ""
    if prefix == "bi":
        return "AnalyticsAgent"
    if prefix == "market_erp" or tool_name == "pim__draft_sku_proposal":
        return "SourcingAgent"
    if prefix in ("oms", "logistics"):
        return "OrderAgent"
    if prefix == "pim":
        return "ShoppingGuideAgent"
    return fallback_agent or "Agent"


class GatewaySSEToolHook(ToolLifecycleHook):
    """将 `ToolRuntime` 的 `before_tool_call` / `after_tool_call` / `on_tool_error` 实时桥接为前端 SSE 事件，
    并同步记录补充错误码与拦截原因供 `/api/audit/logs` 查询。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._collectors_by_trace: dict[str, StreamSessionCollector] = {}
        self._audit_enrichment: dict[tuple[str, str], dict[str, Any]] = {}

    def register_collector(self, collector: StreamSessionCollector) -> None:
        self._collectors_by_trace[collector.trace_id] = collector

    def unregister_collector(self, trace_id: str) -> None:
        self._collectors_by_trace.pop(trace_id, None)

    def get_audit_enrichment(self, trace_id: str, tool_call_id: str) -> dict[str, Any]:
        return self._audit_enrichment.get((trace_id, tool_call_id), {})

    def _resolve_collector(self, context: ExecutionContext) -> StreamSessionCollector | None:
        return _ACTIVE_STREAM_COLLECTOR_VAR.get() or self._collectors_by_trace.get(
            context.trace_id
        )

    def _resolve_risk_and_domain(self, tool_name: str) -> tuple[str, str]:
        domain = tool_name.split("__", 1)[0] if "__" in tool_name else "unknown"
        policy = DEFAULT_TOOL_SECURITY_POLICIES.get(tool_name)
        if policy is not None:
            return policy.risk, domain
        if self._registry.has_tool(tool_name):
            snap = self._registry.build_snapshot([tool_name])
            td = snap.resolve(tool_name)
            if td is not None:
                return td.risk, td.domain
        return "unknown", domain

    async def before_tool_call(
        self, tool_call: ToolCall, context: ExecutionContext
    ) -> None:
        collector = self._resolve_collector(context)
        if collector is None:
            return
        risk, domain = self._resolve_risk_and_domain(tool_call.name)
        agent_name = _infer_agent_for_tool(tool_call.name, collector.current_agent)
        collector.emit(
            "tool_start",
            {
                "thread_id": collector.thread_id,
                "trace_id": context.trace_id,
                "tool_call_id": tool_call.tool_call_id,
                "tool_name": tool_call.name,
                "domain": domain,
                "agent": agent_name,
                "risk": risk,
                "arguments": sanitize_sensitive_data(tool_call.arguments),
            },
        )

    async def on_tool_error(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        error: ErrorPayload,
        duration_ms: float,
    ) -> None:
        self._audit_enrichment[(context.trace_id, tool_call.tool_call_id)] = {
            "error_code": error.code,
            "guard_reason": error.message,
            "retryable": error.retryable,
        }
        collector = self._resolve_collector(context)
        if collector is not None:
            collector.errors_by_call_id[tool_call.tool_call_id] = error

    async def after_tool_call(
        self,
        tool_call: ToolCall,
        context: ExecutionContext,
        result: ToolResultMessage,
        duration_ms: float,
    ) -> None:
        if result.status == "hitl_required" and result.hitl is not None:
            self._audit_enrichment[(context.trace_id, tool_call.tool_call_id)] = {
                "error_code": "HITL_APPROVAL_REQUIRED",
                "guard_reason": result.hitl.reason,
                "retryable": False,
            }
        elif result.status == "error" and result.error is not None:
            self._audit_enrichment.setdefault(
                (context.trace_id, tool_call.tool_call_id),
                {
                    "error_code": result.error.code,
                    "guard_reason": result.error.message,
                    "retryable": result.error.retryable,
                },
            )

        collector = self._resolve_collector(context)
        if collector is None:
            return

        risk, domain = self._resolve_risk_and_domain(tool_call.name)
        agent_name = _infer_agent_for_tool(tool_call.name, collector.current_agent)
        recorded_error = result.error or collector.errors_by_call_id.get(tool_call.tool_call_id)
        event_payload: dict[str, Any] = {
            "thread_id": collector.thread_id,
            "trace_id": context.trace_id,
            "tool_call_id": result.tool_call_id,
            "tool_name": result.tool_name or tool_call.name,
            "domain": domain,
            "agent": agent_name,
            "risk": risk,
            "status": result.status,
            "duration_ms": round(duration_ms, 2),
            "data": result.data,
            "error": (
                recorded_error.model_dump(mode="json")
                if recorded_error is not None
                else None
            ),
            "hitl": (
                result.hitl.model_dump(mode="json")
                if result.hitl is not None
                else None
            ),
        }
        collector.tool_events.append(event_payload)
        collector.emit("tool_result", event_payload)


# ============================================================================
# 4. 结构化卡片 / HITL 确认载荷组装辅助函数
# ============================================================================


def _format_sse_frame(event_type: str, data: Mapping[str, Any]) -> str:
    serialized = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {serialized}\n\n"


def _to_plain_dict(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Mapping):
        return dict(obj)
    return None


def _extract_orders_from_traces(traces: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    orders_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for item in traces:
        tname = item.get("tool_name")
        status_val = item.get("status")
        data = item.get("data")
        if status_val != "success" or not isinstance(data, Mapping):
            continue
        if tname == "oms__search_orders":
            for raw_ord in data.get("orders") or []:
                if isinstance(raw_ord, Mapping) and raw_ord.get("order_id"):
                    oid = str(raw_ord["order_id"])
                    if oid not in orders_by_id:
                        ordered_ids.append(oid)
                    orders_by_id[oid] = dict(raw_ord)
        elif tname == "oms__update_address" and data.get("order_id"):
            oid = str(data["order_id"])
            if oid in orders_by_id:
                orders_by_id[oid] = {
                    **orders_by_id[oid],
                    "status": data.get("status", orders_by_id[oid].get("status")),
                    "buyer_name": data.get("receiver_name", orders_by_id[oid].get("buyer_name")),
                    "buyer_phone": data.get("receiver_phone", orders_by_id[oid].get("buyer_phone")),
                    "shipping_address": data.get(
                        "shipping_address", orders_by_id[oid].get("shipping_address")
                    ),
                }
        elif tname == "oms__create_refund" and data.get("order_id"):
            oid = str(data["order_id"])
            if oid in orders_by_id:
                orders_by_id[oid] = {
                    **orders_by_id[oid],
                    "status": data.get("order_status", "refunded"),
                    "refund_id": data.get("refund_id"),
                }
    return [orders_by_id[oid] for oid in ordered_ids]


def _build_hitl_interrupt_payload(
    *,
    thread_id: str,
    trace_id: str,
    raw_interrupt: Mapping[str, Any],
    state_values: Mapping[str, Any],
    turn_tool_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    pending_gate = PendingHITLGatePayload.model_validate(
        {
            k: v
            for k, v in raw_interrupt.items()
            if k in PendingHITLGatePayload.model_fields
        }
    )
    sanitized_args = (
        dict(pending_gate.hitl_required.sanitized_arguments)
        if pending_gate.hitl_required is not None
        else sanitize_sensitive_data(pending_gate.tool_call.arguments)
    )
    risk_level = (
        pending_gate.hitl_required.risk
        if pending_gate.hitl_required is not None
        else "medium"
    )

    refund_details: dict[str, Any] | None = None
    if pending_gate.tool_call.name == "oms__create_refund":
        target_order_id = str(sanitized_args.get("order_id", ""))
        combined_traces = list(state_values.get("c_tool_trace") or []) + list(turn_tool_events)
        matched_order = next(
            (
                o
                for o in _extract_orders_from_traces(combined_traces)
                if o.get("order_id") == target_order_id
            ),
            {},
        )
        refund_details = {
            "order_id": target_order_id,
            "refund_amount": float(sanitized_args.get("refund_amount", 0.0)),
            "reason": str(sanitized_args.get("reason", "")),
            "sku_id": matched_order.get("sku_id"),
            "sku_name": matched_order.get("sku_name"),
            "quantity": matched_order.get("quantity"),
            "total_amount": matched_order.get("total_amount"),
            "order_status": matched_order.get("status"),
            "created_at": matched_order.get("created_at"),
            "buyer_name": matched_order.get("buyer_name"),
            "buyer_phone": matched_order.get("buyer_phone"),
            "shipping_address": matched_order.get("shipping_address"),
        }

    proposal_dict = _to_plain_dict(
        raw_interrupt.get("selection_proposal") or state_values.get("selection_proposal")
    )
    diagnosis_dict = _to_plain_dict(state_values.get("diagnosis_artifact"))

    return {
        "thread_id": thread_id,
        "trace_id": trace_id,
        "domain": pending_gate.domain,
        "gate_type": pending_gate.gate_type,
        "agent": pending_gate.agent_name,
        "tool_call_id": pending_gate.tool_call.tool_call_id,
        "tool_name": pending_gate.tool_call.name,
        "risk": risk_level,
        "permission": (
            pending_gate.hitl_required.permission
            if pending_gate.hitl_required is not None
            else "oms:orders:update_address"
        ),
        "reason": pending_gate.prompt_to_human,
        "arguments": sanitized_args,
        "refund_details": refund_details,
        "selection_proposal": proposal_dict,
        "diagnosis_artifact": diagnosis_dict,
    }


def _build_final_artifacts(
    *,
    state_values: Mapping[str, Any],
    turn_tool_events: Sequence[Mapping[str, Any]],
    include_checkpoint_c_traces: bool = False,
) -> dict[str, Any]:
    sku_comparisons: list[dict[str, Any]] = []
    search_results: list[dict[str, Any]] = []
    stock_checks: list[dict[str, Any]] = []
    logistics_packages: list[dict[str, Any]] = []
    refund_results: list[dict[str, Any]] = []
    address_updates: list[dict[str, Any]] = []

    source_traces: list[Mapping[str, Any]] = []
    if include_checkpoint_c_traces:
        source_traces.extend(state_values.get("c_tool_trace") or [])
    source_traces.extend(turn_tool_events)

    seen_logistics: set[str] = set()
    seen_refunds: set[str] = set()
    for ev in source_traces:
        if ev.get("status") != "success" or not isinstance(ev.get("data"), Mapping):
            continue
        tname = ev.get("tool_name")
        data = dict(ev["data"])
        if tname == "pim__compare_skus" and ev in turn_tool_events:
            sku_comparisons.append(data)
        elif tname == "pim__hybrid_search" and ev in turn_tool_events:
            search_results.append(data)
        elif tname == "pim__check_stock" and ev in turn_tool_events:
            stock_checks.append(data)
        elif tname == "logistics__track_pkg":
            tno = str(data.get("tracking_no", ""))
            if tno and tno not in seen_logistics:
                seen_logistics.add(tno)
                logistics_packages.append(data)
        elif tname == "oms__create_refund":
            rid = str(data.get("refund_id", ""))
            if rid and rid not in seen_refunds:
                seen_refunds.add(rid)
                refund_results.append(data)
        elif tname == "oms__update_address" and ev in turn_tool_events:
            address_updates.append(data)

    orders = _extract_orders_from_traces(source_traces)

    diagnosis_obj = state_values.get("diagnosis_artifact")
    proposal_obj = state_values.get("selection_proposal")
    diagnosis_dict = (
        diagnosis_obj.model_dump(mode="json")
        if isinstance(diagnosis_obj, DiagnosisArtifact)
        else _to_plain_dict(diagnosis_obj)
    )
    proposal_dict = (
        proposal_obj.model_dump(mode="json")
        if isinstance(proposal_obj, SelectionProposal)
        else _to_plain_dict(proposal_obj)
    )

    return {
        "sku_comparisons": sku_comparisons,
        "search_results": search_results,
        "stock_checks": stock_checks,
        "orders": orders,
        "logistics": logistics_packages,
        "refund_results": refund_results,
        "address_updates": address_updates,
        "diagnosis_artifact": diagnosis_dict,
        "selection_proposal": proposal_dict,
        "committed_proposal": _to_plain_dict(state_values.get("b_committed_proposal")),
        "approval_status": state_values.get("b_approval_status")
        or (
            "approved"
            if isinstance(state_values.get("c_last_hitl_decision"), Mapping)
            and state_values["c_last_hitl_decision"].get("approved")
            else (
                "rejected"
                if state_values.get("c_confirmation_declined")
                else None
            )
        ),
        "circuit_breaker_triggered": bool(state_values.get("c_circuit_breaker_triggered", False)),
        "circuit_breaker_reason": state_values.get("c_circuit_breaker_reason"),
    }


# ============================================================================
# 5. FastAPI 应用与全链路运行时装配
# ============================================================================


@dataclass
class GatewayRuntimeContainer:
    settings: AppSettings
    mcp_cluster: ManagedMCPCluster | None
    mcp_endpoints: dict[str, str]
    mcp_manager: MCPClientManager
    registry: ToolRegistry
    sse_hook: GatewaySSEToolHook
    audit_store: AuditStore
    audit_backend: str
    checkpointer_backend: str
    tool_runtime: ToolRuntime
    orchestrator: MultiAgentOrchestrator
    llm_backend: str


def create_gateway_app(
    *,
    mcp_endpoints: Mapping[str, str] | None = None,
    llm: AsyncAgentLLM | None = None,
    jwt_secret: str | None = None,
    cors_origins: Sequence[str] | None = None,
    app_settings: AppSettings | None = None,
    audit_store: AuditStore | None = None,
) -> FastAPI:
    """创建电商多 Agent 全栈 FastAPI 网关应用实例。"""
    initial_settings = app_settings or get_settings()
    effective_jwt_secret = (
        jwt_secret
        if jwt_secret is not None
        else (app_settings.JWT_SECRET if app_settings is not None else None)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_cfg = app_settings or get_settings()
        configure_structured_logging(active_cfg.LOG_LEVEL)
        managed_cluster: ManagedMCPCluster | None = None

        if mcp_endpoints is not None:
            resolved_endpoints = dict(mcp_endpoints)
            mcp_manager = MCPClientManager.from_settings(
                active_cfg,
                endpoint_overrides=resolved_endpoints,
            )
        elif active_cfg.MCP_EXTERNAL_ENDPOINTS_JSON:
            resolved_endpoints = active_cfg.mcp_domain_urls()
            mcp_manager = MCPClientManager.from_settings(
                active_cfg,
                endpoint_overrides=resolved_endpoints,
            )
        elif active_cfg.MCP_SPAWN_LOCAL_SUBPROCESS:
            managed_cluster = ManagedMCPCluster()
            resolved_endpoints = await managed_cluster.start()
            configs = build_mcp_server_configs(
                active_cfg,
                endpoint_overrides=resolved_endpoints,
            )
            mcp_manager = MCPClientManager(
                configs,
                default_timeout_seconds=active_cfg.MCP_CLIENT_TIMEOUT_SECONDS,
            )
        else:
            # 生产 / Docker 独立服务模式：绝不拉起任何本地子进程，直连 settings 中的 5 域远程 URL
            resolved_endpoints = active_cfg.mcp_domain_urls()
            mcp_manager = MCPClientManager.from_settings(active_cfg)

        registry = ToolRegistry()

        async with AsyncExitStack() as stack:
            try:
                await mcp_manager.__aenter__()
                await mcp_manager.discover_and_register(registry)

                checkpointer, checkpointer_backend = await stack.enter_async_context(
                    checkpointer_lifespan(active_cfg)
                )

                if audit_store is not None:
                    await audit_store.initialize()
                    active_audit_store = audit_store
                    audit_backend = f"injected:{type(audit_store).__name__}"
                else:
                    active_audit_store, audit_backend = await stack.enter_async_context(
                        audit_store_lifespan(active_cfg)
                    )

                sse_hook = GatewaySSEToolHook(registry)
                runtime_hooks: list[Any] = [mcp_manager.create_runtime_hook(), sse_hook]
                langfuse_hook = create_langfuse_tool_hook(active_cfg)
                if langfuse_hook is not None:
                    runtime_hooks.append(langfuse_hook)

                tool_runtime = ToolRuntime(
                    hooks=runtime_hooks,
                    audit_store=active_audit_store,
                    timeout_seconds=active_cfg.TOOL_RUNTIME_TIMEOUT_SECONDS,
                )

                if llm is not None:
                    active_llm = llm
                    llm_label = f"injected:{type(llm).__name__}"
                else:
                    active_llm, llm_label = build_configured_llm(active_cfg)

                orchestrator = MultiAgentOrchestrator(
                    registry=registry,
                    tool_runtime=tool_runtime,
                    mcp_manager=mcp_manager,
                    llm=active_llm,
                    checkpointer=checkpointer,
                )

                app.state.container = GatewayRuntimeContainer(
                    settings=active_cfg,
                    mcp_cluster=managed_cluster,
                    mcp_endpoints=resolved_endpoints,
                    mcp_manager=mcp_manager,
                    registry=registry,
                    sse_hook=sse_hook,
                    audit_store=active_audit_store,
                    audit_backend=audit_backend,
                    checkpointer_backend=checkpointer_backend,
                    tool_runtime=tool_runtime,
                    orchestrator=orchestrator,
                    llm_backend=llm_label,
                )
                yield
            finally:
                await mcp_manager.aclose()
                if managed_cluster is not None:
                    await managed_cluster.stop()

    app = FastAPI(
        title="YunShang Womenswear Multi-Agent Gateway",
        version="1.0.0",
        description="五层解耦女装电商多 Agent 双域隔离全栈 API 网关（FastAPI + LangGraph + ToolRuntime + 5-Domain MCP）",
        lifespan=lifespan,
    )

    # 先挂载 JWT 鉴权中间件，再挂载最外层 CORS 中间件，确保预检与 401/403 响应均携带完整 CORS 头
    app.add_middleware(JWTExecutionContextMiddleware, jwt_secret=effective_jwt_secret)

    allowed_origins = list(cors_origins or initial_settings.GATEWAY_CORS_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
        expose_headers=["X-Trace-Id", "X-Thread-Id"],
    )

    def _get_container(request: Request) -> GatewayRuntimeContainer:
        container = getattr(request.app.state, "container", None)
        if container is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="网关编排引擎与 MCP 集群尚未完成初始化",
            )
        return container

    # ------------------------------------------------------------------
    # 健康检查与登录鉴权路由
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health_check(request: Request) -> dict[str, Any]:
        container = _get_container(request)
        return {
            "status": "ok",
            "mcp_domains": list(container.mcp_manager.domains),
            "registered_tools_count": container.registry.registered_count,
            "llm_backend": container.llm_backend,
            "checkpointer_backend": container.checkpointer_backend,
            "audit_backend": container.audit_backend,
            "spawn_local_subprocess": container.mcp_cluster is not None,
        }

    @app.get("/api/auth/accounts")
    async def list_preset_accounts(
        primary_only: bool = Query(default=False, description="是否仅返回主工作台双视角演示账号"),
    ) -> dict[str, Any]:
        accounts = [
            acc.to_public_dict()
            for acc in PRESET_ACCOUNTS.values()
            if (not primary_only or acc.primary_demo)
        ]
        return {"accounts": accounts}

    @app.post("/api/auth/login")
    async def login_preset_account(body: LoginRequest) -> dict[str, Any]:
        account = PRESET_ACCOUNTS.get(body.account_id.strip())
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"未知的测试账号标识: {body.account_id!r}（支持 customer_01 / merchant_01）",
            )
        token, expires_in = issue_access_token(
            account,
            secret=effective_jwt_secret,
            app_settings=app_settings,
        )
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "account": account.to_public_dict(),
        }

    @app.get("/api/auth/me")
    async def get_current_identity(
        request: Request,
        exec_ctx: ExecutionContext = Depends(require_execution_context),
    ) -> dict[str, Any]:
        claims = getattr(request.state, "jwt_claims", {}) or {}
        account_id = str(claims.get("account_id") or exec_ctx.user_id)
        preset = PRESET_ACCOUNTS.get(account_id)
        return {
            "account_id": account_id,
            "display_name": (
                preset.display_name
                if preset is not None
                else str(claims.get("display_name") or exec_ctx.user_id)
            ),
            "badge": preset.badge if preset is not None else exec_ctx.role,
            "trace_id": exec_ctx.trace_id,
            "tenant_id": exec_ctx.tenant_id,
            "user_id": exec_ctx.user_id,
            "role": exec_ctx.role,
            "permissions": sorted(exec_ctx.permissions),
        }

    # ------------------------------------------------------------------
    # 核心流式编排与 HITL 审批断点恢复路由
    # ------------------------------------------------------------------

    async def _stream_graph_execution(
        *,
        container: GatewayRuntimeContainer,
        graph_input: Any,
        graph_config: dict[str, Any],
        collector: StreamSessionCollector,
        exec_context: ExecutionContext | None = None,
        include_checkpoint_c_traces: bool = False,
    ) -> AsyncIterator[str]:
        container.sse_hook.register_collector(collector)
        lf_handler = build_langfuse_callback_handler(
            container.settings,
            context=exec_context,
            session_id=collector.thread_id,
        )
        effective_graph_config = dict(graph_config)
        if lf_handler is not None:
            effective_graph_config["callbacks"] = [lf_handler]

        async def _drive_graph() -> None:
            token = _ACTIVE_STREAM_COLLECTOR_VAR.set(collector)
            obs_cm = bind_observability_context(
                exec_context,
                trace_id=collector.trace_id,
                role=collector.domain,
            )
            obs_cm.__enter__()
            interrupted_raw: Mapping[str, Any] | None = None
            try:
                async for mode, chunk in container.orchestrator.graph.astream(
                    graph_input,
                    config=effective_graph_config,
                    stream_mode=["tasks", "updates"],
                ):
                    if mode == "tasks" and isinstance(chunk, Mapping):
                        # 仅在节点刚启动（含 input 字段，尚未产出 result）时发送 thinking 事件
                        if "input" in chunk and "result" not in chunk:
                            node_name = str(chunk.get("name", ""))
                            if node_name in NODE_DISPLAY_METADATA:
                                agent_label, desc = NODE_DISPLAY_METADATA[node_name]
                                collector.current_agent = agent_label
                                thinking_payload: dict[str, Any] = {
                                    "thread_id": collector.thread_id,
                                    "trace_id": collector.trace_id,
                                    "node": node_name,
                                    "agent": agent_label,
                                    "domain": collector.domain,
                                    "message": desc,
                                }
                                if node_name == "b_contract_handoff" and isinstance(
                                    chunk.get("input"), Mapping
                                ):
                                    diag_raw = chunk["input"].get("diagnosis_artifact")
                                    diag_dict = _to_plain_dict(diag_raw)
                                    if diag_dict is not None:
                                        thinking_payload["diagnosis_artifact"] = diag_dict
                                collector.emit("thinking", thinking_payload)
                    elif mode == "updates" and isinstance(chunk, Mapping):
                        if "__interrupt__" in chunk and chunk["__interrupt__"]:
                            first_intr = chunk["__interrupt__"][0]
                            intr_val = getattr(first_intr, "value", first_intr)
                            if isinstance(intr_val, Mapping):
                                interrupted_raw = intr_val

                snapshot = await container.orchestrator.graph.aget_state(graph_config)
                state_values = dict(snapshot.values) if snapshot and snapshot.values else {}

                # 若发生 HITL interrupt()，构建富结构化确认载荷推送给前端弹窗
                if interrupted_raw is None and snapshot and snapshot.tasks:
                    for t in snapshot.tasks:
                        intrs = getattr(t, "interrupts", None) or ()
                        if intrs:
                            val = getattr(intrs[0], "value", None)
                            if isinstance(val, Mapping):
                                interrupted_raw = val
                                break

                if interrupted_raw is not None:
                    hitl_payload = _build_hitl_interrupt_payload(
                        thread_id=collector.thread_id,
                        trace_id=collector.trace_id,
                        raw_interrupt=interrupted_raw,
                        state_values=state_values,
                        turn_tool_events=collector.tool_events,
                    )
                    collector.emit("hitl_interrupt", hitl_payload)
                else:
                    final_text = str(state_values.get("final_response") or "")
                    artifacts = _build_final_artifacts(
                        state_values=state_values,
                        turn_tool_events=collector.tool_events,
                        include_checkpoint_c_traces=include_checkpoint_c_traces,
                    )
                    collector.emit(
                        "final_answer",
                        {
                            "thread_id": collector.thread_id,
                            "trace_id": collector.trace_id,
                            "domain": collector.domain,
                            "answer": final_text,
                            "final_response": final_text,
                            "artifacts": artifacts,
                        },
                    )
            except Exception as exc:
                collector.emit(
                    "error",
                    {
                        "thread_id": collector.thread_id,
                        "trace_id": collector.trace_id,
                        "code": "GRAPH_EXECUTION_ERROR",
                        "message": f"编排流执行异常: {exc}",
                    },
                )
            finally:
                obs_cm.__exit__(None, None, None)
                _ACTIVE_STREAM_COLLECTOR_VAR.reset(token)
                collector.close()

        runner_task = asyncio.create_task(_drive_graph())
        try:
            while True:
                item = await collector.queue.get()
                if item is None:
                    break
                event_type, payload = item
                yield _format_sse_frame(event_type, payload)
            await runner_task
        finally:
            if not runner_task.done():
                runner_task.cancel()
                try:
                    await runner_task
                except BaseException:
                    pass
            container.sse_hook.unregister_collector(collector.trace_id)

    @app.post("/api/chat/stream")
    async def chat_stream(
        body: ChatStreamRequest,
        request: Request,
        exec_ctx: ExecutionContext = Depends(require_execution_context),
    ) -> StreamingResponse:
        container = _get_container(request)
        cleaned_msg = body.message.strip()
        if not cleaned_msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="消息内容不得为空白字符串",
            )

        thread_id = (
            body.thread_id.strip()
            if body.thread_id and body.thread_id.strip()
            else f"thd_{exec_ctx.role}_{uuid.uuid4().hex[:10]}"
        )
        graph_config = {"configurable": {"thread_id": thread_id}}

        # 若客户端传入已有 thread_id，严格校验线程归属边界，防止跨用户/跨角色复用或劫持他人会话
        existing_snapshot = await container.orchestrator.graph.aget_state(graph_config)
        if existing_snapshot and existing_snapshot.values:
            raw_owner_ctx = existing_snapshot.values.get("execution_context")
            if raw_owner_ctx is not None:
                owner_ctx = parse_execution_context(raw_owner_ctx)
                if (
                    owner_ctx.tenant_id != exec_ctx.tenant_id
                    or owner_ctx.user_id != exec_ctx.user_id
                    or owner_ctx.role != exec_ctx.role
                ):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="禁止跨租户、跨用户或跨角色访问他人会话线程",
                    )

        collector = StreamSessionCollector(
            thread_id=thread_id,
            trace_id=exec_ctx.trace_id,
            domain=exec_ctx.role,
        )
        graph_input: dict[str, Any] = {
            "user_input": cleaned_msg,
            "execution_context": _dump_context_for_checkpoint(exec_ctx),
        }

        return StreamingResponse(
            _stream_graph_execution(
                container=container,
                graph_input=graph_input,
                graph_config=graph_config,
                collector=collector,
                exec_context=exec_ctx,
                include_checkpoint_c_traces=False,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": exec_ctx.trace_id,
                "X-Thread-Id": thread_id,
            },
        )

    @app.post("/api/hitl/resume")
    async def hitl_resume_stream(
        body: HITLResumeRequest,
        request: Request,
        caller_ctx: ExecutionContext = Depends(require_execution_context),
    ) -> StreamingResponse:
        container = _get_container(request)
        thread_id = body.thread_id.strip()
        base_config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

        snapshot = await container.orchestrator.graph.aget_state(base_config)
        if not snapshot or not snapshot.values:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未找到指定的会话线程 '{thread_id}'",
            )

        state_values = snapshot.values
        raw_owner_ctx = state_values.get("execution_context")
        if raw_owner_ctx is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="目标线程缺少可信身份上下文，拒绝恢复执行",
            )

        owner_ctx = parse_execution_context(raw_owner_ctx)
        routed_domain = state_values.get("routed_domain")

        # 严格校验租户、角色、双域边界与会话本人归属（C 端越权恢复 B 端或跨用户一律 403）
        if caller_ctx.tenant_id != owner_ctx.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="跨租户 HITL 审批越权被拒绝",
            )
        if caller_ctx.role != owner_ctx.role or (
            routed_domain in ("customer", "merchant") and caller_ctx.role != routed_domain
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"跨双域角色越权被拒绝：当前角色 '{caller_ctx.role}' 无权恢复 '{owner_ctx.role}' 域挂起线程",
            )
        if caller_ctx.user_id != owner_ctx.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="跨用户越权被拒绝：仅允许会话所属主体恢复本人挂起审批流程",
            )

        pending_raw: Mapping[str, Any] | None = (
            state_values.get("c_pending_hitl") or state_values.get("b_pending_hitl")
        )
        if pending_raw is None and snapshot.tasks:
            for t in snapshot.tasks:
                intrs = getattr(t, "interrupts", None) or ()
                if intrs:
                    val = getattr(intrs[0], "value", None)
                    if isinstance(val, Mapping):
                        pending_raw = val
                        break

        if not snapshot.next or pending_raw is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"会话线程 '{thread_id}' 当前未处于待人工审批挂起状态",
            )

        pending_gate = PendingHITLGatePayload.model_validate(
            {
                k: v
                for k, v in pending_raw.items()
                if k in PendingHITLGatePayload.model_fields
            }
        )
        if (
            pending_gate.hitl_required is not None
            and pending_gate.hitl_required.permission not in caller_ctx.permissions
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"当前身份缺少审批放行高危工具 '{pending_gate.tool_call.name}' "
                    f"所需的权限 '{pending_gate.hitl_required.permission}'"
                ),
            )

        is_approved = body.decision == "approve"
        resume_configurable: dict[str, Any] = {"thread_id": thread_id}

        if is_approved:
            digest = (
                pending_gate.hitl_required.arguments_digest
                if pending_gate.hitl_required is not None
                else None
            )
            approved_ctx = caller_ctx.with_hitl_approval(
                tool_name=pending_gate.tool_call.name,
                tool_call_id=pending_gate.tool_call.tool_call_id,
                approver_id=caller_ctx.user_id,
                arguments_digest=digest,
            )
            resume_configurable["execution_context"] = approved_ctx
            active_trace_id = approved_ctx.trace_id
        else:
            active_trace_id = caller_ctx.trace_id

        resume_payload = {
            "decision": body.decision,
            "approved": is_approved,
            "approver_id": caller_ctx.user_id,
            "comment": body.comment.strip(),
        }
        collector = StreamSessionCollector(
            thread_id=thread_id,
            trace_id=active_trace_id,
            domain=caller_ctx.role,
        )

        return StreamingResponse(
            _stream_graph_execution(
                container=container,
                graph_input=Command(resume=resume_payload),
                graph_config={"configurable": resume_configurable},
                collector=collector,
                exec_context=caller_ctx,
                include_checkpoint_c_traces=(caller_ctx.role == "customer"),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": active_trace_id,
                "X-Thread-Id": thread_id,
            },
        )

    # ------------------------------------------------------------------
    # 结构化审计日志查询路由（租户与角色可见性隔离）
    # ------------------------------------------------------------------

    @app.get("/api/audit/logs")
    async def get_audit_logs(
        request: Request,
        trace_id: str | None = Query(default=None, description="按链路 trace_id 精确过滤"),
        status_filter: str | None = Query(
            default=None,
            alias="status",
            description="按状态过滤（success / error / hitl_required）",
        ),
        tool_name: str | None = Query(default=None, description="按工具限定名过滤"),
        limit: int = Query(default=100, ge=1, le=500, description="最大返回记录条数"),
        caller_ctx: ExecutionContext = Depends(require_execution_context),
    ) -> dict[str, Any]:
        container = _get_container(request)
        records = await container.audit_store.query(
            caller_ctx,
            trace_id=trace_id,
            status=status_filter,
            tool_name=tool_name,
            limit=limit,
        )
        visible_items = [rec.model_dump(mode="json") for rec in records]

        return {
            "total": len(visible_items),
            "viewer": {
                "tenant_id": caller_ctx.tenant_id,
                "user_id": caller_ctx.user_id,
                "role": caller_ctx.role,
            },
            "logs": visible_items,
        }

    return app


app = create_gateway_app()


__all__ = [
    "ChatStreamRequest",
    "DOMAIN_SERVER_MODULES",
    "GatewayRuntimeContainer",
    "GatewaySSEToolHook",
    "HITLResumeRequest",
    "LoginRequest",
    "ManagedMCPCluster",
    "app",
    "build_configured_llm",
    "create_gateway_app",
]
