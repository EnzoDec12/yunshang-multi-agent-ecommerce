"""容器化基建（Docker / Compose / SQL）与 GitHub Actions CI 配置契约单测。"""

from __future__ import annotations

from pathlib import Path

import yaml

from infra.audit_store import PostgresAuditStore

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


def test_docker_init_sql_is_file_and_aligns_with_audit_store() -> None:
    """`docker/init.sql` 必须为 SQL 文件（非目录），且字段对齐 `PostgresAuditStore`。"""
    init_sql_path = WORKSPACE_ROOT / "docker" / "init.sql"
    assert init_sql_path.exists(), "缺少 docker/init.sql"
    assert init_sql_path.is_file() and not init_sql_path.is_dir(), (
        "docker/init.sql 必须是文件而非目录"
    )

    sql = init_sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.audit_logs" in sql

    required_columns = (
        "trace_id",
        "tool_call_id",
        "tenant_id",
        "user_id",
        "role",
        "tool_name",
        "risk_level",
        "status",
        "latency_ms",
        "created_at",
    )
    for col in required_columns:
        assert col in sql, f"docker/init.sql 缺少必需审计字段: {col}"

    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "CREATE SCHEMA IF NOT EXISTS langgraph" in sql
    assert "GRANT" in sql

    store = PostgresAuditStore("postgresql://user:pwd@localhost:5432/db")
    assert store._table_name == "audit_logs"


def test_dockerfiles_use_multistage_production_bases() -> None:
    """校验后端与前端 Dockerfile 多阶段构建基线。"""
    backend_df = (WORKSPACE_ROOT / "docker" / "Dockerfile.backend").read_text(
        encoding="utf-8"
    )
    assert "FROM python:3.11-slim AS builder" in backend_df
    assert "FROM python:3.11-slim AS runtime" in backend_df
    assert "uvicorn" in backend_df

    web_df = (WORKSPACE_ROOT / "docker" / "Dockerfile.web").read_text(encoding="utf-8")
    assert "FROM node:20-alpine AS deps" in web_df
    assert "FROM node:20-alpine AS builder" in web_df
    assert "FROM node:20-alpine AS runner" in web_df
    assert ".next/standalone" in web_df
    assert 'CMD ["node", "server.js"]' in web_df

    next_cfg = (WORKSPACE_ROOT / "web" / "next.config.ts").read_text(encoding="utf-8")
    assert 'output: "standalone"' in next_cfg


def test_docker_compose_orchestrates_eight_services_with_strict_network_exposure() -> None:
    """校验 `docker-compose.yml` 包含全部 8 个服务、健康检查与严格端口暴露策略。"""
    compose_path = WORKSPACE_ROOT / "docker-compose.yml"
    assert compose_path.is_file(), "缺少根目录 docker-compose.yml"

    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose.get("services", {})
    expected_services = {
        "postgres",
        "mcp-pim",
        "mcp-oms",
        "mcp-logistics",
        "mcp-bi",
        "mcp-market-erp",
        "gateway",
        "web",
    }
    assert set(services.keys()) == expected_services

    # 1) postgres 挂载 docker/init.sql 并配置 healthcheck
    pg = services["postgres"]
    assert "healthcheck" in pg
    pg_volumes = " ".join(str(v) for v in pg.get("volumes", []))
    assert "./docker/init.sql:" in pg_volumes
    assert "ports" not in pg, "postgres 不应对宿主机直接暴露端口"

    # 2) 5 个独立 MCP Server 均配置 healthcheck 且不暴露宿主机端口
    mcp_names = ("mcp-pim", "mcp-oms", "mcp-logistics", "mcp-bi", "mcp-market-erp")
    for name in mcp_names:
        svc = services[name]
        assert "healthcheck" in svc, f"{name} 缺少 healthcheck"
        assert "ports" not in svc, f"{name} 不应对宿主机直接暴露端口"

    # 3) gateway 禁用本地子进程拉起，依赖 postgres + 5 个 MCP 服务就绪，且暴露 8000
    gw = services["gateway"]
    gw_env = gw.get("environment", {})
    assert str(gw_env.get("MCP_SPAWN_LOCAL_SUBPROCESS")).lower() == "false"
    gw_deps = gw.get("depends_on", {})
    for dep in ("postgres", *mcp_names):
        assert dep in gw_deps, f"gateway 缺少对 {dep} 的依赖声明"
        assert gw_deps[dep].get("condition") == "service_healthy"
    assert gw.get("ports") == ["8000:8000"]

    # 4) web 仅暴露 3000
    web = services["web"]
    assert web.get("ports") == ["3000:3000"]


def test_github_actions_ci_workflow_covers_backend_and_frontend() -> None:
    """校验 `.github/workflows/ci.yml` 同时覆盖后端 Ruff+Pytest 与前端 TSC+Build。"""
    ci_path = WORKSPACE_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file(), "缺少 .github/workflows/ci.yml"

    workflow = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs", {})
    assert "backend" in jobs and "frontend" in jobs

    backend_steps = "\n".join(str(s.get("run", "")) for s in jobs["backend"]["steps"])
    assert "ruff check" in backend_steps
    assert "pytest" in backend_steps

    frontend_job = jobs["frontend"]
    assert frontend_job.get("defaults", {}).get("run", {}).get("working-directory") == "web"
    frontend_steps = "\n".join(str(s.get("run", "")) for s in frontend_job["steps"])
    assert "npm ci" in frontend_steps
    assert "npx tsc --noEmit" in frontend_steps
    assert "npm run build" in frontend_steps
