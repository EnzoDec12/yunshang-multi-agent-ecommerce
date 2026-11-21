"""LangGraph 双域隔离编排层状态定义与契约投影模型。

核心安全设计：
1. C 端实时子图与 B 端决策流水线子图通过 Root Router 硬分流，各自读写独立命名的状态槽位；
2. B 端 `SourcingAgent` 节点绑定最小输入投影 `SourcingNodeInputState`，由 LangGraph 引擎在节点入口
   物理剥离 `user_input` 与上游 `AnalyticsAgent` 的工具轨迹/原始消息，确保只接收经 Pydantic 强校验的
   `DiagnosisArtifact`；
3. 支持 LangGraph Checkpointer（`InMemorySaver` / `MemorySaver`）对所有 Pydantic 契约类型的安全序列化。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from schemas.context import ExecutionContext
from schemas.contracts import (
    DiagnosisArtifact,
    HITLRequired,
    SelectionProposal,
    ToolCall,
)

CDomainTarget = Literal["ShoppingGuideAgent", "OrderAgent", "FINISH"]
BPipelineStage = Literal[
    "init",
    "analytics_completed",
    "handoff_verified",
    "awaiting_hitl",
    "approved_committed",
    "rejected_terminated",
]


class SupervisorRouteDecision(BaseModel):
    """C-Supervisor 结构化子任务调度与完成决策契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    next_agent: CDomainTarget
    task_instruction: str = ""
    final_response: str | None = None


class SubAgentExecutionReport(BaseModel):
    """C 端子 Agent 向 C-Supervisor 汇报的结构化事实摘要（不含跨租户或底层未脱敏字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: Literal["ShoppingGuideAgent", "OrderAgent"]
    task_instruction: str
    summary: str
    tool_names_called: list[str] = Field(default_factory=list)
    resources_read: list[str] = Field(default_factory=list)


class PendingHITLGatePayload(BaseModel):
    """触发 LangGraph `interrupt()` 时持久化与对外暴露的标准挂起上下文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: Literal["customer", "merchant"]
    gate_type: Literal["high_risk_tool", "address_confirmation"]
    agent_name: str
    tool_call: ToolCall
    hitl_required: HITLRequired | None = None
    prompt_to_human: str


class SourcingNodeInputState(TypedDict):
    """B 端 `SourcingAgent` 物理隔离输入投影 Schema。

    通过 `StateGraph.add_node(..., input_schema=SourcingNodeInputState)` 强制约束：
    `SourcingAgent` 节点函数入参字典仅包含受信任 `execution_context` 与校验后的 `diagnosis_artifact`，
    从引擎层面彻底杜绝上游 `AnalyticsAgent` 原始对话历史、SQL 日志或用户自由文本混入。
    """

    execution_context: ExecutionContext | dict[str, Any]
    diagnosis_artifact: DiagnosisArtifact | dict[str, Any]


class OrchestrationState(TypedDict, total=False):
    """电商多 Agent 根图及双域子图统一状态契约。"""

    # --- 根入口与通用输出 ---
    user_input: str
    execution_context: ExecutionContext | dict[str, Any]
    routed_domain: Literal["customer", "merchant"]
    final_response: str

    # --- C 端实时服务子图专属状态 ---
    c_handoff_count: int
    c_total_steps: int
    c_next_agent: CDomainTarget
    c_active_instruction: str
    c_agent_reports: list[dict[str, Any]]
    c_tool_trace: list[dict[str, Any]]
    c_resources_read: list[str]
    c_circuit_breaker_triggered: bool
    c_circuit_breaker_reason: str | None
    c_require_address_confirmation: bool
    c_address_confirmed: bool
    c_confirmation_declined: bool
    c_pending_hitl: dict[str, Any] | None
    c_last_hitl_decision: dict[str, Any] | None

    # --- B 端经营决策流水线子图专属状态 ---
    b_pipeline_stage: BPipelineStage
    diagnosis_artifact: DiagnosisArtifact | dict[str, Any] | None
    b_handoff_artifact_json: str | None
    selection_proposal: SelectionProposal | dict[str, Any] | None
    b_pending_hitl: dict[str, Any] | None
    b_approval_status: Literal["pending", "approved", "rejected"] | None
    b_committed_proposal: dict[str, Any] | None
    b_tool_trace: Annotated[list[dict[str, Any]], operator.add]
    b_resources_read: Annotated[list[str], operator.add]


def parse_execution_context(raw: ExecutionContext | dict[str, Any] | None) -> ExecutionContext:
    """从图状态或配置中提取并强校验不可变 `ExecutionContext`。"""
    if isinstance(raw, ExecutionContext):
        return raw
    if isinstance(raw, dict):
        return ExecutionContext.model_validate(raw)
    raise ValueError("缺失可信 ExecutionContext；严禁无身份上下文启动编排图")


def parse_diagnosis_artifact(raw: DiagnosisArtifact | dict[str, Any] | None) -> DiagnosisArtifact:
    """从图状态中提取并通过 Pydantic 强校验还原冻结的 `DiagnosisArtifact`。"""
    if isinstance(raw, DiagnosisArtifact):
        return raw
    if isinstance(raw, dict):
        return DiagnosisArtifact.model_validate(raw)
    raise ValueError("缺失或无效的 DiagnosisArtifact 强类型交接物")


def parse_selection_proposal(raw: SelectionProposal | dict[str, Any] | None) -> SelectionProposal:
    """从图状态中提取并通过 Pydantic 强校验还原冻结的 `SelectionProposal`。"""
    if isinstance(raw, SelectionProposal):
        return raw
    if isinstance(raw, dict):
        return SelectionProposal.model_validate(raw)
    raise ValueError("缺失或无效的 SelectionProposal 强类型选品提案")


__all__ = [
    "BPipelineStage",
    "CDomainTarget",
    "OrchestrationState",
    "PendingHITLGatePayload",
    "SourcingNodeInputState",
    "SubAgentExecutionReport",
    "SupervisorRouteDecision",
    "parse_diagnosis_artifact",
    "parse_execution_context",
    "parse_selection_proposal",
]
