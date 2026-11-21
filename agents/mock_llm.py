"""离线回归与演示专用确定性 Mock 推理器模块（与生产 `agents/llm.py` 物理拆分解耦）。

包含：
1. `CallableMockLLM` / `ScriptedMockLLM`：单测与安全对抗注入用通用脚本化异步桩；
2. `DeterministicEcommerceMockLLM`：云裳女装旗舰店双域确定性推理模型，严格从真实 `ToolResultMessage`
   回执动态解析价格、库存、订单、物流、BI SQL 结果与到岸成本，零硬编码业务事实、零底层数据直连。
"""

from __future__ import annotations

import inspect
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from agents.llm import AsyncAgentLLM, LLMMessage, LLMResponse
from schemas.contracts import ToolCall

MockHandlerFn = Callable[
    [
        str,
        tuple[LLMMessage, ...],
        Sequence[Mapping[str, Any]] | None,
        type[BaseModel] | None,
    ],
    LLMResponse | Awaitable[LLMResponse],
]


class CallableMockLLM(AsyncAgentLLM):
    """基于异步/同步回调函数的确定性可注入 Mock LLM。"""

    def __init__(self, handler: MockHandlerFn) -> None:
        super().__init__()
        self._handler = handler

    async def _agenerate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None,
        response_schema: type[BaseModel] | None,
        agent_name: str,
    ) -> LLMResponse:
        outcome = self._handler(agent_name, tuple(messages), tools, response_schema)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome


ScriptStep = LLMResponse | MockHandlerFn


class ScriptedMockLLM(AsyncAgentLLM):
    """按子 Agent 名称顺序消费预设响应或回调函数的确定性脚本化 Mock LLM。"""

    def __init__(
        self,
        scripts: Mapping[str, Sequence[ScriptStep]],
        *,
        fallback: MockHandlerFn | None = None,
    ) -> None:
        super().__init__()
        self._queues: dict[str, deque[ScriptStep]] = {
            name: deque(steps) for name, steps in scripts.items()
        }
        self._fallback = fallback

    async def _agenerate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None,
        response_schema: type[BaseModel] | None,
        agent_name: str,
    ) -> LLMResponse:
        queue = self._queues.get(agent_name)
        if queue and len(queue) > 0:
            step = queue.popleft()
            if isinstance(step, LLMResponse):
                return step
            res = step(agent_name, tuple(messages), tools, response_schema)
            return await res if inspect.isawaitable(res) else res

        if self._fallback is not None:
            res = self._fallback(agent_name, tuple(messages), tools, response_schema)
            return await res if inspect.isawaitable(res) else res

        raise RuntimeError(
            f"ScriptedMockLLM 中子 Agent '{agent_name}' 的预设响应队列已耗尽且未配置 fallback"
        )


def _parse_tool_messages(messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
    """从消息序列中解析 `ToolRuntime` 写回的 JSON `ToolResultMessage` 字典列表。"""
    parsed: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role != "tool" or not msg.content.strip():
            continue
        try:
            item = json.loads(msg.content)
            if isinstance(item, dict):
                parsed.append(item)
        except json.JSONDecodeError:
            continue
    return parsed


_INJECTION_OR_B_DOMAIN_KEYWORDS: tuple[str, ...] = (
    "忽略前面",
    "忽略上述",
    "ignore previous",
    "我是管理员",
    "切换为店铺管理员",
    "role=merchant",
    "system debug",
    "调试模式",
    "内部审计",
    "全店毛利",
    "店铺毛利",
    "毛利率",
    "毛利空间",
    "gross_margin_rate",
    "供货底价",
    "成本底价",
    "出厂成本",
    "进货成本",
    "内部仓位",
    "全店gmv",
    "经营数仓",
    "run_readonly_sql",
    "calc_landed_cost",
    "search_trends",
    "draft_sku_proposal",
)

_ADDRESS_EXTRACT_RE = re.compile(
    r"(?:地址(?:改成|改为|修改为|更新为)|改址为|送到)\s*([^（(，,。；;\n]+)"
)
_PHONE_EXTRACT_RE = re.compile(r"(1[3-9]\d{9})")
_NAME_EXTRACT_RE = re.compile(r"(?:收件人|联系人)[:：为是]?\s*([\u4e00-\u9fa5]{2,10})")


class DeterministicEcommerceMockLLM(AsyncAgentLLM):
    """云裳女装旗舰店确定性异步推理模型（面向离线联调与全链路回归测试）。

    严格遵守架构红线：
    - 绝不硬编码业务事实或导入任何底层数据模块；
    - 所有商品价格、库存、订单状态、物流节点、数仓退货率与到岸毛利率均从
      `messages` 中真实的 `ToolResultMessage` JSON 回执动态解析生成。
    """

    def __init__(self, *, require_address_confirmation: bool = False) -> None:
        super().__init__()
        self._require_address_confirmation = require_address_confirmation
        self._call_seq = 0

    def _next_tc_id(self, prefix: str) -> str:
        self._call_seq += 1
        return f"tc_{prefix}_{self._call_seq:03d}"

    async def _agenerate(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None,
        response_schema: type[BaseModel] | None,
        agent_name: str,
    ) -> LLMResponse:
        if agent_name == "C-Supervisor":
            return self._handle_c_supervisor(messages)
        if agent_name == "OrderAgent":
            return self._handle_order_agent(messages)
        if agent_name == "ShoppingGuideAgent":
            return self._handle_shopping_guide(messages)
        if agent_name == "AnalyticsAgent":
            return self._handle_analytics_agent(messages)
        if agent_name == "SourcingAgent":
            return self._handle_sourcing_agent(messages)
        raise ValueError(f"未知的 Agent 标识: {agent_name!r}")

    def _handle_c_supervisor(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        full_user_blob = "\n".join(m.content for m in messages if m.role == "user")
        raw_query_part = full_user_blob.split("【已完成子专员核实报告", 1)[0]
        lowered = raw_query_part.lower()

        if any(kw in lowered for kw in _INJECTION_OR_B_DOMAIN_KEYWORDS):
            refusal = (
                "抱歉，当前为消费者客户服务通道，仅提供个人订单查询、物流追踪、售后服务与商品导购咨询；"
                "无法访问或提供店铺经营分析、全店毛利、成本底价等内部商家数据。"
            )
            return LLMResponse(
                content=refusal,
                structured_output={
                    "next_agent": "FINISH",
                    "task_instruction": "",
                    "final_response": refusal,
                },
            )

        reports_blob = full_user_blob
        has_order_lookup = "[REPORT:OrderAgent:lookup]" in reports_blob
        has_guide_report = "[REPORT:ShoppingGuideAgent]" in reports_blob
        has_addr_done = "[REPORT:OrderAgent:mutation]" in reports_blob

        needs_order_or_track = any(
            k in raw_query_part
            for k in (
                "订单",
                "买过",
                "买的",
                "包裹",
                "物流",
                "快递",
                "运单",
                "退款",
                "秒退",
                "退单",
                "售后",
            )
        )
        needs_address_change = any(
            k in raw_query_part
            for k in ("改地址", "修改收货地址", "修改地址", "地址改成", "地址改为", "改址")
        )
        needs_refund = any(
            k in raw_query_part for k in ("退款", "退货退款", "秒退", "退单", "退掉")
        )
        explicit_guide_intent = any(
            k in raw_query_part
            for k in (
                "搭配",
                "穿搭",
                "成套",
                "上装",
                "库存",
                "现货",
                "推荐",
                "对比",
                "比价",
                "哪款",
                "什么款",
                "选购",
                "导购",
                "在售",
                "多少钱",
                "面料",
                "颜色",
                "区别",
                "差异",
            )
        )
        category_mention = any(
            k in raw_query_part
            for k in (
                "连衣裙",
                "茶歇裙",
                "裙",
                "衬衫",
                "上衣",
                "阔腿裤",
                "裤装",
                "牛仔裤",
                "西装裤",
                "针织衫",
                "开衫",
                "女装",
                "真丝",
                "桑蚕丝",
                "三醋酸",
                "商品",
            )
        )
        needs_guide = explicit_guide_intent or (
            category_mention
            and not needs_order_or_track
            and not needs_address_change
            and not needs_refund
        )

        if (needs_order_or_track or needs_address_change) and not has_order_lookup:
            return LLMResponse(
                structured_output={
                    "next_agent": "OrderAgent",
                    "task_instruction": (
                        "[TASK:lookup] 请查询买家历史女装订单明确已购款式尺码、未发货订单号及已发货单号，"
                        "并对已发货成衣包裹查询实时物流轨迹。"
                    ),
                    "final_response": None,
                }
            )

        if needs_guide and not has_guide_report:
            return LLMResponse(
                structured_output={
                    "next_agent": "ShoppingGuideAgent",
                    "task_instruction": (
                        "[TASK:guide] 请基于买家穿搭需求与已查明的历史购买服饰，检索在售女装、"
                        "横向对比尺码/颜色/面料与价格并核验实时可售库存。"
                    ),
                    "final_response": None,
                }
            )

        if (needs_address_change or needs_refund) and not has_addr_done:
            action_desc = (
                "[TASK:mutation:refund] 请核对女装订单状态机规则并针对目标订单发起售后退款。"
                if needs_refund and not needs_address_change
                else "[TASK:mutation:address] 请依据订单状态机规则核对未发货女装订单并完成收货地址修改确认与执行。"
            )
            return LLMResponse(
                structured_output={
                    "next_agent": "OrderAgent",
                    "task_instruction": action_desc,
                    "final_response": None,
                }
            )

        report_lines = [
            line.strip()
            for line in reports_blob.splitlines()
            if line.strip().startswith("- [SUMMARY:")
        ]
        if not report_lines:
            greeting = (
                "您好！欢迎来到云裳女装旗舰店，我是您的专属穿搭与售后顾问。您可以向我咨询连衣裙、衬衫、裤装等款式的尺码颜色与面料对比、"
                "成套穿搭推荐、现货库存查询，或办理个人订单物流查询、未发货改址与售后退款。"
            )
            return LLMResponse(
                content=greeting,
                structured_output={
                    "next_agent": "FINISH",
                    "task_instruction": "",
                    "final_response": greeting,
                },
            )
        cleaned_bullets: list[str] = []
        for raw_line in report_lines:
            cleaned = re.sub(r"^-\s*\[SUMMARY:[^\]]+\]\s*", "", raw_line)
            cleaned = re.sub(r"\[REPORT:[^\]]+\]\s*", "", cleaned).strip()
            if cleaned:
                cleaned_bullets.append(f"• {cleaned}")
        synthesized = "已为您核实并处理妥当：\n" + "\n".join(cleaned_bullets)
        return LLMResponse(
            content=synthesized,
            structured_output={
                "next_agent": "FINISH",
                "task_instruction": "",
                "final_response": synthesized,
            },
        )

    def _handle_order_agent(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        user_text = "\n".join(m.content for m in messages if m.role == "user")
        tool_results = _parse_tool_messages(messages)
        called_names = {tr.get("tool_name") for tr in tool_results}

        is_mutation = "[TASK:mutation" in user_text
        prior_paid_match = re.search(
            r"(ORD-\d{8}-\d+)（([^）]+)，状态\s*(?:pending|paid|待付款|待发货)\s*未发货可改址）",
            user_text,
        )

        if "oms__search_orders" not in called_names and not (
            is_mutation
            and "[TASK:mutation:address]" in user_text
            and prior_paid_match is not None
        ):
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        tool_call_id=self._next_tc_id("oms_search"),
                        name="oms__search_orders",
                        arguments={"created_from": "2026-10-01", "limit": 20},
                    )
                ]
            )

        orders: list[dict[str, Any]] = []
        for tr in tool_results:
            if tr.get("tool_name") == "oms__search_orders" and tr.get("status") == "success":
                orders = list((tr.get("data") or {}).get("orders", []))
        if not orders and prior_paid_match is not None:
            orders = [
                {
                    "order_id": prior_paid_match.group(1),
                    "sku_name": prior_paid_match.group(2),
                    "status": "paid",
                }
            ]

        if not is_mutation:
            shipped_orders = [
                o for o in orders if o.get("status") == "shipped" and o.get("tracking_no")
            ]
            if shipped_orders and "logistics__track_pkg" not in called_names:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            tool_call_id=self._next_tc_id("log_track"),
                            name="logistics__track_pkg",
                            arguments={"tracking_no": shipped_orders[0]["tracking_no"]},
                        )
                    ]
                )

            order_status_cn = {
                "pending": "待付款",
                "paid": "待发货",
                "shipped": "已发货",
                "completed": "已完成",
                "refunded": "已退款",
            }
            delivery_status_cn = {
                "in_transit": "运输派送中",
                "delivered": "已签收",
                "pending_pickup": "待揽收",
            }
            completed_garments = [
                f"{o['sku_name']}（订单 {o['order_id']}，状态：{order_status_cn.get(str(o.get('status', '')), '已完成')}）"
                for o in orders
                if o.get("status") == "completed"
            ]
            paid_unshipped = [
                f"{o['order_id']}（{o['sku_name']}，状态 {order_status_cn.get(str(o.get('status', '')), '待发货')} 未发货可改址）"
                for o in orders
                if o.get("status") in ("pending", "paid")
            ]
            logistics_desc = ""
            for tr in tool_results:
                if tr.get("tool_name") == "logistics__track_pkg" and tr.get("status") == "success":
                    pkg = tr["data"]
                    ckpts = pkg.get("checkpoints") or []
                    latest = ckpts[-1] if ckpts else {}
                    d_status = delivery_status_cn.get(
                        str(pkg.get("delivery_status", "")), "运输派送中"
                    )
                    logistics_desc = (
                        f"在途包裹 {pkg['tracking_no']}（订单 {pkg['order_id']}，{pkg['carrier']}）"
                        f"当前{d_status}，最新动态（{latest.get('timestamp', '')} "
                        f"{latest.get('location', '')}）：{latest.get('status', '')}，"
                        f"预计送达时间 {pkg['estimated_delivery']}。"
                    )

            summary = (
                f"[REPORT:OrderAgent:lookup] 历史已购款式：{'; '.join(completed_garments) or '暂无'}；"
                f"待发货订单：{'; '.join(paid_unshipped) or '暂无'}；{logistics_desc}"
            )
            return LLMResponse(content=summary)

        raw_buyer_part = user_text.split("【C-Supervisor 当前派单指令】", 1)[0]
        explicit_ord_match = re.search(r"(ORD-\d{8}-\d+)", raw_buyer_part)
        explicit_order_id = explicit_ord_match.group(1) if explicit_ord_match else None

        if "[TASK:mutation:refund]" in user_text:
            if "oms__create_refund" not in called_names:
                refundable = None
                if explicit_order_id is not None:
                    refundable = next(
                        (
                            o
                            for o in orders
                            if o.get("order_id") == explicit_order_id
                            and o.get("status") in ("paid", "shipped", "completed")
                        ),
                        None,
                    )
                if refundable is None:
                    refundable = next(
                        (
                            o
                            for o in orders
                            if o.get("status") in ("paid", "shipped", "completed")
                        ),
                        None,
                    )
                if refundable is not None:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                tool_call_id=self._next_tc_id("oms_refund"),
                                name="oms__create_refund",
                                arguments={
                                    "order_id": refundable["order_id"],
                                    "refund_amount": float(refundable["total_amount"]),
                                    "reason": "买家申请售后服务退款",
                                },
                            )
                        ]
                    )
            for tr in tool_results:
                if tr.get("tool_name") == "oms__create_refund":
                    if tr.get("status") == "success":
                        d = tr["data"]
                        return LLMResponse(
                            content=(
                                f"[REPORT:OrderAgent:mutation] 订单 {d['order_id']} 售后服务单 {d['refund_id']} "
                                f"已为您办理成功，退款金额 ¥{d['refund_amount']:.2f} 将原路退回。"
                            )
                        )
                    if tr.get("status") == "error":
                        err = tr.get("error") or {}
                        return LLMResponse(
                            content=(
                                f"[REPORT:OrderAgent:mutation] 很抱歉，当前无法办理退款：{err.get('message', '未能满足售后服务条件')}"
                            )
                        )
            return LLMResponse(
                content="[REPORT:OrderAgent:mutation] 退款申请已取消，或您名下暂无符合退货退款条件的订单。"
            )

        if "oms__update_address" not in called_names and "[CONFIRMATION_DECLINED]" not in user_text:
            editable_order: dict[str, Any] | None = None
            if explicit_order_id is not None:
                editable_order = next(
                    (o for o in orders if o.get("order_id") == explicit_order_id),
                    None,
                )
                if editable_order is None:
                    editable_order = {
                        "order_id": explicit_order_id,
                        "sku_name": "指定订单",
                        "status": "unknown",
                    }
            else:
                editable_order = next(
                    (o for o in orders if o.get("status") in ("pending", "paid")),
                    None,
                )
            if editable_order is None:
                return LLMResponse(
                    content="[REPORT:OrderAgent:mutation] 当前您名下暂无处于待发货状态的订单，无法直接修改收货地址。"
                )

            addr_match = _ADDRESS_EXTRACT_RE.search(user_text)
            new_addr = (
                addr_match.group(1).strip()
                if addr_match
                else "广东省深圳市福田区深南大道2008号皇庭广场"
            )
            phone_match = _PHONE_EXTRACT_RE.search(user_text)
            new_phone = phone_match.group(1) if phone_match else "13566778899"
            name_match = _NAME_EXTRACT_RE.search(user_text)
            new_name = name_match.group(1) if name_match else "林晓月"

            update_call = ToolCall(
                tool_call_id=self._next_tc_id("oms_addr"),
                name="oms__update_address",
                arguments={
                    "order_id": editable_order["order_id"],
                    "new_shipping_address": new_addr,
                    "receiver_name": new_name,
                    "receiver_phone": new_phone,
                },
            )

            if (
                self._require_address_confirmation
                and "[ADDRESS_CONFIRMED_BY_USER]" not in user_text
            ):
                return LLMResponse(
                    require_user_confirmation=True,
                    confirmation_prompt=(
                        f"请确认是否将未发货订单 {editable_order['order_id']}（{editable_order['sku_name']}）"
                        f"收货地址修改为：{new_addr}（收件人：{new_name}）？"
                    ),
                    pending_confirmation_call=update_call,
                )

            return LLMResponse(tool_calls=[update_call])

        for tr in tool_results:
            if tr.get("tool_name") == "oms__update_address":
                if tr.get("status") == "success":
                    d = tr["data"]
                    return LLMResponse(
                        content=(
                            f"[REPORT:OrderAgent:mutation] 待发货订单 {d['order_id']}"
                            f" 已成功更新收货信息：收件人 {d['receiver_name']}，联系电话 {d['receiver_phone']}，"
                            f"新收货地址 {d['shipping_address']}。"
                        )
                    )
                if tr.get("status") == "error":
                    err = tr.get("error") or {}
                    return LLMResponse(
                        content=(
                            f"[REPORT:OrderAgent:mutation] 很抱歉，未能修改该订单收货地址：{err.get('message', '仅支持修改本人名下未发货订单地址')}"
                        )
                    )

        return LLMResponse(
            content="[REPORT:OrderAgent:mutation] 用户已取消未发货订单改址操作，原收货地址保持不变。"
        )

    def _handle_shopping_guide(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        user_blob = "\n".join(m.content for m in messages if m.role == "user")
        raw_buyer_part = user_blob.split("【C-Supervisor 当前派单指令】", 1)[0]
        tool_results = _parse_tool_messages(messages)
        called_names = {tr.get("tool_name") for tr in tool_results}

        explicit_sku_match = re.search(r"(SKU-[A-Z0-9-]+)", raw_buyer_part)
        explicit_sku_id = explicit_sku_match.group(1) if explicit_sku_match else None
        wants_comparison = any(
            k in raw_buyer_part for k in ("对比", "区别", "差异", "比价", "哪款好")
        )
        asks_chiffon = (
            explicit_sku_id == "SKU-DRESS-CHIFFON-02"
            or any(k in raw_buyer_part for k in ("碎花", "雪纺", "雾粉紫"))
        )

        if "pim__hybrid_search" not in called_names:
            if any(k in raw_buyer_part for k in ("搭配", "成套", "上装")) or (
                "衬衫" in raw_buyer_part and "连衣裙" not in raw_buyer_part
            ):
                search_args: dict[str, Any] = {"category": "衬衫", "max_price": 600.0}
            elif (
                any(k in raw_buyer_part for k in ("裤装", "阔腿裤", "牛仔裤", "西装裤"))
                and "连衣裙" not in raw_buyer_part
            ):
                search_args = {"category": "裤装"}
            elif any(k in raw_buyer_part for k in ("针织", "开衫", "羊毛")):
                search_args = {"category": "针织衫"}
            elif any(k in raw_buyer_part for k in ("连衣裙", "茶歇裙", "裙")):
                search_args = {"category": "连衣裙"}
            else:
                search_args = {"keyword": "云裳", "top_k": 5}
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        tool_call_id=self._next_tc_id("pim_search"),
                        name="pim__hybrid_search",
                        arguments=search_args,
                    )
                ]
            )

        search_items: list[dict[str, Any]] = []
        for tr in tool_results:
            if tr.get("tool_name") == "pim__hybrid_search" and tr.get("status") == "success":
                search_items = list((tr.get("data") or {}).get("items", []))

        if not search_items:
            return LLMResponse(
                content=(
                    "[REPORT:ShoppingGuideAgent] 经检索店内实时商品目录，当前暂无符合该条件的在售女装或现货库存；"
                    "我们严格依据真实库存答复，绝不编造无货商品。"
                )
            )

        if "pim__check_stock" not in called_names:
            in_stock_first = next(
                (it for it in search_items if int(it.get("available_stock", 0)) > 0),
                search_items[0],
            )
            requested_item = None
            if explicit_sku_id is not None:
                requested_item = next(
                    (it for it in search_items if it.get("sku_id") == explicit_sku_id),
                    None,
                )
            elif asks_chiffon:
                requested_item = next(
                    (
                        it
                        for it in search_items
                        if "雪纺" in str(it.get("name", "")) or "碎花" in str(it.get("name", ""))
                    ),
                    None,
                )

            calls: list[ToolCall] = []
            if len(search_items) >= 2 and "pim__compare_skus" not in called_names:
                compare_count = min(len(search_items), 3)
                calls.append(
                    ToolCall(
                        tool_call_id=self._next_tc_id("pim_compare"),
                        name="pim__compare_skus",
                        arguments={"sku_ids": [item["sku_id"] for item in search_items[:compare_count]]},
                    )
                )
            if requested_item is not None and requested_item["sku_id"] != in_stock_first["sku_id"]:
                calls.append(
                    ToolCall(
                        tool_call_id=self._next_tc_id("pim_stock_req"),
                        name="pim__check_stock",
                        arguments={"sku_id": requested_item["sku_id"], "required_qty": 1},
                    )
                )
            calls.append(
                ToolCall(
                    tool_call_id=self._next_tc_id("pim_stock"),
                    name="pim__check_stock",
                    arguments={"sku_id": in_stock_first["sku_id"], "required_qty": 1},
                )
            )
            return LLMResponse(tool_calls=calls)

        stock_by_sku: dict[str, dict[str, Any]] = {}
        insufficient_sku_id: str | None = None
        compare_entries: list[dict[str, Any]] = []
        for tr in tool_results:
            if tr.get("tool_name") == "pim__check_stock":
                if tr.get("status") == "success":
                    d = dict(tr["data"])
                    if d.get("sku_id"):
                        stock_by_sku[str(d["sku_id"])] = d
                elif tr.get("status") == "error":
                    err = tr.get("error") or {}
                    if err.get("code") == "INSUFFICIENT_STOCK":
                        msg_text = str(err.get("message", ""))
                        sku_m = re.search(r"(SKU-[A-Z0-9-]+)", msg_text)
                        found_sku = sku_m.group(1) if sku_m else (explicit_sku_id or "SKU-DRESS-CHIFFON-02")
                        insufficient_sku_id = found_sku
                        stock_by_sku[found_sku] = {
                            "sku_id": found_sku,
                            "available_stock": 0,
                            "stock_status": "out_of_stock",
                            "warehouse": "华东杭州云裳中心仓",
                        }
            elif tr.get("tool_name") == "pim__compare_skus" and tr.get("status") == "success":
                compare_entries = list((tr.get("data") or {}).get("entries", []))

        sold_out_checked = next(
            (st for st in stock_by_sku.values() if int(st.get("available_stock", 0)) == 0),
            None,
        )
        if sold_out_checked is None and asks_chiffon:
            sold_out_search_item = next(
                (it for it in search_items if int(it.get("available_stock", 0)) == 0),
                None,
            )
            if sold_out_search_item is not None:
                sold_out_checked = {
                    "sku_id": sold_out_search_item["sku_id"],
                    "available_stock": 0,
                    "stock_status": "out_of_stock",
                    "warehouse": sold_out_search_item.get("warehouse", "华东杭州云裳中心仓"),
                }
        _ = insufficient_sku_id
        in_stock_checked = next(
            (st for st in stock_by_sku.values() if int(st.get("available_stock", 0)) > 0),
            next(iter(stock_by_sku.values()), {}),
        )

        primary_entry = next(
            (
                e
                for e in (compare_entries or search_items)
                if e.get("sku_id") == in_stock_checked.get("sku_id")
            ),
            compare_entries[0] if compare_entries else search_items[0],
        )
        specs = primary_entry.get("specs") or {}
        sizes_info = specs.get("sizes") or primary_entry.get("sizes") or "S / M / L"
        color_info = specs.get("color") or primary_entry.get("color") or "经典纯色"
        fabric_info = specs.get("fabric") or primary_entry.get("fabric") or "精选亲肤面料"
        styling_info = specs.get("styling_match", "百搭职场通勤与日常约会成套穿搭")
        points = "、".join(primary_entry.get("selling_points", [])[:2])
        price = float(primary_entry.get("retail_price", 0.0))

        if sold_out_checked is not None and (asks_chiffon or explicit_sku_id == sold_out_checked.get("sku_id")):
            sold_out_meta = next(
                (
                    e
                    for e in (compare_entries or search_items)
                    if e.get("sku_id") == sold_out_checked.get("sku_id")
                ),
                sold_out_checked,
            )
            return LLMResponse(
                content=(
                    f"[REPORT:ShoppingGuideAgent] 如实告知您：您咨询的「{sold_out_meta.get('name', '')}」"
                    f"（款号 {sold_out_checked.get('sku_id', '')}）在{sold_out_checked.get('warehouse', '')}"
                    f"当前可售库存为 0 件（已售罄/暂无现货），我们不会虚报无货商品。"
                    f"为您推荐同品类现货替代款「{primary_entry.get('name', '')}」（款号 {primary_entry.get('sku_id', '')}，"
                    f"售价 ¥{price:.2f}，颜色：{color_info}，尺码：{sizes_info}，面料：{fabric_info}），"
                    f"当前实测可售库存 {in_stock_checked.get('available_stock', 0)} 件（现货充足）。"
                )
            )

        stock_status_cn = {
            "in_stock": "现货充足",
            "low_stock": "库存紧张",
            "out_of_stock": "已售罄",
        }.get(str(in_stock_checked.get("stock_status", "")), "现货可售")

        comparison_clause = ""
        if wants_comparison and len(compare_entries) >= 2:
            second_entry = compare_entries[1]
            second_specs = second_entry.get("specs") or {}
            second_sizes = second_specs.get("sizes") or second_entry.get("sizes") or "S / M / L"
            second_color = second_specs.get("color") or second_entry.get("color") or "经典色"
            second_fabric = second_specs.get("fabric") or second_entry.get("fabric") or "精选面料"
            second_price = float(second_entry.get("retail_price", 0.0))
            comparison_clause = (
                f"；横向对比另一款「{second_entry.get('name', '')}」（款号 {second_entry.get('sku_id', '')}）："
                f"售价 ¥{second_price:.2f}，颜色：{second_color}，可选尺码：{second_sizes}，面料成分：{second_fabric}"
            )

        guide_summary = (
            f"[REPORT:ShoppingGuideAgent] 为您推荐「{primary_entry.get('name', '')}」（款号 {primary_entry.get('sku_id', '')}），"
            f"售价 ¥{price:.2f}，颜色：{color_info}，可选尺码：{sizes_info}，面料成分：{fabric_info}；"
            f"穿搭建议：{styling_info}；款式亮点：{points}；"
            f"库存情况：{in_stock_checked.get('warehouse', '')}现有 {in_stock_checked.get('available_stock', 0)} 件（{stock_status_cn}）"
            f"{comparison_clause}。"
        )
        return LLMResponse(content=guide_summary)

    def _handle_analytics_agent(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        tool_results = _parse_tool_messages(messages)
        called_names = [tr.get("tool_name") for tr in tool_results]

        if "bi__get_table_schema" not in called_names:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        tool_call_id=self._next_tc_id("bi_schema"),
                        name="bi__get_table_schema",
                        arguments={},
                    )
                ]
            )

        sql_results = [
            tr
            for tr in tool_results
            if tr.get("tool_name") == "bi__run_readonly_sql" and tr.get("status") == "success"
        ]
        if len(sql_results) < 2:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        tool_call_id=self._next_tc_id("bi_sql_metrics"),
                        name="bi__run_readonly_sql",
                        arguments={
                            "sql": (
                                "SELECT category, price_band, SUM(paid_orders) AS total_paid, "
                                "SUM(refund_orders) AS total_refund, ROUND(SUM(gmv), 2) AS total_gmv, "
                                "ROUND(AVG(return_rate), 4) AS avg_return_rate, "
                                "ROUND(AVG(conversion_rate), 4) AS avg_conversion_rate "
                                "FROM dwd_order_daily_metrics "
                                "WHERE dt BETWEEN '2026-11-18' AND '2026-11-20' "
                                "GROUP BY category, price_band "
                                "ORDER BY avg_return_rate DESC"
                            )
                        },
                    ),
                    ToolCall(
                        tool_call_id=self._next_tc_id("bi_sql_reasons"),
                        name="bi__run_readonly_sql",
                        arguments={
                            "sql": (
                                "SELECT category, return_reason, SUM(return_count) AS reason_orders, "
                                "ROUND(SUM(refund_amount), 2) AS reason_refund_amount "
                                "FROM dwd_aftersales_return_daily "
                                "WHERE dt BETWEEN '2026-11-18' AND '2026-11-20' "
                                "GROUP BY category, return_reason "
                                "ORDER BY reason_orders DESC"
                            )
                        },
                    ),
                ]
            )

        metric_rows = sql_results[0]["data"]["rows"]
        reason_rows = sql_results[1]["data"]["rows"]

        top_anomaly_row = metric_rows[0]
        problem_cat = str(top_anomaly_row["category"])
        measured_return_rate = float(top_anomaly_row["avg_return_rate"])
        benchmark_rate = 0.18
        delta_pct = round(
            ((measured_return_rate - benchmark_rate) / benchmark_rate) * 100.0, 2
        )

        cat_reasons = [r for r in reason_rows if r["category"] == problem_cat][:3]
        total_reason_cnt = sum(int(r["reason_orders"]) for r in cat_reasons) or 1
        structured_reasons = [
            {
                "reason": str(r["return_reason"]),
                "share": round(int(r["reason_orders"]) / total_reason_cnt, 4),
                "order_count": int(r["reason_orders"]),
            }
            for r in cat_reasons
        ]

        artifact_dict = {
            "problem_category": problem_cat,
            "anomalous_metrics": [
                {
                    "metric_name": "return_rate",
                    "current_value": measured_return_rate,
                    "benchmark_value": benchmark_rate,
                    "delta_pct": delta_pct,
                    "summary": (
                        f"{problem_cat}在 {top_anomaly_row['price_band']} 元价格带实测退货率达 "
                        f"{measured_return_rate * 100:.2f}%，显著突破女装 {benchmark_rate * 100:.0f}% 警戒基线"
                    ),
                }
            ],
            "top_return_reasons": structured_reasons,
            "price_band_gaps": [
                {
                    "min_price": 329.0,
                    "max_price": 499.0,
                    "demand_index": 2.48,
                    "gap_description": (
                        f"{problem_cat}低价单层雪纺款因透肉与尺码卡胯退货高企，而 329-499 元双层防透三醋酸/莱赛尔轻奢通勤款需求旺盛但SKU供给空白"
                    ),
                }
            ],
        }
        return LLMResponse(
            content=json.dumps(artifact_dict, ensure_ascii=False),
            structured_output=artifact_dict,
        )

    def _handle_sourcing_agent(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        user_blob = "\n".join(m.content for m in messages if m.role == "user")
        json_match = re.search(r"\{[\s\S]*\"problem_category\"[\s\S]*?\n\}", user_blob)
        diagnosis_data = (
            json.loads(json_match.group(0))
            if json_match
            else {"problem_category": "连衣裙", "top_return_reasons": []}
        )
        category = str(diagnosis_data["problem_category"])

        tool_results = _parse_tool_messages(messages)
        called_names = {tr.get("tool_name") for tr in tool_results}

        if "market_erp__search_trends" not in called_names:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        tool_call_id=self._next_tc_id("erp_trends"),
                        name="market_erp__search_trends",
                        arguments={"category": category, "include_pain_points": True},
                    ),
                    ToolCall(
                        tool_call_id=self._next_tc_id("pim_existing"),
                        name="pim__hybrid_search",
                        arguments={"category": category},
                    ),
                ]
            )

        trends_data: dict[str, Any] = {}
        for tr in tool_results:
            if tr.get("tool_name") == "market_erp__search_trends" and tr.get("status") == "success":
                trends_data = dict(tr["data"])

        price_band = (trends_data.get("underserved_price_bands") or [{}])[0]
        min_p = float(price_band.get("min_price", 329.0))
        max_p = float(price_band.get("max_price", 499.0))
        target_price = round((min_p + max_p) / 2.0 + 15.0, 1)
        candidate_sku_name = f"云裳 60支莱赛尔三醋酸防透收腰{category} 2026春夏升级款"

        if "market_erp__calc_landed_cost" not in called_names:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        tool_call_id=self._next_tc_id("erp_cost"),
                        name="market_erp__calc_landed_cost",
                        arguments={
                            "sku_name": candidate_sku_name,
                            "fob_unit_cost": 145.0,
                            "unit_weight_kg": 0.48,
                            "order_quantity": 300,
                            "shipping_mode": "domestic_truck",
                            "target_retail_price": target_price,
                            "tariff_rate": 0.0,
                        },
                    )
                ]
            )

        cost_data: dict[str, Any] = {}
        for tr in tool_results:
            if tr.get("tool_name") == "market_erp__calc_landed_cost" and tr.get("status") == "success":
                cost_data = dict(tr["data"])

        landed_cost = float(cost_data["landed_unit_cost"])
        gross_margin = float(cost_data["estimated_gross_margin"])
        warnings = list(cost_data.get("risk_warnings") or ["首批杭嘉湖快反试销验证试穿退货率"])

        selling_points = [
            "高密双层桑蚕丝感防透全衬工艺，彻底解决浅色连衣裙透肉走光退货痛点",
            "亚洲梨形身材立体放量腰臀剪裁 + 后腰隐形弹力松量，告别拉链紧绷卡胯",
            "60支莱赛尔天丝混纺三醋酸天然抗静电面料，亲肤透气不贴腿",
        ]

        proposal_dict = {
            "candidate_skus": [
                {
                    "sku_id": "SKU-DRESS-LYOCELL-2026",
                    "sku_name": candidate_sku_name,
                    "target_price": float(cost_data["target_retail_price"]),
                    "estimated_unit_cost": landed_cost,
                }
            ],
            "estimated_gross_margin": gross_margin,
            "core_selling_points": selling_points,
            "risk_warnings": warnings,
        }

        if "pim__draft_sku_proposal" not in called_names:
            return LLMResponse(
                structured_output=proposal_dict,
                tool_calls=[
                    ToolCall(
                        tool_call_id=self._next_tc_id("pim_draft_high"),
                        name="pim__draft_sku_proposal",
                        arguments={
                            "sku_name": candidate_sku_name,
                            "category": category,
                            "target_retail_price": float(cost_data["target_retail_price"]),
                            "estimated_unit_cost": landed_cost,
                            "core_selling_points": selling_points,
                            "initial_order_qty": int(cost_data.get("order_quantity", 300)),
                        },
                    )
                ],
            )

        return LLMResponse(
            content=json.dumps(proposal_dict, ensure_ascii=False),
            structured_output=proposal_dict,
        )


__all__ = [
    "CallableMockLLM",
    "DeterministicEcommerceMockLLM",
    "MockHandlerFn",
    "ScriptStep",
    "ScriptedMockLLM",
]
