"""MCP 独立域服务矩阵导出包（五层架构第 5 层：独立域 MCP Server -> Handler/Resource）。

包含 5 个独立可跨进程启动的 Streamable HTTP MCP Server：
1. `pim_server`：商品与库存域（`hybrid_search` / `compare_skus` / `check_stock` / `draft_sku_proposal` + 商品卖点知识库）
2. `oms_server`：订单与售后域（`search_orders` / `update_address` / `create_refund` + 订单状态机说明）
3. `logistics_server`：物流域（`track_pkg` + 承运商时效说明）
4. `bi_server`：经营分析域（`get_table_schema` / `run_readonly_sql` / `render_chart` + 指标口径字典）
5. `market_erp_server`：选品与供应链域（`search_trends` / `calc_landed_cost` + 退换货政策文档）
"""

from mcp_servers.bi_server import create_app as create_bi_app
from mcp_servers.bi_server import create_server as create_bi_server
from mcp_servers.logistics_server import create_app as create_logistics_app
from mcp_servers.logistics_server import create_server as create_logistics_server
from mcp_servers.market_erp_server import create_app as create_market_erp_app
from mcp_servers.market_erp_server import create_server as create_market_erp_server
from mcp_servers.oms_server import create_app as create_oms_app
from mcp_servers.oms_server import create_server as create_oms_server
from mcp_servers.pim_server import create_app as create_pim_app
from mcp_servers.pim_server import create_server as create_pim_server

SERVER_MODULES: dict[str, str] = {
    "pim": "mcp_servers.pim_server",
    "oms": "mcp_servers.oms_server",
    "logistics": "mcp_servers.logistics_server",
    "bi": "mcp_servers.bi_server",
    "market_erp": "mcp_servers.market_erp_server",
}

__all__ = [
    "SERVER_MODULES",
    "create_bi_app",
    "create_bi_server",
    "create_logistics_app",
    "create_logistics_server",
    "create_market_erp_app",
    "create_market_erp_server",
    "create_oms_app",
    "create_oms_server",
    "create_pim_app",
    "create_pim_server",
]
