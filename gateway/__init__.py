"""API Gateway 层导出包（五层架构第 1 层）。

职责边界：
1. 接收外部 HTTP / SSE 请求，完成 JWT 登录态鉴权并生成不可变 `ExecutionContext`；
2. 依据 `ExecutionContext.role` 严格分流 C 端（`customer`）与 B 端（`merchant`）入口；
3. 接收人工审批回调并签发受信任的 `HITLApproval` 凭证注入 `ExecutionContext`。
"""

from gateway.auth import (
    CUSTOMER_DEFAULT_PERMISSIONS,
    JWTExecutionContextMiddleware,
    MERCHANT_DEFAULT_PERMISSIONS,
    PRESET_ACCOUNTS,
    PresetAccount,
    issue_access_token,
    require_execution_context,
    verify_jwt_and_build_context,
)
from gateway.main import app, create_gateway_app

__all__ = [
    "CUSTOMER_DEFAULT_PERMISSIONS",
    "JWTExecutionContextMiddleware",
    "MERCHANT_DEFAULT_PERMISSIONS",
    "PRESET_ACCOUNTS",
    "PresetAccount",
    "app",
    "create_gateway_app",
    "issue_access_token",
    "require_execution_context",
    "verify_jwt_and_build_context",
]
