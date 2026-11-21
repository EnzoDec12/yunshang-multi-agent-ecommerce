"""MCP 独立域服务带外上下文协议、结构化错误信封与通用运行时辅助模块。

安全红线实现：
1. `ExecutionContext` 绝不进入任何 MCP Tool 的模型可见 `inputSchema`；
2. 客户端通过 HTTP 请求头 `X-MCP-Execution-Context` 与 MCP 请求 `_meta["ecommerce.execution_context"]`
   双通道带外透传不可变身份与租户边界；
3. 远端 MCP Server 在执行任何业务读写前强制提取并校验 `ExecutionContext`、角色边界与 RBAC 权限；
4. 业务拒绝、参数语义错误、权限拒绝均编码为结构化错误载荷返回，供 `MCPClientManager` 精准还原四分类错误。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from schemas.context import ExecutionContext, RoleType
from schemas.contracts import ErrorType, RiskLevel

EXECUTION_CONTEXT_HEADER = "x-mcp-execution-context"
TRACE_ID_HEADER = "x-trace-id"
TENANT_ID_HEADER = "x-tenant-id"
USER_ID_HEADER = "x-user-id"
ROLE_HEADER = "x-role"
TOOL_CALL_ID_HEADER = "x-tool-call-id"

EXECUTION_CONTEXT_META_KEY = "ecommerce.execution_context"
DOMAIN_ERROR_SENTINEL = "__mcp_domain_error__"


def serialize_execution_context(
    context: ExecutionContext,
    *,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """将不可变 `ExecutionContext` 序列化为可跨进程传输的 JSON 字典。"""
    payload: dict[str, Any] = {
        "trace_id": context.trace_id,
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "role": context.role,
        "permissions": sorted(context.permissions),
        "approved_tool_call_ids": sorted(context.approved_tool_call_ids),
        "hitl_approvals": [
            approval.model_dump(mode="json") for approval in context.hitl_approvals
        ],
    }
    if tool_call_id is not None:
        payload["tool_call_id"] = tool_call_id
    return payload


def encode_execution_context_header(
    context: ExecutionContext,
    *,
    tool_call_id: str | None = None,
) -> str:
    """将 `ExecutionContext` 编码为 URL-safe Base64 HTTP 请求头字符串。"""
    raw_bytes = json.dumps(
        serialize_execution_context(context, tool_call_id=tool_call_id),
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")


def decode_execution_context_header(encoded: str) -> ExecutionContext:
    """从 Base64 HTTP 请求头解码并强校验还原 `ExecutionContext`。"""
    padded = encoded + ("=" * (-len(encoded) % 4))
    raw_json = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    parsed = json.loads(raw_json)
    if not isinstance(parsed, Mapping):
        raise ValueError("ExecutionContext header 载荷必须是 JSON 对象")
    return _build_context_from_mapping(parsed)


def _build_context_from_mapping(data: Mapping[str, Any]) -> ExecutionContext:
    allowed_keys = {
        "trace_id",
        "tenant_id",
        "user_id",
        "role",
        "permissions",
        "hitl_approvals",
        "approved_tool_call_ids",
    }
    filtered = {k: v for k, v in data.items() if k in allowed_keys}
    return ExecutionContext.model_validate(filtered)


def build_tool_meta(
    *,
    permission: str,
    risk: RiskLevel,
    allowed_roles: set[RoleType] | frozenset[RoleType],
) -> dict[str, Any]:
    """构造挂载在 MCP Tool `_meta` 上的安全治理元数据（供 `MCPClientManager` 动态发现）。"""
    return {
        "permission": permission,
        "risk": risk,
        "allowed_roles": sorted(allowed_roles),
    }


def raise_mcp_domain_error(
    *,
    error_type: ErrorType,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    """在远端 MCP Tool 内部抛出结构化领域异常（经 MCP `ToolError` 协议编码返回）。"""
    envelope: dict[str, Any] = {
        DOMAIN_ERROR_SENTINEL: True,
        "error_type": error_type,
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": details,
    }
    raise ToolError(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))


def parse_mcp_domain_error(raw_text: str) -> dict[str, Any] | None:
    """从 MCP `CallToolResult` 错误文本中提取结构化领域错误字典。"""
    marker_idx = raw_text.find(f'"{DOMAIN_ERROR_SENTINEL}"')
    if marker_idx == -1:
        return None
    brace_idx = raw_text.rfind("{", 0, marker_idx)
    if brace_idx == -1:
        return None
    candidate = raw_text[brace_idx:].strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and parsed.get(DOMAIN_ERROR_SENTINEL) is True:
        return parsed
    return None


def extract_execution_context(ctx: Context[Any, Any]) -> ExecutionContext:
    """从远端 MCP 请求的 HTTP Header 或 `_meta` 带外通道提取并验证 `ExecutionContext`。"""
    headers = ctx.headers
    if headers is not None:
        lower_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        encoded_header = lower_headers.get(EXECUTION_CONTEXT_HEADER)
        if encoded_header:
            try:
                return decode_execution_context_header(encoded_header)
            except Exception as exc:
                raise_mcp_domain_error(
                    error_type="permission_error",
                    code="INVALID_EXECUTION_CONTEXT_HEADER",
                    message=f"带外 ExecutionContext 请求头校验失败: {exc}",
                )

    try:
        req_meta = ctx.request_context.meta
    except ValueError:
        req_meta = None

    if isinstance(req_meta, Mapping):
        raw_ctx = req_meta.get(EXECUTION_CONTEXT_META_KEY)
        if isinstance(raw_ctx, Mapping):
            try:
                return _build_context_from_mapping(raw_ctx)
            except Exception as exc:
                raise_mcp_domain_error(
                    error_type="permission_error",
                    code="INVALID_EXECUTION_CONTEXT_META",
                    message=f"带外 ExecutionContext 元数据校验失败: {exc}",
                )

    raise_mcp_domain_error(
        error_type="permission_error",
        code="MISSING_TRUSTED_CONTEXT",
        message="拒绝未携带可信 ExecutionContext 的调用；禁止绕过 ToolRuntime 直连 MCP Server",
    )
    raise AssertionError("unreachable")


def authorize_domain_call(
    ctx: Context[Any, Any],
    *,
    permission: str,
    allowed_roles: set[RoleType] | frozenset[RoleType],
) -> ExecutionContext:
    """远端 MCP Server 纵深防御鉴权：校验带外身份、双域角色边界与 RBAC 权限点。"""
    exec_ctx = extract_execution_context(ctx)
    if exec_ctx.role not in allowed_roles:
        raise_mcp_domain_error(
            error_type="permission_error",
            code="REMOTE_ROLE_BOUNDARY_DENIED",
            message=f"远端域服务拒绝角色 '{exec_ctx.role}' 访问（仅允许 {sorted(allowed_roles)}）",
        )
    if permission not in exec_ctx.permissions:
        raise_mcp_domain_error(
            error_type="permission_error",
            code="REMOTE_RBAC_DENIED",
            message=f"远端域服务拒绝访问：当前主体缺少权限 '{permission}'",
        )
    return exec_ctx


@dataclass
class FaultInjectionState:
    """供跨进程集成测试注入响应延迟或服务降级故障的受控状态容器。"""

    delay_seconds: float = 0.0
    target_tool: str | None = None
    fail_healthcheck: bool = False


async def maybe_apply_fault_delay(
    fault_state: FaultInjectionState,
    tool_name: str,
) -> None:
    """若当前域服务配置了故障延迟注入，则在执行前按设定异步挂起以复现真实网络超时。"""
    if fault_state.delay_seconds > 0.0 and (
        fault_state.target_tool is None or fault_state.target_tool == tool_name
    ):
        await asyncio.sleep(fault_state.delay_seconds)


def attach_standard_routes(
    server: MCPServer[Any],
    *,
    domain: str,
    fault_state: FaultInjectionState,
) -> None:
    """为独立域 MCP Server 挂载 `/health` 探针与本地测试 `/admin/fault` 故障注入端点。"""

    @server.custom_route("/health", methods=["GET"])
    async def health_endpoint(_request: Request) -> Response:
        if fault_state.fail_healthcheck:
            return JSONResponse(
                {
                    "status": "unhealthy",
                    "domain": domain,
                    "service": server.name,
                },
                status_code=503,
            )
        return JSONResponse(
            {
                "status": "ok",
                "domain": domain,
                "service": server.name,
            }
        )

    @server.custom_route("/admin/fault", methods=["POST"])
    async def configure_fault(request: Request) -> Response:
        body = await request.json()
        if not isinstance(body, Mapping):
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        if "delay_seconds" in body:
            fault_state.delay_seconds = max(0.0, float(body["delay_seconds"]))
        if "target_tool" in body:
            target = body["target_tool"]
            fault_state.target_tool = str(target) if target is not None else None
        if "fail_healthcheck" in body:
            fault_state.fail_healthcheck = bool(body["fail_healthcheck"])
        return JSONResponse(
            {
                "domain": domain,
                "delay_seconds": fault_state.delay_seconds,
                "target_tool": fault_state.target_tool,
                "fail_healthcheck": fault_state.fail_healthcheck,
            }
        )


def build_mcp_cli_parser(
    *,
    default_port: int,
    description: str,
) -> argparse.ArgumentParser:
    """构建独立域 MCP Server 标准 CLI 参数解析器（支持环境变量默认值覆盖）。"""
    default_host = os.environ.get("MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_env_port = os.environ.get("MCP_PORT", "").strip()
    resolved_port = int(raw_env_port) if raw_env_port.isdigit() else default_port
    default_path = os.environ.get("MCP_PATH", "/mcp").strip() or "/mcp"
    default_log_level = (
        os.environ.get("MCP_LOG_LEVEL", "warning").strip().lower() or "warning"
    )

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--host",
        default=default_host,
        help=f"监听地址（默认 {default_host}，可通过 MCP_HOST 覆盖）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=resolved_port,
        help=f"监听端口（默认 {resolved_port}，可通过 MCP_PORT 覆盖）",
    )
    parser.add_argument(
        "--path",
        default=default_path,
        help=f"Streamable HTTP 挂载路径（默认 {default_path}）",
    )
    parser.add_argument(
        "--log-level",
        default=default_log_level,
        choices=["debug", "info", "warning", "error", "critical"],
        help="Uvicorn 日志级别",
    )
    return parser


def run_mcp_http_cli(
    *,
    create_app_fn: Callable[[str, str], Starlette],
    default_port: int,
    description: str,
    argv: Sequence[str] | None = None,
) -> None:
    """统一命令行启动入口：支持各独立域 MCP Server 作为真实独立操作系统进程运行。"""
    parser = build_mcp_cli_parser(
        default_port=default_port,
        description=description,
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    app = create_app_fn(args.host, args.path)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
