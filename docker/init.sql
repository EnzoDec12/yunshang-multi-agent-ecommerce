-- ============================================================================
-- 云裳女装多 Agent 双域电商系统 - PostgreSQL 初始化脚本 (docker/init.sql)
-- 自动挂载至 postgres 容器 /docker-entrypoint-initdb.d/01-init.sql
-- ============================================================================

-- 1. 预留与授权公共 Schema 及 LangGraph Checkpoint 持久化所需 Schema 权限
--    确保 AsyncPostgresSaver.setup() 在首次连接时具备创建 checkpoints /
--    checkpoint_blobs / checkpoint_writes / checkpoint_migrations 的完整权限。
CREATE SCHEMA IF NOT EXISTS public;
CREATE SCHEMA IF NOT EXISTS langgraph;

GRANT USAGE, CREATE ON SCHEMA public TO CURRENT_USER;
GRANT ALL PRIVILEGES ON SCHEMA langgraph TO CURRENT_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO CURRENT_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO CURRENT_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA langgraph GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO CURRENT_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA langgraph GRANT USAGE, SELECT ON SEQUENCES TO CURRENT_USER;

-- 2. 工具调用与风控拦截结构化审计流水表（对齐 ToolRuntime & PostgresAuditStore）
CREATE TABLE IF NOT EXISTS public.audit_logs (
    id              BIGSERIAL PRIMARY KEY,
    trace_id        VARCHAR(128) NOT NULL,
    tool_call_id    VARCHAR(128) NOT NULL,
    tenant_id       VARCHAR(128) NOT NULL,
    user_id         VARCHAR(128) NOT NULL,
    role            VARCHAR(32)  NOT NULL CHECK (role IN ('customer', 'merchant')),
    tool_name       VARCHAR(128) NOT NULL,
    domain          VARCHAR(64)  NOT NULL DEFAULT 'unknown',
    risk_level      VARCHAR(32)  NOT NULL,
    status          VARCHAR(32)  NOT NULL CHECK (status IN ('success', 'error', 'hitl_required')),
    intercepted     BOOLEAN      NOT NULL DEFAULT FALSE,
    error_type      VARCHAR(64),
    error_code      VARCHAR(128),
    guard_reason    TEXT,
    latency_ms      DOUBLE PRECISION NOT NULL CHECK (latency_ms >= 0),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.audit_logs IS 'ToolRuntime 受控工具执行与高风险 HITL / 越权拦截结构化审计表';

-- 3. 多租户与双域角色可见性隔离查询索引
--    (a) B 端商家按所属租户倒序审计流水
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_created_at
    ON public.audit_logs (tenant_id, created_at DESC, id DESC);

--    (b) C 端顾客仅查询本人名下工具执行记录
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_user_created_at
    ON public.audit_logs (tenant_id, user_id, created_at DESC, id DESC);

--    (c) 按单次请求全链路 trace_id 精准追踪
CREATE INDEX IF NOT EXISTS idx_audit_logs_trace_id
    ON public.audit_logs (trace_id);

--    (d) 按执行状态（success / error / hitl_required）筛选异常或挂起拦截
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_status_created_at
    ON public.audit_logs (tenant_id, status, created_at DESC);
