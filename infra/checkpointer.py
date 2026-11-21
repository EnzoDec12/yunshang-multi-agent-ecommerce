"""LangGraph 状态机与 HITL 断点持久化 Checkpointer 工厂模块。

策略：
1. 当 `settings.DATABASE_URL` 配置为 PostgreSQL 连接串时，优先启用 `AsyncPostgresSaver`
   并自动执行 `await saver.setup()` 初始化断点表；
2. 当未配置 `DATABASE_URL`、处于离线单测环境、或开发环境下驱动/数据库不可达且允许降级时，
   优雅回退到注册了契约类型白名单的 `MemorySaver`，保障零外部依赖可运行。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from config.settings import AppSettings, get_settings
from infra.observability import get_structured_logger

ALLOWED_CHECKPOINT_MODULES: list[tuple[str, str]] = [
    ("schemas.context", "ExecutionContext"),
    ("schemas.context", "HITLApproval"),
    ("schemas.contracts", "AnomalousMetric"),
    ("schemas.contracts", "CandidateSKU"),
    ("schemas.contracts", "DiagnosisArtifact"),
    ("schemas.contracts", "ErrorPayload"),
    ("schemas.contracts", "HITLRequired"),
    ("schemas.contracts", "PriceBandGap"),
    ("schemas.contracts", "ReturnReasonStat"),
    ("schemas.contracts", "SelectionProposal"),
    ("schemas.contracts", "ToolCall"),
    ("schemas.contracts", "ToolResultMessage"),
    ("agents.state", "PendingHITLGatePayload"),
    ("agents.state", "SubAgentExecutionReport"),
    ("agents.state", "SupervisorRouteDecision"),
]


def build_checkpoint_serde() -> JsonPlusSerializer:
    """构建注册了强类型业务与安全契约白名单的 `JsonPlusSerializer`。"""
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_CHECKPOINT_MODULES)


def normalize_postgres_dsn(database_url: str | None) -> str | None:
    """将 SQLAlchemy 风格或标准 PostgreSQL URL 归一化为 libpq/psycopg 兼容连接串。"""
    if not database_url or not database_url.strip():
        return None
    raw = database_url.strip()
    lower = raw.lower()
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://", "postgresql+psycopg2://"):
        if lower.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    if lower.startswith(("postgresql://", "postgres://")):
        return raw
    return None


def create_memory_checkpointer() -> MemorySaver:
    """创建注册了强类型契约序列化白名单的内存态 `MemorySaver`。"""
    return MemorySaver(serde=build_checkpoint_serde())


def create_default_checkpointer(
    app_settings: AppSettings | None = None,
) -> BaseCheckpointSaver[Any]:
    """同步图编译默认 Checkpointer 工厂（测试与未配置外部连接池时返回 `MemorySaver`）。"""
    _ = app_settings or get_settings()
    return create_memory_checkpointer()


def _load_async_postgres_saver_cls() -> Any:
    from langgraph.checkpoint.postgres.aio import (  # type: ignore[import-not-found]
        AsyncPostgresSaver,
    )

    return AsyncPostgresSaver


@asynccontextmanager
async def checkpointer_lifespan(
    app_settings: AppSettings | None = None,
    *,
    allow_fallback: bool = True,
) -> AsyncIterator[tuple[BaseCheckpointSaver[Any], str]]:
    """异步生命周期 Checkpointer 工厂：PostgreSQL 模式下启用 `AsyncPostgresSaver`，否则优雅回退 `MemorySaver`。"""
    cfg = app_settings or get_settings()
    pg_dsn = normalize_postgres_dsn(cfg.DATABASE_URL)
    logger = get_structured_logger("infra.checkpointer")

    if pg_dsn is None:
        yield create_memory_checkpointer(), "memory:MemorySaver"
        return

    try:
        saver_cls = _load_async_postgres_saver_cls()
    except ImportError as exc:
        if not allow_fallback:
            raise
        logger.warning(
            "已配置 PostgreSQL DATABASE_URL，但当前环境未安装 langgraph-checkpoint-postgres，已优雅回退至 MemorySaver",
            extra={"event": "checkpointer_fallback_memory", "reason": str(exc)},
        )
        yield create_memory_checkpointer(), "memory:MemorySaver"
        return

    serde = build_checkpoint_serde()
    cm: Any = None
    try:
        try:
            cm = saver_cls.from_conn_string(pg_dsn, serde=serde)
        except TypeError:
            cm = saver_cls.from_conn_string(pg_dsn)
        saver = await cm.__aenter__()
    except Exception as exc:
        if not allow_fallback:
            raise
        logger.warning(
            f"连接 PostgreSQL Checkpointer 失败 ({exc})，已优雅回退至 MemorySaver",
            extra={"event": "checkpointer_fallback_memory", "reason": str(exc)},
        )
        yield create_memory_checkpointer(), "memory:MemorySaver"
        return

    try:
        if getattr(saver, "serde", None) is not serde:
            try:
                saver.serde = serde
            except Exception:
                pass
        setup_fn = getattr(saver, "setup", None)
        if callable(setup_fn):
            await setup_fn()
        yield saver, "postgres:AsyncPostgresSaver"
    finally:
        await cm.__aexit__(None, None, None)


__all__ = [
    "ALLOWED_CHECKPOINT_MODULES",
    "build_checkpoint_serde",
    "checkpointer_lifespan",
    "create_default_checkpointer",
    "create_memory_checkpointer",
    "normalize_postgres_dsn",
]
