"""API Gateway JWT 身份认证与可信 `ExecutionContext` 签发模块。

严格遵守架构安全红线：
1. 仅从 HTTP `Authorization: Bearer <token>` 解析可信身份声明（HMAC-SHA256 / PyJWT）；
2. 每次请求动态生成全局唯一 `trace_id`，组装冻结不可变的 `ExecutionContext` 写入 `request.state.execution_context`；
3. 严禁信任客户端 JSON Body 或 Query 参数传入的 `user_id`、`tenant_id`、`role`、`permissions`、`approved` 等字段。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from config.settings import DEFAULT_JWT_SECRET, AppSettings, get_settings
from infra.observability import bind_observability_context
from schemas.context import ExecutionContext, RoleType

_INITIAL_SETTINGS = get_settings()
JWT_ALGORITHM = _INITIAL_SETTINGS.JWT_ALGORITHM
JWT_ISSUER = _INITIAL_SETTINGS.JWT_ISSUER
DEFAULT_JWT_TTL_SECONDS = _INITIAL_SETTINGS.JWT_TTL_SECONDS

CUSTOMER_DEFAULT_PERMISSIONS: frozenset[str] = frozenset(
    {
        "pim:catalog:read",
        "pim:stock:read",
        "oms:orders:read",
        "oms:orders:update_address",
        "oms:refund:create",
        "logistics:package:read",
    }
)

MERCHANT_DEFAULT_PERMISSIONS: frozenset[str] = CUSTOMER_DEFAULT_PERMISSIONS | frozenset(
    {
        "pim:sku:propose",
        "bi:schema:read",
        "bi:sql:query",
        "bi:chart:render",
        "market_erp:trends:read",
        "market_erp:cost:calculate",
    }
)


@dataclass(frozen=True, kw_only=True)
class PresetAccount:
    """演示与联调测试预设身份账号（与五域 Mock 数据租户及买家/商家 ID 对齐）。"""

    account_id: str
    display_name: str
    tenant_id: str
    user_id: str
    role: RoleType
    permissions: frozenset[str]
    badge: str
    description: str
    primary_demo: bool = True

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "badge": self.badge,
            "description": self.description,
            "primary_demo": self.primary_demo,
        }


PRESET_ACCOUNTS: dict[str, PresetAccount] = {
    "customer_01": PresetAccount(
        account_id="customer_01",
        display_name="林晓月 · 黑卡会员",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_1001",
        role="customer",
        permissions=CUSTOMER_DEFAULT_PERMISSIONS,
        badge="顾客导购",
        description="为您提供当季女装款式推荐、尺码颜色与面料对比、成套穿搭建议，以及个人订单物流查询与售后服务。",
        primary_demo=True,
    ),
    "merchant_01": PresetAccount(
        account_id="merchant_01",
        display_name="周企划 · 商品总监",
        tenant_id="tenant_yunshang_cn",
        user_id="merchant_ops_01",
        role="merchant",
        permissions=MERCHANT_DEFAULT_PERMISSIONS,
        badge="商家经营台",
        description="为您提供店铺女装退货原因诊断、高潜力价格带机会分析、供应链成衣成本与毛利率测算及新款建档管理。",
        primary_demo=True,
    ),
    "customer_02": PresetAccount(
        account_id="customer_02",
        display_name="周明姝 · 会员顾客",
        tenant_id="tenant_yunshang_cn",
        user_id="buyer_1002",
        role="customer",
        permissions=CUSTOMER_DEFAULT_PERMISSIONS,
        badge="顾客 02",
        description="同店另一会员顾客账号，仅可查看本人订单与售后服务记录。",
        primary_demo=False,
    ),
    "merchant_other": PresetAccount(
        account_id="merchant_other",
        display_name="陈店长 · 外部店铺",
        tenant_id="tenant_other_shop",
        user_id="merchant_2001",
        role="merchant",
        permissions=MERCHANT_DEFAULT_PERMISSIONS,
        badge="外部商家",
        description="外部独立女装店铺商家账号，仅可访问所属店铺自身经营数据。",
        primary_demo=False,
    ),
}


class GatewayAuthError(Exception):
    """网关 JWT 鉴权失败异常（统一映射为 HTTP 401）。"""

    def __init__(self, message: str, *, code: str = "UNAUTHORIZED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def get_jwt_secret(app_settings: AppSettings | None = None) -> str:
    cfg = app_settings or get_settings()
    return cfg.JWT_SECRET


def get_jwt_ttl_seconds(app_settings: AppSettings | None = None) -> int:
    cfg = app_settings or get_settings()
    return cfg.JWT_TTL_SECONDS


def generate_trace_id() -> str:
    return f"trc_{uuid.uuid4().hex[:16]}"


def issue_access_token(
    account: PresetAccount,
    *,
    secret: str | None = None,
    ttl_seconds: int | None = None,
    app_settings: AppSettings | None = None,
) -> tuple[str, int]:
    """为预设账号签发标准 HMAC-SHA256 JWT 访问令牌。"""
    cfg = app_settings or get_settings()
    signing_secret = secret if secret is not None else cfg.JWT_SECRET
    expires_in = ttl_seconds if ttl_seconds is not None else cfg.JWT_TTL_SECONDS
    now_ts = int(time.time())
    payload: dict[str, Any] = {
        "iss": cfg.JWT_ISSUER,
        "sub": account.user_id,
        "account_id": account.account_id,
        "display_name": account.display_name,
        "tenant_id": account.tenant_id,
        "role": account.role,
        "permissions": sorted(account.permissions),
        "iat": now_ts,
        "exp": now_ts + expires_in,
        "jti": f"jwt_{uuid.uuid4().hex[:12]}",
    }
    token = jwt.encode(payload, signing_secret, algorithm=cfg.JWT_ALGORITHM)
    return token, expires_in


def extract_bearer_token(auth_header: str | None) -> str:
    if not auth_header or not auth_header.strip():
        raise GatewayAuthError(
            "缺少 Authorization Bearer 访问令牌",
            code="MISSING_AUTH_TOKEN",
        )
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise GatewayAuthError(
            "Authorization 请求头格式无效，必须为 'Bearer <token>'",
            code="MALFORMED_AUTH_HEADER",
        )
    return parts[1].strip()


def verify_jwt_and_build_context(
    token: str,
    *,
    secret: str | None = None,
    trace_id: str | None = None,
    app_settings: AppSettings | None = None,
) -> tuple[ExecutionContext, dict[str, Any]]:
    """验签 JWT 并构造全新的不可变 `ExecutionContext`（每次请求独立 `trace_id`，初始零 HITL 授权）。"""
    cfg = app_settings or get_settings()
    signing_secret = secret if secret is not None else cfg.JWT_SECRET
    try:
        claims = jwt.decode(
            token,
            signing_secret,
            algorithms=[cfg.JWT_ALGORITHM],
            issuer=cfg.JWT_ISSUER,
            options={"require": ["iss", "sub", "tenant_id", "role", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise GatewayAuthError("登录令牌已过期，请重新登录", code="TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise GatewayAuthError("无效或被篡改的 JWT 访问令牌", code="INVALID_TOKEN") from exc

    role = claims.get("role")
    if role not in ("customer", "merchant"):
        raise GatewayAuthError("令牌中包含非法的角色声明", code="INVALID_ROLE_CLAIM")

    raw_perms = claims.get("permissions")
    if not isinstance(raw_perms, list):
        raise GatewayAuthError("令牌权限集格式非法", code="INVALID_PERMISSIONS_CLAIM")

    try:
        exec_ctx = ExecutionContext(
            trace_id=trace_id or generate_trace_id(),
            tenant_id=str(claims["tenant_id"]),
            user_id=str(claims["sub"]),
            role=role,
            permissions=frozenset(str(p) for p in raw_perms if str(p).strip()),
            hitl_approvals=frozenset(),
            approved_tool_call_ids=frozenset(),
        )
    except Exception as exc:
        raise GatewayAuthError("令牌身份声明未通过可信上下文校验", code="INVALID_CONTEXT_CLAIMS") from exc

    return exec_ctx, claims


PUBLIC_API_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/api/auth/login",
        "/api/auth/accounts",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)


class JWTExecutionContextMiddleware:
    """纯异步流式兼容 ASGI 鉴权中间件（避免 BaseHTTPMiddleware 阻塞 SSE 流）。

    对所有受保护 `/api/*` 路由强制解析 `Authorization: Bearer <token>`，
    并将不可变 `ExecutionContext` 挂载至 `request.state.execution_context`。
    """

    def __init__(self, app: ASGIApp, *, jwt_secret: str | None = None) -> None:
        self.app = app
        self._jwt_secret = jwt_secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))

        # 放行 CORS 预检请求与公开路由
        if method == "OPTIONS" or not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            await self.app(scope, receive, send)
            return

        auth_header: str | None = None
        for raw_key, raw_val in scope.get("headers", []):
            if raw_key.lower() == b"authorization":
                auth_header = raw_val.decode("latin-1")
                break

        try:
            token = extract_bearer_token(auth_header)
            exec_ctx, claims = verify_jwt_and_build_context(
                token,
                secret=self._jwt_secret,
            )
        except GatewayAuthError as err:
            resp = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": err.message, "code": err.code},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return

        state_dict = scope.setdefault("state", {})
        state_dict["execution_context"] = exec_ctx
        state_dict["jwt_claims"] = claims
        state_dict["account_id"] = str(claims.get("account_id") or exec_ctx.user_id)
        with bind_observability_context(exec_ctx):
            await self.app(scope, receive, send)


def require_execution_context(request: Request) -> ExecutionContext:
    """FastAPI 路由依赖：提取中间件已验签挂载的不可变 `ExecutionContext`。"""
    ctx = getattr(request.state, "execution_context", None)
    if isinstance(ctx, ExecutionContext):
        return ctx

    # 兜底防线：若路由在未挂载中间件的子应用中被直接调用，依然强制验签
    try:
        token = extract_bearer_token(request.headers.get("Authorization"))
        exec_ctx, claims = verify_jwt_and_build_context(token)
    except GatewayAuthError as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=err.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from err

    request.state.execution_context = exec_ctx
    request.state.jwt_claims = claims
    request.state.account_id = str(claims.get("account_id") or exec_ctx.user_id)
    return exec_ctx


__all__ = [
    "CUSTOMER_DEFAULT_PERMISSIONS",
    "DEFAULT_JWT_SECRET",
    "DEFAULT_JWT_TTL_SECONDS",
    "GatewayAuthError",
    "JWTExecutionContextMiddleware",
    "JWT_ALGORITHM",
    "JWT_ISSUER",
    "MERCHANT_DEFAULT_PERMISSIONS",
    "PRESET_ACCOUNTS",
    "PUBLIC_API_PATHS",
    "PresetAccount",
    "extract_bearer_token",
    "generate_trace_id",
    "get_jwt_secret",
    "issue_access_token",
    "require_execution_context",
    "verify_jwt_and_build_context",
]
