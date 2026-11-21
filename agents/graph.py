"""LangGraph 多 Agent 双域硬隔离编排核心（Root Router + C 端实时服务子图 + B 端决策流水线子图）。

严格落地的架构与安全红线：
1. 五层解耦：所有工具调用强制经过 `ToolRuntime` -> `MCPClientManager` -> 独立域 MCP Server，
   所有知识库/规则文档读取强制经过受白名单约束的 `MCPClientManager.read_resource()`，
   严禁直接访问数据库、裸 HTTP 或导入 `mcp_servers/mock_data.py`；
2. Root Router 硬分流：纯代码读取不可变 `ExecutionContext.role`，`customer` 仅能进入 C 端子图，
   `merchant` 方可进入 B 端子图，绝不交由大模型判断跨双域路由；
3. 最小权限 `ToolSnapshot` 裁剪：
   - `ShoppingGuideAgent`：仅挂载 `pim__hybrid_search`、`pim__compare_skus`、`pim__check_stock`（严禁含 `pim__draft_sku_proposal`）；
   - `OrderAgent`：仅挂载 `oms__search_orders`、`oms__update_address`、`oms__create_refund`、`logistics__track_pkg`；
   - `AnalyticsAgent`：仅挂载 `bi__get_table_schema`、`bi__run_readonly_sql`、`bi__render_chart`；
   - `SourcingAgent`：仅挂载 `market_erp__*` 与 B 端 `pim__*`；
4. C 端防死循环熔断：强制执行 `max_handoffs=4` 与 `max_total_steps=12` 双重计数器守卫；
5. B 端强类型契约交接：`SourcingAgent` 节点绑定 `input_schema=SourcingNodeInputState`，物理剥离上游
   `AnalyticsAgent` 的原始对话与工具日志，仅接收校验通过的冻结 `DiagnosisArtifact`；
6. Checkpointer 与 HITL 断点恢复：默认挂载安全序列化的 `InMemorySaver`，高危动作触发 `HITLRequired`
   时调用 LangGraph `interrupt()` 挂起，经 `Command(resume=...)` 由宿主注入受信任 `ExecutionContext`
   审批凭证后恢复落库并完成 `pim://knowledge/selling-points` 卖点知识库飞轮回写。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver, MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from agents.llm import (
    AsyncAgentLLM,
    LLMMessage,
    create_llm_client,
)
from agents.prompts import (
    ANALYTICS_AGENT_PROMPT,
    B_ORCHESTRATOR_PROMPT,
    C_SUPERVISOR_PROMPT,
    ORDER_AGENT_PROMPT,
    SHOPPING_GUIDE_PROMPT,
    SOURCING_AGENT_PROMPT,
)
from agents.state import (
    OrchestrationState,
    PendingHITLGatePayload,
    SourcingNodeInputState,
    SubAgentExecutionReport,
    SupervisorRouteDecision,
    parse_diagnosis_artifact,
    parse_execution_context,
    parse_selection_proposal,
)
from infra.checkpointer import create_default_checkpointer
from runtime.executor import ToolRuntime
from runtime.mcp_adapter import MCPClientManager, MCPResourcePayload
from runtime.registry import ToolRegistry, ToolSnapshot
from schemas.context import ExecutionContext
from schemas.contracts import (
    DiagnosisArtifact,
    SelectionProposal,
    ToolPermissionError,
    ToolResultMessage,
)

# ============================================================================
# 1. 各子 Agent 最小权限工具快照与 Resource 访问白名单常量
# ============================================================================

SHOPPING_GUIDE_ALLOWED_TOOLS: tuple[str, ...] = (
    "pim__hybrid_search",
    "pim__compare_skus",
    "pim__check_stock",
)

ORDER_AGENT_ALLOWED_TOOLS: tuple[str, ...] = (
    "oms__search_orders",
    "oms__update_address",
    "oms__create_refund",
    "logistics__track_pkg",
)

ANALYTICS_AGENT_ALLOWED_TOOLS: tuple[str, ...] = (
    "bi__get_table_schema",
    "bi__run_readonly_sql",
    "bi__render_chart",
)

SOURCING_AGENT_ALLOWED_TOOLS: tuple[str, ...] = (
    "market_erp__search_trends",
    "market_erp__calc_landed_cost",
    "pim__hybrid_search",
    "pim__compare_skus",
    "pim__check_stock",
    "pim__draft_sku_proposal",
)

SHOPPING_GUIDE_ALLOWED_RESOURCES: frozenset[str] = frozenset(
    {"pim://knowledge/selling-points"}
)
ORDER_AGENT_ALLOWED_RESOURCES: frozenset[str] = frozenset(
    {"oms://docs/order-state-machine", "logistics://docs/carrier-sla"}
)
ANALYTICS_AGENT_ALLOWED_RESOURCES: frozenset[str] = frozenset(
    {"bi://dictionary/metrics"}
)
SOURCING_AGENT_ALLOWED_RESOURCES: frozenset[str] = frozenset(
    {"market_erp://policies/return-exchange", "pim://knowledge/selling-points"}
)

DEFAULT_MAX_HANDOFFS = 4
DEFAULT_MAX_TOTAL_STEPS = 12
DEFAULT_B_AGENT_MAX_STEPS = 8


@dataclass(frozen=True, kw_only=True)
class AgentToolSnapshots:
    """按子 Agent 职责严格裁剪的只读最小权限 `ToolSnapshot` 集合。"""

    shopping_guide: ToolSnapshot
    order_agent: ToolSnapshot
    analytics_agent: ToolSnapshot
    sourcing_agent: ToolSnapshot


def build_agent_snapshots(registry: ToolRegistry) -> AgentToolSnapshots:
    """从 `ToolRegistry` 裁剪生成四个业务子 Agent 的专属最小权限 `ToolSnapshot`。"""
    shopping_guide = registry.build_snapshot(SHOPPING_GUIDE_ALLOWED_TOOLS)
    order_agent = registry.build_snapshot(ORDER_AGENT_ALLOWED_TOOLS)
    analytics_agent = registry.build_snapshot(ANALYTICS_AGENT_ALLOWED_TOOLS)
    sourcing_agent = registry.build_snapshot(SOURCING_AGENT_ALLOWED_TOOLS)

    # 防御性安全不变量断言：确保 C 端快照绝不混入高危建品或任何 B 端经营/供应链工具
    if "pim__draft_sku_proposal" in shopping_guide:
        raise RuntimeError("安全违例：ShoppingGuideAgent 快照严禁包含 pim__draft_sku_proposal")
    for c_snap in (shopping_guide, order_agent):
        for tool_name in c_snap.allowed_tools:
            if tool_name.startswith(("bi__", "market_erp__")) or tool_name == "pim__draft_sku_proposal":
                raise RuntimeError(f"安全违例：C 端快照违规混入 B 端工具 '{tool_name}'")

    return AgentToolSnapshots(
        shopping_guide=shopping_guide,
        order_agent=order_agent,
        analytics_agent=analytics_agent,
        sourcing_agent=sourcing_agent,
    )


def _dump_context_for_checkpoint(ctx: ExecutionContext) -> dict[str, Any]:
    """将不可变 `ExecutionContext` 转为纯 JSON 可序列化字典供任意 Checkpointer 持久化。"""
    return {
        "trace_id": ctx.trace_id,
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "role": ctx.role,
        "permissions": sorted(ctx.permissions),
        "approved_tool_call_ids": sorted(ctx.approved_tool_call_ids),
        "hitl_approvals": [
            item.model_dump(mode="json") for item in ctx.hitl_approvals
        ],
    }


async def _read_scoped_resource(
    mcp_manager: MCPClientManager,
    uri: str,
    *,
    allowed_uris: frozenset[str],
    agent_name: str,
) -> MCPResourcePayload:
    """受子 Agent 最小权限白名单保护的 MCP Resource 读取辅助函数。"""
    if uri not in allowed_uris:
        raise ToolPermissionError(
            f"子 Agent '{agent_name}' 禁止跨边界读取未授权的 MCP Resource '{uri}'",
            code="RESOURCE_NOT_IN_AGENT_SCOPE",
            details={"agent_name": agent_name, "uri": uri},
        )
    return await mcp_manager.read_resource(uri)


def _serialize_tool_trace_item(
    *,
    agent_name: str,
    result: ToolResultMessage,
) -> dict[str, Any]:
    return {
        "agent_name": agent_name,
        "tool_call_id": result.tool_call_id,
        "tool_name": result.tool_name,
        "status": result.status,
        "error_type": result.error.error_type if result.error is not None else None,
        "error_code": result.error.code if result.error is not None else None,
        "data": result.data,
        "hitl": (
            result.hitl.model_dump(mode="json") if result.hitl is not None else None
        ),
    }


def _is_resume_approved(resume_value: Any) -> bool:
    """解析 `Command(resume=...)` 人工审批决议。"""
    if isinstance(resume_value, bool):
        return resume_value
    if isinstance(resume_value, str):
        return resume_value.strip().lower() in ("approve", "approved", "yes", "true", "confirm", "confirmed")
    if isinstance(resume_value, Mapping):
        if "approved" in resume_value:
            return bool(resume_value["approved"])
        if "confirmed" in resume_value:
            return bool(resume_value["confirmed"])
        decision = str(resume_value.get("decision", "")).strip().lower()
        if decision in ("approve", "approved", "confirm", "confirmed", "accept"):
            return True
        if decision in ("reject", "rejected", "deny", "denied", "cancel", "cancelled"):
            return False
    return False


def _resolve_trusted_resumed_context(
    *,
    base_context: ExecutionContext,
    config: RunnableConfig,
    resume_value: Any,
    pending_gate: PendingHITLGatePayload,
) -> ExecutionContext:
    """在人工审批通过后，由网关/宿主层在受信任 `ExecutionContext` 上签发绑定摘要的审批凭证。

    严格遵守安全红线：
    - 绝不修改模型生成的 `ToolCall.arguments`（严禁注入 `approved=True`）；
    - 若网关已通过 `config["configurable"]["execution_context"]` 传入带凭证的上下文，先校验租户/用户/角色
      与原会话一致再放行；否则由宿主层基于人工审批结果调用 `base_context.with_hitl_approval(...)` 派生。
    """
    configurable = config.get("configurable") or {}
    candidate_ctx_raw = configurable.get("execution_context")
    if candidate_ctx_raw is None and isinstance(resume_value, Mapping):
        candidate_ctx_raw = resume_value.get("execution_context")

    if candidate_ctx_raw is not None:
        candidate_ctx = parse_execution_context(candidate_ctx_raw)
        if (
            candidate_ctx.tenant_id != base_context.tenant_id
            or candidate_ctx.user_id != base_context.user_id
            or candidate_ctx.role != base_context.role
        ):
            raise ToolPermissionError(
                "断点恢复时传入的 ExecutionContext 身份边界（tenant_id/user_id/role）与挂起会话不一致",
                code="CONTEXT_IDENTITY_MISMATCH_ON_RESUME",
            )
        if pending_gate.hitl_required is not None and candidate_ctx.has_valid_approval(
            tool_name=pending_gate.tool_call.name,
            tool_call_id=pending_gate.tool_call.tool_call_id,
            canonical_args_digest=pending_gate.hitl_required.arguments_digest,
        ):
            return candidate_ctx

    approver_id = "trusted_human_reviewer"
    approval_id: str | None = None
    if isinstance(resume_value, Mapping):
        if isinstance(resume_value.get("approver_id"), str) and resume_value["approver_id"].strip():
            approver_id = resume_value["approver_id"].strip()
        if isinstance(resume_value.get("approval_id"), str) and resume_value["approval_id"].strip():
            approval_id = resume_value["approval_id"].strip()

    digest = (
        pending_gate.hitl_required.arguments_digest
        if pending_gate.hitl_required is not None
        else None
    )
    return base_context.with_hitl_approval(
        tool_name=pending_gate.tool_call.name,
        tool_call_id=pending_gate.tool_call.tool_call_id,
        approver_id=approver_id,
        arguments_digest=digest,
        approval_id=approval_id,
    )


def _extract_json_dict(resp_content: str) -> dict[str, Any]:
    cleaned = resp_content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("模型结构化输出必须是 JSON 对象")
    return parsed


# ============================================================================
# 2. Root Router 硬分流函数（绝不调用 LLM）
# ============================================================================


def root_role_router(
    state: OrchestrationState,
) -> Literal["c_domain_subgraph", "b_domain_subgraph"]:
    """Root Router 硬路由：100% 基于受信任 `ExecutionContext.role` 分流 C/B 双域。"""
    exec_ctx = parse_execution_context(state.get("execution_context"))
    if exec_ctx.role == "customer":
        return "c_domain_subgraph"
    if exec_ctx.role == "merchant":
        return "b_domain_subgraph"
    raise ToolPermissionError(
        f"Root Router 拒绝未知角色: {exec_ctx.role!r}",
        code="UNKNOWN_EXECUTION_ROLE",
    )


# ============================================================================
# 3. C 端实时客户服务子图构建（C-Supervisor + ShoppingGuideAgent + OrderAgent）
# ============================================================================


def _attach_c_domain_topology(
    graph: StateGraph[Any],
    *,
    llm: AsyncAgentLLM,
    tool_runtime: ToolRuntime,
    mcp_manager: MCPClientManager,
    snapshots: AgentToolSnapshots,
    max_handoffs: int = DEFAULT_MAX_HANDOFFS,
    max_total_steps: int = DEFAULT_MAX_TOTAL_STEPS,
    connect_start: bool = True,
) -> str:
    """将 C 端实时服务子图拓扑挂载至目标 `StateGraph` 并返回入口节点名 `'c_init'`。"""

    async def c_init_node(state: OrchestrationState) -> dict[str, Any]:
        exec_ctx = parse_execution_context(state.get("execution_context"))
        if exec_ctx.role != "customer":
            raise ToolPermissionError(
                f"C 端实时子图仅允许 customer 角色进入，当前为 '{exec_ctx.role}'",
                code="C_SUBGRAPH_ROLE_VIOLATION",
            )
        return {
            "routed_domain": "customer",
            "execution_context": _dump_context_for_checkpoint(exec_ctx),
            "c_handoff_count": 0,
            "c_total_steps": 0,
            "c_agent_reports": [],
            "c_tool_trace": [],
            "c_resources_read": [],
            "c_circuit_breaker_triggered": False,
            "c_circuit_breaker_reason": None,
            "c_address_confirmed": bool(state.get("c_address_confirmed", False)),
            "c_confirmation_declined": False,
            "c_pending_hitl": None,
        }

    def _synthesize_fallback_c_reply(
        reports: list[dict[str, Any]],
        breaker_reason: str | None = None,
    ) -> str:
        lines = [
            f"- {r.get('summary', '')}"
            for r in reports
            if isinstance(r, Mapping) and r.get("summary")
        ]
        body = (
            "已为您整理当前核实结果：\n" + "\n".join(lines)
            if lines
            else "当前请求因触发会话安全步数保护已暂停进一步调度，请精简诉求后重试或联系人工客服。"
        )
        if breaker_reason:
            return f"{body}\n（系统安全守卫提示：{breaker_reason}）"
        return body

    async def c_supervisor_node(state: OrchestrationState) -> dict[str, Any]:
        exec_ctx = parse_execution_context(state.get("execution_context"))
        if exec_ctx.role != "customer":
            raise ToolPermissionError(
                "C-Supervisor 拒绝非 customer 角色执行",
                code="C_SUPERVISOR_ROLE_DENIED",
            )

        handoffs = int(state.get("c_handoff_count", 0))
        steps = int(state.get("c_total_steps", 0))
        reports = list(state.get("c_agent_reports") or [])

        if state.get("c_circuit_breaker_triggered"):
            reason = state.get("c_circuit_breaker_reason")
            return {
                "c_next_agent": "FINISH",
                "final_response": _synthesize_fallback_c_reply(reports, reason),
            }

        if steps >= max_total_steps:
            reason = (
                f"达到 C 端最大总步数上限（total_steps={steps}/{max_total_steps}），自动熔断收敛"
            )
            return {
                "c_circuit_breaker_triggered": True,
                "c_circuit_breaker_reason": reason,
                "c_next_agent": "FINISH",
                "final_response": _synthesize_fallback_c_reply(reports, reason),
            }

        steps += 1
        report_lines = [
            f"- [SUMMARY:{r['agent_name']}] {r['summary']}"
            for r in reports
        ]
        supervisor_user_content = (
            f"【买家原始诉求】\n{state.get('user_input', '')}\n\n"
            f"【已完成子专员核实报告（共 {len(reports)} 条，当前 handoffs={handoffs}/{max_handoffs}, "
            f"steps={steps}/{max_total_steps}）】\n"
            + ("\n".join(report_lines) if report_lines else "（暂无子专员执行记录）")
        )

        decision = await llm.ainvoke_structured(
            [
                LLMMessage(role="system", content=C_SUPERVISOR_PROMPT),
                LLMMessage(role="user", content=supervisor_user_content),
            ],
            SupervisorRouteDecision,
            agent_name="C-Supervisor",
        )

        if decision.next_agent == "FINISH":
            final_text = (
                decision.final_response.strip()
                if decision.final_response and decision.final_response.strip()
                else _synthesize_fallback_c_reply(reports)
            )
            return {
                "c_total_steps": steps,
                "c_next_agent": "FINISH",
                "final_response": final_text,
            }

        # 检查是否超出最大子图交接次数或最大总步数熔断线
        if handoffs >= max_handoffs or steps >= max_total_steps:
            reason = (
                f"触发 C 端防乒乓死循环熔断守卫（handoffs={handoffs}/{max_handoffs}, "
                f"total_steps={steps}/{max_total_steps}）"
            )
            return {
                "c_total_steps": steps,
                "c_circuit_breaker_triggered": True,
                "c_circuit_breaker_reason": reason,
                "c_next_agent": "FINISH",
                "final_response": _synthesize_fallback_c_reply(reports, reason),
            }

        return {
            "c_handoff_count": handoffs + 1,
            "c_total_steps": steps,
            "c_next_agent": decision.next_agent,
            "c_active_instruction": decision.task_instruction,
        }

    async def shopping_guide_node(state: OrchestrationState) -> dict[str, Any]:
        exec_ctx = parse_execution_context(state.get("execution_context"))
        steps = int(state.get("c_total_steps", 0))
        reports = list(state.get("c_agent_reports") or [])
        tool_trace = list(state.get("c_tool_trace") or [])
        resources_read = list(state.get("c_resources_read") or [])

        kb_res = await _read_scoped_resource(
            mcp_manager,
            "pim://knowledge/selling-points",
            allowed_uris=SHOPPING_GUIDE_ALLOWED_RESOURCES,
            agent_name="ShoppingGuideAgent",
        )
        if "pim://knowledge/selling-points" not in resources_read:
            resources_read.append("pim://knowledge/selling-points")

        prior_order_facts = "\n".join(
            r["summary"] for r in reports if r.get("agent_name") == "OrderAgent"
        )
        local_messages: list[LLMMessage] = [
            LLMMessage(role="system", content=SHOPPING_GUIDE_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"【买家原始诉求】\n{state.get('user_input', '')}\n\n"
                    f"【C-Supervisor 当前派单指令】\n{state.get('c_active_instruction', '')}\n\n"
                    f"【已核实订单背景】\n{prior_order_facts or '暂无'}\n\n"
                    f"【商品卖点知识库（pim://knowledge/selling-points）】\n{kb_res.text}"
                ),
            ),
        ]

        called_tools: list[str] = []
        summary_text = ""
        breaker_triggered = False
        breaker_reason: str | None = None

        while True:
            if steps >= max_total_steps:
                breaker_triggered = True
                breaker_reason = (
                    f"ShoppingGuideAgent 触发最大步数熔断守卫（total_steps={steps}/{max_total_steps}）"
                )
                summary_text = (
                    "[REPORT:ShoppingGuideAgent] 因达到单会话最大安全步数限制，导购检索已安全收敛。"
                )
                break

            steps += 1
            resp = await llm.ainvoke(
                local_messages,
                tools=snapshots.shopping_guide.to_llm_schemas(),
                agent_name="ShoppingGuideAgent",
            )

            if not resp.tool_calls:
                summary_text = resp.content.strip() or "[REPORT:ShoppingGuideAgent] 已完成商品导购核验。"
                break

            local_messages.append(
                LLMMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                )
            )
            batch_results = await tool_runtime.execute_batch(
                resp.tool_calls,
                exec_ctx,
                snapshot=snapshots.shopping_guide,
            )
            for res in batch_results:
                if res.tool_name:
                    called_tools.append(res.tool_name)
                tool_trace.append(
                    _serialize_tool_trace_item(
                        agent_name="ShoppingGuideAgent", result=res
                    )
                )
                local_messages.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=res.tool_call_id,
                        name=res.tool_name,
                        content=res.model_dump_json(),
                    )
                )

        report = SubAgentExecutionReport(
            agent_name="ShoppingGuideAgent",
            task_instruction=state.get("c_active_instruction", ""),
            summary=summary_text,
            tool_names_called=called_tools,
            resources_read=["pim://knowledge/selling-points"],
        )
        reports.append(report.model_dump(mode="json"))

        update: dict[str, Any] = {
            "c_total_steps": steps,
            "c_agent_reports": reports,
            "c_tool_trace": tool_trace,
            "c_resources_read": resources_read,
        }
        if breaker_triggered:
            update["c_circuit_breaker_triggered"] = True
            update["c_circuit_breaker_reason"] = breaker_reason
        return update

    async def order_agent_node(state: OrchestrationState) -> dict[str, Any]:
        exec_ctx = parse_execution_context(state.get("execution_context"))
        steps = int(state.get("c_total_steps", 0))
        reports = list(state.get("c_agent_reports") or [])
        tool_trace = list(state.get("c_tool_trace") or [])
        resources_read = list(state.get("c_resources_read") or [])

        oms_policy = await _read_scoped_resource(
            mcp_manager,
            "oms://docs/order-state-machine",
            allowed_uris=ORDER_AGENT_ALLOWED_RESOURCES,
            agent_name="OrderAgent",
        )
        if "oms://docs/order-state-machine" not in resources_read:
            resources_read.append("oms://docs/order-state-machine")

        confirmation_flags = ""
        if state.get("c_address_confirmed"):
            confirmation_flags += "\n[ADDRESS_CONFIRMED_BY_USER]"
        if state.get("c_confirmation_declined"):
            confirmation_flags += "\n[CONFIRMATION_DECLINED]"

        prior_order_facts = "\n".join(
            r["summary"] for r in reports if r.get("agent_name") == "OrderAgent"
        )
        local_messages: list[LLMMessage] = [
            LLMMessage(role="system", content=ORDER_AGENT_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"【买家原始诉求】\n{state.get('user_input', '')}\n\n"
                    f"【C-Supervisor 当前派单指令】\n{state.get('c_active_instruction', '')}"
                    f"{confirmation_flags}\n\n"
                    f"【本轮已核实订单摘要】\n{prior_order_facts or '暂无'}\n\n"
                    f"【订单状态机政策（oms://docs/order-state-machine）】\n{oms_policy.text}"
                ),
            ),
        ]

        called_tools: list[str] = []
        summary_text = ""
        breaker_triggered = False
        breaker_reason: str | None = None

        while True:
            if steps >= max_total_steps:
                breaker_triggered = True
                breaker_reason = (
                    f"OrderAgent 触发最大步数熔断守卫（total_steps={steps}/{max_total_steps}）"
                )
                summary_text = (
                    "[REPORT:OrderAgent:lookup] 因达到单会话最大安全步数限制，订单操作已安全收敛。"
                )
                break

            steps += 1
            resp = await llm.ainvoke(
                local_messages,
                tools=snapshots.order_agent.to_llm_schemas(),
                agent_name="OrderAgent",
            )

            # 情况 A：模型显式请求向买家二次确认敏感操作（如未发货订单改址确认）
            if resp.require_user_confirmation and resp.pending_confirmation_call is not None:
                gate = PendingHITLGatePayload(
                    domain="customer",
                    gate_type="address_confirmation",
                    agent_name="OrderAgent",
                    tool_call=resp.pending_confirmation_call,
                    hitl_required=None,
                    prompt_to_human=(
                        resp.confirmation_prompt
                        or f"请确认是否执行订单操作 {resp.pending_confirmation_call.name}"
                    ),
                )
                return {
                    "c_total_steps": steps,
                    "c_tool_trace": tool_trace,
                    "c_resources_read": resources_read,
                    "c_pending_hitl": gate.model_dump(mode="json"),
                }

            if not resp.tool_calls:
                summary_text = resp.content.strip() or "[REPORT:OrderAgent:lookup] 已完成订单核实。"
                break

            local_messages.append(
                LLMMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                )
            )
            batch_results = await tool_runtime.execute_batch(
                resp.tool_calls,
                exec_ctx,
                snapshot=snapshots.order_agent,
            )

            call_by_id = {tc.tool_call_id: tc for tc in resp.tool_calls}
            for res in batch_results:
                if res.tool_name:
                    called_tools.append(res.tool_name)
                tool_trace.append(
                    _serialize_tool_trace_item(agent_name="OrderAgent", result=res)
                )

                # 情况 B：高风险动作（如 oms__create_refund）触发 ToolRuntime HITLRequired 阻断
                if res.status == "hitl_required" and res.hitl is not None:
                    orig_call = call_by_id[res.tool_call_id]
                    gate = PendingHITLGatePayload(
                        domain="customer",
                        gate_type="high_risk_tool",
                        agent_name="OrderAgent",
                        tool_call=orig_call,
                        hitl_required=res.hitl,
                        prompt_to_human=res.hitl.reason,
                    )
                    return {
                        "c_total_steps": steps,
                        "c_tool_trace": tool_trace,
                        "c_resources_read": resources_read,
                        "c_pending_hitl": gate.model_dump(mode="json"),
                    }

                local_messages.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=res.tool_call_id,
                        name=res.tool_name,
                        content=res.model_dump_json(),
                    )
                )

        report = SubAgentExecutionReport(
            agent_name="OrderAgent",
            task_instruction=state.get("c_active_instruction", ""),
            summary=summary_text,
            tool_names_called=called_tools,
            resources_read=["oms://docs/order-state-machine"],
        )
        reports.append(report.model_dump(mode="json"))

        update: dict[str, Any] = {
            "c_total_steps": steps,
            "c_agent_reports": reports,
            "c_tool_trace": tool_trace,
            "c_resources_read": resources_read,
            "c_pending_hitl": None,
        }
        if breaker_triggered:
            update["c_circuit_breaker_triggered"] = True
            update["c_circuit_breaker_reason"] = breaker_reason
        return update

    async def c_hitl_gate_node(
        state: OrchestrationState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        raw_pending = state.get("c_pending_hitl")
        if not raw_pending:
            return {"c_pending_hitl": None}

        pending_gate = PendingHITLGatePayload.model_validate(raw_pending)
        human_decision = interrupt(raw_pending)

        base_ctx = parse_execution_context(state.get("execution_context"))
        reports = list(state.get("c_agent_reports") or [])
        tool_trace = list(state.get("c_tool_trace") or [])

        action_label = (
            "售后服务退款申请"
            if pending_gate.tool_call.name == "oms__create_refund"
            else (
                "修改收货地址"
                if pending_gate.tool_call.name == "oms__update_address"
                else "当前订单服务操作"
            )
        )

        if not _is_resume_approved(human_decision):
            cancel_summary = (
                f"[REPORT:OrderAgent:mutation] 已按您的意愿拒绝执行本次{action_label}，"
                "未对原订单状态或收货信息做任何修改。"
            )
            reports.append(
                SubAgentExecutionReport(
                    agent_name="OrderAgent",
                    task_instruction=state.get("c_active_instruction", ""),
                    summary=cancel_summary,
                    tool_names_called=[],
                    resources_read=[],
                ).model_dump(mode="json")
            )
            return {
                "c_pending_hitl": None,
                "c_confirmation_declined": True,
                "c_last_hitl_decision": {"approved": False, "raw": human_decision},
                "c_agent_reports": reports,
            }

        exec_ctx = (
            _resolve_trusted_resumed_context(
                base_context=base_ctx,
                config=config,
                resume_value=human_decision,
                pending_gate=pending_gate,
            )
            if pending_gate.gate_type == "high_risk_tool"
            else base_ctx
        )

        res = await tool_runtime.execute(
            pending_gate.tool_call,
            exec_ctx,
            snapshot=snapshots.order_agent,
        )
        tool_trace.append(
            _serialize_tool_trace_item(agent_name="OrderAgent", result=res)
        )

        if res.status == "success" and res.data is not None:
            if pending_gate.tool_call.name == "oms__update_address":
                d = res.data
                summary = (
                    f"[REPORT:OrderAgent:mutation] 已为您将待发货订单 {d['order_id']}"
                    f" 的收货地址修改为 {d['shipping_address']}（收件人 {d['receiver_name']}，"
                    f"联系电话 {d['receiver_phone']}）。"
                )
            elif pending_gate.tool_call.name == "oms__create_refund":
                d = res.data
                summary = (
                    f"[REPORT:OrderAgent:mutation] 您的退款申请已确认提交，订单 {d['order_id']} "
                    f"服务单号 {d['refund_id']} 已生效，退款金额 ¥{d['refund_amount']:.2f} 将原路退回。"
                )
            else:
                summary = f"[REPORT:OrderAgent:mutation] 已为您完成{action_label}。"
        else:
            err_msg = res.error.message if res.error else "暂时无法办理该操作"
            summary = f"[REPORT:OrderAgent:mutation] 很抱歉，{action_label}未能完成：{err_msg}"

        reports.append(
            SubAgentExecutionReport(
                agent_name="OrderAgent",
                task_instruction=state.get("c_active_instruction", ""),
                summary=summary,
                tool_names_called=[pending_gate.tool_call.name],
                resources_read=[],
            ).model_dump(mode="json")
        )
        return {
            "execution_context": _dump_context_for_checkpoint(exec_ctx),
            "c_pending_hitl": None,
            "c_address_confirmed": True,
            "c_last_hitl_decision": {"approved": True, "raw": human_decision},
            "c_agent_reports": reports,
            "c_tool_trace": tool_trace,
        }

    def _route_from_c_supervisor(
        state: OrchestrationState,
    ) -> Literal["shopping_guide_agent", "order_agent", "__end__"]:
        nxt = state.get("c_next_agent", "FINISH")
        if nxt == "ShoppingGuideAgent":
            return "shopping_guide_agent"
        if nxt == "OrderAgent":
            return "order_agent"
        return "__end__"

    def _route_after_order_agent(
        state: OrchestrationState,
    ) -> Literal["c_hitl_gate", "c_supervisor"]:
        if state.get("c_pending_hitl") is not None:
            return "c_hitl_gate"
        return "c_supervisor"

    graph.add_node("c_init", c_init_node)
    graph.add_node("c_supervisor", c_supervisor_node)
    graph.add_node("shopping_guide_agent", shopping_guide_node)
    graph.add_node("order_agent", order_agent_node)
    graph.add_node("c_hitl_gate", c_hitl_gate_node)

    if connect_start:
        graph.add_edge(START, "c_init")
    graph.add_edge("c_init", "c_supervisor")
    graph.add_conditional_edges(
        "c_supervisor",
        _route_from_c_supervisor,
        {
            "shopping_guide_agent": "shopping_guide_agent",
            "order_agent": "order_agent",
            "__end__": END,
        },
    )
    graph.add_edge("shopping_guide_agent", "c_supervisor")
    graph.add_conditional_edges(
        "order_agent",
        _route_after_order_agent,
        {
            "c_hitl_gate": "c_hitl_gate",
            "c_supervisor": "c_supervisor",
        },
    )
    graph.add_edge("c_hitl_gate", "c_supervisor")
    return "c_init"


def build_c_domain_subgraph(
    *,
    llm: AsyncAgentLLM,
    tool_runtime: ToolRuntime,
    mcp_manager: MCPClientManager,
    snapshots: AgentToolSnapshots,
    max_handoffs: int = DEFAULT_MAX_HANDOFFS,
    max_total_steps: int = DEFAULT_MAX_TOTAL_STEPS,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:
    """独立编译面向 C 端消费者（`role == 'customer'`）的最小权限实时服务子图。"""
    graph = StateGraph(OrchestrationState)
    _attach_c_domain_topology(
        graph,
        llm=llm,
        tool_runtime=tool_runtime,
        mcp_manager=mcp_manager,
        snapshots=snapshots,
        max_handoffs=max_handoffs,
        max_total_steps=max_total_steps,
        connect_start=True,
    )
    return graph.compile(checkpointer=checkpointer)


# ============================================================================
# 4. B 端经营决策流水线子图构建（Analytics -> Contract Handoff -> Sourcing -> HITL）
# ============================================================================


def _attach_b_domain_topology(
    graph: StateGraph[Any],
    *,
    llm: AsyncAgentLLM,
    tool_runtime: ToolRuntime,
    mcp_manager: MCPClientManager,
    snapshots: AgentToolSnapshots,
    max_agent_steps: int = DEFAULT_B_AGENT_MAX_STEPS,
    connect_start: bool = True,
) -> str:
    """将 B 端经营决策流水线子图拓扑挂载至目标 `StateGraph` 并返回入口节点名 `'b_init'`。"""

    async def b_init_node(state: OrchestrationState) -> dict[str, Any]:
        exec_ctx = parse_execution_context(state.get("execution_context"))
        if exec_ctx.role != "merchant":
            raise ToolPermissionError(
                f"B 端决策流水线子图仅允许 merchant 角色进入，当前为 '{exec_ctx.role}'",
                code="B_SUBGRAPH_ROLE_VIOLATION",
            )
        return {
            "routed_domain": "merchant",
            "execution_context": _dump_context_for_checkpoint(exec_ctx),
            "b_pipeline_stage": "init",
            "diagnosis_artifact": None,
            "b_handoff_artifact_json": None,
            "selection_proposal": None,
            "b_pending_hitl": None,
            "b_approval_status": None,
            "b_committed_proposal": None,
        }

    async def analytics_agent_node(state: OrchestrationState) -> dict[str, Any]:
        exec_ctx = parse_execution_context(state.get("execution_context"))
        if exec_ctx.role != "merchant":
            raise ToolPermissionError(
                "AnalyticsAgent 仅限 merchant 角色执行",
                code="ANALYTICS_ROLE_DENIED",
            )

        metrics_dict_res = await _read_scoped_resource(
            mcp_manager,
            "bi://dictionary/metrics",
            allowed_uris=ANALYTICS_AGENT_ALLOWED_RESOURCES,
            agent_name="AnalyticsAgent",
        )

        # 局部隔离对话栈：绝不写入共享 State，确保下游 SourcingAgent 零原始日志污染
        local_messages: list[LLMMessage] = [
            LLMMessage(role="system", content=ANALYTICS_AGENT_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"【商家经营诊断任务】\n{state.get('user_input', '')}\n\n"
                    f"【数仓指标口径字典（bi://dictionary/metrics）】\n{metrics_dict_res.text}"
                ),
            ),
        ]

        local_traces: list[dict[str, Any]] = []
        raw_artifact_payload: dict[str, Any] | None = None

        for _step in range(max_agent_steps):
            resp = await llm.ainvoke(
                local_messages,
                tools=snapshots.analytics_agent.to_llm_schemas(),
                response_schema=None,
                agent_name="AnalyticsAgent",
            )

            if resp.tool_calls:
                local_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=resp.content,
                        tool_calls=resp.tool_calls,
                    )
                )
                batch_results = await tool_runtime.execute_batch(
                    resp.tool_calls,
                    exec_ctx,
                    snapshot=snapshots.analytics_agent,
                )
                for res in batch_results:
                    local_traces.append(
                        _serialize_tool_trace_item(
                            agent_name="AnalyticsAgent", result=res
                        )
                    )
                    local_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=res.tool_call_id,
                            name=res.tool_name,
                            content=res.model_dump_json(),
                        )
                    )
                continue

            raw_artifact_payload = (
                resp.structured_output
                if resp.structured_output is not None
                else _extract_json_dict(resp.content)
            )
            break

        if raw_artifact_payload is None:
            raise RuntimeError("AnalyticsAgent 在最大推理步数内未产出结构化 DiagnosisArtifact")

        validated_artifact = DiagnosisArtifact.model_validate(raw_artifact_payload)
        return {
            "diagnosis_artifact": validated_artifact,
            "b_pipeline_stage": "analytics_completed",
            "b_tool_trace": local_traces,
            "b_resources_read": ["bi://dictionary/metrics"],
        }

    async def b_contract_handoff_node(state: OrchestrationState) -> dict[str, Any]:
        """B-Orchestrator 阶段交接守卫：强校验 `DiagnosisArtifact` 并净化交接载荷。"""
        _ = B_ORCHESTRATOR_PROMPT  # 编排器确定性阶段契约核验策略载体
        artifact = parse_diagnosis_artifact(state.get("diagnosis_artifact"))
        return {
            "diagnosis_artifact": artifact,
            "b_handoff_artifact_json": artifact.model_dump_json(indent=2),
            "b_pipeline_stage": "handoff_verified",
        }

    async def sourcing_agent_node(state: SourcingNodeInputState) -> dict[str, Any]:
        """选品企划节点：通过 `SourcingNodeInputState` 物理隔离，仅接收 `execution_context` 与 `diagnosis_artifact`。"""
        # 防御性运行时验证：确认引擎未传入任何上游原始对话或工具轨迹字段
        unexpected_keys = set(state.keys()) - {"execution_context", "diagnosis_artifact"}
        if unexpected_keys:
            raise RuntimeError(
                f"强类型契约交接红线违例：SourcingAgent 收到未授权的额外状态字段 {unexpected_keys}"
            )

        exec_ctx = parse_execution_context(state["execution_context"])
        if exec_ctx.role != "merchant":
            raise ToolPermissionError(
                "SourcingAgent 仅限 merchant 角色执行",
                code="SOURCING_ROLE_DENIED",
            )
        diagnosis = parse_diagnosis_artifact(state["diagnosis_artifact"])

        policy_res = await _read_scoped_resource(
            mcp_manager,
            "market_erp://policies/return-exchange",
            allowed_uris=SOURCING_AGENT_ALLOWED_RESOURCES,
            agent_name="SourcingAgent",
        )

        # 上下文仅注入 SOURCING_AGENT_PROMPT + 冻结 DiagnosisArtifact JSON + 准入政策文档
        local_messages: list[LLMMessage] = [
            LLMMessage(role="system", content=SOURCING_AGENT_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    "请严格消费以下上游校验通过的强类型经营诊断契约（DiagnosisArtifact），"
                    "调用趋势与供应链成本核算工具生成 SelectionProposal，并提交新品立项建档草稿：\n"
                    f"{diagnosis.model_dump_json(indent=2)}\n\n"
                    f"【平台退换货与供应链选品风控政策（market_erp://policies/return-exchange）】\n{policy_res.text}"
                ),
            ),
        ]

        local_traces: list[dict[str, Any]] = []
        raw_proposal_payload: dict[str, Any] | None = None
        pending_gate: PendingHITLGatePayload | None = None
        committed_data: dict[str, Any] | None = None

        for _step in range(max_agent_steps):
            resp = await llm.ainvoke(
                local_messages,
                tools=snapshots.sourcing_agent.to_llm_schemas(),
                agent_name="SourcingAgent",
            )
            if resp.structured_output is not None:
                raw_proposal_payload = resp.structured_output

            if not resp.tool_calls:
                if raw_proposal_payload is None and resp.content.strip():
                    raw_proposal_payload = _extract_json_dict(resp.content)
                break

            local_messages.append(
                LLMMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                )
            )
            batch_results = await tool_runtime.execute_batch(
                resp.tool_calls,
                exec_ctx,
                snapshot=snapshots.sourcing_agent,
            )
            call_by_id = {tc.tool_call_id: tc for tc in resp.tool_calls}

            hitl_triggered = False
            for res in batch_results:
                local_traces.append(
                    _serialize_tool_trace_item(agent_name="SourcingAgent", result=res)
                )
                if res.status == "hitl_required" and res.hitl is not None:
                    orig_call = call_by_id[res.tool_call_id]
                    pending_gate = PendingHITLGatePayload(
                        domain="merchant",
                        gate_type="high_risk_tool",
                        agent_name="SourcingAgent",
                        tool_call=orig_call,
                        hitl_required=res.hitl,
                        prompt_to_human=res.hitl.reason,
                    )
                    hitl_triggered = True
                    break

                if (
                    res.tool_name == "pim__draft_sku_proposal"
                    and res.status == "success"
                    and res.data is not None
                ):
                    committed_data = res.data

                local_messages.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=res.tool_call_id,
                        name=res.tool_name,
                        content=res.model_dump_json(),
                    )
                )

            if hitl_triggered:
                break

        if raw_proposal_payload is None:
            raise RuntimeError("SourcingAgent 未产出有效的 SelectionProposal 结构化提案")

        validated_proposal = SelectionProposal.model_validate(raw_proposal_payload)

        if pending_gate is not None:
            return {
                "selection_proposal": validated_proposal,
                "b_pending_hitl": pending_gate.model_dump(mode="json"),
                "b_approval_status": "pending",
                "b_pipeline_stage": "awaiting_hitl",
                "b_tool_trace": local_traces,
                "b_resources_read": ["market_erp://policies/return-exchange"],
            }

        return {
            "selection_proposal": validated_proposal,
            "b_pending_hitl": None,
            "b_approval_status": "approved" if committed_data is not None else None,
            "b_committed_proposal": committed_data,
            "b_pipeline_stage": "approved_committed",
            "b_tool_trace": local_traces,
            "b_resources_read": ["market_erp://policies/return-exchange"],
        }

    async def b_hitl_gate_node(
        state: OrchestrationState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """高风险新品立项 HITL 挂起与恢复执行节点（挂起期间零提前落库，恢复后触发卖点知识库飞轮回写）。"""
        raw_pending = state.get("b_pending_hitl")
        if not raw_pending:
            return {"b_pending_hitl": None}

        pending_gate = PendingHITLGatePayload.model_validate(raw_pending)
        proposal = parse_selection_proposal(state.get("selection_proposal"))

        interrupt_envelope = {
            **raw_pending,
            "selection_proposal": proposal.model_dump(mode="json"),
        }
        human_decision = interrupt(interrupt_envelope)

        if not _is_resume_approved(human_decision):
            return {
                "b_pending_hitl": None,
                "b_approval_status": "rejected",
                "b_committed_proposal": None,
                "b_pipeline_stage": "rejected_terminated",
            }

        base_ctx = parse_execution_context(state.get("execution_context"))
        approved_ctx = _resolve_trusted_resumed_context(
            base_context=base_ctx,
            config=config,
            resume_value=human_decision,
            pending_gate=pending_gate,
        )

        # 通过 ToolRuntime -> MCPClientManager -> pim_server 执行建品并同步写回卖点知识库
        commit_res = await tool_runtime.execute(
            pending_gate.tool_call,
            approved_ctx,
            snapshot=snapshots.sourcing_agent,
        )
        trace_entry = _serialize_tool_trace_item(
            agent_name="SourcingAgent", result=commit_res
        )
        if commit_res.status != "success" or commit_res.data is None:
            err_desc = commit_res.error.message if commit_res.error else commit_res.status
            raise RuntimeError(f"审批通过后执行新品立项失败: {err_desc}")

        # 跨进程读取 pim://knowledge/selling-points 验证核心卖点已真实落库形成飞轮
        updated_kb = await _read_scoped_resource(
            mcp_manager,
            "pim://knowledge/selling-points",
            allowed_uris=SOURCING_AGENT_ALLOWED_RESOURCES,
            agent_name="SourcingAgent",
        )
        flywheel_verified = all(
            pt in updated_kb.text for pt in proposal.core_selling_points
        )
        committed_payload = {
            **commit_res.data,
            "flywheel_kb_verified": flywheel_verified,
        }

        return {
            "execution_context": _dump_context_for_checkpoint(approved_ctx),
            "b_pending_hitl": None,
            "b_approval_status": "approved",
            "b_committed_proposal": committed_payload,
            "b_pipeline_stage": "approved_committed",
            "b_tool_trace": [trace_entry],
            "b_resources_read": ["pim://knowledge/selling-points"],
        }

    async def b_finalize_node(state: OrchestrationState) -> dict[str, Any]:
        diagnosis = parse_diagnosis_artifact(state.get("diagnosis_artifact"))
        proposal = parse_selection_proposal(state.get("selection_proposal"))
        approval_status = state.get("b_approval_status")
        committed = state.get("b_committed_proposal")

        lead_sku = proposal.candidate_skus[0]
        if approval_status == "rejected":
            summary = (
                f"【新款开发方案已退回（人工审批已驳回）】\n"
                f"1. 诊断品类：{diagnosis.problem_category}（首要退货原因：{diagnosis.top_return_reasons[0].reason}）；\n"
                f"2. 候选款式：{lead_sku.sku_name}（建议售价 ¥{lead_sku.target_price:.2f}，"
                f"预估毛利率 {proposal.estimated_gross_margin * 100:.2f}%）；\n"
                f"3. 处理结论：人工驳回立项，本次未建立新款商品档案，门店导购卖点库保持不变。"
            )
            return {"final_response": summary}

        proposal_id = committed.get("proposal_id", "待生成") if isinstance(committed, Mapping) else "待生成"
        kb_synced = bool(committed.get("flywheel_kb_verified")) if isinstance(committed, Mapping) else False
        metric_label_map = {
            "return_rate": "异常退货率",
            "conversion_rate": "支付转化率",
            "gross_margin_rate": "品类毛利率",
        }
        m0 = diagnosis.anomalous_metrics[0]
        m0_label = metric_label_map.get(m0.metric_name, "核心经营指标")
        summary = (
            f"【新款开发方案已审批通过并落库】\n"
            f"1. 经营诊断：{diagnosis.problem_category}品类{m0_label}为 {m0.current_value * 100:.2f}%"
            f"（警戒基准 {m0.benchmark_value * 100:.0f}%），首要退货原因为「{diagnosis.top_return_reasons[0].reason}」；\n"
            f"2. 立项款式：{lead_sku.sku_name}（建议售价 ¥{lead_sku.target_price:.2f}，"
            f"单件预估成本 ¥{lead_sku.estimated_unit_cost:.2f}，预估毛利率 {proposal.estimated_gross_margin * 100:.2f}%）；\n"
            f"3. 建档单号：{proposal_id}，新款核心卖点已同步更新至门店导购卖点库（{'已生效' if kb_synced else '同步中'}）。"
        )
        return {"final_response": summary}

    def _route_after_sourcing(
        state: OrchestrationState,
    ) -> Literal["b_hitl_gate", "b_finalize"]:
        if state.get("b_pending_hitl") is not None:
            return "b_hitl_gate"
        return "b_finalize"

    graph.add_node("b_init", b_init_node)
    graph.add_node("analytics_agent", analytics_agent_node)
    graph.add_node("b_contract_handoff", b_contract_handoff_node)
    graph.add_node(
        "sourcing_agent",
        sourcing_agent_node,
        input_schema=SourcingNodeInputState,
    )
    graph.add_node("b_hitl_gate", b_hitl_gate_node)
    graph.add_node("b_finalize", b_finalize_node)

    if connect_start:
        graph.add_edge(START, "b_init")
    graph.add_edge("b_init", "analytics_agent")
    graph.add_edge("analytics_agent", "b_contract_handoff")
    graph.add_edge("b_contract_handoff", "sourcing_agent")
    graph.add_conditional_edges(
        "sourcing_agent",
        _route_after_sourcing,
        {
            "b_hitl_gate": "b_hitl_gate",
            "b_finalize": "b_finalize",
        },
    )
    graph.add_edge("b_hitl_gate", "b_finalize")
    graph.add_edge("b_finalize", END)
    return "b_init"


def build_b_domain_subgraph(
    *,
    llm: AsyncAgentLLM,
    tool_runtime: ToolRuntime,
    mcp_manager: MCPClientManager,
    snapshots: AgentToolSnapshots,
    max_agent_steps: int = DEFAULT_B_AGENT_MAX_STEPS,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:
    """独立编译面向 B 端商家（`role == 'merchant'`）的强契约交接与 HITL 审批飞轮子图。"""
    graph = StateGraph(OrchestrationState)
    _attach_b_domain_topology(
        graph,
        llm=llm,
        tool_runtime=tool_runtime,
        mcp_manager=mcp_manager,
        snapshots=snapshots,
        max_agent_steps=max_agent_steps,
        connect_start=True,
    )
    return graph.compile(checkpointer=checkpointer)


# ============================================================================
# 5. 顶层双域隔离编排图编译入口与高层宿主封装
# ============================================================================


def build_multi_agent_graph(
    *,
    registry: ToolRegistry,
    tool_runtime: ToolRuntime,
    mcp_manager: MCPClientManager,
    llm: AsyncAgentLLM | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    max_handoffs: int = DEFAULT_MAX_HANDOFFS,
    max_total_steps: int = DEFAULT_MAX_TOTAL_STEPS,
    b_agent_max_steps: int = DEFAULT_B_AGENT_MAX_STEPS,
) -> CompiledStateGraph:
    """构建挂载 Checkpointer 的顶层电商多 Agent 双域硬隔离状态机。

    - 根入口 `START` 经纯代码硬路由 `root_role_router` 分流至物理无连通边的 C 端拓扑与 B 端拓扑；
    - 每个阶段节点独立落盘 Checkpoint，确保 `interrupt()` 挂起时完整暴露前序阶段产物，
      且 `Command(resume=...)` 仅精准续跑审批门控节点，绝不重复执行上游推理与工具调用；
    - 若未显式传入 `checkpointer`，默认挂载注册了契约类型白名单的 `InMemorySaver`（等价于 `MemorySaver`）。
    """
    active_llm = llm if llm is not None else create_llm_client()
    active_checkpointer = (
        checkpointer if checkpointer is not None else create_default_checkpointer()
    )
    snapshots = build_agent_snapshots(registry)

    root = StateGraph(OrchestrationState)
    c_entry = _attach_c_domain_topology(
        root,
        llm=active_llm,
        tool_runtime=tool_runtime,
        mcp_manager=mcp_manager,
        snapshots=snapshots,
        max_handoffs=max_handoffs,
        max_total_steps=max_total_steps,
        connect_start=False,
    )
    b_entry = _attach_b_domain_topology(
        root,
        llm=active_llm,
        tool_runtime=tool_runtime,
        mcp_manager=mcp_manager,
        snapshots=snapshots,
        max_agent_steps=b_agent_max_steps,
        connect_start=False,
    )
    root.add_conditional_edges(
        START,
        root_role_router,
        {
            "c_domain_subgraph": c_entry,
            "b_domain_subgraph": b_entry,
        },
    )

    return root.compile(checkpointer=active_checkpointer)


class MultiAgentOrchestrator:
    """面向 API Gateway 暴露的高层双域编排宿主封装（持有快照、Checkpointer 与编译图）。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        tool_runtime: ToolRuntime,
        mcp_manager: MCPClientManager,
        llm: AsyncAgentLLM | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        max_handoffs: int = DEFAULT_MAX_HANDOFFS,
        max_total_steps: int = DEFAULT_MAX_TOTAL_STEPS,
    ) -> None:
        self.llm = llm if llm is not None else create_llm_client()
        self.checkpointer = (
            checkpointer if checkpointer is not None else create_default_checkpointer()
        )
        self.snapshots = build_agent_snapshots(registry)
        self.graph = build_multi_agent_graph(
            registry=registry,
            tool_runtime=tool_runtime,
            mcp_manager=mcp_manager,
            llm=self.llm,
            checkpointer=self.checkpointer,
            max_handoffs=max_handoffs,
            max_total_steps=max_total_steps,
        )

    async def ainvoke(
        self,
        user_input: str,
        context: ExecutionContext,
        *,
        thread_id: str,
        extra_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """以受信任 `ExecutionContext` 异步发起一轮新对话或流水线执行。"""
        payload: dict[str, Any] = {
            "user_input": user_input,
            "execution_context": _dump_context_for_checkpoint(context),
            **(dict(extra_state) if extra_state else {}),
        }
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return await self.graph.ainvoke(payload, config=config)

    async def aresume(
        self,
        resume_decision: Any,
        *,
        thread_id: str,
        approved_context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """在 HITL `interrupt()` 挂起后通过 `Command(resume=...)` 异步恢复执行。"""
        configurable: dict[str, Any] = {"thread_id": thread_id}
        if approved_context is not None:
            configurable["execution_context"] = approved_context
        return await self.graph.ainvoke(
            Command(resume=resume_decision),
            config={"configurable": configurable},
        )


__all__ = [
    "ANALYTICS_AGENT_ALLOWED_RESOURCES",
    "ANALYTICS_AGENT_ALLOWED_TOOLS",
    "AgentToolSnapshots",
    "Command",
    "DEFAULT_B_AGENT_MAX_STEPS",
    "DEFAULT_MAX_HANDOFFS",
    "DEFAULT_MAX_TOTAL_STEPS",
    "InMemorySaver",
    "MemorySaver",
    "MultiAgentOrchestrator",
    "ORDER_AGENT_ALLOWED_RESOURCES",
    "ORDER_AGENT_ALLOWED_TOOLS",
    "SHOPPING_GUIDE_ALLOWED_RESOURCES",
    "SHOPPING_GUIDE_ALLOWED_TOOLS",
    "SOURCING_AGENT_ALLOWED_RESOURCES",
    "SOURCING_AGENT_ALLOWED_TOOLS",
    "build_agent_snapshots",
    "build_b_domain_subgraph",
    "build_c_domain_subgraph",
    "build_multi_agent_graph",
    "create_default_checkpointer",
    "root_role_router",
]
