"""电商多 Agent 系统核心契约与 Tool Runtime 安全守卫测试套件。

覆盖验证项：
1. 用例 1：查询订单缺少必填过滤条件（无 `status` 且无 `created_from`）或 `limit > 50` 拦截返回 `validation_error`，及正常调用后置裁剪脱敏；
2. 用例 2：C 端用户越权调用 B 端工具（Snapshot 白名单防线 + RBAC/双域角色边界防线）、参数伪造 `user_id` / `approved=True` 拦截，以及注册期红线与 `ExecutionContext` 不可变防线；
3. 用例 3：`risk="high"` 退款工具触发 `HITLRequired` 挂起中断（handler 零执行）、参数篡改摘要失效阻断，以及受信任 `ExecutionContext` 注入审批凭证后放行恢复；
4. 用例 4：`execute_batch()` 并发调用两个同名查询工具（后发起者先完成），验证返回 `ToolResultMessage` 严格按 `tool_call_id` 精准对齐不串台；
5. 补充验证：四分类错误全覆盖（含依赖不可用 `transport_error` 与业务异常 `business_error`）、异步生命周期 Hook、以及 `DiagnosisArtifact` / `SelectionProposal` 强类型交接契约。
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field, ValidationError, model_validator

from runtime import (
    StrictToolInput,
    StrictToolOutput,
    ToolDefinition,
    ToolLifecycleHook,
    ToolRegistrationError,
    ToolRegistry,
    ToolRuntime,
)
from schemas import (
    AnomalousMetric,
    CandidateSKU,
    DiagnosisArtifact,
    ErrorPayload,
    ExecutionContext,
    HITLRequired,
    PriceBandGap,
    ReturnReasonStat,
    SelectionProposal,
    ToolBusinessError,
    ToolCall,
    ToolResultMessage,
)


# ============================================================================
# 测试用领域工具 Schema 与异步 Mock Fixtures
# ============================================================================


class SearchOrdersInput(StrictToolInput):
    """订单检索入参契约：禁止无条件全表扫描，单页上限 50 条。"""

    status: Literal["pending", "paid", "shipped", "completed", "refunded"] | None = (
        Field(default=None, description="订单状态过滤")
    )
    created_from: date | None = Field(
        default=None, description="订单创建起始日期（YYYY-MM-DD）"
    )
    limit: int = Field(default=20, ge=1, le=50, description="单次返回条数上限（1~50）")

    @model_validator(mode="after")
    def _require_at_least_one_filter(self) -> SearchOrdersInput:
        if self.status is None and self.created_from is None:
            raise ValueError(
                "查询订单必须至少提供 status 或 created_from 过滤条件之一，禁止无过滤全量扫描"
            )
        return self


class OrderItemOutput(StrictToolOutput):
    order_id: str
    status: str
    amount: float
    buyer_name: str
    buyer_phone: str
    shipping_address: str


class SearchOrdersOutput(StrictToolOutput):
    tenant_id: str
    owner_user_id: str
    orders: list[OrderItemOutput]


class ApplyRefundInput(StrictToolInput):
    order_id: str = Field(..., min_length=1)
    refund_amount: float = Field(..., gt=0.0, le=50000.0)
    reason: str = Field(..., min_length=2)


class ApplyRefundOutput(StrictToolOutput):
    refund_id: str
    order_id: str
    refund_amount: float
    status: Literal["refund_approved_and_executed"]
    operator_user_id: str


class DiagnoseCategoryInput(StrictToolInput):
    category_name: str = Field(..., min_length=1)
    window_days: int = Field(default=30, ge=7, le=90)


class DiagnoseCategoryOutput(StrictToolOutput):
    artifact: DiagnosisArtifact


@pytest.fixture
def harness_env() -> dict[str, Any]:
    """构建带调用追踪计数器的隔离 Registry、C/B 双域 Snapshot 与 ToolRuntime。"""
    call_tracker: dict[str, list[dict[str, Any]]] = {
        "search_orders": [],
        "apply_refund": [],
        "diagnose_category": [],
    }

    async def handle_search_orders(
        params: SearchOrdersInput, ctx: ExecutionContext
    ) -> dict[str, Any]:
        call_tracker["search_orders"].append(
            {"params": params.model_dump(mode="json"), "user_id": ctx.user_id}
        )
        effective_status = params.status or "paid"
        return {
            "tenant_id": ctx.tenant_id,
            "owner_user_id": ctx.user_id,
            "_internal_db_shard": "shard_cn_east_03",
            "orders": [
                {
                    "order_id": f"ORD-{effective_status.upper()}-001",
                    "status": effective_status,
                    "amount": 299.0,
                    "buyer_name": "张三丰",
                    "buyer_phone": "13812345678",
                    "shipping_address": "广东省深圳市南山区粤海街道科技园88号",
                    "_internal_supplier_cost": 112.5,
                }
            ],
        }

    async def handle_apply_refund(
        params: ApplyRefundInput, ctx: ExecutionContext
    ) -> dict[str, Any]:
        if params.order_id == "ORD-LOCKED-999":
            raise ToolBusinessError(
                "该订单处于售后冻结期，无法发起自动退款",
                code="ORDER_REFUND_FROZEN",
            )
        call_tracker["apply_refund"].append(
            {
                "order_id": params.order_id,
                "refund_amount": params.refund_amount,
                "user_id": ctx.user_id,
                "tenant_id": ctx.tenant_id,
            }
        )
        return {
            "refund_id": f"RFD-{params.order_id}",
            "order_id": params.order_id,
            "refund_amount": params.refund_amount,
            "status": "refund_approved_and_executed",
            "operator_user_id": ctx.user_id,
        }

    async def handle_diagnose_category(
        params: DiagnoseCategoryInput, ctx: ExecutionContext
    ) -> dict[str, Any]:
        call_tracker["diagnose_category"].append(
            {"category_name": params.category_name, "tenant_id": ctx.tenant_id}
        )
        artifact = DiagnosisArtifact(
            problem_category=params.category_name,
            anomalous_metrics=[
                AnomalousMetric(
                    metric_name="return_rate",
                    current_value=0.31,
                    benchmark_value=0.14,
                    delta_pct=121.4,
                    summary="近30天退货率显著高于行业均值",
                )
            ],
            top_return_reasons=[
                ReturnReasonStat(reason="尺码偏小", share=0.48, order_count=240),
                ReturnReasonStat(reason="面料透气性差", share=0.32, order_count=160),
                ReturnReasonStat(reason="色差明显", share=0.20, order_count=100),
            ],
            price_band_gaps=[
                PriceBandGap(
                    min_price=199.0,
                    max_price=299.0,
                    demand_index=1.85,
                    gap_description="中端通勤功能面料款在 199-299 元区间供给稀缺",
                )
            ],
        )
        return {"artifact": artifact.model_dump(mode="json")}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="oms__search_orders",
            description="按状态或起始日期检索当前主体订单列表；不用于修改订单状态或全表无条件导出。",
            input_schema=SearchOrdersInput,
            output_schema=SearchOrdersOutput,
            error_schema=ErrorPayload,
            permission="oms:orders:read",
            risk="low",
            handler=handle_search_orders,
            allowed_roles={"customer", "merchant"},
        )
    )
    registry.register(
        ToolDefinition(
            name="oms__apply_refund",
            description="针对指定订单执行资金原路退款操作；禁止在未核实订单归属或未经人工审批时直接放款。",
            input_schema=ApplyRefundInput,
            output_schema=ApplyRefundOutput,
            error_schema=ErrorPayload,
            permission="oms:refund:create",
            risk="high",
            handler=handle_apply_refund,
            allowed_roles={"customer", "merchant"},
        )
    )
    registry.register(
        ToolDefinition(
            name="analytics__diagnose_category",
            description="聚合店铺品类经营异常指标与退货归因；仅限 B 端商家经营分析使用，禁止向 C 端买家开放。",
            input_schema=DiagnoseCategoryInput,
            output_schema=DiagnoseCategoryOutput,
            error_schema=ErrorPayload,
            permission="analytics:diagnosis:read",
            risk="medium",
            handler=handle_diagnose_category,
            allowed_roles={"merchant"},
        )
    )

    customer_snapshot = registry.build_snapshot(
        ["oms__search_orders", "oms__apply_refund"]
    )
    merchant_snapshot = registry.build_snapshot(
        ["oms__search_orders", "oms__apply_refund", "analytics__diagnose_category"]
    )

    return {
        "registry": registry,
        "customer_snapshot": customer_snapshot,
        "merchant_snapshot": merchant_snapshot,
        "call_tracker": call_tracker,
    }


# ============================================================================
# 用例 1：订单查询入参边界强校验（缺失必填过滤条件 / limit > 50）与出参裁剪脱敏
# ============================================================================


async def test_case_1_search_orders_validation_and_output_sanitization(
    harness_env: dict[str, Any],
) -> None:
    runtime = ToolRuntime(snapshot=harness_env["customer_snapshot"])
    customer_ctx = ExecutionContext(
        trace_id="trace_case1_001",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_10086",
        role="customer",
        permissions={"oms:orders:read"},
    )

    # 1a. 无 status 且无 created_from -> Pydantic 拦截并返回 validation_error
    missing_filter_res = await runtime.execute(
        ToolCall(
            tool_call_id="call_missing_filter",
            name="oms__search_orders",
            arguments={"limit": 10},
        ),
        customer_ctx,
    )
    assert missing_filter_res.tool_call_id == "call_missing_filter"
    assert missing_filter_res.status == "error"
    assert missing_filter_res.data is None
    assert missing_filter_res.error is not None
    assert missing_filter_res.error.error_type == "validation_error"
    assert len(harness_env["call_tracker"]["search_orders"]) == 0

    # 1b. limit > 50 -> Pydantic 拦截并返回 validation_error
    exceed_limit_res = await runtime.execute(
        ToolCall(
            tool_call_id="call_limit_51",
            name="oms__search_orders",
            arguments={"status": "paid", "limit": 51},
        ),
        customer_ctx,
    )
    assert exceed_limit_res.tool_call_id == "call_limit_51"
    assert exceed_limit_res.status == "error"
    assert exceed_limit_res.error is not None
    assert exceed_limit_res.error.error_type == "validation_error"
    assert len(harness_env["call_tracker"]["search_orders"]) == 0

    # 1c. 合规参数放行 -> 验证后置多余内部字段剔除与买家隐私字段脱敏
    valid_res = await runtime.execute(
        ToolCall(
            tool_call_id="call_valid_search",
            name="oms__search_orders",
            arguments={"status": "paid", "limit": 20},
        ),
        customer_ctx,
    )
    assert valid_res.tool_call_id == "call_valid_search"
    assert valid_res.status == "success"
    assert valid_res.data is not None
    assert "_internal_db_shard" not in valid_res.data
    first_order = valid_res.data["orders"][0]
    assert "_internal_supplier_cost" not in first_order
    assert first_order["buyer_phone"] == "138****5678"
    assert first_order["buyer_name"] == "张**"
    assert first_order["shipping_address"] == "广东省深圳市****"


# ============================================================================
# 用例 2：C 端越权调 B 端工具 & 伪造 user_id / approved=True 多层防线拒绝
# ============================================================================


async def test_case_2_privilege_escalation_and_forged_fields_blocked(
    harness_env: dict[str, Any],
) -> None:
    customer_snapshot = harness_env["customer_snapshot"]
    merchant_snapshot = harness_env["merchant_snapshot"]

    # 验证导出给 C 端 Agent 的 JSON Schema 列表严格裁剪，绝不泄露 B 端诊断工具
    exported_names = [schema["name"] for schema in customer_snapshot.to_llm_schemas()]
    assert exported_names == ["oms__search_orders", "oms__apply_refund"]
    assert "analytics__diagnose_category" not in customer_snapshot

    customer_ctx = ExecutionContext(
        trace_id="trace_case2_001",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_20001",
        role="customer",
        permissions={"oms:orders:read", "oms:refund:create"},
    )

    # 2a. 防线 1（Snapshot 白名单）：C 端在自身 Snapshot 下强行请求 B 端工具 -> permission_error
    c_runtime = ToolRuntime(snapshot=customer_snapshot)
    snap_denied = await c_runtime.execute(
        ToolCall(
            tool_call_id="call_b_tool_on_c_snap",
            name="analytics__diagnose_category",
            arguments={"category_name": "连衣裙"},
        ),
        customer_ctx,
    )
    assert snap_denied.status == "error"
    assert snap_denied.error is not None
    assert snap_denied.error.error_type == "permission_error"
    assert snap_denied.error.code == "TOOL_NOT_IN_SNAPSHOT"
    assert len(harness_env["call_tracker"]["diagnose_category"]) == 0

    # 2b. 防线 2（RBAC 与双域角色边界）：即便错误挂载含 B 端工具的 Snapshot，C 端角色或缺失权限仍被拦截
    m_runtime = ToolRuntime(snapshot=merchant_snapshot)
    role_denied = await m_runtime.execute(
        ToolCall(
            tool_call_id="call_b_tool_wrong_role",
            name="analytics__diagnose_category",
            arguments={"category_name": "连衣裙"},
        ),
        customer_ctx,
    )
    assert role_denied.status == "error"
    assert role_denied.error is not None
    assert role_denied.error.error_type == "permission_error"
    assert role_denied.error.code == "ROLE_BOUNDARY_DENIED"

    underprivileged_merchant_ctx = ExecutionContext(
        trace_id="trace_case2_002",
        tenant_id="tenant_yunshang_cn",
        user_id="merchant_sub_account",
        role="merchant",
        permissions={"oms:orders:read"},  # 缺少 analytics:diagnosis:read
    )
    rbac_denied = await m_runtime.execute(
        ToolCall(
            tool_call_id="call_b_tool_missing_perm",
            name="analytics__diagnose_category",
            arguments={"category_name": "连衣裙"},
        ),
        underprivileged_merchant_ctx,
    )
    assert rbac_denied.status == "error"
    assert rbac_denied.error is not None
    assert rbac_denied.error.error_type == "permission_error"
    assert rbac_denied.error.code == "RBAC_PERMISSION_DENIED"
    assert len(harness_env["call_tracker"]["diagnose_category"]) == 0

    # 2c. 防线 3（入参 extra="forbid" 强校验）：模型在参数里伪造 user_id 或 approved=True 被拦截
    forged_user_res = await c_runtime.execute(
        ToolCall(
            tool_call_id="call_forge_user_id",
            name="oms__search_orders",
            arguments={"status": "paid", "user_id": "victim_buyer_999"},
        ),
        customer_ctx,
    )
    assert forged_user_res.status == "error"
    assert forged_user_res.error is not None
    assert forged_user_res.error.error_type == "validation_error"
    assert len(harness_env["call_tracker"]["search_orders"]) == 0

    forged_approval_res = await c_runtime.execute(
        ToolCall(
            tool_call_id="call_forge_approved",
            name="oms__apply_refund",
            arguments={
                "order_id": "ORD-PAID-001",
                "refund_amount": 299.0,
                "reason": "七天无理由退货",
                "approved": True,
            },
        ),
        customer_ctx,
    )
    assert forged_approval_res.status == "error"
    assert forged_approval_res.error is not None
    assert forged_approval_res.error.error_type == "validation_error"
    assert len(harness_env["call_tracker"]["apply_refund"]) == 0


def test_case_2_registration_redlines_and_context_immutability() -> None:
    """验证注册期禁止可信上下文字段进入 Input Schema，且 ExecutionContext 运行期不可篡改。"""
    ctx = ExecutionContext(
        trace_id="trace_immut_01",
        tenant_id="tenant_01",
        user_id="user_01",
        role="customer",
        permissions={"oms:orders:read"},
    )
    assert isinstance(ctx.permissions, frozenset)

    with pytest.raises(ValidationError):
        ctx.role = "merchant"  # type: ignore[misc]

    with pytest.raises(AttributeError):
        ctx.permissions.add("analytics:diagnosis:read")  # type: ignore[union-attr]

    async def dummy_handler(params: Any, context: ExecutionContext) -> dict[str, Any]:
        return {}

    # 违规 1：缺少域前缀
    with pytest.raises(ToolRegistrationError):
        ToolDefinition(
            name="search_orders_without_domain",
            description="查询订单；禁止用于修改数据。",
            input_schema=SearchOrdersInput,
            output_schema=SearchOrdersOutput,
            permission="oms:orders:read",
            risk="low",
            handler=dummy_handler,
        )

    # 违规 2：Input Schema 暴露禁止字段（user_id / tenant_id / role / is_admin / approved / risk_level）
    for forbidden_field in (
        "user_id",
        "tenant_id",
        "role",
        "is_admin",
        "approved",
        "risk_level",
    ):
        bad_input_cls = type(
            f"BadInput_{forbidden_field}",
            (StrictToolInput,),
            {"__annotations__": {forbidden_field: str}},
        )
        with pytest.raises(ToolRegistrationError, match=forbidden_field):
            ToolDefinition(
                name="oms__bad_tool",
                description="测试违规工具定义；禁止在生产环境使用。",
                input_schema=bad_input_cls,
                output_schema=SearchOrdersOutput,
                permission="oms:orders:read",
                risk="low",
                handler=dummy_handler,
            )

    # 违规 3：Input Schema 未开启 extra="forbid"
    class LooseInput(BaseModel):
        keyword: str

    with pytest.raises(ToolRegistrationError, match="extra='forbid'"):
        ToolDefinition(
            name="pim__loose_search",
            description="检索商品信息；不用于修改商品库存。",
            input_schema=LooseInput,
            output_schema=SearchOrdersOutput,
            permission="pim:products:read",
            risk="low",
            handler=dummy_handler,
        )


# ============================================================================
# 用例 3：高风险退款工具触发 HITLRequired 挂起中断，注入受信任审批凭证后恢复执行
# ============================================================================


async def test_case_3_high_risk_hitl_interception_and_trusted_resume(
    harness_env: dict[str, Any],
) -> None:
    runtime = ToolRuntime(snapshot=harness_env["customer_snapshot"])
    unapproved_ctx = ExecutionContext(
        trace_id="trace_case3_hitl",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_30009",
        role="customer",
        permissions={"oms:refund:create"},
    )

    refund_call = ToolCall(
        tool_call_id="call_refund_high_risk_01",
        name="oms__apply_refund",
        arguments={
            "order_id": "ORD-20261120-88",
            "refund_amount": 299.0,
            "reason": "外包装挤压受损申请退款",
        },
    )

    # 第一阶段：无受信任审批凭证 -> 触发 HITLRequired 挂起信号，handler 绝不被执行
    suspended_res = await runtime.execute(refund_call, unapproved_ctx)
    assert suspended_res.tool_call_id == "call_refund_high_risk_01"
    assert suspended_res.status == "hitl_required"
    assert suspended_res.data is None
    assert suspended_res.error is None
    assert isinstance(suspended_res.hitl, HITLRequired)
    assert suspended_res.hitl.tool_name == "oms__apply_refund"
    assert suspended_res.hitl.risk == "high"
    assert len(harness_env["call_tracker"]["apply_refund"]) == 0

    # 网关人工审批通过后，签发绑定了 tool_call_id 与 arguments_digest 的新不可变 ExecutionContext
    approved_ctx = unapproved_ctx.with_hitl_approval(
        tool_name="oms__apply_refund",
        tool_call_id=refund_call.tool_call_id,
        approver_id="audit_manager_07",
        arguments_digest=suspended_res.hitl.arguments_digest,
    )
    # 确认原上下文依旧未持有审批态（不可变隔离）
    assert len(unapproved_ctx.hitl_approvals) == 0

    # 安全附加验证：若审批后参数被篡改（金额从 299 改为 9999），摘要失配，依旧被阻断挂起
    tampered_call = ToolCall(
        tool_call_id="call_refund_high_risk_01",
        name="oms__apply_refund",
        arguments={
            "order_id": "ORD-20261120-88",
            "refund_amount": 9999.0,
            "reason": "外包装挤压受损申请退款",
        },
    )
    tampered_res = await runtime.execute(tampered_call, approved_ctx)
    assert tampered_res.status == "hitl_required"
    assert len(harness_env["call_tracker"]["apply_refund"]) == 0

    # 第二阶段：使用获批受信任 ExecutionContext 与原始一致参数恢复执行 -> 放行成功
    resumed_res = await runtime.execute(refund_call, approved_ctx)
    assert resumed_res.tool_call_id == "call_refund_high_risk_01"
    assert resumed_res.status == "success"
    assert resumed_res.error is None
    assert resumed_res.hitl is None
    assert resumed_res.data == {
        "refund_id": "RFD-ORD-20261120-88",
        "order_id": "ORD-20261120-88",
        "refund_amount": 299.0,
        "status": "refund_approved_and_executed",
        "operator_user_id": "buyer_30009",
    }
    assert len(harness_env["call_tracker"]["apply_refund"]) == 1

    # 核验审计日志完整记录了挂起与放行全过程且透传同一 trace_id
    statuses = [(log.trace_id, log.status) for log in runtime.audit_logs]
    assert statuses == [
        ("trace_case3_hitl", "hitl_required"),
        ("trace_case3_hitl", "hitl_required"),
        ("trace_case3_hitl", "success"),
    ]


# ============================================================================
# 用例 4：execute_batch() 并发调用两个同名工具（后发先至），验证 tool_call_id 精准对齐不串台
# ============================================================================


async def test_case_4_execute_batch_concurrent_out_of_order_alignment() -> None:
    second_call_completed = asyncio.Event()
    completion_sequence: list[str] = []

    async def concurrent_search_handler(
        params: SearchOrdersInput, ctx: ExecutionContext
    ) -> dict[str, Any]:
        if params.status == "pending":
            # 第 1 个发起的调用等待第 2 个调用先完成后再返回，确定性模拟乱序完成
            await asyncio.wait_for(second_call_completed.wait(), timeout=2.0)
            completion_sequence.append("first_call_pending_done")
            return {
                "tenant_id": ctx.tenant_id,
                "owner_user_id": ctx.user_id,
                "orders": [
                    {
                        "order_id": "ORD-SLOW-FIRST-101",
                        "status": "pending",
                        "amount": 199.0,
                        "buyer_name": "李四",
                        "buyer_phone": "13900001111",
                        "shipping_address": "上海市浦东新区张江高科技园区1号",
                    }
                ],
            }

        # 第 2 个发起的调用立即完成并唤醒第 1 个调用
        completion_sequence.append("second_call_completed_done")
        second_call_completed.set()
        return {
            "tenant_id": ctx.tenant_id,
            "owner_user_id": ctx.user_id,
            "orders": [
                {
                    "order_id": "ORD-FAST-SECOND-202",
                    "status": "completed",
                    "amount": 899.0,
                    "buyer_name": "王五",
                    "buyer_phone": "13700002222",
                    "shipping_address": "北京市海淀区中关村软件园9号",
                }
            ],
        }

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="oms__search_orders",
            description="并发检索买家订单列表；不用于修改或取消订单。",
            input_schema=SearchOrdersInput,
            output_schema=SearchOrdersOutput,
            permission="oms:orders:read",
            risk="low",
            handler=concurrent_search_handler,
        )
    )
    snapshot = registry.build_snapshot(["oms__search_orders"])
    runtime = ToolRuntime(snapshot=snapshot)

    ctx = ExecutionContext(
        trace_id="trace_case4_batch",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_batch_01",
        role="customer",
        permissions={"oms:orders:read"},
    )

    batch_calls = [
        ToolCall(
            tool_call_id="tc_01_slow_pending",
            name="oms__search_orders",
            arguments={"status": "pending", "limit": 5},
        ),
        ToolCall(
            tool_call_id="tc_02_fast_completed",
            name="oms__search_orders",
            arguments={"status": "completed", "limit": 5},
        ),
    ]

    results: list[ToolResultMessage] = await runtime.execute_batch(batch_calls, ctx)

    # 证明底层确实是第 2 个调用先于第 1 个调用执行完成
    assert completion_sequence == [
        "second_call_completed_done",
        "first_call_pending_done",
    ]

    # 验证返回结果严格与原始 tool_call_id 一一对齐，数据零串台
    assert len(results) == 2

    assert results[0].tool_call_id == "tc_01_slow_pending"
    assert results[0].status == "success"
    assert results[0].data is not None
    assert results[0].data["orders"][0]["order_id"] == "ORD-SLOW-FIRST-101"
    assert results[0].data["orders"][0]["status"] == "pending"
    assert results[0].data["orders"][0]["buyer_phone"] == "139****1111"

    assert results[1].tool_call_id == "tc_02_fast_completed"
    assert results[1].status == "success"
    assert results[1].data is not None
    assert results[1].data["orders"][0]["order_id"] == "ORD-FAST-SECOND-202"
    assert results[1].data["orders"][0]["status"] == "completed"
    assert results[1].data["orders"][0]["buyer_phone"] == "137****2222"


# ============================================================================
# 补充守卫测试：依赖不可用 transport_error、业务异常 business_error、Hook 与交接契约
# ============================================================================


async def test_dependency_unavailable_business_error_and_lifecycle_hooks(
    harness_env: dict[str, Any],
) -> None:
    hook_events: list[tuple[str, str]] = []

    class RecordingHook(ToolLifecycleHook):
        async def before_tool_call(
            self, tool_call: ToolCall, context: ExecutionContext
        ) -> None:
            hook_events.append(("before", tool_call.tool_call_id))

        async def on_tool_error(
            self,
            tool_call: ToolCall,
            context: ExecutionContext,
            error: ErrorPayload,
            duration_ms: float,
        ) -> None:
            hook_events.append(("error", f"{tool_call.tool_call_id}:{error.error_type}"))

        async def after_tool_call(
            self,
            tool_call: ToolCall,
            context: ExecutionContext,
            result: ToolResultMessage,
            duration_ms: float,
        ) -> None:
            hook_events.append(("after", f"{tool_call.tool_call_id}:{result.status}"))

    oms_healthy = False

    async def check_domain_health(domain: str, tool_name: str) -> bool:
        if domain == "oms":
            return oms_healthy
        return True

    runtime = ToolRuntime(
        snapshot=harness_env["customer_snapshot"],
        hooks=[RecordingHook()],
        dependency_checker=check_domain_health,
    )
    ctx = ExecutionContext(
        trace_id="trace_dep_and_biz",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_50001",
        role="customer",
        permissions={"oms:orders:read", "oms:refund:create"},
    )

    # 下游依赖不可用 -> 前置第 5 步拦截返回 transport_error (retryable=True)
    dep_err_res = await runtime.execute(
        ToolCall(
            tool_call_id="call_dep_down",
            name="oms__search_orders",
            arguments={"status": "paid"},
        ),
        ctx,
    )
    assert dep_err_res.status == "error"
    assert dep_err_res.error is not None
    assert dep_err_res.error.error_type == "transport_error"
    assert dep_err_res.error.retryable is True

    # 恢复下游依赖，测试高风险审批通过但业务规则拒绝 -> business_error
    oms_healthy = True
    frozen_order_call = ToolCall(
        tool_call_id="call_biz_frozen",
        name="oms__apply_refund",
        arguments={
            "order_id": "ORD-LOCKED-999",
            "refund_amount": 100.0,
            "reason": "申请退货退款",
        },
    )
    approved_ctx = ctx.with_hitl_approval(
        tool_name="oms__apply_refund",
        tool_call_id="call_biz_frozen",
    )
    biz_err_res = await runtime.execute(frozen_order_call, approved_ctx)
    assert biz_err_res.status == "error"
    assert biz_err_res.error is not None
    assert biz_err_res.error.error_type == "business_error"
    assert biz_err_res.error.code == "ORDER_REFUND_FROZEN"
    assert biz_err_res.error.retryable is False

    assert hook_events == [
        ("before", "call_dep_down"),
        ("error", "call_dep_down:transport_error"),
        ("after", "call_dep_down:error"),
        ("before", "call_biz_frozen"),
        ("error", "call_biz_frozen:business_error"),
        ("after", "call_biz_frozen:error"),
    ]


def test_multi_agent_handoff_artifacts_strong_validation() -> None:
    """验证 B 端多 Agent 交接契约 DiagnosisArtifact 与 SelectionProposal 的强类型约束。"""
    diagnosis = DiagnosisArtifact(
        problem_category="连衣裙",
        anomalous_metrics=[
            AnomalousMetric(
                metric_name="return_rate",
                current_value=0.34,
                benchmark_value=0.18,
                delta_pct=88.9,
                summary="浅色雪纺内衬偏透与梨形尺码偏紧导致退货激增",
            )
        ],
        top_return_reasons=[
            ReturnReasonStat(reason="浅色雪纺内衬偏透", share=0.50, order_count=150),
            ReturnReasonStat(reason="腰臀尺码偏小", share=0.30, order_count=90),
            ReturnReasonStat(reason="面料静电贴腿", share=0.20, order_count=60),
        ],
        price_band_gaps=[
            PriceBandGap(
                min_price=329.0,
                max_price=499.0,
                demand_index=2.48,
                gap_description="双层防透三醋酸连衣裙在 329-499 元价格带转化率高但店铺缺款",
            )
        ],
    )
    assert diagnosis.problem_category == "连衣裙"

    # Top3 退货原因超过 3 项必须被拦截
    with pytest.raises(ValidationError):
        DiagnosisArtifact(
            problem_category="连衣裙",
            anomalous_metrics=diagnosis.anomalous_metrics,
            top_return_reasons=[
                ReturnReasonStat(reason="r1", share=0.25),
                ReturnReasonStat(reason="r2", share=0.25),
                ReturnReasonStat(reason="r3", share=0.25),
                ReturnReasonStat(reason="r4", share=0.25),
            ],
            price_band_gaps=diagnosis.price_band_gaps,
        )

    # 试图在交接物中塞入原始对话日志 messages 必须被 extra="forbid" 拒绝
    with pytest.raises(ValidationError):
        SelectionProposal.model_validate(
            {
                "candidate_skus": [
                    {
                        "sku_id": "SKU-DRESS-501",
                        "sku_name": "双层防透莱赛尔三醋酸连衣裙",
                        "target_price": 429.0,
                        "estimated_unit_cost": 175.0,
                    }
                ],
                "estimated_gross_margin": 0.59,
                "core_selling_points": ["双层真丝感防透全衬", "亚洲梨形立体放量腰臀剪裁"],
                "risk_warnings": ["首批进口醋酸面料交期约 14 天，需提前锁仓"],
                "raw_chat_messages": ["user: 帮我分析...", "assistant: ..."],
            }
        )

    proposal = SelectionProposal(
        candidate_skus=[
            CandidateSKU(
                sku_id="SKU-DRESS-501",
                sku_name="双层防透莱赛尔三醋酸连衣裙",
                target_price=429.0,
                estimated_unit_cost=175.0,
            )
        ],
        estimated_gross_margin=0.59,
        core_selling_points=["双层真丝感防透全衬", "亚洲梨形立体放量腰臀剪裁"],
        risk_warnings=["首批进口醋酸面料交期约 14 天，需提前锁仓"],
    )
    assert proposal.estimated_gross_margin == 0.59
