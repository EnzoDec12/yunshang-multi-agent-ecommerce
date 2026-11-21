"""商品与库存域独立 MCP Server（`pim`）。

暴露能力：
- Tools：
  - `hybrid_search`（`risk="low"`）：多条件混合检索商品目录
  - `compare_skus`（`risk="low"`）：多款 SKU 规格、价格与卖点横向对比
  - `check_stock`（`risk="low"`）：实时可售库存核验（支持缺货/库存不足业务规则拦截）
  - `draft_sku_proposal`（`risk="high"`，仅限 B 端商家）：提交新品立项建档草稿（需经 ToolRuntime HITL 审批）
- Resource：
  - `pim://knowledge/selling-points`：商品卖点知识库与导购合规话术规范
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from mcp_servers.mock_data import SELLING_POINTS_KNOWLEDGE_MD, clone_sku_catalog
from mcp_servers.protocol import (
    FaultInjectionState,
    attach_standard_routes,
    authorize_domain_call,
    build_tool_meta,
    maybe_apply_fault_delay,
    raise_mcp_domain_error,
    run_mcp_http_cli,
)

DOMAIN = "pim"
DEFAULT_PORT = 18101


class WireOutputModel(BaseModel):
    """允许携带后端内部字段跨线传输，由客户端 ToolRuntime Output Schema 统一裁剪。"""

    model_config = ConfigDict(extra="allow")


class SkuSummaryItem(WireOutputModel):
    sku_id: str
    name: str
    category: str
    brand: str
    retail_price: float
    available_stock: int
    sizes: str = ""
    color: str = ""
    fabric: str = ""
    selling_points: list[str]
    rating: float


class HybridSearchOutput(WireOutputModel):
    tenant_id: str
    total_matched: int
    items: list[SkuSummaryItem]


class SkuComparisonEntry(WireOutputModel):
    sku_id: str
    name: str
    category: str
    retail_price: float
    available_stock: int
    sizes: str = ""
    color: str = ""
    fabric: str = ""
    specs: dict[str, str]
    selling_points: list[str]
    rating: float


class CompareSkusOutput(WireOutputModel):
    compared_count: int
    min_price: float
    max_price: float
    entries: list[SkuComparisonEntry]
    comparison_summary: str


class CheckStockOutput(WireOutputModel):
    sku_id: str
    sku_name: str
    available_stock: int
    required_qty: int
    warehouse: str
    stock_status: str


class DraftSkuProposalOutput(WireOutputModel):
    proposal_id: str
    tenant_id: str
    proposer_user_id: str
    sku_name: str
    category: str
    target_retail_price: float
    estimated_unit_cost: float
    estimated_gross_margin: float
    initial_order_qty: int
    core_selling_points: list[str]
    knowledge_base_synced: bool = True
    synced_resource_uri: str = "pim://knowledge/selling-points"
    status: str


def create_server(
    fault_state: FaultInjectionState | None = None,
) -> MCPServer[Any]:
    """创建隔离的商品与库存域 `MCPServer` 实例。"""
    active_fault = fault_state or FaultInjectionState()
    sku_catalog = clone_sku_catalog()
    draft_proposals: dict[str, dict[str, Any]] = {}
    selling_points_flywheel: list[dict[str, Any]] = []

    server = MCPServer(
        name="pim-mcp-server",
        title="PIM Product & Inventory MCP Server",
        description="商品检索、参数对比、实时库存校验与新品立项建档独立域服务",
        version="1.0.0",
    )
    attach_standard_routes(server, domain=DOMAIN, fault_state=active_fault)

    @server.resource(
        "pim://knowledge/selling-points",
        name="product_selling_points_kb",
        title="商品卖点知识库",
        description="全品类核心差异化卖点提炼与导购广告法禁用词合规规范",
        mime_type="text/markdown",
    )
    async def get_selling_points_knowledge() -> str:
        if not selling_points_flywheel:
            return SELLING_POINTS_KNOWLEDGE_MD
        lines = [
            SELLING_POINTS_KNOWLEDGE_MD.rstrip(),
            "",
            "## 三、B端选品立项新增商品卖点同步（数据飞轮）",
        ]
        for idx, entry in enumerate(selling_points_flywheel, start=1):
            joined_points = "；".join(entry["core_selling_points"])
            lines.append(
                f"{idx}. **{entry['sku_name']}**（立项单号 `{entry['proposal_id']}`，"
                f"类目：{entry['category']}，建议零售价 ¥{entry['target_retail_price']:.2f}）："
                f"核心卖点：{joined_points}"
            )
        return "\n".join(lines) + "\n"

    @server.tool(
        name="hybrid_search",
        description=(
            "按关键词、商品品类与价格区间混合检索在售商品目录；"
            "不用于修改商品价格库存，禁止传入任何用户或租户身份字段。"
        ),
        meta=build_tool_meta(
            permission="pim:catalog:read",
            risk="low",
            allowed_roles={"customer", "merchant"},
        ),
    )
    async def hybrid_search(
        keyword: Annotated[
            str | None,
            Field(default=None, description="搜索关键词（匹配商品名称、品牌或核心卖点）"),
        ] = None,
        category: Annotated[
            str | None,
            Field(default=None, description="女装品类过滤（如 连衣裙、衬衫、裤装、针织衫）"),
        ] = None,
        min_price: Annotated[
            float | None,
            Field(default=None, ge=0.0, description="最低零售价下界（元）"),
        ] = None,
        max_price: Annotated[
            float | None,
            Field(default=None, gt=0.0, description="最高零售价上界（元）"),
        ] = None,
        top_k: Annotated[
            int,
            Field(default=10, ge=1, le=50, description="返回条数上限（1~50）"),
        ] = 10,
        *,
        ctx: Context[Any, Any],
    ) -> HybridSearchOutput:
        await maybe_apply_fault_delay(active_fault, "hybrid_search")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="pim:catalog:read",
            allowed_roles={"customer", "merchant"},
        )
        if (
            min_price is not None
            and max_price is not None
            and max_price < min_price
        ):
            raise_mcp_domain_error(
                error_type="validation_error",
                code="INVALID_PRICE_RANGE",
                message="max_price 不得小于 min_price",
                details={"min_price": min_price, "max_price": max_price},
            )

        kw_lower = keyword.strip().lower() if keyword else None
        cat_clean = category.strip() if category else None

        matched: list[dict[str, Any]] = []
        for sku in sku_catalog.values():
            if exec_ctx.tenant_id not in sku["tenant_ids"]:
                continue
            if cat_clean and sku["category"] != cat_clean:
                continue
            if min_price is not None and sku["retail_price"] < min_price:
                continue
            if max_price is not None and sku["retail_price"] > max_price:
                continue
            if kw_lower:
                haystack = " ".join(
                    [
                        sku["sku_id"],
                        sku["name"],
                        sku["brand"],
                        sku["category"],
                        sku.get("color", ""),
                        sku.get("fabric", ""),
                        sku.get("sizes", ""),
                        *sku["selling_points"],
                    ]
                ).lower()
                if kw_lower not in haystack:
                    continue
            matched.append(dict(sku))

        matched.sort(key=lambda item: (-item["rating"], item["retail_price"]))
        sliced = matched[:top_k]
        return HybridSearchOutput.model_validate(
            {
                "tenant_id": exec_ctx.tenant_id,
                "total_matched": len(matched),
                "items": sliced,
            }
        )

    @server.tool(
        name="compare_skus",
        description=(
            "对 2~5 款指定 SKU 进行规格参数、价格、实时库存与核心卖点横向对比；"
            "不用于下单锁库或修改商品主数据。"
        ),
        meta=build_tool_meta(
            permission="pim:catalog:read",
            risk="low",
            allowed_roles={"customer", "merchant"},
        ),
    )
    async def compare_skus(
        sku_ids: Annotated[
            list[str],
            Field(..., min_length=2, max_length=5, description="待横向对比的 2~5 个标准 SKU 编码"),
        ],
        *,
        ctx: Context[Any, Any],
    ) -> CompareSkusOutput:
        await maybe_apply_fault_delay(active_fault, "compare_skus")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="pim:catalog:read",
            allowed_roles={"customer", "merchant"},
        )

        entries: list[dict[str, Any]] = []
        missing: list[str] = []
        for sku_id in sku_ids:
            sku = sku_catalog.get(sku_id)
            if sku is None or exec_ctx.tenant_id not in sku["tenant_ids"]:
                missing.append(sku_id)
                continue
            entries.append(dict(sku))

        if missing:
            raise_mcp_domain_error(
                error_type="business_error",
                code="SKU_NOT_FOUND",
                message=f"部分待对比 SKU 不存在或不属于当前店铺: {', '.join(missing)}",
                details={"missing_sku_ids": missing},
            )

        prices = [item["retail_price"] for item in entries]
        top_rated = max(entries, key=lambda item: item["rating"])
        summary = (
            f"共对比 {len(entries)} 款商品，价格区间 ¥{min(prices):.0f}~¥{max(prices):.0f}，"
            f"综合口碑最高为 {top_rated['name']}（评分 {top_rated['rating']}）。"
        )
        return CompareSkusOutput.model_validate(
            {
                "compared_count": len(entries),
                "min_price": min(prices),
                "max_price": max(prices),
                "entries": entries,
                "comparison_summary": summary,
            }
        )

    @server.tool(
        name="check_stock",
        description=(
            "查询指定 SKU 的实时可售库存与发货仓分布，并核验是否满足需求数量；"
            "不用于实际扣减库存或创建采购单。"
        ),
        meta=build_tool_meta(
            permission="pim:stock:read",
            risk="low",
            allowed_roles={"customer", "merchant"},
        ),
    )
    async def check_stock(
        sku_id: Annotated[
            str,
            Field(..., min_length=1, description="待核验库存的标准 SKU 编码"),
        ],
        required_qty: Annotated[
            int,
            Field(default=1, ge=1, le=10000, description="期望购买或调拨核验数量（>=1）"),
        ] = 1,
        *,
        ctx: Context[Any, Any],
    ) -> CheckStockOutput:
        await maybe_apply_fault_delay(active_fault, "check_stock")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="pim:stock:read",
            allowed_roles={"customer", "merchant"},
        )

        sku = sku_catalog.get(sku_id)
        if sku is None or exec_ctx.tenant_id not in sku["tenant_ids"]:
            raise_mcp_domain_error(
                error_type="business_error",
                code="SKU_NOT_FOUND",
                message=f"商品 SKU '{sku_id}' 不存在或未在当前租户上架",
                details={"sku_id": sku_id},
            )

        available = int(sku["available_stock"])
        if available < required_qty:
            raise_mcp_domain_error(
                error_type="business_error",
                code="INSUFFICIENT_STOCK",
                message=(
                    f"商品 '{sku['name']}'（{sku_id}）可售库存不足："
                    f"当前仅剩 {available} 件，无法满足需求数量 {required_qty} 件"
                ),
                details={
                    "sku_id": sku_id,
                    "available_stock": available,
                    "required_qty": required_qty,
                    "warehouse": sku["warehouse"],
                },
            )

        stock_status = "low_stock" if available <= 10 else "in_stock"
        return CheckStockOutput(
            sku_id=sku_id,
            sku_name=sku["name"],
            available_stock=available,
            required_qty=required_qty,
            warehouse=sku["warehouse"],
            stock_status=stock_status,
        )

    @server.tool(
        name="draft_sku_proposal",
        description=(
            "提交新品选品立项建档草稿至商品中心（高风险写操作，需人工审批放行）；"
            "仅限 B 端商家选品企划使用，禁止向 C 端买家开放或直接绕过审批正式上架。"
        ),
        meta=build_tool_meta(
            permission="pim:sku:propose",
            risk="high",
            allowed_roles={"merchant"},
        ),
    )
    async def draft_sku_proposal(
        sku_name: Annotated[
            str,
            Field(..., min_length=2, max_length=120, description="拟立项新品标准名称"),
        ],
        category: Annotated[
            str,
            Field(..., min_length=2, max_length=50, description="所属叶子类目"),
        ],
        target_retail_price: Annotated[
            float,
            Field(..., gt=0.0, le=100000.0, description="拟定建议零售价（元）"),
        ],
        estimated_unit_cost: Annotated[
            float,
            Field(..., gt=0.0, le=100000.0, description="预估单件综合到岸成本（元）"),
        ],
        core_selling_points: Annotated[
            list[str],
            Field(..., min_length=1, max_length=8, description="核心差异化卖点列表"),
        ],
        initial_order_qty: Annotated[
            int,
            Field(default=200, ge=50, le=50000, description="首批试销备货件数（50~50000）"),
        ] = 200,
        *,
        ctx: Context[Any, Any],
    ) -> DraftSkuProposalOutput:
        await maybe_apply_fault_delay(active_fault, "draft_sku_proposal")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="pim:sku:propose",
            allowed_roles={"merchant"},
        )

        if estimated_unit_cost >= target_retail_price:
            raise_mcp_domain_error(
                error_type="business_error",
                code="NEGATIVE_OR_ZERO_GROSS_MARGIN",
                message="新品预估到岸成本不得大于或等于建议零售价，禁止负毛利立项",
                details={
                    "target_retail_price": target_retail_price,
                    "estimated_unit_cost": estimated_unit_cost,
                },
            )

        cleaned_selling_points = [
            pt.strip() for pt in core_selling_points if pt and pt.strip()
        ]
        gross_margin = round(
            (target_retail_price - estimated_unit_cost) / target_retail_price, 4
        )
        proposal_id = f"PROP-{uuid.uuid4().hex[:8].upper()}"
        record = {
            "proposal_id": proposal_id,
            "tenant_id": exec_ctx.tenant_id,
            "proposer_user_id": exec_ctx.user_id,
            "sku_name": sku_name.strip(),
            "category": category.strip(),
            "target_retail_price": target_retail_price,
            "estimated_unit_cost": estimated_unit_cost,
            "estimated_gross_margin": gross_margin,
            "initial_order_qty": initial_order_qty,
            "core_selling_points": cleaned_selling_points,
            "knowledge_base_synced": True,
            "synced_resource_uri": "pim://knowledge/selling-points",
            "status": "draft_submitted",
        }
        draft_proposals[proposal_id] = record
        selling_points_flywheel.append(
            {
                "proposal_id": proposal_id,
                "tenant_id": exec_ctx.tenant_id,
                "sku_name": record["sku_name"],
                "category": record["category"],
                "target_retail_price": target_retail_price,
                "core_selling_points": cleaned_selling_points,
            }
        )
        return DraftSkuProposalOutput.model_validate(record)

    return server


def create_app(host: str = "127.0.0.1", path: str = "/mcp") -> Starlette:
    """构建 PIM 域 Streamable HTTP Starlette ASGI 应用。"""
    server = create_server()
    return server.streamable_http_app(host=host, streamable_http_path=path)


def main(argv: list[str] | None = None) -> None:
    """独立进程 CLI 启动入口：`python -m mcp_servers.pim_server`。"""
    run_mcp_http_cli(
        create_app_fn=create_app,
        default_port=DEFAULT_PORT,
        description="PIM Product & Inventory Domain MCP Server (Streamable HTTP)",
        argv=argv,
    )


if __name__ == "__main__":
    main()
