"""可信执行上下文契约（ExecutionContext）。

核心安全红线：
1. `ExecutionContext` 仅由 API Gateway 基于可信登录态与审批流签发，实例全生命周期不可变（frozen）；
2. `user_id`、`tenant_id`、`role`、`permissions` 及高风险审批凭证严禁从大模型工具参数读取；
3. 高风险工具（`risk == "high"`）放行仅认 `ExecutionContext` 内挂载的受信任审批凭证。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RoleType = Literal["customer", "merchant"]


class HITLApproval(BaseModel):
    """由网关/审批服务签发的受信任高风险操作审批凭证（不可变）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str = Field(
        default_factory=lambda: f"apr_{uuid.uuid4().hex[:12]}",
        min_length=1,
        description="受信任审批单唯一标识",
    )
    tool_name: str = Field(
        ...,
        min_length=1,
        description="获批执行的高风险工具限定名（如 oms__apply_refund）",
    )
    tool_call_id: str | None = Field(
        default=None,
        min_length=1,
        description="绑定的特定 tool_call_id；提供后可阻断跨调用重放",
    )
    approver_id: str = Field(
        default="trusted_reviewer",
        min_length=1,
        description="完成人工确认的审批主体标识",
    )
    arguments_digest: str | None = Field(
        default=None,
        min_length=1,
        description="规范化工具入参 SHA-256 摘要；提供后若参数被篡改则自动失效",
    )


class ExecutionContext(BaseModel):
    """全链路透传的不可变可信执行上下文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(..., min_length=1, description="全链路追踪唯一 ID")
    tenant_id: str = Field(..., min_length=1, description="租户/店铺隔离边界 ID")
    user_id: str = Field(..., min_length=1, description="当前登录主体唯一 ID")
    role: RoleType = Field(..., description="双域身份角色：customer(C端) / merchant(B端)")
    permissions: frozenset[str] | set[str] = Field(
        default_factory=frozenset,
        description="RBAC 权限点集合（运行期强制冻结为 frozenset）",
    )
    hitl_approvals: frozenset[HITLApproval] | set[HITLApproval] = Field(
        default_factory=frozenset,
        description="受信任 HITL 审批凭证集合（默认空集，严禁由模型入参注入）",
    )
    approved_tool_call_ids: frozenset[str] | set[str] = Field(
        default_factory=frozenset,
        description="经网关人工确认放行的特定 tool_call_id 集合（默认空集）",
    )

    @field_validator("trace_id", "tenant_id", "user_id")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("可信身份上下文字段不得为空白字符串")
        return stripped

    @field_validator("permissions", mode="after")
    @classmethod
    def _freeze_permissions(cls, value: set[str] | frozenset[str]) -> frozenset[str]:
        cleaned: set[str] = set()
        for item in value:
            stripped = item.strip()
            if not stripped:
                raise ValueError("permissions 中不得包含空白权限项")
            cleaned.add(stripped)
        return frozenset(cleaned)

    @field_validator("hitl_approvals", mode="after")
    @classmethod
    def _freeze_approvals(
        cls, value: set[HITLApproval] | frozenset[HITLApproval]
    ) -> frozenset[HITLApproval]:
        return frozenset(value)

    @field_validator("approved_tool_call_ids", mode="after")
    @classmethod
    def _freeze_approved_call_ids(
        cls, value: set[str] | frozenset[str]
    ) -> frozenset[str]:
        cleaned: set[str] = set()
        for item in value:
            stripped = item.strip()
            if not stripped:
                raise ValueError("approved_tool_call_ids 中不得包含空白 ID")
            cleaned.add(stripped)
        return frozenset(cleaned)

    def has_valid_approval(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        canonical_args_digest: str | None = None,
    ) -> bool:
        """校验当前上下文是否持有针对目标高风险工具调用的有效受信任审批凭证。"""
        if tool_call_id in self.approved_tool_call_ids:
            return True

        for approval in self.hitl_approvals:
            if approval.tool_name != tool_name:
                continue
            if approval.tool_call_id is not None and approval.tool_call_id != tool_call_id:
                continue
            if (
                approval.arguments_digest is not None
                and canonical_args_digest is not None
                and approval.arguments_digest != canonical_args_digest
            ):
                continue
            return True
        return False

    def with_hitl_approval(
        self,
        *,
        tool_name: str,
        tool_call_id: str | None = None,
        approver_id: str = "trusted_reviewer",
        arguments_digest: str | None = None,
        approval_id: str | None = None,
    ) -> ExecutionContext:
        """由网关在人工确认后派生携带受信任审批凭证的新不可变上下文实例。"""
        approval_kwargs: dict[str, str | None] = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "approver_id": approver_id,
            "arguments_digest": arguments_digest,
        }
        if approval_id is not None:
            approval_kwargs["approval_id"] = approval_id
        new_approval = HITLApproval(**approval_kwargs)  # type: ignore[arg-type]
        updated_approvals = frozenset(self.hitl_approvals) | {new_approval}
        return self.model_copy(update={"hitl_approvals": updated_approvals})
