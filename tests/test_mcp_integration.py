"""五域独立 MCP Server 矩阵与 `MCPClientManager` 跨进程及故障注入集成测试套件。

覆盖验证项：
1. 真实子进程拉起 5 个独立域 Streamable HTTP MCP Server（`pim`、`oms`、`logistics`、`bi`、`market_erp`），
   验证 `MCPClientManager` 动态发现全部 13 个工具与各域 Resource，强制添加域前缀注册入 `ToolRegistry`，
   校验零可信字段泄露，并生成按 C/B 角色裁剪的最小权限 `ToolSnapshot`；
2. 验证通过 `ToolRuntime` 跨进程调用全矩阵工具：
   - C 端买家查商品/比价/查库存（含库存不足 `business_error`）、查单脱敏、未发货改地址与已发货改地址拦截、
     跨买家/跨租户行级隔离拒绝；
   - B 端商家调用 `bi__run_readonly_sql` 强制拦截写操作/DDL/多语句绕过与缺失 `dt` 时间分区过滤，
     正常执行聚合查询并物理隔离跨租户数据，以及图表配置渲染、市场趋势与到岸成本测算；
   - 高风险工具 `oms__create_refund` 与 `pim__draft_sku_proposal` 未审批触发 `HITLRequired` 挂起
     （远端进程零副作用），注入受信任审批凭证后跨进程放行成功；
   - `execute_batch()` 跨多域子进程乱序并发完成时 `tool_call_id` 100% 保序对齐不串台；
3. 故障对抗注入：模拟 `logistics` MCP Server 响应超时及 `market_erp` MCP Server 进程宕机/端口不可达，
   验证 `MCPClientManager` + `ToolRuntime` 稳定捕获异常并返回携带原始 `tool_call_id`、
   `error.category == "transport_error"` 及结构化自愈诊断信息的 `ToolResultMessage`，其余健康域完全不受影响。
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from mcp_servers import SERVER_MODULES
from runtime import (
    FORBIDDEN_INPUT_FIELDS,
    MCPClientManager,
    MCPServerConfig,
    ToolRegistrationError,
    ToolRegistry,
    ToolRuntime,
    json_schema_to_pydantic_model,
)
from schemas import ExecutionContext, HITLRequired, ToolCall

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


def _allocate_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


@dataclass
class SubprocessMCPServer:
    domain: str
    module: str
    port: int
    process: asyncio.subprocess.Process

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    @property
    def fault_url(self) -> str:
        return f"{self.base_url}/admin/fault"

    async def terminate_cleanly(self) -> None:
        if self.process.returncode is not None:
            return
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError):
            try:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), timeout=3.0)
            except ProcessLookupError:
                pass


async def _wait_for_server_ready(
    srv: SubprocessMCPServer,
    *,
    timeout_seconds: float = 12.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    async with httpx.AsyncClient(timeout=1.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            if srv.process.returncode is not None:
                stderr_bytes = (
                    await srv.process.stderr.read() if srv.process.stderr else b""
                )
                raise RuntimeError(
                    f"子进程 MCP Server '{srv.domain}' 启动异常退出 "
                    f"(code={srv.process.returncode}): {stderr_bytes.decode('utf-8', errors='replace')}"
                )
            try:
                resp = await client.get(srv.health_url)
                if resp.status_code == 200 and resp.json().get("status") == "ok":
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError(f"等待子进程 MCP Server '{srv.domain}' ({srv.health_url}) 就绪超时")


async def _spawn_mcp_cluster() -> dict[str, SubprocessMCPServer]:
    cluster: dict[str, SubprocessMCPServer] = {}
    used_ports: set[int] = set()
    try:
        for domain, module_path in SERVER_MODULES.items():
            port = _allocate_free_port()
            while port in used_ports:
                port = _allocate_free_port()
            used_ports.add(port)

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                module_path,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "error",
                cwd=str(WORKSPACE_ROOT),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            cluster[domain] = SubprocessMCPServer(
                domain=domain,
                module=module_path,
                port=port,
                process=proc,
            )

        await asyncio.gather(*(_wait_for_server_ready(srv) for srv in cluster.values()))
        return cluster
    except Exception:
        await asyncio.gather(
            *(srv.terminate_cleanly() for srv in cluster.values()),
            return_exceptions=True,
        )
        raise


@pytest_asyncio.fixture
async def mcp_cluster_env() -> AsyncIterator[dict[str, Any]]:
    """跨进程拉起全部 5 个 Streamable HTTP MCP Server 并初始化 `MCPClientManager` 与 `ToolRegistry`。"""
    cluster = await _spawn_mcp_cluster()
    configs = [
        MCPServerConfig(
            domain=domain,
            url=srv.mcp_url,
            timeout_seconds=0.6,
            connect_timeout_seconds=0.5,
        )
        for domain, srv in cluster.items()
    ]
    manager = MCPClientManager(configs)
    registry = ToolRegistry()

    try:
        async with manager:
            discovered_tools = await manager.discover_and_register(registry)
            customer_snapshot = registry.build_role_snapshot("customer")
            merchant_snapshot = registry.build_role_snapshot("merchant")
            yield {
                "cluster": cluster,
                "manager": manager,
                "registry": registry,
                "discovered_tools": discovered_tools,
                "customer_snapshot": customer_snapshot,
                "merchant_snapshot": merchant_snapshot,
            }
    finally:
        await manager.aclose()
        await asyncio.gather(
            *(srv.terminate_cleanly() for srv in cluster.values()),
            return_exceptions=True,
        )


CUSTOMER_FULL_PERMS = {
    "pim:catalog:read",
    "pim:stock:read",
    "oms:orders:read",
    "oms:orders:update_address",
    "oms:refund:create",
    "logistics:package:read",
}

MERCHANT_FULL_PERMS = CUSTOMER_FULL_PERMS | {
    "pim:sku:propose",
    "bi:schema:read",
    "bi:sql:query",
    "bi:chart:render",
    "market_erp:trends:read",
    "market_erp:cost:calculate",
}


# ============================================================================
# 1. 跨进程动态发现全部 5 域工具与 Resource、Schema 红线核验与按角色裁剪 Snapshot
# ============================================================================


async def test_cross_process_discovery_resources_and_role_snapshots(
    mcp_cluster_env: dict[str, Any],
) -> None:
    cluster: dict[str, SubprocessMCPServer] = mcp_cluster_env["cluster"]
    manager: MCPClientManager = mcp_cluster_env["manager"]
    registry: ToolRegistry = mcp_cluster_env["registry"]
    customer_snapshot = mcp_cluster_env["customer_snapshot"]
    merchant_snapshot = mcp_cluster_env["merchant_snapshot"]

    # 确认 5 个 MCP Server 均为独立存活的操作系统进程
    pids = {srv.process.pid for srv in cluster.values()}
    assert len(pids) == 5
    assert all(srv.process.returncode is None for srv in cluster.values())

    expected_all_tools = {
        "pim__hybrid_search",
        "pim__compare_skus",
        "pim__check_stock",
        "pim__draft_sku_proposal",
        "oms__search_orders",
        "oms__update_address",
        "oms__create_refund",
        "logistics__track_pkg",
        "bi__get_table_schema",
        "bi__run_readonly_sql",
        "bi__render_chart",
        "market_erp__search_trends",
        "market_erp__calc_landed_cost",
    }
    assert set(registry.list_tool_names()) == expected_all_tools
    assert registry.registered_count == 13

    # 验证 C 端与 B 端按角色裁剪后的专属 ToolSnapshot
    expected_customer_tools = {
        "pim__hybrid_search",
        "pim__compare_skus",
        "pim__check_stock",
        "oms__search_orders",
        "oms__update_address",
        "oms__create_refund",
        "logistics__track_pkg",
    }
    assert set(customer_snapshot.allowed_tools) == expected_customer_tools
    assert "pim__draft_sku_proposal" not in customer_snapshot
    assert "bi__run_readonly_sql" not in customer_snapshot
    assert "market_erp__calc_landed_cost" not in customer_snapshot
    assert set(merchant_snapshot.allowed_tools) == expected_all_tools

    # 核验模型可见 LLM JSON Schema 绝不暴露任何禁止的可信上下文字段，且全部声明 additionalProperties=False
    for llm_schema in merchant_snapshot.to_llm_schemas():
        props = llm_schema["input_schema"].get("properties", {})
        assert set(props.keys()).isdisjoint(FORBIDDEN_INPUT_FIELDS)
        assert llm_schema["input_schema"].get("additionalProperties") is False

    # 核验关键工具风险分级准确性
    assert merchant_snapshot.resolve("pim__draft_sku_proposal").risk == "high"
    assert merchant_snapshot.resolve("oms__update_address").risk == "medium"
    assert merchant_snapshot.resolve("oms__create_refund").risk == "high"
    assert merchant_snapshot.resolve("bi__run_readonly_sql").risk == "medium"
    assert merchant_snapshot.resolve("pim__hybrid_search").risk == "low"

    # 核验若远端违规暴露 user_id 等禁止字段，json_schema_to_pydantic_model 立即阻断
    with pytest.raises(ToolRegistrationError, match="user_id"):
        json_schema_to_pydantic_model(
            {
                "type": "object",
                "properties": {"keyword": {"type": "string"}, "user_id": {"type": "string"}},
            },
            model_name="MaliciousRemoteInput",
            is_input=True,
            tool_name="pim__malicious_tool",
        )

    # 验证动态发现并跨进程读取各域知识库 / 规则文档 Resource
    discovered_res = manager.discovered_resources
    assert set(discovered_res.keys()) == {"pim", "oms", "logistics", "bi", "market_erp"}

    pim_kb = await manager.read_resource("pim://knowledge/selling-points")
    assert "商品卖点知识库" in pim_kb.text
    assert "桑蚕丝" in pim_kb.text

    oms_doc = await manager.read_resource("oms://docs/order-state-machine")
    assert "ORDER_STATUS_DISALLOWS_ADDRESS_UPDATE" in oms_doc.text

    bi_dict = await manager.read_resource("bi://dictionary/metrics")
    assert "dt" in bi_dict.text and "gmv" in bi_dict.text

    erp_policy = await manager.read_resource("market_erp://policies/return-exchange")
    assert "七天无理由" in erp_policy.text and "15%" in erp_policy.text


# ============================================================================
# 2a. C 端买家跨进程全链路：商品检索/比价/库存、查单脱敏、改地址及租户/买家边界隔离
# ============================================================================


async def test_customer_cross_process_pim_oms_logistics_and_isolation(
    mcp_cluster_env: dict[str, Any],
) -> None:
    manager: MCPClientManager = mcp_cluster_env["manager"]
    runtime = ToolRuntime(
        snapshot=mcp_cluster_env["customer_snapshot"],
        hooks=[manager.create_runtime_hook()],
    )
    buyer_1001_ctx = ExecutionContext(
        trace_id="trace_mcp_c_buyer_1001",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_1001",
        role="customer",
        permissions=CUSTOMER_FULL_PERMS,
    )

    # 1) PIM 混合检索 + 女装尺码/颜色/面料字段 + 内部底价字段剔除验证
    search_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_pim_search_01",
            name="pim__hybrid_search",
            arguments={"category": "衬衫", "max_price": 500.0},
        ),
        buyer_1001_ctx,
    )
    assert search_res.tool_call_id == "tc_pim_search_01"
    assert search_res.status == "success"
    assert search_res.data is not None
    assert search_res.data["total_matched"] == 2
    first_sku = search_res.data["items"][0]
    assert first_sku["sku_id"] == "SKU-SHIRT-ACETATE-01"
    assert first_sku["sizes"] == "S / M / L / XL"
    assert first_sku["color"] == "珍珠白"
    assert "三醋酸" in first_sku["fabric"]
    assert "_internal_supplier_cost" not in first_sku
    assert "_internal_warehouse_bin" not in first_sku

    # 2) PIM 多款女装 SKU 横向比价（含尺码、颜色、面料 specs）
    compare_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_pim_compare_01",
            name="pim__compare_skus",
            arguments={"sku_ids": ["SKU-SHIRT-ACETATE-01", "SKU-SHIRT-COTTON-02"]},
        ),
        buyer_1001_ctx,
    )
    assert compare_res.status == "success"
    assert compare_res.data is not None
    assert compare_res.data["compared_count"] == 2
    assert compare_res.data["min_price"] == 189.0
    assert compare_res.data["max_price"] == 399.0
    assert compare_res.data["entries"][0]["specs"]["sizes"] == "S / M / L / XL"

    # 3) PIM 库存核验：库存充足成功 vs 库存不足返回 business_error
    stock_ok = await runtime.execute(
        ToolCall(
            tool_call_id="tc_pim_stock_ok",
            name="pim__check_stock",
            arguments={"sku_id": "SKU-DRESS-SILK-01", "required_qty": 2},
        ),
        buyer_1001_ctx,
    )
    assert stock_ok.status == "success"
    assert stock_ok.data is not None
    assert stock_ok.data["stock_status"] == "in_stock"

    stock_shortage = await runtime.execute(
        ToolCall(
            tool_call_id="tc_pim_stock_shortage",
            name="pim__check_stock",
            arguments={"sku_id": "SKU-SHIRT-COTTON-02", "required_qty": 10},
        ),
        buyer_1001_ctx,
    )
    assert stock_shortage.status == "error"
    assert stock_shortage.error is not None
    assert stock_shortage.error.category == "business_error"
    assert stock_shortage.error.code == "INSUFFICIENT_STOCK"

    # 4) OMS 买家查单：严格只返回 buyer_1001 本人订单（隔离同店 buyer_1002 与跨租户 buyer_2001），且自动脱敏
    orders_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_oms_search_buyer1001",
            name="oms__search_orders",
            arguments={"created_from": "2026-11-01", "limit": 20},
        ),
        buyer_1001_ctx,
    )
    assert orders_res.status == "success"
    assert orders_res.data is not None
    returned_order_ids = {item["order_id"] for item in orders_res.data["orders"]}
    assert returned_order_ids == {
        "ORD-20261118-2101",
        "ORD-20261115-2102",
        "ORD-20261101-2104",
    }
    assert "ORD-20261110-2103" not in returned_order_ids  # 同租户另一买家订单被隔离
    assert "ORD-20261119-3001" not in returned_order_ids  # 跨租户订单被隔离

    sample_order = orders_res.data["orders"][0]
    assert "_internal_channel_fee" not in sample_order
    assert sample_order["buyer_phone"] == "138****5678"
    assert sample_order["buyer_name"] == "林**"
    assert sample_order["buyer_email"] == "l***@example.com"
    assert sample_order["shipping_address"] == "广东省深圳市****"

    # 5) OMS 修改收货地址：paid 未发货订单成功 vs shipped 已发货订单返回 business_error vs 越权改他人订单返回 permission_error
    addr_ok = await runtime.execute(
        ToolCall(
            tool_call_id="tc_oms_addr_ok",
            name="oms__update_address",
            arguments={
                "order_id": "ORD-20261118-2101",
                "new_shipping_address": "广东省深圳市福田区深南大道2008号皇庭广场",
                "receiver_name": "林晓月",
                "receiver_phone": "13566778899",
            },
        ),
        buyer_1001_ctx,
    )
    assert addr_ok.status == "success"
    assert addr_ok.data is not None
    assert addr_ok.data["receiver_phone"] == "135****8899"
    assert addr_ok.data["shipping_address"] == "广东省深圳市****"

    addr_shipped_err = await runtime.execute(
        ToolCall(
            tool_call_id="tc_oms_addr_shipped",
            name="oms__update_address",
            arguments={
                "order_id": "ORD-20261115-2102",
                "new_shipping_address": "北京市朝阳区建国路88号",
                "receiver_name": "林晓月",
                "receiver_phone": "13812345678",
            },
        ),
        buyer_1001_ctx,
    )
    assert addr_shipped_err.status == "error"
    assert addr_shipped_err.error is not None
    assert addr_shipped_err.error.category == "business_error"
    assert addr_shipped_err.error.code == "ORDER_STATUS_DISALLOWS_ADDRESS_UPDATE"

    addr_other_buyer_err = await runtime.execute(
        ToolCall(
            tool_call_id="tc_oms_addr_other_buyer",
            name="oms__update_address",
            arguments={
                "order_id": "ORD-20261110-2103",
                "new_shipping_address": "北京市海淀区学院路1号",
                "receiver_name": "攻击者",
                "receiver_phone": "13100009999",
            },
        ),
        buyer_1001_ctx,
    )
    assert addr_other_buyer_err.status == "error"
    assert addr_other_buyer_err.error is not None
    assert addr_other_buyer_err.error.category == "permission_error"
    assert addr_other_buyer_err.error.code == "ORDER_OWNERSHIP_DENIED"

    addr_other_tenant_err = await runtime.execute(
        ToolCall(
            tool_call_id="tc_oms_addr_other_tenant",
            name="oms__update_address",
            arguments={
                "order_id": "ORD-20261119-3001",
                "new_shipping_address": "北京市海淀区学院路1号",
                "receiver_name": "攻击者",
                "receiver_phone": "13100009999",
            },
        ),
        buyer_1001_ctx,
    )
    assert addr_other_tenant_err.status == "error"
    assert addr_other_tenant_err.error is not None
    assert addr_other_tenant_err.error.category == "permission_error"
    assert addr_other_tenant_err.error.code == "TENANT_ISOLATION_DENIED"

    # 6) Logistics 查询包裹轨迹：本人包裹成功且自由文本派件员手机号脱敏，越权查他人运单被拒
    track_ok = await runtime.execute(
        ToolCall(
            tool_call_id="tc_log_track_ok",
            name="logistics__track_pkg",
            arguments={"tracking_no": "SF2026882102"},
        ),
        buyer_1001_ctx,
    )
    assert track_ok.status == "success"
    assert track_ok.data is not None
    assert track_ok.data["receiver_phone"] == "138****5678"
    assert "136****7766" in track_ok.data["checkpoints"][-1]["status"]

    track_denied = await runtime.execute(
        ToolCall(
            tool_call_id="tc_log_track_denied",
            name="logistics__track_pkg",
            arguments={"tracking_no": "YT8833002103"},
        ),
        buyer_1001_ctx,
    )
    assert track_denied.status == "error"
    assert track_denied.error is not None
    assert track_denied.error.category == "permission_error"


# ============================================================================
# 2b. B 端商家跨进程全链路：BI 只读与时间分区 SQL 强拦截、租户隔离、图表与供应链测算
# ============================================================================


async def test_merchant_cross_process_bi_sql_guards_and_market_erp(
    mcp_cluster_env: dict[str, Any],
) -> None:
    manager: MCPClientManager = mcp_cluster_env["manager"]
    runtime = ToolRuntime(
        snapshot=mcp_cluster_env["merchant_snapshot"],
        hooks=[manager.create_runtime_hook()],
    )
    merchant_ctx = ExecutionContext(
        trace_id="trace_mcp_b_merchant_01",
        tenant_id="tenant_yunshang_cn",
        user_id="merchant_ops_director",
        role="merchant",
        permissions=MERCHANT_FULL_PERMS,
    )

    # 1) BI 获取数仓表结构
    schema_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_bi_schema",
            name="bi__get_table_schema",
            arguments={},
        ),
        merchant_ctx,
    )
    assert schema_res.status == "success"
    assert schema_res.data is not None
    table_names = {t["table_name"] for t in schema_res.data["tables"]}
    assert table_names == {"dwd_order_daily_metrics", "dwd_aftersales_return_daily"}

    # 2) BI SQL 安全拦截 1：拒绝 DDL / 写操作
    ddl_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_bi_sql_drop",
            name="bi__run_readonly_sql",
            arguments={
                "sql": "DROP TABLE dwd_order_daily_metrics WHERE dt = '2026-11-20'"
            },
        ),
        merchant_ctx,
    )
    assert ddl_res.status == "error"
    assert ddl_res.error is not None
    assert ddl_res.error.category == "business_error"
    assert ddl_res.error.code == "READONLY_SQL_VIOLATION"

    # 3) BI SQL 安全拦截 2：拒绝分号多语句拼装绕过
    multi_stmt_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_bi_sql_multistmt",
            name="bi__run_readonly_sql",
            arguments={
                "sql": "SELECT category FROM dwd_order_daily_metrics WHERE dt = '2026-11-20'; DELETE FROM dwd_order_daily_metrics"
            },
        ),
        merchant_ctx,
    )
    assert multi_stmt_res.status == "error"
    assert multi_stmt_res.error is not None
    assert multi_stmt_res.error.category == "business_error"
    assert multi_stmt_res.error.code == "READONLY_SQL_VIOLATION"

    # 4) BI SQL 安全拦截 3：拒绝缺少时间分区 dt 条件的无分区扫描
    no_partition_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_bi_sql_no_dt",
            name="bi__run_readonly_sql",
            arguments={
                "sql": "SELECT category, SUM(gmv) AS total_gmv FROM dwd_order_daily_metrics GROUP BY category"
            },
        ),
        merchant_ctx,
    )
    assert no_partition_res.status == "error"
    assert no_partition_res.error is not None
    assert no_partition_res.error.category == "business_error"
    assert no_partition_res.error.code == "MISSING_TIME_PARTITION_FILTER"

    # 5) BI 合规只读分区 SQL 查询：成功返回女装聚合指标，且严格物理隔离 tenant_other_shop 的千万级 GMV 行
    valid_sql_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_bi_sql_valid",
            name="bi__run_readonly_sql",
            arguments={
                "sql": (
                    "SELECT category, ROUND(SUM(gmv), 2) AS total_gmv, "
                    "ROUND(AVG(return_rate), 4) AS avg_return_rate "
                    "FROM dwd_order_daily_metrics "
                    "WHERE dt BETWEEN '2026-11-18' AND '2026-11-20' "
                    "GROUP BY category ORDER BY total_gmv DESC"
                )
            },
        ),
        merchant_ctx,
    )
    assert valid_sql_res.status == "success"
    assert valid_sql_res.data is not None
    rows_by_cat = {r["category"]: r for r in valid_sql_res.data["rows"]}
    assert set(rows_by_cat.keys()) == {"连衣裙", "衬衫", "裤装"}
    # 若跨租户泄露，连衣裙 GMV 会超过 10,000,000；此处精确等于 tenant_yunshang_cn 三日之和 312357.0
    assert rows_by_cat["连衣裙"]["total_gmv"] == 312357.0
    assert rows_by_cat["连衣裙"]["avg_return_rate"] > 0.25

    # 6) BI 图表渲染配置生成
    chart_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_bi_chart",
            name="bi__render_chart",
            arguments={
                "chart_type": "bar",
                "title": "女装各品类近三日成交额对比",
                "x_axis": list(rows_by_cat.keys()),
                "series_name": "GMV(元)",
                "series_values": [float(r["total_gmv"]) for r in rows_by_cat.values()],
            },
        ),
        merchant_ctx,
    )
    assert chart_res.status == "success"
    assert chart_res.data is not None
    assert chart_res.data["point_count"] == 3
    assert chart_res.data["echarts_option"]["series"][0]["type"] == "bar"

    # 7) Market & ERP 趋势检索与到岸成本测算（含低于 MOQ 业务拦截）
    trends_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_erp_trends",
            name="market_erp__search_trends",
            arguments={"category": "连衣裙"},
        ),
        merchant_ctx,
    )
    assert trends_res.status == "success"
    assert trends_res.data is not None
    assert trends_res.data["underserved_price_bands"][0]["min_price"] == 329.0

    moq_err_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_erp_cost_moq_err",
            name="market_erp__calc_landed_cost",
            arguments={
                "sku_name": "云裳莱赛尔三醋酸防透连衣裙升级款",
                "fob_unit_cost": 145.0,
                "unit_weight_kg": 0.48,
                "order_quantity": 50,  # sea_freight 要求最低 200 件
                "shipping_mode": "sea_freight",
                "target_retail_price": 429.0,
            },
        ),
        merchant_ctx,
    )
    assert moq_err_res.status == "error"
    assert moq_err_res.error is not None
    assert moq_err_res.error.category == "business_error"
    assert moq_err_res.error.code == "MOQ_NOT_MET"

    cost_ok_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_erp_cost_ok",
            name="market_erp__calc_landed_cost",
            arguments={
                "sku_name": "云裳莱赛尔三醋酸防透连衣裙升级款",
                "fob_unit_cost": 145.0,
                "unit_weight_kg": 0.48,
                "order_quantity": 300,
                "shipping_mode": "domestic_truck",
                "target_retail_price": 429.0,
            },
        ),
        merchant_ctx,
    )
    assert cost_ok_res.status == "success"
    assert cost_ok_res.data is not None
    assert cost_ok_res.data["estimated_gross_margin"] > 0.50

    # 8) 商家视角查本店订单：可见本店 buyer_1001 与 buyer_1002 订单，仍严格不可见跨租户订单
    merchant_orders = await runtime.execute(
        ToolCall(
            tool_call_id="tc_oms_merchant_view",
            name="oms__search_orders",
            arguments={"created_from": "2026-11-01", "limit": 50},
        ),
        merchant_ctx,
    )
    assert merchant_orders.status == "success"
    assert merchant_orders.data is not None
    m_order_ids = {item["order_id"] for item in merchant_orders.data["orders"]}
    assert "ORD-20261110-2103" in m_order_ids  # 本店另一买家订单对本店商家可见
    assert "ORD-20261119-3001" not in m_order_ids  # 跨租户订单依旧不可见


# ============================================================================
# 2c. 跨进程高风险 HITL 挂起/恢复（create_refund + draft_sku_proposal）与并发保序对齐
# ============================================================================


async def test_high_risk_hitl_cross_process_and_concurrent_batch_alignment(
    mcp_cluster_env: dict[str, Any],
) -> None:
    cluster: dict[str, SubprocessMCPServer] = mcp_cluster_env["cluster"]
    manager: MCPClientManager = mcp_cluster_env["manager"]
    runtime = ToolRuntime(
        snapshot=mcp_cluster_env["merchant_snapshot"],
        hooks=[manager.create_runtime_hook()],
    )

    merchant_ctx = ExecutionContext(
        trace_id="trace_mcp_hitl_and_batch",
        tenant_id="tenant_yunshang_cn",
        user_id="merchant_general_mgr",
        role="merchant",
        permissions=MERCHANT_FULL_PERMS,
    )

    # --- A. oms__create_refund 高风险 HITL 挂起 -> 审批后跨进程执行成功 ---
    refund_call = ToolCall(
        tool_call_id="tc_hitl_refund_01",
        name="oms__create_refund",
        arguments={
            "order_id": "ORD-20261115-2102",
            "refund_amount": 599.0,
            "reason": "客户七天无理由试穿退货吊牌完好已验货入库",
        },
    )
    refund_suspended = await runtime.execute(refund_call, merchant_ctx)
    assert refund_suspended.tool_call_id == "tc_hitl_refund_01"
    assert refund_suspended.status == "hitl_required"
    assert isinstance(refund_suspended.hitl, HITLRequired)

    # 验证模型伪造 approved=True 参数直接被 Schema extra="forbid" 拦截为 validation_error
    forged_refund = await runtime.execute(
        ToolCall(
            tool_call_id="tc_hitl_refund_forged",
            name="oms__create_refund",
            arguments={
                "order_id": "ORD-20261115-2102",
                "refund_amount": 599.0,
                "reason": "伪造审批标志位",
                "approved": True,
            },
        ),
        merchant_ctx,
    )
    assert forged_refund.status == "error"
    assert forged_refund.error is not None
    assert forged_refund.error.category == "validation_error"

    # 注入网关签发的受信任审批凭证后跨进程真正落地退款
    approved_refund_ctx = merchant_ctx.with_hitl_approval(
        tool_name="oms__create_refund",
        tool_call_id=refund_call.tool_call_id,
        approver_id="finance_supervisor_01",
        arguments_digest=refund_suspended.hitl.arguments_digest,
    )
    refund_executed = await runtime.execute(refund_call, approved_refund_ctx)
    assert refund_executed.tool_call_id == "tc_hitl_refund_01"
    assert refund_executed.status == "success"
    assert refund_executed.data is not None
    assert refund_executed.data["order_status"] == "refunded"
    assert refund_executed.data["refund_amount"] == 599.0

    # --- B. pim__draft_sku_proposal 高风险 HITL 挂起 -> 审批后跨进程建档成功 ---
    proposal_call = ToolCall(
        tool_call_id="tc_hitl_proposal_01",
        name="pim__draft_sku_proposal",
        arguments={
            "sku_name": "云裳 60支莱赛尔三醋酸防透收腰连衣裙 2026春夏款",
            "category": "连衣裙",
            "target_retail_price": 429.0,
            "estimated_unit_cost": 174.63,
            "core_selling_points": [
                "高密双层桑蚕丝感防透全衬",
                "亚洲梨形立体放量腰臀剪裁",
                "莱赛尔天丝混纺三醋酸抗静电面料",
            ],
            "initial_order_qty": 300,
        },
    )
    proposal_suspended = await runtime.execute(proposal_call, merchant_ctx)
    assert proposal_suspended.status == "hitl_required"
    assert proposal_suspended.hitl is not None

    approved_proposal_ctx = merchant_ctx.with_hitl_approval(
        tool_name="pim__draft_sku_proposal",
        tool_call_id=proposal_call.tool_call_id,
        approver_id="merchandising_vp",
        arguments_digest=proposal_suspended.hitl.arguments_digest,
    )
    proposal_executed = await runtime.execute(proposal_call, approved_proposal_ctx)
    assert proposal_executed.tool_call_id == "tc_hitl_proposal_01"
    assert proposal_executed.status == "success"
    assert proposal_executed.data is not None
    assert proposal_executed.data["status"] == "draft_submitted"
    assert proposal_executed.data["proposal_id"].startswith("PROP-")

    # --- C. 跨多进程乱序并发 execute_batch() 保序对齐验证 ---
    # 给第 1 个调用所属的 pim 域临时注入 0.22s 延迟，确保后续零延迟的 oms/bi/market_erp 调用先返回
    async with httpx.AsyncClient() as admin_http:
        await admin_http.post(
            cluster["pim"].fault_url,
            json={"delay_seconds": 0.22, "target_tool": "check_stock"},
        )

    try:
        batch_calls = [
            ToolCall(
                tool_call_id="batch_01_slow_pim",
                name="pim__check_stock",
                arguments={"sku_id": "SKU-DRESS-SILK-01", "required_qty": 1},
            ),
            ToolCall(
                tool_call_id="batch_02_fast_oms",
                name="oms__search_orders",
                arguments={"order_id": "ORD-20261118-2101"},
            ),
            ToolCall(
                tool_call_id="batch_03_fast_bi",
                name="bi__get_table_schema",
                arguments={"table_name": "dwd_order_daily_metrics"},
            ),
            ToolCall(
                tool_call_id="batch_04_fast_erp",
                name="market_erp__search_trends",
                arguments={"category": "衬衫"},
            ),
        ]
        batch_results = await runtime.execute_batch(batch_calls, merchant_ctx)
    finally:
        async with httpx.AsyncClient() as admin_http:
            await admin_http.post(
                cluster["pim"].fault_url,
                json={"delay_seconds": 0.0, "target_tool": None},
            )

    assert [r.tool_call_id for r in batch_results] == [
        "batch_01_slow_pim",
        "batch_02_fast_oms",
        "batch_03_fast_bi",
        "batch_04_fast_erp",
    ]
    assert all(r.status == "success" for r in batch_results)
    assert batch_results[0].data["sku_id"] == "SKU-DRESS-SILK-01"
    assert batch_results[1].data["orders"][0]["order_id"] == "ORD-20261118-2101"
    assert batch_results[2].data["tables"][0]["table_name"] == "dwd_order_daily_metrics"
    assert batch_results[3].data["category"] == "衬衫"


# ============================================================================
# 3. 故障注入对抗测试：单域响应超时 + 单域子进程宕机/端口不可达，验证自愈诊断与故障隔离
# ============================================================================


async def test_fault_injection_timeout_and_server_crash_isolation(
    mcp_cluster_env: dict[str, Any],
) -> None:
    cluster: dict[str, SubprocessMCPServer] = mcp_cluster_env["cluster"]
    manager: MCPClientManager = mcp_cluster_env["manager"]
    runtime = ToolRuntime(
        snapshot=mcp_cluster_env["merchant_snapshot"],
        hooks=[manager.create_runtime_hook()],
    )
    merchant_ctx = ExecutionContext(
        trace_id="trace_mcp_fault_injection",
        tenant_id="tenant_yunshang_cn",
        user_id="merchant_sre_check",
        role="merchant",
        permissions=MERCHANT_FULL_PERMS,
    )

    # ------------------------------------------------------------------
    # 故障场景 A：模拟 logistics 独立域响应慢超时（delay=2.0s > client timeout=0.6s）
    # ------------------------------------------------------------------
    async with httpx.AsyncClient() as admin_http:
        resp = await admin_http.post(
            cluster["logistics"].fault_url,
            json={"delay_seconds": 2.0, "target_tool": "track_pkg"},
        )
        assert resp.status_code == 200

    timeout_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_fault_logistics_timeout",
            name="logistics__track_pkg",
            arguments={"tracking_no": "SF2026882102"},
        ),
        merchant_ctx,
    )
    assert timeout_res.tool_call_id == "tc_fault_logistics_timeout"
    assert timeout_res.status == "error"
    assert timeout_res.data is None
    assert timeout_res.error is not None
    assert timeout_res.error.category == "transport_error"
    assert timeout_res.error.error_type == "transport_error"
    assert timeout_res.error.code == "MCP_RPC_TIMEOUT"
    assert timeout_res.error.retryable is True
    assert timeout_res.error.details is not None
    assert timeout_res.error.details["domain"] == "logistics"
    assert timeout_res.error.details["failure_kind"] == "timeout"
    assert "self_healing_hint" in timeout_res.error.details

    # ------------------------------------------------------------------
    # 故障场景 B：直接终止 market_erp 独立子进程（模拟远端服务宕机 / 端口不可达）
    # ------------------------------------------------------------------
    await cluster["market_erp"].terminate_cleanly()
    assert cluster["market_erp"].process.returncode is not None

    crash_res = await runtime.execute(
        ToolCall(
            tool_call_id="tc_fault_erp_down",
            name="market_erp__search_trends",
            arguments={"category": "连衣裙"},
        ),
        merchant_ctx,
    )
    assert crash_res.tool_call_id == "tc_fault_erp_down"
    assert crash_res.status == "error"
    assert crash_res.data is None
    assert crash_res.error is not None
    assert crash_res.error.category == "transport_error"
    assert crash_res.error.error_type == "transport_error"
    assert crash_res.error.code == "MCP_SERVER_UNREACHABLE"
    assert crash_res.error.retryable is True
    assert crash_res.error.details is not None
    assert crash_res.error.details["domain"] == "market_erp"
    assert (
        crash_res.error.details["failure_kind"]
        == "connection_refused_or_server_down"
    )
    assert "self_healing_hint" in crash_res.error.details

    # ------------------------------------------------------------------
    # 故障隔离验证：在 logistics 超时 + market_erp 宕机的同时，混合并发调用其余健康域（pim/oms/bi）
    # ------------------------------------------------------------------
    mixed_batch = await runtime.execute_batch(
        [
            ToolCall(
                tool_call_id="mix_01_healthy_pim",
                name="pim__hybrid_search",
                arguments={"keyword": "云裳"},
            ),
            ToolCall(
                tool_call_id="mix_02_dead_erp",
                name="market_erp__search_trends",
                arguments={"category": "衬衫"},
            ),
            ToolCall(
                tool_call_id="mix_03_timeout_logistics",
                name="logistics__track_pkg",
                arguments={"tracking_no": "SF2026882102"},
            ),
            ToolCall(
                tool_call_id="mix_04_healthy_oms",
                name="oms__search_orders",
                arguments={"status": "paid"},
            ),
            ToolCall(
                tool_call_id="mix_05_healthy_bi",
                name="bi__run_readonly_sql",
                arguments={
                    "sql": "SELECT sku_id, gmv FROM dwd_order_daily_metrics WHERE dt = '2026-11-20'"
                },
            ),
        ],
        merchant_ctx,
    )

    assert [item.tool_call_id for item in mixed_batch] == [
        "mix_01_healthy_pim",
        "mix_02_dead_erp",
        "mix_03_timeout_logistics",
        "mix_04_healthy_oms",
        "mix_05_healthy_bi",
    ]
    assert mixed_batch[0].status == "success"
    assert mixed_batch[0].data["total_matched"] >= 3

    assert mixed_batch[1].status == "error"
    assert mixed_batch[1].error.category == "transport_error"
    assert mixed_batch[1].error.code == "MCP_SERVER_UNREACHABLE"

    assert mixed_batch[2].status == "error"
    assert mixed_batch[2].error.category == "transport_error"
    assert mixed_batch[2].error.code == "MCP_RPC_TIMEOUT"

    assert mixed_batch[3].status == "success"
    assert len(mixed_batch[3].data["orders"]) >= 1

    assert mixed_batch[4].status == "success", f"unexpected error: {mixed_batch[4].error}"
    assert mixed_batch[4].data["row_count"] >= 2
