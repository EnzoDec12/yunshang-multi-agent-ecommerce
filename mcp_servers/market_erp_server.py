"""选品与供应链域独立 MCP Server（`market_erp`，B 端商家专属域）。

暴露能力：
- Tools：
  - `search_trends`（`risk="low"`）：检索行业大盘品类增长趋势、高潜价格带缺口与消费者痛点
  - `calc_landed_cost`（`risk="low"`）：测算候选新品全链路到岸成本、平台佣金扣点与预估净毛利率
- Resource：
  - `market_erp://policies/return-exchange`：平台退换货履约标准与供应链选品立项准入红线文档
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from mcp_servers.mock_data import (
    MARKET_TRENDS_DB,
    RETURN_EXCHANGE_POLICY_MD,
    SHIPPING_MODE_RATES,
)
from mcp_servers.protocol import (
    FaultInjectionState,
    attach_standard_routes,
    authorize_domain_call,
    build_tool_meta,
    maybe_apply_fault_delay,
    raise_mcp_domain_error,
    run_mcp_http_cli,
)

DOMAIN = "market_erp"
DEFAULT_PORT = 18105

ShippingModeLiteral = Literal[
    "domestic_truck",
    "sea_freight",
    "rail_freight",
    "air_freight",
]


class WireOutputModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class PriceBandGapItem(WireOutputModel):
    min_price: float
    max_price: float
    demand_index: float
    gap_description: str


class SearchTrendsOutput(WireOutputModel):
    category: str
    search_growth_yoy: float
    benchmark_return_rate: float
    hot_keywords: list[str]
    underserved_price_bands: list[PriceBandGapItem]
    consumer_pain_points: list[str]


class CalcLandedCostOutput(WireOutputModel):
    sku_name: str
    shipping_mode: str
    order_quantity: int
    lead_days: int
    fob_unit_cost: float
    freight_per_unit: float
    tariff_and_clearance_per_unit: float
    packaging_and_warehousing_per_unit: float
    platform_commission_per_unit: float
    landed_unit_cost: float
    target_retail_price: float
    estimated_gross_margin: float
    risk_warnings: list[str]


def create_server(
    fault_state: FaultInjectionState | None = None,
) -> MCPServer[Any]:
    """创建隔离的选品与供应链域 `MCPServer` 实例。"""
    active_fault = fault_state or FaultInjectionState()

    server = MCPServer(
        name="market-erp-mcp-server",
        title="Market Trends & Supply Chain ERP MCP Server",
        description="B端选品市场趋势洞察与供应链到岸成本测算独立域服务",
        version="1.0.0",
    )
    attach_standard_routes(server, domain=DOMAIN, fault_state=active_fault)

    @server.resource(
        "market_erp://policies/return-exchange",
        name="return_exchange_and_sourcing_policy",
        title="退换货政策文档",
        description="电商平台七天无理由退换货标准与供应链选品毛利/MOQ准入红线",
        mime_type="text/markdown",
    )
    async def get_return_exchange_policy() -> str:
        return RETURN_EXCHANGE_POLICY_MD

    @server.tool(
        name="search_trends",
        description=(
            "检索指定品类的行业搜索增长趋势、供给空白价格带与高频退货痛点；"
            "仅限 B 端商家选品企划使用，禁止向 C 端买家暴露行业选品情报，不用于创建采购单。"
        ),
        meta=build_tool_meta(
            permission="market_erp:trends:read",
            risk="low",
            allowed_roles={"merchant"},
        ),
    )
    async def search_trends(
        category: Annotated[
            str,
            Field(..., min_length=2, max_length=50, description="目标女装品类（如 连衣裙、衬衫、裤装）"),
        ],
        include_pain_points: Annotated[
            bool,
            Field(default=True, description="是否附带消费者差评与退货痛点词云"),
        ] = True,
        *,
        ctx: Context[Any, Any],
    ) -> SearchTrendsOutput:
        await maybe_apply_fault_delay(active_fault, "search_trends")
        authorize_domain_call(
            ctx,
            permission="market_erp:trends:read",
            allowed_roles={"merchant"},
        )

        cleaned = category.strip()
        trend = MARKET_TRENDS_DB.get(cleaned)
        if trend is None:
            raise_mcp_domain_error(
                error_type="business_error",
                code="CATEGORY_TREND_NOT_FOUND",
                message=(
                    f"暂未收录品类 '{cleaned}' 的行业趋势数据；"
                    f"当前支持品类：{', '.join(sorted(MARKET_TRENDS_DB))}"
                ),
                details={"supported_categories": sorted(MARKET_TRENDS_DB)},
            )
        assert trend is not None

        payload = dict(trend)
        if not include_pain_points:
            payload["consumer_pain_points"] = []
        return SearchTrendsOutput.model_validate(payload)

    @server.tool(
        name="calc_landed_cost",
        description=(
            "测算候选商品从工厂出厂价（FOB/EXW）、头程干线物流、关税清关、仓配包材到平台佣金的全链路单件到岸成本与预估毛利率；"
            "仅限 B 端商家供应链测算使用，不用于实际财务付款或锁仓下单。"
        ),
        meta=build_tool_meta(
            permission="market_erp:cost:calculate",
            risk="low",
            allowed_roles={"merchant"},
        ),
    )
    async def calc_landed_cost(
        sku_name: Annotated[
            str,
            Field(..., min_length=2, max_length=120, description="候选商品名称或规格标识"),
        ],
        fob_unit_cost: Annotated[
            float,
            Field(..., gt=0.0, le=50000.0, description="工厂出厂单价（元/件）"),
        ],
        unit_weight_kg: Annotated[
            float,
            Field(..., gt=0.0, le=100.0, description="单件含包装计费重量（kg）"),
        ],
        order_quantity: Annotated[
            int,
            Field(..., ge=1, le=100000, description="计划采购件数"),
        ],
        target_retail_price: Annotated[
            float,
            Field(..., gt=0.0, le=100000.0, description="目标上架零售价（元）"),
        ],
        shipping_mode: Annotated[
            ShippingModeLiteral,
            Field(
                default="domestic_truck",
                description="头程运输方式（domestic_truck/sea_freight/rail_freight/air_freight）",
            ),
        ] = "domestic_truck",
        tariff_rate: Annotated[
            float,
            Field(default=0.0, ge=0.0, le=0.5, description="进口关税税率（0.0~0.5，国内供应链填 0）"),
        ] = 0.0,
        *,
        ctx: Context[Any, Any],
    ) -> CalcLandedCostOutput:
        await maybe_apply_fault_delay(active_fault, "calc_landed_cost")
        authorize_domain_call(
            ctx,
            permission="market_erp:cost:calculate",
            allowed_roles={"merchant"},
        )

        rate_cfg = SHIPPING_MODE_RATES[shipping_mode]
        min_moq = int(rate_cfg["moq"])
        if order_quantity < min_moq:
            raise_mcp_domain_error(
                error_type="business_error",
                code="MOQ_NOT_MET",
                message=(
                    f"运输方式 '{shipping_mode}' 要求最低起订量（MOQ）为 {min_moq} 件，"
                    f"当前测算数量 {order_quantity} 件不满足供应链集拼准入线"
                ),
                details={
                    "shipping_mode": shipping_mode,
                    "required_moq": min_moq,
                    "order_quantity": order_quantity,
                },
            )

        freight_per_unit = round(float(rate_cfg["per_kg_fee"]) * unit_weight_kg, 2)
        clearance_per_unit = round(float(rate_cfg["customs_clearance_flat"]) / order_quantity, 2)
        tariff_per_unit = round(fob_unit_cost * tariff_rate, 2)
        tariff_and_clearance = round(clearance_per_unit + tariff_per_unit, 2)
        packaging_warehousing = 6.50
        platform_commission = round(target_retail_price * 0.05, 2)

        landed_unit_cost = round(
            fob_unit_cost
            + freight_per_unit
            + tariff_and_clearance
            + packaging_warehousing
            + platform_commission,
            2,
        )
        gross_margin = round(
            (target_retail_price - landed_unit_cost) / target_retail_price,
            4,
        )

        warnings: list[str] = []
        if gross_margin < 0.15:
            warnings.append(
                f"预估净毛利率仅 {gross_margin * 100:.1f}%（低于 15% 安全红线），存在退货损耗倒挂亏损风险"
            )
        if int(rate_cfg["lead_days"]) >= 14:
            warnings.append(
                f"当前运输方式交期约 {int(rate_cfg['lead_days'])} 天，需提前锁定安全库存防范断货"
            )
        if not warnings:
            warnings.append("毛利空间健康，建议首批小单快反试销验证退货率后再放量追单")

        return CalcLandedCostOutput(
            sku_name=sku_name.strip(),
            shipping_mode=shipping_mode,
            order_quantity=order_quantity,
            lead_days=int(rate_cfg["lead_days"]),
            fob_unit_cost=round(fob_unit_cost, 2),
            freight_per_unit=freight_per_unit,
            tariff_and_clearance_per_unit=tariff_and_clearance,
            packaging_and_warehousing_per_unit=packaging_warehousing,
            platform_commission_per_unit=platform_commission,
            landed_unit_cost=landed_unit_cost,
            target_retail_price=round(target_retail_price, 2),
            estimated_gross_margin=gross_margin,
            risk_warnings=warnings,
        )

    return server


def create_app(host: str = "127.0.0.1", path: str = "/mcp") -> Starlette:
    """构建 Market & ERP 域 Streamable HTTP Starlette ASGI 应用。"""
    server = create_server()
    return server.streamable_http_app(host=host, streamable_http_path=path)


def main(argv: list[str] | None = None) -> None:
    """独立进程 CLI 启动入口：`python -m mcp_servers.market_erp_server`。"""
    run_mcp_http_cli(
        create_app_fn=create_app,
        default_port=DEFAULT_PORT,
        description="Market & ERP Supply Chain Domain MCP Server (Streamable HTTP)",
        argv=argv,
    )


if __name__ == "__main__":
    main()
