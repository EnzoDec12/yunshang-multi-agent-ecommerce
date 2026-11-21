"""订单与售后域独立 MCP Server（`oms`）。

暴露能力：
- Tools：
  - `search_orders`（`risk="low"`）：基于带外 `ExecutionContext` 执行租户与买家行级隔离的订单查询
  - `update_address`（`risk="medium"`）：未发货订单修改收货地址与联系人（校验订单状态机与归属权）
  - `create_refund`（`risk="high"`）：创建售后退款单（需经 ToolRuntime HITL 审批放行）
- Resource：
  - `oms://docs/order-state-machine`：订单状态流转与改地址/退款准入规则说明
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from mcp_servers.mock_data import ORDER_STATE_MACHINE_MD, clone_orders_store
from mcp_servers.protocol import (
    FaultInjectionState,
    attach_standard_routes,
    authorize_domain_call,
    build_tool_meta,
    maybe_apply_fault_delay,
    raise_mcp_domain_error,
    run_mcp_http_cli,
)
from schemas.context import ExecutionContext

DOMAIN = "oms"
DEFAULT_PORT = 18102

OrderStatusLiteral = Literal["pending", "paid", "shipped", "completed", "refunded"]


class WireOutputModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class OrderRecordOutput(WireOutputModel):
    order_id: str
    status: str
    created_at: str
    sku_id: str
    sku_name: str
    quantity: int
    total_amount: float
    buyer_name: str
    buyer_phone: str
    buyer_email: str
    shipping_address: str
    tracking_no: str | None = None


class SearchOrdersOutput(WireOutputModel):
    tenant_id: str
    viewer_role: str
    total_count: int
    orders: list[OrderRecordOutput]


class UpdateAddressOutput(WireOutputModel):
    order_id: str
    status: str
    receiver_name: str
    receiver_phone: str
    shipping_address: str
    updated_by: str


class CreateRefundOutput(WireOutputModel):
    refund_id: str
    order_id: str
    refund_amount: float
    order_status: str
    reason: str
    operator_user_id: str


def _verify_order_access(
    order: dict[str, Any] | None,
    *,
    order_id: str,
    exec_ctx: ExecutionContext,
) -> dict[str, Any]:
    """强制执行租户边界与 C 端买家本人订单归属隔离校验。"""
    if order is None:
        raise_mcp_domain_error(
            error_type="business_error",
            code="ORDER_NOT_FOUND",
            message=f"订单 '{order_id}' 不存在",
            details={"order_id": order_id},
        )
    assert order is not None

    if order["tenant_id"] != exec_ctx.tenant_id:
        raise_mcp_domain_error(
            error_type="permission_error",
            code="TENANT_ISOLATION_DENIED",
            message=f"跨租户访问被拒绝：订单 '{order_id}' 不属于当前租户边界",
            details={"order_id": order_id},
        )

    if exec_ctx.role == "customer" and order["buyer_user_id"] != exec_ctx.user_id:
        raise_mcp_domain_error(
            error_type="permission_error",
            code="ORDER_OWNERSHIP_DENIED",
            message=f"越权访问被拒绝：C 端买家仅允许查询或操作本人名下订单（'{order_id}'）",
            details={"order_id": order_id},
        )

    return order


def create_server(
    fault_state: FaultInjectionState | None = None,
) -> MCPServer[Any]:
    """创建隔离的订单与售后域 `MCPServer` 实例。"""
    active_fault = fault_state or FaultInjectionState()
    orders_store = clone_orders_store()

    server = MCPServer(
        name="oms-mcp-server",
        title="OMS Order & After-Sales MCP Server",
        description="订单检索、未发货改地址与售后退款独立域服务（强制租户与买家隔离）",
        version="1.0.0",
    )
    attach_standard_routes(server, domain=DOMAIN, fault_state=active_fault)

    @server.resource(
        "oms://docs/order-state-machine",
        name="order_state_machine_spec",
        title="订单状态机说明",
        description="订单生命周期流转规则、改地址截止节点与售后退款准入边界",
        mime_type="text/markdown",
    )
    async def get_order_state_machine_doc() -> str:
        return ORDER_STATE_MACHINE_MD

    @server.tool(
        name="search_orders",
        description=(
            "按订单状态、起始创建日期或指定订单号检索当前权限范围内的订单列表；"
            "必须至少提供一项过滤条件，禁止无条件全表扫描，不用于修改订单或发起退款。"
        ),
        meta=build_tool_meta(
            permission="oms:orders:read",
            risk="low",
            allowed_roles={"customer", "merchant"},
        ),
    )
    async def search_orders(
        status: Annotated[
            OrderStatusLiteral | None,
            Field(default=None, description="订单状态过滤（pending/paid/shipped/completed/refunded）"),
        ] = None,
        created_from: Annotated[
            str | None,
            Field(
                default=None,
                pattern=r"^\d{4}-\d{2}-\d{2}$",
                description="订单创建起始日期（YYYY-MM-DD）",
            ),
        ] = None,
        order_id: Annotated[
            str | None,
            Field(default=None, min_length=3, description="精确检索的订单号"),
        ] = None,
        limit: Annotated[
            int,
            Field(default=20, ge=1, le=50, description="单次返回最大订单条数（1~50）"),
        ] = 20,
        *,
        ctx: Context[Any, Any],
    ) -> SearchOrdersOutput:
        await maybe_apply_fault_delay(active_fault, "search_orders")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="oms:orders:read",
            allowed_roles={"customer", "merchant"},
        )

        if status is None and created_from is None and order_id is None:
            raise_mcp_domain_error(
                error_type="validation_error",
                code="MISSING_ORDER_FILTER",
                message="查询订单必须至少提供 status、created_from 或 order_id 过滤条件之一，禁止全量扫描",
            )

        results: list[dict[str, Any]] = []
        for record in orders_store.values():
            if record["tenant_id"] != exec_ctx.tenant_id:
                continue
            if (
                exec_ctx.role == "customer"
                and record["buyer_user_id"] != exec_ctx.user_id
            ):
                continue
            if order_id is not None and record["order_id"] != order_id:
                continue
            if status is not None and record["status"] != status:
                continue
            if created_from is not None and record["created_at"] < created_from:
                continue
            results.append(dict(record))

        results.sort(key=lambda item: (item["created_at"], item["order_id"]), reverse=True)
        sliced = results[:limit]
        return SearchOrdersOutput.model_validate(
            {
                "tenant_id": exec_ctx.tenant_id,
                "viewer_role": exec_ctx.role,
                "total_count": len(sliced),
                "orders": sliced,
            }
        )

    @server.tool(
        name="update_address",
        description=(
            "修改待支付或已支付未发货订单的收货地址与收件人联系方式；"
            "已发货、已签收或已退款订单禁止调用本工具改地址，不用于订单取消或退款。"
        ),
        meta=build_tool_meta(
            permission="oms:orders:update_address",
            risk="medium",
            allowed_roles={"customer", "merchant"},
        ),
    )
    async def update_address(
        order_id: Annotated[
            str,
            Field(..., min_length=3, description="目标订单号"),
        ],
        new_shipping_address: Annotated[
            str,
            Field(..., min_length=6, max_length=200, description="新详细收货地址"),
        ],
        receiver_name: Annotated[
            str,
            Field(..., min_length=1, max_length=40, description="新收件人姓名"),
        ],
        receiver_phone: Annotated[
            str,
            Field(..., min_length=7, max_length=20, description="新收件人联系手机号"),
        ],
        *,
        ctx: Context[Any, Any],
    ) -> UpdateAddressOutput:
        await maybe_apply_fault_delay(active_fault, "update_address")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="oms:orders:update_address",
            allowed_roles={"customer", "merchant"},
        )

        order = _verify_order_access(
            orders_store.get(order_id),
            order_id=order_id,
            exec_ctx=exec_ctx,
        )

        if order["status"] not in ("pending", "paid"):
            raise_mcp_domain_error(
                error_type="business_error",
                code="ORDER_STATUS_DISALLOWS_ADDRESS_UPDATE",
                message=(
                    f"订单 '{order_id}' 当前状态为 '{order['status']}'，"
                    "仓配已出库或交易已终结，不允许修改收货地址"
                ),
                details={
                    "order_id": order_id,
                    "current_status": order["status"],
                    "allowed_statuses": ["pending", "paid"],
                },
            )

        order["shipping_address"] = new_shipping_address.strip()
        order["buyer_name"] = receiver_name.strip()
        order["buyer_phone"] = receiver_phone.strip()

        return UpdateAddressOutput(
            order_id=order_id,
            status=order["status"],
            receiver_name=order["buyer_name"],
            receiver_phone=order["buyer_phone"],
            shipping_address=order["shipping_address"],
            updated_by=exec_ctx.user_id,
        )

    @server.tool(
        name="create_refund",
        description=(
            "针对已支付或已履约订单创建售后退款单并触发原路退费（高风险资金操作，必须经人工审批）；"
            "禁止对已退款订单重复退费或超出实付金额退费，禁止伪造身份或审批状态。"
        ),
        meta=build_tool_meta(
            permission="oms:refund:create",
            risk="high",
            allowed_roles={"customer", "merchant"},
        ),
    )
    async def create_refund(
        order_id: Annotated[
            str,
            Field(..., min_length=3, description="申请退款的目标订单号"),
        ],
        refund_amount: Annotated[
            float,
            Field(..., gt=0.0, le=100000.0, description="申请退款金额（元，不得超过订单实付总额）"),
        ],
        reason: Annotated[
            str,
            Field(..., min_length=2, max_length=200, description="退款原因说明"),
        ],
        *,
        ctx: Context[Any, Any],
    ) -> CreateRefundOutput:
        await maybe_apply_fault_delay(active_fault, "create_refund")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="oms:refund:create",
            allowed_roles={"customer", "merchant"},
        )

        order = _verify_order_access(
            orders_store.get(order_id),
            order_id=order_id,
            exec_ctx=exec_ctx,
        )

        if order["status"] == "refunded":
            raise_mcp_domain_error(
                error_type="business_error",
                code="ORDER_ALREADY_REFUNDED",
                message=f"订单 '{order_id}' 已完成退款关闭，禁止重复发起退款",
                details={"order_id": order_id, "current_status": "refunded"},
            )

        if order["status"] == "pending":
            raise_mcp_domain_error(
                error_type="business_error",
                code="ORDER_UNPAID_CANNOT_REFUND",
                message=f"订单 '{order_id}' 尚未支付，无需创建资金退款单",
                details={"order_id": order_id, "current_status": "pending"},
            )

        paid_total = float(order["total_amount"])
        if refund_amount > paid_total:
            raise_mcp_domain_error(
                error_type="business_error",
                code="REFUND_AMOUNT_EXCEEDS_PAID",
                message=(
                    f"申请退款金额 ¥{refund_amount:.2f} 超过订单实付金额 ¥{paid_total:.2f}"
                ),
                details={
                    "order_id": order_id,
                    "refund_amount": refund_amount,
                    "paid_total": paid_total,
                },
            )

        order["status"] = "refunded"
        refund_id = f"RFD-{order_id}-{uuid.uuid4().hex[:6].upper()}"
        return CreateRefundOutput(
            refund_id=refund_id,
            order_id=order_id,
            refund_amount=round(refund_amount, 2),
            order_status="refunded",
            reason=reason.strip(),
            operator_user_id=exec_ctx.user_id,
        )

    return server


def create_app(host: str = "127.0.0.1", path: str = "/mcp") -> Starlette:
    """构建 OMS 域 Streamable HTTP Starlette ASGI 应用。"""
    server = create_server()
    return server.streamable_http_app(host=host, streamable_http_path=path)


def main(argv: list[str] | None = None) -> None:
    """独立进程 CLI 启动入口：`python -m mcp_servers.oms_server`。"""
    run_mcp_http_cli(
        create_app_fn=create_app,
        default_port=DEFAULT_PORT,
        description="OMS Order & After-Sales Domain MCP Server (Streamable HTTP)",
        argv=argv,
    )


if __name__ == "__main__":
    main()
