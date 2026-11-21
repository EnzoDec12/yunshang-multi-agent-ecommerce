"""物流域独立 MCP Server（`logistics`）。

暴露能力：
- Tool：
  - `track_pkg`（`risk="low"`）：按运单号或关联订单号查询包裹实时物流轨迹与预计送达时间（强制租户与买家隔离）
- Resource：
  - `logistics://docs/carrier-sla`：主流承运商揽派时效与异常件升级规范
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from mcp_servers.mock_data import clone_logistics_packages
from mcp_servers.protocol import (
    FaultInjectionState,
    attach_standard_routes,
    authorize_domain_call,
    build_tool_meta,
    maybe_apply_fault_delay,
    raise_mcp_domain_error,
    run_mcp_http_cli,
)

DOMAIN = "logistics"
DEFAULT_PORT = 18103

CARRIER_SLA_MD = """# 物流承运商时效与异常件升级规范（Logistics）

- **顺丰速运（SF）**：次日达/隔日达，揽收后 24 小时无轨迹更新触发揽收超时预警。
- **京东物流（JD）**：211 限时达/次日达，支持送货上门与预约派送。
- **圆通速递（YT）**：华东/华南跨省 48~72 小时达，末端驿站代收需发送取件码通知。
"""


class WireOutputModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TrackingCheckpoint(WireOutputModel):
    timestamp: str
    location: str
    status: str


class TrackPkgOutput(WireOutputModel):
    tracking_no: str
    order_id: str
    carrier: str
    delivery_status: str
    estimated_delivery: str
    receiver_name: str
    receiver_phone: str
    receiver_address: str
    checkpoints: list[TrackingCheckpoint]


def create_server(
    fault_state: FaultInjectionState | None = None,
) -> MCPServer[Any]:
    """创建隔离的物流域 `MCPServer` 实例。"""
    active_fault = fault_state or FaultInjectionState()
    packages_db = clone_logistics_packages()

    server = MCPServer(
        name="logistics-mcp-server",
        title="Logistics Package Tracking MCP Server",
        description="包裹实时路由轨迹查询与签收状态核验独立域服务",
        version="1.0.0",
    )
    attach_standard_routes(server, domain=DOMAIN, fault_state=active_fault)

    @server.resource(
        "logistics://docs/carrier-sla",
        name="carrier_sla_guide",
        title="物流承运商时效说明",
        description="各快递承运商标准时效与停滞异常件处理指南",
        mime_type="text/markdown",
    )
    async def get_carrier_sla_doc() -> str:
        return CARRIER_SLA_MD

    @server.tool(
        name="track_pkg",
        description=(
            "按运单号（tracking_no）或订单号（order_id）查询包裹实时物流路由轨迹；"
            "必须至少提供运单号或订单号之一，不用于修改收货地址、催派改派或拦截退回包裹。"
        ),
        meta=build_tool_meta(
            permission="logistics:package:read",
            risk="low",
            allowed_roles={"customer", "merchant"},
        ),
    )
    async def track_pkg(
        tracking_no: Annotated[
            str | None,
            Field(default=None, min_length=4, description="快递运单号（如 SF2026882102）"),
        ] = None,
        order_id: Annotated[
            str | None,
            Field(default=None, min_length=4, description="关联平台订单号（如 ORD-20261115-2102）"),
        ] = None,
        *,
        ctx: Context[Any, Any],
    ) -> TrackPkgOutput:
        await maybe_apply_fault_delay(active_fault, "track_pkg")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="logistics:package:read",
            allowed_roles={"customer", "merchant"},
        )

        if not tracking_no and not order_id:
            raise_mcp_domain_error(
                error_type="validation_error",
                code="MISSING_TRACKING_IDENTIFIER",
                message="查询物流轨迹必须提供 tracking_no 或 order_id 至少一项",
            )

        pkg: dict[str, Any] | None = None
        if tracking_no:
            pkg = packages_db.get(tracking_no.strip())
        elif order_id:
            target_order = order_id.strip()
            for candidate in packages_db.values():
                if candidate["order_id"] == target_order:
                    pkg = candidate
                    break

        if pkg is None:
            raise_mcp_domain_error(
                error_type="business_error",
                code="PACKAGE_NOT_FOUND",
                message="未找到匹配的运单轨迹记录（订单可能尚未发货揽收或单号有误）",
                details={"tracking_no": tracking_no, "order_id": order_id},
            )
        assert pkg is not None

        if pkg["tenant_id"] != exec_ctx.tenant_id:
            raise_mcp_domain_error(
                error_type="permission_error",
                code="TENANT_ISOLATION_DENIED",
                message="跨租户访问物流运单被拒绝",
            )

        if exec_ctx.role == "customer" and pkg["buyer_user_id"] != exec_ctx.user_id:
            raise_mcp_domain_error(
                error_type="permission_error",
                code="PACKAGE_OWNERSHIP_DENIED",
                message="C 端买家仅允许查询本人名下订单关联的包裹轨迹",
            )

        return TrackPkgOutput.model_validate(dict(pkg))

    return server


def create_app(host: str = "127.0.0.1", path: str = "/mcp") -> Starlette:
    """构建 Logistics 域 Streamable HTTP Starlette ASGI 应用。"""
    server = create_server()
    return server.streamable_http_app(host=host, streamable_http_path=path)


def main(argv: list[str] | None = None) -> None:
    """独立进程 CLI 启动入口：`python -m mcp_servers.logistics_server`。"""
    run_mcp_http_cli(
        create_app_fn=create_app,
        default_port=DEFAULT_PORT,
        description="Logistics Package Tracking Domain MCP Server (Streamable HTTP)",
        argv=argv,
    )


if __name__ == "__main__":
    main()
