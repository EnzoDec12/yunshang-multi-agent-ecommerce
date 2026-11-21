"""结构化工具调用与风控拦截审计存储抽象层（`InMemoryAuditStore` + `PostgresAuditStore`）。

安全不变量：
1. 所有审计查询接口强制校验 `viewer_context`：
   - 物理租户隔离：仅可查询 `record.tenant_id == viewer_context.tenant_id` 的记录，严禁跨租户泄露；
   - 双域角色可见性隔离：`customer` 角色仅能读取本人（`record.user_id == viewer_context.user_id`）记录，
     `merchant` 角色可审计所属租户内的工具调用与风控拦截流水；
2. 生产配置 PostgreSQL `DATABASE_URL` 时采用异步连接池落库，未配置或本地测试时默认使用 `InMemoryAuditStore`。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from config.settings import AppSettings, get_settings
from infra.checkpointer import normalize_postgres_dsn
from infra.observability import get_structured_logger
from schemas.context import ExecutionContext
from schemas.contracts import (
    AuditLogEntry,
    ErrorType,
    RiskLevel,
    ToolExecutionStatus,
)

_SAFE_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AuditRecord(BaseModel):
    """面向持久化与网关审计接口的富结构化只读审计记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    trace_id: str = Field(..., min_length=1)
    tool_call_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    role: Literal["customer", "merchant"]
    tool_name: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    risk: RiskLevel | Literal["unknown"]
    status: ToolExecutionStatus
    intercepted: bool
    error_type: ErrorType | None = None
    error_code: str | None = None
    guard_reason: str | None = None
    duration_ms: float = Field(..., ge=0.0)

    @classmethod
    def from_entry(
        cls,
        entry: AuditLogEntry,
        *,
        error_code: str | None = None,
        guard_reason: str | None = None,
    ) -> AuditRecord:
        domain = (
            entry.tool_name.split("__", 1)[0]
            if "__" in entry.tool_name
            else "unknown"
        )
        return cls(
            timestamp=entry.timestamp,
            trace_id=entry.trace_id,
            tool_call_id=entry.tool_call_id,
            tenant_id=entry.tenant_id,
            user_id=entry.user_id,
            role=entry.role,
            tool_name=entry.tool_name,
            domain=domain,
            risk=entry.risk,
            status=entry.status,
            intercepted=entry.status in ("error", "hitl_required"),
            error_type=entry.error_type,
            error_code=error_code,
            guard_reason=guard_reason,
            duration_ms=entry.duration_ms,
        )

    def to_entry(self) -> AuditLogEntry:
        """还原为基础 `AuditLogEntry` 契约模型。"""
        return AuditLogEntry(
            timestamp=self.timestamp,
            trace_id=self.trace_id,
            tool_call_id=self.tool_call_id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            role=self.role,
            tool_name=self.tool_name,
            risk=self.risk,
            status=self.status,
            error_type=self.error_type,
            duration_ms=self.duration_ms,
        )


class AuditStore(ABC):
    """统一审计日志持久化与按角色隔离查询抽象基类。"""

    async def initialize(self) -> None:
        """初始化底层存储结构（如建表与索引）。"""

    @abstractmethod
    async def append(
        self,
        entry: AuditLogEntry,
        *,
        error_code: str | None = None,
        guard_reason: str | None = None,
    ) -> AuditRecord:
        """异步写入单条审计日志记录。"""

    @abstractmethod
    async def query(
        self,
        viewer_context: ExecutionContext,
        *,
        trace_id: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """按调用者 `ExecutionContext` 的租户与角色可见性边界查询最新审计记录（按时间倒序）。"""

    async def aclose(self) -> None:
        """优雅释放底层连接池资源。"""


class InMemoryAuditStore(AuditStore):
    """本地开发与单测默认的内存态审计存储实现。"""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._records)

    @property
    def entries(self) -> tuple[AuditLogEntry, ...]:
        return tuple(r.to_entry() for r in self._records)

    async def append(
        self,
        entry: AuditLogEntry,
        *,
        error_code: str | None = None,
        guard_reason: str | None = None,
    ) -> AuditRecord:
        record = AuditRecord.from_entry(
            entry,
            error_code=error_code,
            guard_reason=guard_reason,
        )
        self._records.append(record)
        return record

    async def query(
        self,
        viewer_context: ExecutionContext,
        *,
        trace_id: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        capped_limit = max(1, min(int(limit), 500))
        visible: list[AuditRecord] = []
        for rec in reversed(self._records):
            if rec.tenant_id != viewer_context.tenant_id:
                continue
            if viewer_context.role == "customer" and rec.user_id != viewer_context.user_id:
                continue
            if trace_id and rec.trace_id != trace_id:
                continue
            if status and rec.status != status:
                continue
            if tool_name and rec.tool_name != tool_name:
                continue
            visible.append(rec)
            if len(visible) >= capped_limit:
                break
        return visible


class PostgresAuditStore(AuditStore):
    """基于异步连接池的生产级 PostgreSQL 审计存储实现（对齐 `docker/init.sql` 的 `audit_logs` 表结构）。"""

    def __init__(
        self,
        dsn: str,
        *,
        pool: Any | None = None,
        table_name: str = "audit_logs",
    ) -> None:
        normalized = normalize_postgres_dsn(dsn)
        if normalized is None:
            raise ValueError(f"无效的 PostgreSQL 连接串: {dsn!r}")
        if not _SAFE_SQL_IDENTIFIER_RE.match(table_name):
            raise ValueError(f"非法的审计表名标识符: {table_name!r}")

        self._dsn = normalized
        self._pool = pool
        self._owns_pool = pool is None
        self._table_name = table_name

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool

        from psycopg_pool import AsyncConnectionPool  # type: ignore[import-not-found]

        pool = AsyncConnectionPool(conninfo=self._dsn, open=False)
        await pool.open()
        self._pool = pool
        return pool

    async def initialize(self) -> None:
        pool = await self._ensure_pool()
        ddl_table = f"""
        CREATE TABLE IF NOT EXISTS {self._table_name} (
            id BIGSERIAL PRIMARY KEY,
            trace_id VARCHAR(128) NOT NULL,
            tool_call_id VARCHAR(128) NOT NULL,
            tenant_id VARCHAR(128) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            role VARCHAR(32) NOT NULL,
            tool_name VARCHAR(128) NOT NULL,
            domain VARCHAR(64) NOT NULL,
            risk_level VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            intercepted BOOLEAN NOT NULL,
            error_type VARCHAR(64),
            error_code VARCHAR(128),
            guard_reason TEXT,
            latency_ms DOUBLE PRECISION NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        """
        ddl_idx_tenant = (
            f"CREATE INDEX IF NOT EXISTS idx_{self._table_name}_tenant_created_at "
            f"ON {self._table_name} (tenant_id, created_at DESC, id DESC);"
        )
        ddl_idx_user = (
            f"CREATE INDEX IF NOT EXISTS idx_{self._table_name}_tenant_user_created_at "
            f"ON {self._table_name} (tenant_id, user_id, created_at DESC, id DESC);"
        )
        ddl_idx_trace = (
            f"CREATE INDEX IF NOT EXISTS idx_{self._table_name}_trace_id "
            f"ON {self._table_name} (trace_id);"
        )
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(ddl_table)
                await cur.execute(ddl_idx_tenant)
                await cur.execute(ddl_idx_user)
                await cur.execute(ddl_idx_trace)
            commit_fn = getattr(conn, "commit", None)
            if callable(commit_fn):
                await commit_fn()

    async def append(
        self,
        entry: AuditLogEntry,
        *,
        error_code: str | None = None,
        guard_reason: str | None = None,
    ) -> AuditRecord:
        record = AuditRecord.from_entry(
            entry,
            error_code=error_code,
            guard_reason=guard_reason,
        )
        pool = await self._ensure_pool()
        insert_sql = f"""
        INSERT INTO {self._table_name} (
            trace_id, tool_call_id, tenant_id, user_id, role,
            tool_name, domain, risk_level, status, intercepted,
            error_type, error_code, guard_reason, latency_ms, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params = (
            record.trace_id,
            record.tool_call_id,
            record.tenant_id,
            record.user_id,
            record.role,
            record.tool_name,
            record.domain,
            record.risk,
            record.status,
            record.intercepted,
            record.error_type,
            record.error_code,
            record.guard_reason,
            record.duration_ms,
            record.timestamp,
        )
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(insert_sql, params)
            commit_fn = getattr(conn, "commit", None)
            if callable(commit_fn):
                await commit_fn()
        return record

    async def query(
        self,
        viewer_context: ExecutionContext,
        *,
        trace_id: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        capped_limit = max(1, min(int(limit), 500))
        pool = await self._ensure_pool()

        clauses: list[str] = ["tenant_id = %s"]
        params: list[Any] = [viewer_context.tenant_id]

        if viewer_context.role == "customer":
            clauses.append("user_id = %s")
            params.append(viewer_context.user_id)
        if trace_id:
            clauses.append("trace_id = %s")
            params.append(trace_id)
        if status:
            clauses.append("status = %s")
            params.append(status)
        if tool_name:
            clauses.append("tool_name = %s")
            params.append(tool_name)

        params.append(capped_limit)
        where_sql = " AND ".join(clauses)
        select_sql = f"""
        SELECT
            created_at, trace_id, tool_call_id, tenant_id, user_id, role,
            tool_name, domain, risk_level, status, intercepted,
            error_type, error_code, guard_reason, latency_ms
        FROM {self._table_name}
        WHERE {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(select_sql, tuple(params))
                rows = await cur.fetchall()

        records: list[AuditRecord] = []
        for row in rows:
            ts_val = row[0]
            if isinstance(ts_val, str):
                ts_val = datetime.fromisoformat(ts_val)
            elif isinstance(ts_val, datetime) and ts_val.tzinfo is None:
                ts_val = ts_val.replace(tzinfo=timezone.utc)
            records.append(
                AuditRecord(
                    timestamp=ts_val,
                    trace_id=str(row[1]),
                    tool_call_id=str(row[2]),
                    tenant_id=str(row[3]),
                    user_id=str(row[4]),
                    role=row[5],
                    tool_name=str(row[6]),
                    domain=str(row[7]),
                    risk=row[8],
                    status=row[9],
                    intercepted=bool(row[10]),
                    error_type=row[11],
                    error_code=row[12],
                    guard_reason=row[13],
                    duration_ms=float(row[14]),
                )
            )
        return records

    async def aclose(self) -> None:
        if self._pool is not None and self._owns_pool:
            close_fn = getattr(self._pool, "close", None)
            if callable(close_fn):
                await close_fn()
        self._pool = None


@asynccontextmanager
async def audit_store_lifespan(
    app_settings: AppSettings | None = None,
    *,
    allow_fallback: bool = True,
) -> AsyncIterator[tuple[AuditStore, str]]:
    """异步生命周期审计存储工厂：配置了 PostgreSQL 时启用 `PostgresAuditStore`，否则使用 `InMemoryAuditStore`。"""
    cfg = app_settings or get_settings()
    pg_dsn = normalize_postgres_dsn(cfg.DATABASE_URL)
    logger = get_structured_logger("infra.audit_store")

    if pg_dsn is None:
        store = InMemoryAuditStore()
        await store.initialize()
        yield store, "memory:InMemoryAuditStore"
        return

    pg_store = PostgresAuditStore(pg_dsn)
    try:
        await pg_store.initialize()
    except Exception as exc:
        await pg_store.aclose()
        if not allow_fallback:
            raise
        logger.warning(
            f"初始化 PostgresAuditStore 失败 ({exc})，已优雅回退至 InMemoryAuditStore",
            extra={"event": "audit_store_fallback_memory", "reason": str(exc)},
        )
        mem_store = InMemoryAuditStore()
        await mem_store.initialize()
        yield mem_store, "memory:InMemoryAuditStore"
        return

    try:
        yield pg_store, "postgres:PostgresAuditStore"
    finally:
        await pg_store.aclose()


def create_audit_store(app_settings: AppSettings | None = None) -> AuditStore:
    """按配置创建审计存储实例（注意：`PostgresAuditStore` 使用前需调用 `await initialize()`）。"""
    cfg = app_settings or get_settings()
    pg_dsn = normalize_postgres_dsn(cfg.DATABASE_URL)
    if pg_dsn is not None:
        return PostgresAuditStore(pg_dsn)
    return InMemoryAuditStore()


__all__ = [
    "AuditRecord",
    "AuditStore",
    "InMemoryAuditStore",
    "PostgresAuditStore",
    "audit_store_lifespan",
    "create_audit_store",
]
