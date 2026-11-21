"""FastAPI 网关（`gateway/`）端到端集成与安全边界测试套件。

完整验证：
1. 五层解耦静态审计与 `lifespan` 独立 5 域 MCP Server 子进程集群自动拉起/零残留回收；
2. `customer_01` / `merchant_01` 登录签发 JWT、缺失/伪造/过期 Token `401` 拦截、
   `request.state.execution_context` 冻结不可变性与 JSON Body 伪造字段（`user_id`/`tenant_id`/`role`/`approved`）完全无效化；
3. `POST /api/chat/stream` 在 C 端（商品对比导购 + 查单物流 + 高危退款触发 `hitl_interrupt`）与
   B 端（经营诊断 -> 选品提案 -> 高危建品触发 `hitl_interrupt`）完整输出
   `thinking`、`tool_start`、`tool_result`、`hitl_interrupt`、`final_answer` 五类 SSE 事件；
4. `POST /api/hitl/resume` 跨账号/跨角色/跨租户越权一律返回 `403`，合法 `approve` 通过
   `ExecutionContext.with_hitl_approval(...)` 恢复流式落库与卖点飞轮回写，合法 `reject` 零副作用安全终止；
5. `GET /api/audit/logs` 准确记录 `trace_id`、`tool_call_id`、耗时与风控拦截记录，并严格执行租户/买家可见性隔离。
"""

from __future__ import annotations

import ast
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import Depends, Request
from pydantic import ValidationError

from gateway.auth import (
    JWT_ALGORITHM,
    JWT_ISSUER,
    PRESET_ACCOUNTS,
    require_execution_context,
)
from gateway.main import create_gateway_app
from schemas.context import ExecutionContext

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = WORKSPACE_ROOT / "gateway"
TEST_JWT_SECRET = "test-gateway-e2e-hs256-signing-secret-key-2026-secure"


def parse_sse_events(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    """解析标准 `text/event-stream` 响应文本为 `(event_type, json_payload)` 列表。"""
    events: list[tuple[str, dict[str, Any]]] = []
    blocks = raw_text.replace("\r\n", "\n").split("\n\n")
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in stripped.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            payload = json.loads("\n".join(data_lines))
            events.append((event_name, payload))
    return events


async def collect_sse_response(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, httpx.Headers, list[tuple[str, dict[str, Any]]]]:
    async with client.stream(
        method,
        url,
        headers=headers,
        json=json_body,
    ) as resp:
        body_bytes = await resp.aread()
        text = body_bytes.decode("utf-8")
        if resp.status_code != 200:
            try:
                err_json = json.loads(text)
            except json.JSONDecodeError:
                err_json = {"raw": text}
            return resp.status_code, resp.headers, [("http_error", err_json)]
        return resp.status_code, resp.headers, parse_sse_events(text)


@pytest_asyncio.fixture
async def gateway_client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    """启动完整带 Lifespan 的 FastAPI 网关（自动拉起内管 5 域 MCP Server 集群并在退出后核验零残留）。"""
    app = create_gateway_app(jwt_secret=TEST_JWT_SECRET)

    # 挂载一个测试探针路由用于直接断言 request.state.execution_context 的不可变性与防篡改性
    @app.post("/api/debug/context-probe")
    async def _context_probe(
        request: Request,
        ctx: ExecutionContext = Depends(require_execution_context),
    ) -> dict[str, Any]:
        assert request.state.execution_context is ctx
        with pytest.raises(ValidationError):
            ctx.role = "merchant"  # type: ignore[misc]
        return {
            "trace_id": ctx.trace_id,
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "role": ctx.role,
            "permissions": sorted(ctx.permissions),
            "hitl_approvals_count": len(ctx.hitl_approvals),
        }

    cluster_procs: list[Any] = []
    async with app.router.lifespan_context(app):
        container = app.state.container
        if container.mcp_cluster is not None:
            cluster_procs = [n.process for n in container.mcp_cluster.nodes.values()]
            assert len(cluster_procs) == 5
            assert all(p.returncode is None for p in cluster_procs)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://gateway.test",
            timeout=30.0,
        ) as client:
            yield client, app

    # 退出 lifespan 后确认 5 个 MCP Server 子进程已 100% 干净退出无残留
    for proc in cluster_procs:
        assert proc.returncode is not None


async def _login(client: httpx.AsyncClient, account_id: str) -> str:
    resp = await client.post("/api/auth/login", json={"account_id": account_id})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


# ============================================================================
# 1. 静态红线审计、JWT 鉴权、401 拦截、CORS 与 ExecutionContext 不可篡改性验证
# ============================================================================


async def test_gateway_auth_jwt_cors_and_execution_context_immutability(
    gateway_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, _app = gateway_client

    # 0) AST 静态扫描 gateway/*.py，确保绝不跨层导入 sqlite3 或 mcp_servers.mock_data
    forbidden_imports = {"sqlite3", "sqlalchemy", "mcp_servers.mock_data"}
    for py_file in sorted(GATEWAY_DIR.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_imports
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in forbidden_imports
                assert not node.module.startswith("mcp_servers")

    # 1) 健康检查与预设账号列表（对齐 mock_data.py 的 buyer_1001 与 tenant_yunshang_cn）
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json()["registered_tools_count"] == 13
    assert set(health.json()["mcp_domains"]) == {
        "pim",
        "oms",
        "logistics",
        "bi",
        "market_erp",
    }

    acc_resp = await client.get("/api/auth/accounts?primary_only=true")
    assert acc_resp.status_code == 200
    primary_ids = [a["account_id"] for a in acc_resp.json()["accounts"]]
    assert primary_ids == ["customer_01", "merchant_01"]
    assert PRESET_ACCOUNTS["customer_01"].user_id == "buyer_1001"
    assert PRESET_ACCOUNTS["customer_01"].tenant_id == "tenant_yunshang_cn"
    assert PRESET_ACCOUNTS["merchant_01"].tenant_id == "tenant_yunshang_cn"

    # 2) CORS 预检请求校验（允许 Next.js 本地端口携带 Authorization 跨域请求）
    preflight = await client.options(
        "/api/chat/stream",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization,Content-Type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "Authorization" in preflight.headers.get("access-control-allow-headers", "")

    # 3) 缺失 Token、错误格式、伪造签名、过期 Token 均严格拦截返回 401
    for protected_ep, method in (
        ("/api/auth/me", "GET"),
        ("/api/audit/logs", "GET"),
        ("/api/chat/stream", "POST"),
        ("/api/hitl/resume", "POST"),
    ):
        no_tok = await client.request(method, protected_ep, json={"message": "hi"})
        assert no_tok.status_code == 401

    forged_token = jwt.encode(
        {
            "iss": JWT_ISSUER,
            "sub": "buyer_1001",
            "tenant_id": "tenant_yunshang_cn",
            "role": "merchant",
            "permissions": ["bi:sql:query"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        "wrong-attacker-signing-key-0000000000000000",
        algorithm=JWT_ALGORITHM,
    )
    forged_resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {forged_token}"},
    )
    assert forged_resp.status_code == 401

    expired_token = jwt.encode(
        {
            "iss": JWT_ISSUER,
            "sub": "buyer_1001",
            "tenant_id": "tenant_yunshang_cn",
            "role": "customer",
            "permissions": ["oms:orders:read"],
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 60,
        },
        TEST_JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    expired_resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert expired_resp.status_code == 401

    unknown_login = await client.post("/api/auth/login", json={"account_id": "hacker_99"})
    assert unknown_login.status_code == 401

    # 4) 验证 customer_01 与 merchant_01 正常签发 JWT，且每次请求生成独立 trace_id，完全无视 Body 伪造字段
    cust_token = await _login(client, "customer_01")
    merch_token = await _login(client, "merchant_01")

    probe_1 = await client.post(
        "/api/debug/context-probe",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={
            "user_id": "merchant_root",
            "tenant_id": "tenant_other_shop",
            "role": "merchant",
            "approved": True,
            "permissions": ["bi:sql:query", "pim:sku:propose"],
        },
    )
    probe_2 = await client.post(
        "/api/debug/context-probe",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"role": "merchant"},
    )
    assert probe_1.status_code == 200 and probe_2.status_code == 200
    p1, p2 = probe_1.json(), probe_2.json()
    assert p1["trace_id"] != p2["trace_id"]
    assert p1["user_id"] == "buyer_1001"
    assert p1["tenant_id"] == "tenant_yunshang_cn"
    assert p1["role"] == "customer"
    assert p1["hitl_approvals_count"] == 0
    assert "bi:sql:query" not in p1["permissions"]

    me_merch = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {merch_token}"},
    )
    assert me_merch.status_code == 200
    assert me_merch.json()["role"] == "merchant"
    assert "pim:sku:propose" in me_merch.json()["permissions"]


# ============================================================================
# 2. C 端 SSE 流式验证：商品对比 + 查单物流 + 高危退款 HITL 挂起、403 越权拦截与审批恢复
# ============================================================================


async def test_c_domain_sse_streaming_cards_and_refund_hitl_lifecycle(
    gateway_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, _app = gateway_client
    cust01_token = await _login(client, "customer_01")
    cust02_token = await _login(client, "customer_02")
    merch01_token = await _login(client, "merchant_01")

    cust01_headers = {"Authorization": f"Bearer {cust01_token}"}

    # --- 场景 2A：C 端女装横向对比（尺码/颜色/面料）+ 查单顺丰物流轨迹全链路 SSE ---
    status_code, resp_headers, events = await collect_sse_response(
        client,
        "POST",
        "/api/chat/stream",
        headers=cust01_headers,
        json_body={
            "message": "帮我查询近期订单状态和顺丰包裹物流轨迹，并横向对比店里的连衣裙尺码颜色面料与实时库存",
            # 故意夹带伪造字段验证网关绝不采信
            "user_id": "buyer_1002",
            "role": "merchant",
            "approved": True,
        },
    )
    assert status_code == 200
    assert "text/event-stream" in resp_headers.get("content-type", "")
    event_names = [ev[0] for ev in events]
    assert "thinking" in event_names
    assert "tool_start" in event_names
    assert "tool_result" in event_names
    assert event_names[-1] == "final_answer"

    tool_starts = {
        ev[1]["tool_call_id"]: ev[1] for ev in events if ev[0] == "tool_start"
    }
    tool_results = {
        ev[1]["tool_call_id"]: ev[1] for ev in events if ev[0] == "tool_result"
    }
    assert set(tool_starts.keys()) == set(tool_results.keys())
    called_tool_names = {v["tool_name"] for v in tool_results.values()}
    assert {
        "oms__search_orders",
        "logistics__track_pkg",
        "pim__hybrid_search",
        "pim__compare_skus",
        "pim__check_stock",
    }.issubset(called_tool_names)

    final_payload = events[-1][1]
    artifacts = final_payload["artifacts"]
    assert len(artifacts["sku_comparisons"]) >= 1
    assert artifacts["sku_comparisons"][0]["compared_count"] == 3
    top_dress = artifacts["sku_comparisons"][0]["entries"][0]
    assert top_dress["specs"]["sizes"] == "XS / S / M / L / XL"
    assert top_dress["specs"]["color"] == "香槟杏"
    assert "桑蚕丝" in top_dress["specs"]["fabric"]
    # 订单仅含 buyer_1001 本人订单且隐私已脱敏
    returned_order_ids = {o["order_id"] for o in artifacts["orders"]}
    assert "ORD-20261118-2101" in returned_order_ids
    assert "ORD-20261110-2103" not in returned_order_ids
    assert artifacts["orders"][0]["buyer_phone"] == "138****5678"
    assert len(artifacts["logistics"]) == 1
    assert artifacts["logistics"][0]["tracking_no"] == "SF2026882102"

    # --- 场景 2B：C 端高危退款触发 hitl_interrupt（即便请求体伪造 approved=True 仍强制阻断）---
    r_code, _, r_events = await collect_sse_response(
        client,
        "POST",
        "/api/chat/stream",
        headers=cust01_headers,
        json_body={
            "message": "请帮我对未发货衬衫订单 ORD-20261118-2101 申请售后服务退款",
            "approved": True,
        },
    )
    assert r_code == 200
    r_event_names = [ev[0] for ev in r_events]
    assert "thinking" in r_event_names
    assert "tool_start" in r_event_names
    assert "tool_result" in r_event_names
    assert r_event_names[-1] == "hitl_interrupt"
    assert "final_answer" not in r_event_names

    hitl_ev = r_events[-1][1]
    refund_thread_id = hitl_ev["thread_id"]
    assert hitl_ev["tool_name"] == "oms__create_refund"
    assert hitl_ev["risk"] == "high"
    assert hitl_ev["refund_details"]["order_id"] == "ORD-20261118-2101"
    assert hitl_ev["refund_details"]["refund_amount"] == 399.0
    assert "重磅三醋酸垂感飘带法式衬衫" in (hitl_ev["refund_details"]["sku_name"] or "")

    # 越权验证 1：另一买家 customer_02 尝试恢复 customer_01 的退款线程 -> 403
    cross_user_res = await client.post(
        "/api/hitl/resume",
        headers={"Authorization": f"Bearer {cust02_token}"},
        json={"thread_id": refund_thread_id, "decision": "approve", "comment": "越权批准"},
    )
    assert cross_user_res.status_code == 403

    # 越权验证 2：商家角色 merchant_01 尝试跨角色直接恢复 C 端买家私人会话线程 -> 403
    cross_role_res = await client.post(
        "/api/hitl/resume",
        headers={"Authorization": f"Bearer {merch01_token}"},
        json={"thread_id": refund_thread_id, "decision": "approve", "comment": "跨角色恢复"},
    )
    assert cross_role_res.status_code == 403

    # 合法批准：customer_01 确认同意 -> 签发受信任 HITLApproval 凭证恢复流式执行并成功生成退款单
    ok_code, _, ok_events = await collect_sse_response(
        client,
        "POST",
        "/api/hitl/resume",
        headers=cust01_headers,
        json_body={
            "thread_id": refund_thread_id,
            "decision": "approve",
            "comment": "确认衬衫想换色重拍，同意原路退款",
        },
    )
    assert ok_code == 200
    ok_names = [ev[0] for ev in ok_events]
    assert "thinking" in ok_names
    assert "tool_start" in ok_names
    assert "tool_result" in ok_names
    assert ok_names[-1] == "final_answer"

    refund_tool_res = next(
        ev[1]
        for ev in ok_events
        if ev[0] == "tool_result" and ev[1]["tool_name"] == "oms__create_refund"
    )
    assert refund_tool_res["status"] == "success"
    assert refund_tool_res["data"]["refund_id"].startswith("RFD-ORD-20261118-2101")
    assert refund_tool_res["data"]["refund_amount"] == 399.0
    assert "RFD-ORD-20261118-2101" in ok_events[-1][1]["answer"]

    # 补充验证：针对另一可退女装订单发起退款后人工点击 reject -> 零副作用安全终止
    rej_code, _, rej_pause_events = await collect_sse_response(
        client,
        "POST",
        "/api/chat/stream",
        headers=cust01_headers,
        json_body={"message": "请帮我对连衣裙订单 ORD-20261115-2102 申请售后服务退款"},
    )
    assert rej_code == 200
    assert rej_pause_events[-1][0] == "hitl_interrupt"
    rej_tid = rej_pause_events[-1][1]["thread_id"]

    rej_resume_code, _, rej_resume_events = await collect_sse_response(
        client,
        "POST",
        "/api/hitl/resume",
        headers=cust01_headers,
        json_body={"thread_id": rej_tid, "decision": "reject", "comment": "误触取消申请"},
    )
    assert rej_resume_code == 200
    assert rej_resume_events[-1][0] == "final_answer"
    assert not any(
        ev[0] == "tool_result" and ev[1]["tool_name"] == "oms__create_refund"
        for ev in rej_resume_events
    )
    assert "拒绝执行" in rej_resume_events[-1][1]["answer"]


# ============================================================================
# 3. B 端 SSE 流式验证：经营诊断 -> 选品提案 -> 高危建品 HITL、C 端越权 403、落库飞轮与审计查询
# ============================================================================


async def test_b_domain_sse_pipeline_hitl_resume_flywheel_and_audit_logs(
    gateway_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, app = gateway_client
    cust01_token = await _login(client, "customer_01")
    merch01_token = await _login(client, "merchant_01")
    merch_other_token = await _login(client, "merchant_other")

    merch01_headers = {"Authorization": f"Bearer {merch01_token}"}
    cust01_headers = {"Authorization": f"Bearer {cust01_token}"}

    # --- 阶段 3A：启动 B 端退货异常归因与新品建档流水线，直至 hitl_interrupt ---
    b_code, b_headers, b_events = await collect_sse_response(
        client,
        "POST",
        "/api/chat/stream",
        headers=merch01_headers,
        json_body={
            "message": "诊断近期店铺退货率异常偏高的核心品类与退货原因，结合供应链到岸成本测算给出选品提案并提交新品建档草稿",
        },
    )
    assert b_code == 200
    b_trace_id = b_headers["x-trace-id"]
    b_event_names = [ev[0] for ev in b_events]
    assert "thinking" in b_event_names
    assert "tool_start" in b_event_names
    assert "tool_result" in b_event_names
    assert b_event_names[-1] == "hitl_interrupt"

    b_tools_called = [
        ev[1]["tool_name"] for ev in b_events if ev[0] == "tool_result"
    ]
    assert "bi__get_table_schema" in b_tools_called
    assert "bi__run_readonly_sql" in b_tools_called
    assert "market_erp__search_trends" in b_tools_called
    assert "market_erp__calc_landed_cost" in b_tools_called
    assert b_tools_called[-1] == "pim__draft_sku_proposal"

    b_hitl = b_events[-1][1]
    b_thread_id = b_hitl["thread_id"]
    assert b_hitl["domain"] == "merchant"
    assert b_hitl["tool_name"] == "pim__draft_sku_proposal"
    assert b_hitl["risk"] == "high"
    assert b_hitl["diagnosis_artifact"]["problem_category"] == "连衣裙"
    proposal = b_hitl["selection_proposal"]
    assert proposal is not None
    assert len(proposal["candidate_skus"]) >= 1
    assert proposal["estimated_gross_margin"] > 0.50
    assert len(proposal["core_selling_points"]) >= 3
    assert len(proposal["risk_warnings"]) >= 1

    # --- 阶段 3B：C 端买家 customer_01 与跨租户商家尝试越权恢复 B 端挂起线程 -> 严格返回 403 ---
    c_hijack_b = await client.post(
        "/api/hitl/resume",
        headers=cust01_headers,
        json={"thread_id": b_thread_id, "decision": "approve", "comment": "C端试图越权批B端立项"},
    )
    assert c_hijack_b.status_code == 403

    other_tenant_hijack = await client.post(
        "/api/hitl/resume",
        headers={"Authorization": f"Bearer {merch_other_token}"},
        json={"thread_id": b_thread_id, "decision": "approve", "comment": "跨租户越权"},
    )
    assert other_tenant_hijack.status_code == 403

    # --- 阶段 3C：B 端 merchant_01 批准立项 -> 流式建档落库并同步写回卖点知识库飞轮 ---
    res_code, res_headers, res_events = await collect_sse_response(
        client,
        "POST",
        "/api/hitl/resume",
        headers=merch01_headers,
        json_body={
            "thread_id": b_thread_id,
            "decision": "approve",
            "comment": "毛利率与全压胶升级方案符合冬季企划标准，准予立项试销",
        },
    )
    assert res_code == 200
    resume_trace_id = res_headers["x-trace-id"]
    res_names = [ev[0] for ev in res_events]
    assert "thinking" in res_names
    assert "tool_start" in res_names
    assert "tool_result" in res_names
    assert res_names[-1] == "final_answer"

    draft_tool_res = next(
        ev[1]
        for ev in res_events
        if ev[0] == "tool_result" and ev[1]["tool_name"] == "pim__draft_sku_proposal"
    )
    assert draft_tool_res["status"] == "success"
    assert draft_tool_res["data"]["proposal_id"].startswith("PROP-")
    assert draft_tool_res["data"]["knowledge_base_synced"] is True

    final_b = res_events[-1][1]
    assert final_b["artifacts"]["committed_proposal"]["flywheel_kb_verified"] is True
    assert "审批通过并落库" in final_b["answer"]

    # 额外核验 B 端 reject 分支：新开线程挂起后驳回立项，安全终止零落库
    rej_b_code, _, rej_b_events = await collect_sse_response(
        client,
        "POST",
        "/api/chat/stream",
        headers=merch01_headers,
        json_body={"message": "请再次执行退货异常品类归因并生成选品提案草稿"},
    )
    assert rej_b_code == 200
    assert rej_b_events[-1][0] == "hitl_interrupt"
    rej_b_tid = rej_b_events[-1][1]["thread_id"]

    rej_b_res_code, _, rej_b_res_events = await collect_sse_response(
        client,
        "POST",
        "/api/hitl/resume",
        headers=merch01_headers,
        json_body={"thread_id": rej_b_tid, "decision": "reject", "comment": "本批次暂缓上架"},
    )
    assert rej_b_res_code == 200
    assert rej_b_res_events[-1][0] == "final_answer"
    assert rej_b_res_events[-1][1]["artifacts"]["approval_status"] == "rejected"
    assert rej_b_res_events[-1][1]["artifacts"]["committed_proposal"] is None
    assert "人工审批已驳回" in rej_b_res_events[-1][1]["answer"]

    # --- 阶段 3D：触发一次 C 端越权改他人订单操作，并验证 /api/audit/logs 全要素与租户/本人隔离 ---
    await collect_sse_response(
        client,
        "POST",
        "/api/chat/stream",
        headers=cust01_headers,
        json_body={
            "message": "帮我把订单 ORD-20261110-2103 的收货地址改为北京市海淀区学院路1号",
        },
    )

    # 商家视角查询本租户审计日志（包含 hitl_required、permission_error 拦截与耗时毫秒数）
    merch_audit_resp = await client.get("/api/audit/logs?limit=200", headers=merch01_headers)
    assert merch_audit_resp.status_code == 200
    merch_logs = merch_audit_resp.json()["logs"]
    assert len(merch_logs) >= 10

    for entry in merch_logs:
        assert entry["tenant_id"] == "tenant_yunshang_cn"
        assert entry["trace_id"].startswith("trc_")
        assert len(entry["tool_call_id"]) > 0
        assert entry["duration_ms"] >= 0.0

    # 按首次 B 端挂起 trace_id 精确过滤验证 hitl_required 风控拦截记录
    hitl_trace_resp = await client.get(
        f"/api/audit/logs?trace_id={b_trace_id}",
        headers=merch01_headers,
    )
    assert hitl_trace_resp.status_code == 200
    hitl_entries = [
        item for item in hitl_trace_resp.json()["logs"] if item["status"] == "hitl_required"
    ]
    assert len(hitl_entries) == 1
    assert hitl_entries[0]["tool_name"] == "pim__draft_sku_proposal"
    assert hitl_entries[0]["intercepted"] is True
    assert hitl_entries[0]["error_code"] == "HITL_APPROVAL_REQUIRED"

    # 按审批恢复 trace_id 过滤验证放行成功记录
    approved_trace_resp = await client.get(
        f"/api/audit/logs?trace_id={resume_trace_id}",
        headers=merch01_headers,
    )
    assert approved_trace_resp.status_code == 200
    assert any(
        item["tool_name"] == "pim__draft_sku_proposal" and item["status"] == "success"
        for item in approved_trace_resp.json()["logs"]
    )

    # 验证 C 端买家越权改他人订单触发的 permission_error 被完整审计记录
    assert any(
        item["tool_name"] == "oms__update_address"
        and item["status"] == "error"
        and item["error_type"] == "permission_error"
        and item["error_code"] == "ORDER_OWNERSHIP_DENIED"
        for item in merch_logs
    )

    # 验证 C 端买家调用 /api/audit/logs 仅可见 buyer_1001 本人记录，绝不可见商家 BI/选品审计日志
    cust_audit_resp = await client.get("/api/audit/logs?limit=200", headers=cust01_headers)
    assert cust_audit_resp.status_code == 200
    cust_logs = cust_audit_resp.json()["logs"]
    assert len(cust_logs) >= 1
    assert all(
        item["user_id"] == "buyer_1001" and item["role"] == "customer"
        for item in cust_logs
    )
    assert not any(item["tool_name"].startswith(("bi__", "market_erp__")) for item in cust_logs)

    # 验证跨租户商家查询不到任何 tenant_yunshang_cn 审计记录
    other_audit_resp = await client.get(
        "/api/audit/logs?limit=200",
        headers={"Authorization": f"Bearer {merch_other_token}"},
    )
    assert other_audit_resp.status_code == 200
    assert other_audit_resp.json()["logs"] == []
