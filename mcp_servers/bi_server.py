"""经营分析域独立 MCP Server（`bi`，B 端商家专属域）。

暴露能力：
- Tools：
  - `get_table_schema`（`risk="low"`）：获取按天分区（`dt`）经营分析数仓表结构与查询约束
  - `run_readonly_sql`（`risk="medium"`）：执行强制只读 + 强制时间分区（`dt`）过滤的租户隔离分析 SQL
  - `render_chart`（`risk="low"`）：将分析结果序列转化为标准图表声明式配置
- Resource：
  - `bi://dictionary/metrics`：经营分析核心指标口径字典与分区查询规范
"""

from __future__ import annotations

import re
import sqlite3
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from mcp_servers.mock_data import (
    BI_DAILY_METRICS_ROWS,
    BI_METRIC_DICTIONARY_MD,
    BI_RETURN_REASON_ROWS,
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

DOMAIN = "bi"
DEFAULT_PORT = 18104

_SQL_LINE_COMMENT_RE = re.compile(r"--[^\r\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_SQL_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
_DATE_LITERAL_RE = re.compile(r"'\d{4}-\d{2}-\d{2}'")

_FORBIDDEN_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "REPLACE",
        "MERGE",
        "GRANT",
        "REVOKE",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "VACUUM",
        "REINDEX",
        "EXEC",
        "EXECUTE",
        "INTO",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
    }
)

_PARTITION_PREDICATE_RE = re.compile(
    r"\b(?:[a-z_][a-z0-9_]*\.)?dt\s*(?:=|>=|>|<=|<|between\b|in\s*\()",
    re.IGNORECASE,
)

_ALLOWED_SQLITE_ACTIONS: frozenset[int] = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

TABLE_SCHEMAS_METADATA: dict[str, dict[str, Any]] = {
    "dwd_order_daily_metrics": {
        "table_name": "dwd_order_daily_metrics",
        "partition_column": "dt",
        "partition_format": "YYYY-MM-DD",
        "description": "店铺分品类、分SKU天级流量转化与退货毛利核心经营明细表（已按当前登录商家租户自动行级隔离）",
        "columns": [
            {"name": "dt", "type": "TEXT", "description": "天级时间分区键（必填过滤条件，YYYY-MM-DD）"},
            {"name": "category", "type": "TEXT", "description": "商品一级类目"},
            {"name": "sku_id", "type": "TEXT", "description": "标准 SKU 编码"},
            {"name": "price_band", "type": "TEXT", "description": "价格带区间标签（如 200-299、400-599）"},
            {"name": "uv", "type": "INTEGER", "description": "商品详情页独立访客数"},
            {"name": "paid_orders", "type": "INTEGER", "description": "支付子订单数"},
            {"name": "gmv", "type": "REAL", "description": "支付成交总额（元）"},
            {"name": "refund_orders", "type": "INTEGER", "description": "退货退款单量"},
            {"name": "return_rate", "type": "REAL", "description": "退货率（0~1）"},
            {"name": "conversion_rate", "type": "REAL", "description": "支付转化率（0~1）"},
            {"name": "gross_margin_rate", "type": "REAL", "description": "毛利率（0~1）"},
        ],
    },
    "dwd_aftersales_return_daily": {
        "table_name": "dwd_aftersales_return_daily",
        "partition_column": "dt",
        "partition_format": "YYYY-MM-DD",
        "description": "店铺天级退货原因归因统计明细表（已按当前登录商家租户自动行级隔离）",
        "columns": [
            {"name": "dt", "type": "TEXT", "description": "天级时间分区键（必填过滤条件，YYYY-MM-DD）"},
            {"name": "category", "type": "TEXT", "description": "商品一级类目"},
            {"name": "sku_id", "type": "TEXT", "description": "标准 SKU 编码"},
            {"name": "return_reason", "type": "TEXT", "description": "标准化退货原因标签"},
            {"name": "return_count", "type": "INTEGER", "description": "对应原因退货单数"},
            {"name": "refund_amount", "type": "REAL", "description": "对应原因退款总金额（元）"},
        ],
    },
}


class WireOutputModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ColumnSpecOutput(WireOutputModel):
    name: str
    type: str
    description: str


class TableSchemaItemOutput(WireOutputModel):
    table_name: str
    partition_column: str
    partition_format: str
    description: str
    columns: list[ColumnSpecOutput]


class GetTableSchemaOutput(WireOutputModel):
    tenant_id: str
    mandatory_partition_rule: str
    tables: list[TableSchemaItemOutput]


class RunReadonlySqlOutput(WireOutputModel):
    tenant_id: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


class RenderChartOutput(WireOutputModel):
    chart_type: str
    title: str
    point_count: int
    echarts_option: dict[str, Any]


def validate_readonly_partitioned_sql(raw_sql: str) -> str:
    """强制校验只读单语句 SQL 与 `dt` 时间分区裁剪谓词，阻断写操作、DDL、多语句绕过与全表扫描。"""
    if not raw_sql or not raw_sql.strip():
        raise_mcp_domain_error(
            error_type="validation_error",
            code="EMPTY_SQL_QUERY",
            message="SQL 查询语句不得为空",
        )

    if "/*" in raw_sql and "*/" not in raw_sql:
        raise_mcp_domain_error(
            error_type="business_error",
            code="READONLY_SQL_VIOLATION",
            message="检测到未闭合块注释，疑似 SQL 语法绕过，已拒绝执行",
        )

    no_comments = _SQL_BLOCK_COMMENT_RE.sub(" ", raw_sql)
    no_comments = _SQL_LINE_COMMENT_RE.sub(" ", no_comments).strip()

    if not no_comments:
        raise_mcp_domain_error(
            error_type="validation_error",
            code="EMPTY_SQL_QUERY",
            message="剔除注释后 SQL 语句为空",
        )

    # 仅允许末尾单个可选分号；移除字符串字面量后若仍存在分号一律判定为多语句注入绕过
    trimmed = no_comments[:-1].strip() if no_comments.endswith(";") else no_comments
    masked_literals = _SQL_STRING_LITERAL_RE.sub("''", trimmed)

    if ";" in masked_literals:
        raise_mcp_domain_error(
            error_type="business_error",
            code="READONLY_SQL_VIOLATION",
            message="安全策略拒绝多语句 SQL（检测到分号拼装绕过）；仅允许单条只读 SELECT 查询",
            details={
                "self_healing_hint": "请拆分为单条 SELECT 语句并移除多余分号及任何写操作语句。"
            },
        )

    word_tokens = [tok.upper() for tok in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", masked_literals)]
    if not word_tokens or word_tokens[0] not in ("SELECT", "WITH"):
        raise_mcp_domain_error(
            error_type="business_error",
            code="READONLY_SQL_VIOLATION",
            message=(
                f"只读数仓仅允许执行 SELECT 或 WITH ... SELECT 查询，"
                f"禁止执行 '{word_tokens[0] if word_tokens else 'UNKNOWN'}' 语句"
            ),
            details={
                "first_keyword": word_tokens[0] if word_tokens else None,
                "self_healing_hint": "请改写为以 SELECT 或 WITH 开头的只读聚合/明细查询。",
            },
        )

    violated_keywords = sorted({tok for tok in word_tokens if tok in _FORBIDDEN_SQL_KEYWORDS})
    if violated_keywords:
        raise_mcp_domain_error(
            error_type="business_error",
            code="READONLY_SQL_VIOLATION",
            message=(
                f"只读 SQL 安全守卫拦截到禁止的写操作/DDL/管理关键字: {', '.join(violated_keywords)}"
            ),
            details={
                "forbidden_keywords": violated_keywords,
                "self_healing_hint": "严禁任何写操作、建删表或 PRAGMA 指令，请仅保留纯 SELECT 查询逻辑。",
            },
        )

    if "SELECT" not in word_tokens:
        raise_mcp_domain_error(
            error_type="business_error",
            code="READONLY_SQL_VIOLATION",
            message="WITH 子句必须最终跟进只读 SELECT 查询",
        )

    # 强制时间分区校验：必须包含 WHERE 且对分区列 dt 指定明确的 YYYY-MM-DD 时间范围条件
    has_where = "WHERE" in word_tokens
    has_dt_predicate = bool(_PARTITION_PREDICATE_RE.search(masked_literals))
    has_date_literal = bool(_DATE_LITERAL_RE.search(trimmed))

    if not (has_where and has_dt_predicate and has_date_literal):
        raise_mcp_domain_error(
            error_type="business_error",
            code="MISSING_TIME_PARTITION_FILTER",
            message=(
                "数仓查询强制要求包含时间分区列 `dt` 过滤条件（格式 'YYYY-MM-DD'），"
                "禁止无时间分区全表扫描（示例：WHERE dt >= '2026-11-18' 或 dt BETWEEN '2026-11-18' AND '2026-11-20'）"
            ),
            details={
                "required_partition_column": "dt",
                "date_format": "YYYY-MM-DD",
                "self_healing_hint": "请在 SQL 的 WHERE 子句中补充 `dt >= '2026-11-18'` 等明确日期分区条件后重试。",
            },
        )

    return trimmed


def _readonly_sqlite_authorizer(
    action_code: int,
    _param1: str | None,
    _param2: str | None,
    _db_name: str | None,
    _trigger_name: str | None,
) -> int:
    """SQLite 内核级只读 Authorizer（第二道引擎级防线）。"""
    if action_code in _ALLOWED_SQLITE_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _build_tenant_sqlite_db(tenant_id: str) -> sqlite3.Connection:
    """为指定商家租户构建物理隔离的内存数仓连接，杜绝子查询跨租户越权泄露。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE dwd_order_daily_metrics (
            dt TEXT NOT NULL,
            category TEXT NOT NULL,
            sku_id TEXT NOT NULL,
            price_band TEXT NOT NULL,
            uv INTEGER NOT NULL,
            paid_orders INTEGER NOT NULL,
            gmv REAL NOT NULL,
            refund_orders INTEGER NOT NULL,
            return_rate REAL NOT NULL,
            conversion_rate REAL NOT NULL,
            gross_margin_rate REAL NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE dwd_aftersales_return_daily (
            dt TEXT NOT NULL,
            category TEXT NOT NULL,
            sku_id TEXT NOT NULL,
            return_reason TEXT NOT NULL,
            return_count INTEGER NOT NULL,
            refund_amount REAL NOT NULL
        )
        """
    )

    for row in BI_DAILY_METRICS_ROWS:
        if row["tenant_id"] != tenant_id:
            continue
        cur.execute(
            """
            INSERT INTO dwd_order_daily_metrics (
                dt, category, sku_id, price_band, uv, paid_orders,
                gmv, refund_orders, return_rate, conversion_rate, gross_margin_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["dt"],
                row["category"],
                row["sku_id"],
                row["price_band"],
                row["uv"],
                row["paid_orders"],
                row["gmv"],
                row["refund_orders"],
                row["return_rate"],
                row["conversion_rate"],
                row["gross_margin_rate"],
            ),
        )

    for row in BI_RETURN_REASON_ROWS:
        if row["tenant_id"] != tenant_id:
            continue
        cur.execute(
            """
            INSERT INTO dwd_aftersales_return_daily (
                dt, category, sku_id, return_reason, return_count, refund_amount
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["dt"],
                row["category"],
                row["sku_id"],
                row["return_reason"],
                row["return_count"],
                row["refund_amount"],
            ),
        )

    conn.commit()
    cur.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_readonly_sqlite_authorizer)
    return conn


def create_server(
    fault_state: FaultInjectionState | None = None,
) -> MCPServer[Any]:
    """创建隔离的经营分析域 `MCPServer` 实例。"""
    active_fault = fault_state or FaultInjectionState()
    tenant_db_cache: dict[str, sqlite3.Connection] = {}

    def get_tenant_conn(tenant_id: str) -> sqlite3.Connection:
        if tenant_id not in tenant_db_cache:
            tenant_db_cache[tenant_id] = _build_tenant_sqlite_db(tenant_id)
        return tenant_db_cache[tenant_id]

    server = MCPServer(
        name="bi-mcp-server",
        title="BI Operational Analytics MCP Server",
        description="B端商家专属经营分析数仓元数据、受限只读分区 SQL 执行与图表配置服务",
        version="1.0.0",
    )
    attach_standard_routes(server, domain=DOMAIN, fault_state=active_fault)

    @server.resource(
        "bi://dictionary/metrics",
        name="bi_metrics_dictionary",
        title="指标口径字典",
        description="经营分析 GMV、转化率、退货率、毛利率标准计算口径与 dt 时间分区强制规范",
        mime_type="text/markdown",
    )
    async def get_metrics_dictionary() -> str:
        return BI_METRIC_DICTIONARY_MD

    @server.tool(
        name="get_table_schema",
        description=(
            "查询经营分析数仓明细表结构、字段含义与必填时间分区键（dt）说明；"
            "仅限 B 端商家经营分析使用，禁止向 C 端买家开放，不用于直接查询明细行数据。"
        ),
        meta=build_tool_meta(
            permission="bi:schema:read",
            risk="low",
            allowed_roles={"merchant"},
        ),
    )
    async def get_table_schema(
        table_name: Annotated[
            str | None,
            Field(
                default=None,
                description="可选表名过滤（dwd_order_daily_metrics 或 dwd_aftersales_return_daily）；留空返回全部表结构",
            ),
        ] = None,
        *,
        ctx: Context[Any, Any],
    ) -> GetTableSchemaOutput:
        await maybe_apply_fault_delay(active_fault, "get_table_schema")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="bi:schema:read",
            allowed_roles={"merchant"},
        )

        if table_name is not None:
            cleaned = table_name.strip()
            if cleaned not in TABLE_SCHEMAS_METADATA:
                raise_mcp_domain_error(
                    error_type="business_error",
                    code="TABLE_NOT_FOUND",
                    message=f"数仓表 '{cleaned}' 不存在；可用表：{', '.join(TABLE_SCHEMAS_METADATA)}",
                    details={"available_tables": sorted(TABLE_SCHEMAS_METADATA.keys())},
                )
            tables = [TABLE_SCHEMAS_METADATA[cleaned]]
        else:
            tables = list(TABLE_SCHEMAS_METADATA.values())

        return GetTableSchemaOutput.model_validate(
            {
                "tenant_id": exec_ctx.tenant_id,
                "mandatory_partition_rule": "所有 SQL 查询必须在 WHERE 子句中包含 dt ('YYYY-MM-DD') 时间分区条件",
                "tables": tables,
            }
        )

    @server.tool(
        name="run_readonly_sql",
        description=(
            "在当前商家租户经营数仓上执行单条只读 SELECT 查询；"
            "强制要求 WHERE 子句包含 `dt`（YYYY-MM-DD）时间分区过滤条件，"
            "严禁执行任何 INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA 或分号多语句，仅限 B 端商家调用。"
        ),
        meta=build_tool_meta(
            permission="bi:sql:query",
            risk="medium",
            allowed_roles={"merchant"},
        ),
    )
    async def run_readonly_sql(
        sql: Annotated[
            str,
            Field(..., min_length=10, max_length=4000, description="带 dt 时间分区过滤条件的单条只读 SELECT SQL"),
        ],
        max_rows: Annotated[
            int,
            Field(default=100, ge=1, le=500, description="最大返回行数上限（1~500）"),
        ] = 100,
        *,
        ctx: Context[Any, Any],
    ) -> RunReadonlySqlOutput:
        await maybe_apply_fault_delay(active_fault, "run_readonly_sql")
        exec_ctx = authorize_domain_call(
            ctx,
            permission="bi:sql:query",
            allowed_roles={"merchant"},
        )

        validated_sql = validate_readonly_partitioned_sql(sql)
        conn = get_tenant_conn(exec_ctx.tenant_id)

        try:
            cursor = conn.execute(validated_sql)
            fetched = cursor.fetchmany(max_rows + 1)
            columns = [desc[0] for desc in (cursor.description or [])]
        except sqlite3.DatabaseError as exc:
            raise_mcp_domain_error(
                error_type="business_error",
                code="SQL_EXECUTION_REJECTED",
                message=f"只读数仓执行 SQL 失败: {exc}",
                details={"self_healing_hint": "请调用 bi__get_table_schema 核对表名与字段名后修正 SQL。"},
            )
            raise AssertionError("unreachable")

        truncated = len(fetched) > max_rows
        sliced_rows = [dict(row) for row in fetched[:max_rows]]
        return RunReadonlySqlOutput(
            tenant_id=exec_ctx.tenant_id,
            columns=columns,
            rows=sliced_rows,
            row_count=len(sliced_rows),
            truncated=truncated,
        )

    @server.tool(
        name="render_chart",
        description=(
            "将经营分析聚合指标序列渲染为前端标准化 ECharts 图表配置 JSON；"
            "仅限 B 端商家经营报表可视化使用，不用于执行数据库查询或修改指标数据。"
        ),
        meta=build_tool_meta(
            permission="bi:chart:render",
            risk="low",
            allowed_roles={"merchant"},
        ),
    )
    async def render_chart(
        chart_type: Annotated[
            Literal["line", "bar", "pie", "funnel"],
            Field(..., description="图表类型（line/bar/pie/funnel）"),
        ],
        title: Annotated[
            str,
            Field(..., min_length=2, max_length=100, description="图表主标题"),
        ],
        x_axis: Annotated[
            list[str],
            Field(..., min_length=1, max_length=60, description="横轴维度或类目名称列表"),
        ],
        series_name: Annotated[
            str,
            Field(..., min_length=1, max_length=50, description="指标序列名称"),
        ],
        series_values: Annotated[
            list[float],
            Field(..., min_length=1, max_length=60, description="与 x_axis 一一对应的指标数值列表"),
        ],
        *,
        ctx: Context[Any, Any],
    ) -> RenderChartOutput:
        await maybe_apply_fault_delay(active_fault, "render_chart")
        authorize_domain_call(
            ctx,
            permission="bi:chart:render",
            allowed_roles={"merchant"},
        )

        if len(x_axis) != len(series_values):
            raise_mcp_domain_error(
                error_type="validation_error",
                code="CHART_AXIS_LENGTH_MISMATCH",
                message=(
                    f"x_axis 长度（{len(x_axis)}）与 series_values 长度（{len(series_values)}）必须一致"
                ),
            )

        if chart_type in ("pie", "funnel"):
            option: dict[str, Any] = {
                "title": {"text": title},
                "tooltip": {"trigger": "item"},
                "series": [
                    {
                        "name": series_name,
                        "type": chart_type,
                        "data": [
                            {"name": label, "value": val}
                            for label, val in zip(x_axis, series_values, strict=True)
                        ],
                    }
                ],
            }
        else:
            option = {
                "title": {"text": title},
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": x_axis},
                "yAxis": {"type": "value"},
                "series": [
                    {
                        "name": series_name,
                        "type": chart_type,
                        "data": series_values,
                    }
                ],
            }

        return RenderChartOutput(
            chart_type=chart_type,
            title=title,
            point_count=len(x_axis),
            echarts_option=option,
        )

    return server


def create_app(host: str = "127.0.0.1", path: str = "/mcp") -> Starlette:
    """构建 BI 域 Streamable HTTP Starlette ASGI 应用。"""
    server = create_server()
    return server.streamable_http_app(host=host, streamable_http_path=path)


def main(argv: list[str] | None = None) -> None:
    """独立进程 CLI 启动入口：`python -m mcp_servers.bi_server`。"""
    run_mcp_http_cli(
        create_app_fn=create_app,
        default_port=DEFAULT_PORT,
        description="BI Operational Analytics Domain MCP Server (Streamable HTTP)",
        argv=argv,
    )


if __name__ == "__main__":
    main()
