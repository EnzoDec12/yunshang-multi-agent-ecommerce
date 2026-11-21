import type {
  AuditLogEntry,
  RiskLevel,
  RoleType,
  ThinkingEvent,
  ToolStepRecord,
} from "@/types/agent";

export const ORDER_STATUS_LABELS: Record<
  string,
  { label: string; badgeClass: string; dotClass: string; hint?: string }
> = {
  pending: {
    label: "待付款",
    badgeClass: "bg-stone-100 text-stone-700 border-stone-200",
    dotClass: "bg-stone-400",
  },
  paid: {
    label: "待发货",
    badgeClass: "bg-amber-50 text-amber-800 border-amber-200/90",
    dotClass: "bg-amber-500",
    hint: "支持修改地址",
  },
  shipped: {
    label: "已发货",
    badgeClass: "bg-sky-50 text-sky-800 border-sky-200/90",
    dotClass: "bg-sky-500",
  },
  completed: {
    label: "已完成",
    badgeClass: "bg-emerald-50 text-emerald-800 border-emerald-200/90",
    dotClass: "bg-emerald-500",
  },
  refunded: {
    label: "已退款",
    badgeClass: "bg-rose-50 text-rose-800 border-rose-200/90",
    dotClass: "bg-rose-500",
  },
};

export const METRIC_NAME_LABELS: Record<string, string> = {
  return_rate: "近 3 日退货率",
  conversion_rate: "支付转化率",
  gross_margin_rate: "品类毛利率",
};

const COLOR_SWATCH_MAP: Array<[string, string]> = [
  ["香槟杏", "#E6D2B8"],
  ["燕麦米杏", "#DFD5C5"],
  ["珍珠白", "#F7F5F0"],
  ["奶霜白", "#FAF4E8"],
  ["曜石黑", "#222225"],
  ["雾粉紫", "#D6C0D4"],
  ["浅雾蓝", "#B8CCE0"],
  ["丹宁蓝", "#4C6B8A"],
  ["牛仔蓝", "#4C6B8A"],
];

export function resolveColorSwatch(colorName?: string): string {
  if (!colorName) return "#E5DDD3";
  for (const [key, hex] of COLOR_SWATCH_MAP) {
    if (colorName.includes(key)) {
      return hex;
    }
  }
  return "#D9CEC1";
}

/**
 * 清理文本中可能残留的内部款号编码、英文状态值或内部资源路径，转换为自然中文表达。
 */
export function cleanDisplayText(raw: string | undefined | null): string {
  if (!raw) return "";
  return raw
    .replace(/（\s*款号\s*SKU-[A-Za-z0-9-]+\s*）/g, "")
    .replace(/\(\s*款号\s*SKU-[A-Za-z0-9-]+\s*\)/g, "")
    .replace(/（\s*SKU-[A-Za-z0-9-]+\s*）/g, "")
    .replace(/\(\s*SKU-[A-Za-z0-9-]+\s*\)/g, "")
    .replace(/款号\s*SKU-[A-Za-z0-9-]+，?/g, "")
    .replace(/\bSKU-[A-Za-z0-9-]+\b/g, "")
    .replace(/pim:\/\/knowledge\/selling-points/g, "门店导购卖点库")
    .replace(/oms:\/\/docs\/order-state-machine/g, "售后服务规范")
    .replace(/bi:\/\/dictionary\/metrics/g, "经营指标口径")
    .replace(/market_erp:\/\/policies\/return-exchange/g, "退换货与选品标准")
    .replace(/\breturn_rate\b/g, "退货率")
    .replace(/\bconversion_rate\b/g, "转化率")
    .replace(/\bin_stock\b/g, "现货充足")
    .replace(/\blow_stock\b/g, "库存紧张")
    .replace(/\bout_of_stock\b/g, "已售罄")
    .replace(/\bin_transit\b/g, "运输派送中")
    .replace(/\bdelivered\b/g, "已签收")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/**
 * 格式化最终回复正文，去除任何内部子专员路由前缀与工程标记。
 */
export function formatFinalAnswerText(raw: string | undefined | null): string {
  if (!raw) return "";
  const lines = raw.split("\n").map((line) => {
    const withoutTags = line
      .replace(/^-\s*\[SUMMARY:[^\]]+\]\s*/g, "• ")
      .replace(/\[SUMMARY:[^\]]+\]\s*/g, "")
      .replace(/\[REPORT:[^\]]+\]\s*/g, "");
    return cleanDisplayText(withoutTags);
  });
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

/**
 * 将后端节点思考事件映射为安静、易懂的中文进度文案。
 */
export function describeThinkingStep(ev?: ThinkingEvent | null): string {
  if (!ev) return "正在为您处理…";
  switch (ev.node) {
    case "c_supervisor":
      return "正在理解您的需求并安排专属顾问…";
    case "shopping_guide_agent":
      return "正在为您挑选款式、比对尺码与面料…";
    case "order_agent":
      return "正在帮您查询订单状态与包裹物流…";
    case "c_hitl_gate":
      return "正在等待您确认本次服务申请…";
    case "analytics_agent":
      return "正在分析近 3 日各品类退货率与售后反馈…";
    case "b_contract_handoff":
      return "已完成退货问题归因，正在衔接新款选品测算…";
    case "sourcing_agent":
      return "正在测算成衣成本、建议售价与预估毛利率…";
    case "b_hitl_gate":
      return "正在等待或执行新款建档确认…";
    case "b_finalize":
      return "正在为您整理经营诊断与新款建议结论…";
    default:
      return cleanDisplayText(ev.message) || "正在为您处理…";
  }
}

/**
 * 将单次工具调用步骤映射为顾客和商家一眼能懂的自然中文步骤说明。
 */
export function describeToolStep(step: ToolStepRecord): {
  runningText: string;
  doneText: string;
  tone: "normal" | "warning" | "blocked";
} {
  const args = step.arguments ?? {};
  const data = step.data ?? {};
  const cat = typeof args.category === "string" ? args.category : "";

  if (step.status === "error") {
    const errCode = step.error?.code ?? "";
    if (errCode === "ORDER_OWNERSHIP_DENIED" || errCode === "TENANT_ISOLATION_DENIED") {
      return {
        runningText: "正在核对订单归属信息…",
        doneText: "已拦截：仅支持查询或修改您本人名下的订单",
        tone: "blocked",
      };
    }
    if (errCode === "ORDER_STATUS_DISALLOWS_ADDRESS_UPDATE") {
      return {
        runningText: "正在核对订单发货状态…",
        doneText: "包裹已发出，暂不支持直接修改收货地址",
        tone: "warning",
      };
    }
    if (errCode === "INSUFFICIENT_STOCK") {
      return {
        runningText: "正在核对商品实时库存…",
        doneText: "该款式当前库存不足",
        tone: "warning",
      };
    }
    return {
      runningText: "正在核对服务规则…",
      doneText: cleanDisplayText(step.error?.message) || "当前操作未满足办理条件",
      tone: "blocked",
    };
  }

  switch (step.tool_name) {
    case "pim__hybrid_search": {
      const items = Array.isArray(data.items) ? data.items : [];
      const targetLabel = cat || "在售女装";
      return {
        runningText: `正在挑选店内${targetLabel}款式…`,
        doneText:
          items.length > 0
            ? `已为您筛出 ${items.length} 款${targetLabel}`
            : `已完成店内${targetLabel}款式检索`,
        tone: "normal",
      };
    }
    case "pim__compare_skus": {
      const count =
        typeof data.compared_count === "number" ? data.compared_count : null;
      return {
        runningText: "正在对比各款尺码、颜色与面料细节…",
        doneText: count
          ? `已完成 ${count} 款女装的尺码、颜色与面料对比`
          : "已完成款式尺码与面料对比",
        tone: "normal",
      };
    }
    case "pim__check_stock": {
      const qty =
        typeof data.available_stock === "number" ? data.available_stock : null;
      return {
        runningText: "正在核对中心仓现货库存…",
        doneText:
          qty !== null
            ? `已确认现货库存（当前可售 ${qty} 件）`
            : "已完成实时库存核对",
        tone: "normal",
      };
    }
    case "oms__search_orders": {
      const orders = Array.isArray(data.orders) ? data.orders : null;
      return {
        runningText: "正在帮您查询历史订单…",
        doneText:
          orders !== null
            ? `已查到您的 ${orders.length} 笔订单记录`
            : "已完成订单记录查询",
        tone: "normal",
      };
    }
    case "logistics__track_pkg": {
      const carrier =
        typeof data.carrier === "string" && data.carrier
          ? data.carrier
          : "快递";
      return {
        runningText: "正在查询包裹最新物流动态…",
        doneText: `已同步${carrier}包裹最新物流轨迹`,
        tone: "normal",
      };
    }
    case "oms__update_address": {
      if (step.status === "hitl_required") {
        return {
          runningText: "正在核对收货地址变更信息…",
          doneText: "请确认您的新收货地址信息",
          tone: "warning",
        };
      }
      return {
        runningText: "正在为您更新未发货订单收货地址…",
        doneText: "已为您更新订单收货地址",
        tone: "normal",
      };
    }
    case "oms__create_refund": {
      if (step.status === "hitl_required") {
        return {
          runningText: "正在核实订单退款金额与售后条件…",
          doneText: "已准备好退款申请，等待您确认提交",
          tone: "warning",
        };
      }
      return {
        runningText: "正在为您提交原路退款申请…",
        doneText: "售后服务退款申请已提交生效",
        tone: "normal",
      };
    }
    case "bi__get_table_schema":
      return {
        runningText: "正在读取店铺近 3 日销售与退货统计口径…",
        doneText: "已就绪近 3 日经营与退货统计数据",
        tone: "normal",
      };
    case "bi__run_readonly_sql": {
      const sqlStr = typeof args.sql === "string" ? args.sql : "";
      const isReturnReason = sqlStr.includes("return_reason");
      return {
        runningText: isReturnReason
          ? "正在统计消费者主要退货原因占比…"
          : "正在分析近 3 日各女装品类成交与退货率…",
        doneText: isReturnReason
          ? "已完成消费者退货原因排行统计"
          : "已完成各品类退货率与价格带表现分析",
        tone: "normal",
      };
    }
    case "bi__render_chart":
      return {
        runningText: "正在整理经营指标对比结果…",
        doneText: "已生成各品类经营对比汇总",
        tone: "normal",
      };
    case "market_erp__search_trends":
      return {
        runningText: `正在调研${cat || "女装"}价格带需求与面料趋势…`,
        doneText: `已定位${cat || "女装"}空白价格带机会与升级方向`,
        tone: "normal",
      };
    case "market_erp__calc_landed_cost":
      return {
        runningText: "正在测算成衣单件成本与预估毛利率…",
        doneText: "已完成供应链单件成本与毛利率测算",
        tone: "normal",
      };
    case "pim__draft_sku_proposal":
      if (step.status === "hitl_required") {
        return {
          runningText: "正在整理新款开发与备货方案…",
          doneText: "新款建档方案已就绪，等待您确认通过",
          tone: "warning",
        };
      }
      return {
        runningText: "正在创建新款档案并同步门店卖点库…",
        doneText: "已完成新款建档并同步至门店导购卖点库",
        tone: "normal",
      };
    default:
      return {
        runningText: "正在为您查询相关信息…",
        doneText: "已完成查询核对",
        tone: "normal",
      };
  }
}

export function formatCurrency(amount: number): string {
  if (!Number.isFinite(amount)) return "¥0";
  return Number.isInteger(amount) ? `¥${amount}` : `¥${amount.toFixed(2)}`;
}

/**
 * 仅供右上角低调「操作审计」抽屉使用的中文映射。
 */
export function describeAuditOperation(toolName: string): string {
  const map: Record<string, string> = {
    pim__hybrid_search: "检索店内商品款式",
    pim__compare_skus: "对比款式尺码与面料",
    pim__check_stock: "核对中心仓实时库存",
    pim__draft_sku_proposal: "提交新款建档方案",
    oms__search_orders: "查询订单记录",
    oms__update_address: "修改未发货订单地址",
    oms__create_refund: "办理订单售后退款",
    logistics__track_pkg: "查询包裹物流轨迹",
    bi__get_table_schema: "读取经营统计表结构",
    bi__run_readonly_sql: "统计近 3 日退货与销量",
    bi__render_chart: "生成经营对比图表",
    market_erp__search_trends: "调研女装价格带趋势",
    market_erp__calc_landed_cost: "测算成衣成本与毛利",
  };
  return map[toolName] ?? "系统业务查询";
}

export function describeAuditRole(role: RoleType): string {
  return role === "merchant" ? "商家运营" : "会员顾客";
}

export function describeAuditRisk(risk: RiskLevel): {
  label: string;
  cls: string;
} {
  switch (risk) {
    case "high":
      return {
        label: "重要变更",
        cls: "bg-rose-50 text-rose-700 border-rose-200",
      };
    case "medium":
      return {
        label: "信息修改",
        cls: "bg-amber-50 text-amber-800 border-amber-200",
      };
    case "low":
      return {
        label: "常规查询",
        cls: "bg-stone-100 text-stone-600 border-stone-200",
      };
    default:
      return {
        label: "一般操作",
        cls: "bg-stone-100 text-stone-600 border-stone-200",
      };
  }
}

export function describeAuditStatus(log: AuditLogEntry): {
  label: string;
  cls: string;
} {
  if (log.status === "success") {
    return {
      label: "已完成",
      cls: "bg-emerald-50 text-emerald-700 border-emerald-200",
    };
  }
  if (log.status === "hitl_required") {
    return {
      label: "待人工确认",
      cls: "bg-amber-50 text-amber-800 border-amber-200",
    };
  }
  return {
    label: "已安全拦截",
    cls: "bg-rose-50 text-rose-700 border-rose-200",
  };
}
