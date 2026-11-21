"""网关/MCP 进程解耦、独立 MCP CLI 与 `infra/`（Checkpointer / AuditStore / Observability）测试套件。"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver

from config.settings import AppSettings
from gateway.main import create_gateway_app
from infra.audit_store import AuditStore, InMemoryAuditStore, PostgresAuditStore
from infra.checkpointer import (
    checkpointer_lifespan,
    create_default_checkpointer,
    normalize_postgres_dsn,
)
from infra.local_mcp_cluster import DOMAIN_SERVER_MODULES, ManagedMCPCluster
from infra.observability import (
    bind_observability_context,
    build_langfuse_callback_handler,
    configure_structured_logging,
    create_langfuse_tool_hook,
    is_langfuse_enabled,
    log_structured_event,
)
from schemas.context import ExecutionContext
from schemas.contracts import AuditLogEntry

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 辅助：兼容 psycopg AsyncConnectionPool 协议的轻量 SQLite 异步适配器（用于离线验证真实 SQL 读写与隔离）
# ============================================================================


class _SQLiteFakeAsyncCursor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> _SQLiteFakeAsyncCursor:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        # 将 PostgreSQL DDL 与 %s 占位符转为 SQLite 等价语法以真实执行 SQL 过滤逻辑
        normalized_sql = (
            sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            .replace("TIMESTAMPTZ", "TEXT")
            .replace("DOUBLE PRECISION", "REAL")
            .replace("%s", "?")
        )
        normalized_params = tuple(
            p.isoformat() if isinstance(p, datetime) else (int(p) if isinstance(p, bool) else p)
            for p in params
        )
        cur = self._conn.execute(normalized_sql, normalized_params)
        if normalized_sql.lstrip().upper().startswith("SELECT"):
            self._rows = list(cur.fetchall())

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _SQLiteFakeAsyncConn:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def cursor(self) -> _SQLiteFakeAsyncCursor:
        return _SQLiteFakeAsyncCursor(self._conn)

    async def commit(self) -> None:
        self._conn.commit()


class _SQLiteBackedAsyncPool:
    def __init__(self) -> None:
        self.sqlite_conn = sqlite3.connect(":memory:")
        self.closed = False

    @asynccontextmanager
    async def connection(self):
        yield _SQLiteFakeAsyncConn(self.sqlite_conn)

    async def close(self) -> None:
        self.closed = True
        self.sqlite_conn.close()


# ============================================================================
# 1. 验证 5 域独立 CLI、`GET /health` 与 `MCP_SPAWN_LOCAL_SUBPROCESS=False` 彻底零子进程拉起
# ============================================================================


async def test_mcp_standalone_cli_health_and_gateway_subprocess_decoupling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1) 验证全部 5 个 MCP Server 均可作为独立 CLI 模块执行 (`python -m mcp_servers.<domain>_server --help`)
    procs = await asyncio.gather(
        *(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                mod_name,
                "--help",
                cwd=str(WORKSPACE_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            for mod_name in DOMAIN_SERVER_MODULES.values()
        )
    )
    for domain, proc in zip(DOMAIN_SERVER_MODULES.keys(), procs, strict=True):
        out, err = await proc.communicate()
        assert proc.returncode == 0, f"{domain} CLI --help 执行失败: {err.decode()}"
        help_text = out.decode("utf-8")
        assert "--host" in help_text and "--port" in help_text and "--path" in help_text

    # 2) 先在外部独立启动 5 域 MCP 服务，再将 ManagedMCPCluster.start 打桩为禁止调用，
    #    严格证明网关在 MCP_SPAWN_LOCAL_SUBPROCESS=False 时绝不拉起任何本地子进程
    external_cluster = ManagedMCPCluster()
    urls = await external_cluster.start()

    async def _forbid_gateway_spawning_cluster(self: ManagedMCPCluster, **kwargs: Any) -> dict[str, str]:
        pytest.fail("当 MCP_SPAWN_LOCAL_SUBPROCESS=False 时，网关严禁启动任何本地 MCP 子进程！")

    monkeypatch.setattr(ManagedMCPCluster, "start", _forbid_gateway_spawning_cluster)

    custom_audit_store = InMemoryAuditStore()
    prod_settings = AppSettings(
        _env_file=None,
        APP_ENV="prod",
        MCP_SPAWN_LOCAL_SUBPROCESS=False,
        JWT_SECRET="decoupled-gateway-hs256-secret-key-2026-prod",
        MCP_PIM_URL=urls["pim"],
        MCP_OMS_URL=urls["oms"],
        MCP_LOGISTICS_URL=urls["logistics"],
        MCP_BI_URL=urls["bi"],
        MCP_MARKET_ERP_URL=urls["market_erp"],
    )

    try:
        gw_app = create_gateway_app(
            app_settings=prod_settings,
            audit_store=custom_audit_store,
        )
        async with gw_app.router.lifespan_context(gw_app):
            container = gw_app.state.container
            assert container.mcp_cluster is None

            # 验证 MCPClientManager 对 5 个远程独立服务的 /health 探针全部返回健康
            domain_health = await container.mcp_manager.check_all_health()
            assert set(domain_health.keys()) == {"pim", "oms", "logistics", "bi", "market_erp"}
            for dom, status_info in domain_health.items():
                assert status_info["healthy"] is True, f"{dom} /health 探针异常: {status_info}"
                assert status_info["payload"]["domain"] == dom

            async with AsyncClient(
                transport=ASGITransport(app=gw_app),
                base_url="http://testserver",
            ) as client:
                health_resp = await client.get("/health")
                assert health_resp.status_code == 200
                h_json = health_resp.json()
                assert h_json["spawn_local_subprocess"] is False
                assert h_json["registered_tools_count"] == 13
                assert h_json["checkpointer_backend"] == "memory:MemorySaver"
                assert h_json["audit_backend"] == "injected:InMemoryAuditStore"

                # 端到端走一轮真实对话并校验注入的 AuditStore 已完成落库与查询
                login_resp = await client.post(
                    "/api/auth/login",
                    json={"account_id": "customer_01"},
                )
                token = login_resp.json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

                async with client.stream(
                    "POST",
                    "/api/chat/stream",
                    headers=headers,
                    json={"message": "帮我推荐店里的连衣裙并看看库存"},
                ) as sse_resp:
                    assert sse_resp.status_code == 200
                    sse_text = (await sse_resp.aread()).decode("utf-8")
                    assert "event: final_answer" in sse_text

                audit_resp = await client.get("/api/audit/logs", headers=headers)
                assert audit_resp.status_code == 200
                logs_body = audit_resp.json()
                assert logs_body["total"] >= 2
                assert len(custom_audit_store.records) >= 2
    finally:
        await external_cluster.stop()


# ============================================================================
# 2. 验证 AuditStore（InMemoryAuditStore & PostgresAuditStore）读写与按租户/角色隔离查询
# ============================================================================


async def _verify_audit_store_contract(store: AuditStore) -> None:
    await store.initialize()
    base_ts = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)

    entries = [
        # 1) 租户 yunshang - 顾客 buyer_1001
        (
            AuditLogEntry(
                timestamp=base_ts,
                trace_id="trc_cust_01",
                tool_call_id="tc_01",
                tenant_id="tenant_yunshang_cn",
                user_id="buyer_1001",
                role="customer",
                tool_name="oms__search_orders",
                risk="low",
                status="success",
                duration_ms=12.4,
            ),
            None,
            None,
        ),
        # 2) 租户 yunshang - 同店另一顾客 buyer_1002
        (
            AuditLogEntry(
                timestamp=base_ts + timedelta(seconds=1),
                trace_id="trc_cust_02",
                tool_call_id="tc_02",
                tenant_id="tenant_yunshang_cn",
                user_id="buyer_1002",
                role="customer",
                tool_name="logistics__track_pkg",
                risk="low",
                status="success",
                duration_ms=18.2,
            ),
            None,
            None,
        ),
        # 3) 租户 yunshang - 商家高危建档触发 HITL 拦截
        (
            AuditLogEntry(
                timestamp=base_ts + timedelta(seconds=2),
                trace_id="trc_merch_01",
                tool_call_id="tc_03",
                tenant_id="tenant_yunshang_cn",
                user_id="merchant_ops_01",
                role="merchant",
                tool_name="pim__draft_sku_proposal",
                risk="high",
                status="hitl_required",
                duration_ms=3.5,
            ),
            "HITL_APPROVAL_REQUIRED",
            "高风险工具需人工审批",
        ),
        # 4) 外部租户 tenant_other_shop - 商家 SQL 查询
        (
            AuditLogEntry(
                timestamp=base_ts + timedelta(seconds=3),
                trace_id="trc_other_shop",
                tool_call_id="tc_04",
                tenant_id="tenant_other_shop",
                user_id="merchant_2001",
                role="merchant",
                tool_name="bi__run_readonly_sql",
                risk="medium",
                status="success",
                duration_ms=25.0,
            ),
            None,
            None,
        ),
    ]

    for entry, err_code, reason in entries:
        await store.append(entry, error_code=err_code, guard_reason=reason)

    ctx_buyer_1 = ExecutionContext(
        trace_id="trc_q1",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_1001",
        role="customer",
        permissions=frozenset({"oms:orders:read"}),
    )
    ctx_merchant_yunshang = ExecutionContext(
        trace_id="trc_q2",
        tenant_id="tenant_yunshang_cn",
        user_id="merchant_ops_01",
        role="merchant",
        permissions=frozenset({"pim:sku:propose"}),
    )
    ctx_merchant_other = ExecutionContext(
        trace_id="trc_q3",
        tenant_id="tenant_other_shop",
        user_id="merchant_2001",
        role="merchant",
        permissions=frozenset({"bi:sql:query"}),
    )

    # A. C 端顾客仅能看到本人记录（严禁看到同店 buyer_1002、商家记录或外部租户记录）
    buyer1_logs = await store.query(ctx_buyer_1)
    assert [r.tool_call_id for r in buyer1_logs] == ["tc_01"]
    assert buyer1_logs[0].domain == "oms"
    assert buyer1_logs[0].intercepted is False

    # B. B 端商家可查看本租户全部 3 条记录（按时间倒序），但绝对看不到外部租户 tc_04
    merch_logs = await store.query(ctx_merchant_yunshang)
    assert [r.tool_call_id for r in merch_logs] == ["tc_03", "tc_02", "tc_01"]
    assert merch_logs[0].intercepted is True
    assert merch_logs[0].error_code == "HITL_APPROVAL_REQUIRED"
    assert merch_logs[0].guard_reason == "高风险工具需人工审批"

    # C. 按状态与 trace_id 精准过滤
    hitl_only = await store.query(ctx_merchant_yunshang, status="hitl_required")
    assert [r.tool_call_id for r in hitl_only] == ["tc_03"]

    by_trace = await store.query(ctx_merchant_yunshang, trace_id="trc_cust_02")
    assert [r.tool_call_id for r in by_trace] == ["tc_02"]

    # D. 外部租户商家仅能看到本租户 tc_04
    other_logs = await store.query(ctx_merchant_other)
    assert [r.tool_call_id for r in other_logs] == ["tc_04"]


async def test_audit_store_in_memory_and_postgres_implementations() -> None:
    mem_store = InMemoryAuditStore()
    await _verify_audit_store_contract(mem_store)
    await mem_store.aclose()

    fake_pg_pool = _SQLiteBackedAsyncPool()
    pg_store = PostgresAuditStore(
        "postgresql+psycopg://yunshang:pwd@127.0.0.1:5432/yunshang_db",
        pool=fake_pg_pool,
    )
    await _verify_audit_store_contract(pg_store)
    await pg_store.aclose()
    assert fake_pg_pool.closed is False  # 外部注入连接池由外部持有生命周期

    with pytest.raises(ValueError):
        PostgresAuditStore(
            "postgresql://yunshang:pwd@127.0.0.1:5432/db",
            table_name="bad_table; DROP TABLE users;--",
        )


# ============================================================================
# 3. 验证 infra/checkpointer.py（PostgreSQL AsyncPostgresSaver 装配与 MemorySaver 回退）
# ============================================================================


async def test_checkpointer_lifespan_postgres_and_memory_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        normalize_postgres_dsn("postgresql+psycopg://u:p@db:5432/shop")
        == "postgresql://u:p@db:5432/shop"
    )
    assert normalize_postgres_dsn(None) is None
    assert normalize_postgres_dsn("sqlite:///local.db") is None

    # 1) 未配置 DATABASE_URL 时直接返回 MemorySaver
    default_saver = create_default_checkpointer(AppSettings(_env_file=None))
    assert isinstance(default_saver, MemorySaver)

    async with checkpointer_lifespan(AppSettings(_env_file=None)) as (saver, label):
        assert isinstance(saver, MemorySaver)
        assert label == "memory:MemorySaver"

    # 2) 配置了 DATABASE_URL 但驱动未安装或连接不可达时优雅降级回退至 MemorySaver
    pg_cfg = AppSettings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://user:pwd@127.0.0.1:5432/nonexistent_db",
    )
    async with checkpointer_lifespan(pg_cfg, allow_fallback=True) as (fb_saver, fb_label):
        assert isinstance(fb_saver, MemorySaver)
        assert fb_label == "memory:MemorySaver"

    # 3) 模拟安装并连通 AsyncPostgresSaver 时自动调用 setup() 并在退出时释放连接
    lifecycle_events: list[str] = []

    class _FakeAsyncPostgresSaver:
        def __init__(self, conn_str: str, serde: Any = None) -> None:
            self.conn_str = conn_str
            self.serde = serde

        @classmethod
        @asynccontextmanager
        async def from_conn_string(cls, conn_str: str, *, serde: Any = None):
            lifecycle_events.append(f"enter:{conn_str}")
            instance = cls(conn_str, serde=serde)
            try:
                yield instance
            finally:
                lifecycle_events.append("exit")

        async def setup(self) -> None:
            lifecycle_events.append("setup")

    monkeypatch.setattr(
        "infra.checkpointer._load_async_postgres_saver_cls",
        lambda: _FakeAsyncPostgresSaver,
    )
    async with checkpointer_lifespan(pg_cfg) as (active_pg_saver, pg_label):
        assert isinstance(active_pg_saver, _FakeAsyncPostgresSaver)
        assert pg_label == "postgres:AsyncPostgresSaver"
        assert active_pg_saver.serde is not None
        assert lifecycle_events == [
            "enter:postgresql://user:pwd@127.0.0.1:5432/nonexistent_db",
            "setup",
        ]
    assert lifecycle_events[-1] == "exit"


# ============================================================================
# 4. 验证 infra/observability.py 结构化 JSON 日志绑定与可选 Langfuse 静默开关
# ============================================================================


def test_structured_json_logging_context_binding_and_optional_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    test_logger = configure_structured_logging(
        level="INFO",
        logger_name="ecommerce_agent.test_obs",
        stream=stream,
        force_replace_handlers=True,
    )

    ctx = ExecutionContext(
        trace_id="trc_obs_9988",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_1001",
        role="customer",
        permissions=frozenset({"pim:catalog:read"}),
    )

    with bind_observability_context(ctx):
        test_logger.info("收到女装尺码对比请求")
        log_structured_event(
            test_logger,
            "sku_compare_completed",
            "已完成两款连衣裙对比",
            sku_count=2,
        )

    lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2

    first_json = json.loads(lines[0])
    assert first_json["trace_id"] == "trc_obs_9988"
    assert first_json["user_id"] == "buyer_1001"
    assert first_json["role"] == "customer"
    assert first_json["tenant_id"] == "tenant_yunshang_cn"
    assert first_json["message"] == "收到女装尺码对比请求"

    second_json = json.loads(lines[1])
    assert second_json["event"] == "sku_compare_completed"
    assert second_json["sku_count"] == 2
    assert second_json["trace_id"] == "trc_obs_9988"

    # 未配置 Langfuse Key 时静默返回 None，零报错
    no_lf_cfg = AppSettings(_env_file=None)
    assert is_langfuse_enabled(no_lf_cfg) is False
    assert build_langfuse_callback_handler(no_lf_cfg, context=ctx) is None
    assert create_langfuse_tool_hook(no_lf_cfg) is None

    # 配置了 Langfuse Key 但未安装 SDK 时同样零崩溃优雅返回 None
    lf_cfg = AppSettings(
        _env_file=None,
        LANGFUSE_PUBLIC_KEY="pk-lf-test-123",
        LANGFUSE_SECRET_KEY="sk-lf-test-456",
        LANGFUSE_HOST="https://langfuse.internal.example.com",
    )
    assert is_langfuse_enabled(lf_cfg) is True
    assert build_langfuse_callback_handler(lf_cfg, context=ctx) is None

    # 模拟安装 langfuse.langchain.CallbackHandler 后验证自动透传身份元数据
    captured_lf_init: dict[str, Any] = {}

    class _FakeLangfuseCallbackHandler:
        def __init__(self, **kwargs: Any) -> None:
            captured_lf_init.update(kwargs)

    fake_lf_pkg = ModuleType("langfuse")
    fake_lf_lc = ModuleType("langfuse.langchain")
    fake_lf_lc.CallbackHandler = _FakeLangfuseCallbackHandler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_lf_pkg)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_lf_lc)

    handler = build_langfuse_callback_handler(
        lf_cfg,
        context=ctx,
        session_id="thd_customer_001",
    )
    assert isinstance(handler, _FakeLangfuseCallbackHandler)
    assert captured_lf_init["public_key"] == "pk-lf-test-123"
    assert captured_lf_init["user_id"] == "buyer_1001"
    assert captured_lf_init["session_id"] == "thd_customer_001"
    assert captured_lf_init["metadata"]["trace_id"] == "trc_obs_9988"
    assert captured_lf_init["metadata"]["role"] == "customer"
