"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowUpRight,
  Briefcase,
  CheckCircle2,
  ClipboardList,
  Loader2,
  Plus,
  Send,
  ShoppingBag,
  Sparkles,
  XCircle,
} from "lucide-react";
import {
  fetchAuditLogs,
  loginPresetAccount,
  streamChatTurn,
  streamHitlResume,
} from "@/lib/api";
import { cleanDisplayText, formatFinalAnswerText } from "@/lib/presenters";
import type {
  AccountProfile,
  AuditLogEntry,
  CheckStockData,
  CompareSkusData,
  ConversationTurn,
  CreateRefundData,
  HITLInterruptEvent,
  LogisticsPackageData,
  OrderRecord,
  PerspectiveId,
  RoleType,
  SkuComparisonEntry,
  TurnArtifacts,
} from "@/types/agent";
import { ToolCallStatusList } from "@/components/ToolCallStatusList";
import {
  DiagnosisArtifactCard,
  OrderAndLogisticsCard,
  ProductComparisonCard,
  SelectionProposalCard,
} from "@/components/BusinessCards";
import { HitlApprovalModal } from "@/components/HitlApprovalModal";
import { AuditLogsDrawer } from "@/components/AuditLogsDrawer";

interface ScenarioPromptCard {
  category: string;
  title: string;
  description: string;
  prompt: string;
}

const SCENARIO_CARDS: Record<PerspectiveId, ScenarioPromptCard[]> = {
  customer_01: [
    {
      category: "款式与尺码对比",
      title: "挑选显瘦防透连衣裙",
      description:
        "对比店内桑蚕丝、三醋酸与雪纺连衣裙的颜色、尺码、面料质感与现货库存",
      prompt:
        "帮我推荐并横向对比店里的连衣裙，看看尺码、颜色、面料成分差异和实时库存",
    },
    {
      category: "成套穿搭与查快递",
      title: "已购阔腿裤配衬衫 & 查物流",
      description:
        "根据您买过的西装阔腿裤推荐百搭衬衫，并查看在途连衣裙包裹的顺丰动态",
      prompt:
        "帮我查下历史订单买的阔腿裤款式尺码，推荐店里能成套搭配的衬衫并核验现货库存，顺便追踪已发货连衣裙包裹的最新顺丰物流轨迹",
    },
    {
      category: "订单售后服务",
      title: "未发货衬衫申请退款",
      description:
        "核对尚未发出的珍珠白三醋酸衬衫订单金额，在线确认并提交原路退款申请",
      prompt: "请帮我对未发货衬衫订单 ORD-20261118-2101 申请售后服务退款",
    },
  ],
  merchant_01: [
    {
      category: "退货诊断与新款立项",
      title: "连衣裙高退货率归因与改良款建档",
      description:
        "定位近 3 日连衣裙退货核心原因，测算防透升级款单件成本与毛利率并提交建档",
      prompt:
        "近期店铺女装连衣裙退货率异常偏高，请诊断核心退货原因与价格带缺口，测算成衣供应链到岸成本生成选品提案并提交新款建档草稿",
    },
    {
      category: "空白价格带企划",
      title: "329–499 元通勤女装成本与毛利测算",
      description:
        "围绕高需求潜力价格带核算莱赛尔三醋酸连衣裙建议售价、到岸成本与首单备货量",
      prompt:
        "请分析近期高退货女装品类原因，结合 329-499 元价格带空白测算莱赛尔三醋酸连衣裙成本毛利率并提交新款建档",
    },
    {
      category: "卖点升级与导购同步",
      title: "防透与梨形版型改良方案确认",
      description:
        "针对浅色透肉与腰臀紧绷反馈生成新款开发建议，确认通过后同步至门店导购卖点库",
      prompt:
        "请梳理近期女装连衣裙主要退货痛点，测算双层防透升级款毛利空间并提交新款建档申请",
    },
  ],
};

function createEmptyArtifacts(): TurnArtifacts {
  return {
    sku_comparisons: [],
    search_results: [],
    stock_checks: [],
    orders: [],
    logistics: [],
    refund_results: [],
    address_updates: [],
    diagnosis_artifact: null,
    selection_proposal: null,
    committed_proposal: null,
    approval_status: null,
    circuit_breaker_triggered: false,
    circuit_breaker_reason: null,
  };
}

function buildFallbackComparisonFromSearch(
  searchResults: Record<string, unknown>[]
): CompareSkusData[] {
  if (!searchResults.length) return [];
  const latest = searchResults[searchResults.length - 1];
  const rawItems = Array.isArray(latest?.items) ? latest.items : [];
  if (!rawItems.length) return [];

  const entries: SkuComparisonEntry[] = rawItems.slice(0, 3).map((raw) => {
    const it = raw as Record<string, unknown>;
    const specs = (it.specs as Record<string, string>) ?? {};
    return {
      sku_id: String(it.sku_id ?? ""),
      name: String(it.name ?? ""),
      category: String(it.category ?? "女装"),
      retail_price: Number(it.retail_price ?? 0),
      available_stock: Number(it.available_stock ?? 0),
      sizes: typeof it.sizes === "string" ? it.sizes : specs.sizes,
      color: typeof it.color === "string" ? it.color : specs.color,
      fabric: typeof it.fabric === "string" ? it.fabric : specs.fabric,
      specs,
      selling_points: Array.isArray(it.selling_points)
        ? (it.selling_points as string[])
        : [],
      rating: Number(it.rating ?? 4.9),
    };
  });

  const prices = entries.map((e) => e.retail_price);
  return [
    {
      compared_count: entries.length,
      min_price: Math.min(...prices),
      max_price: Math.max(...prices),
      entries,
      comparison_summary: "",
    },
  ];
}

interface PerspectiveSessionState {
  token: string | null;
  account: AccountProfile | null;
  authLoading: boolean;
  authError: string | null;
  threadId: string | null;
  turns: ConversationTurn[];
  draft: string;
}

const INITIAL_SESSIONS: Record<PerspectiveId, PerspectiveSessionState> = {
  customer_01: {
    token: null,
    account: null,
    authLoading: false,
    authError: null,
    threadId: null,
    turns: [],
    draft: "",
  },
  merchant_01: {
    token: null,
    account: null,
    authLoading: false,
    authError: null,
    threadId: null,
    turns: [],
    draft: "",
  },
};

export default function DualPerspectiveWorkbenchPage() {
  const [activeTab, setActiveTab] = useState<PerspectiveId>("customer_01");
  const [sessions, setSessions] =
    useState<Record<PerspectiveId, PerspectiveSessionState>>(INITIAL_SESSIONS);

  const [activeHitlModal, setActiveHitlModal] = useState<{
    perspective: PerspectiveId;
    turnId: string;
    interrupt: HITLInterruptEvent;
  } | null>(null);
  const [isResumingHitl, setIsResumingHitl] = useState(false);

  const [isAuditOpen, setIsAuditOpen] = useState(false);
  const [auditLogs, setAuditLogs] = useState<AuditLogEntry[]>([]);
  const [auditViewer, setAuditViewer] = useState<{
    tenant_id: string;
    user_id: string;
    role: RoleType;
  } | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);

  const bottomEndRef = useRef<HTMLDivElement | null>(null);
  const currentSession = sessions[activeTab];
  const isMerchantView = activeTab === "merchant_01";
  const isBusy =
    currentSession.turns.some((t) => t.isStreaming) || isResumingHitl;

  const refreshAuditTrail = useCallback(async (jwtToken: string) => {
    setAuditLoading(true);
    try {
      const data = await fetchAuditLogs(jwtToken, { limit: 150 });
      setAuditLogs(data.logs);
      setAuditViewer(data.viewer);
    } catch {
      // 静默容错，不影响主界面使用
    } finally {
      setAuditLoading(false);
    }
  }, []);

  const ensureLoggedIn = useCallback(
    async (
      perspective: PerspectiveId,
      forceRelogin = false
    ): Promise<string | null> => {
      const existing = sessions[perspective];
      if (!forceRelogin && existing.token) {
        void refreshAuditTrail(existing.token);
        return existing.token;
      }

      setSessions((prev) => ({
        ...prev,
        [perspective]: {
          ...prev[perspective],
          authLoading: true,
          authError: null,
        },
      }));

      try {
        const res = await loginPresetAccount(perspective);
        setSessions((prev) => ({
          ...prev,
          [perspective]: {
            ...prev[perspective],
            token: res.access_token,
            account: res.account,
            authLoading: false,
            authError: null,
          },
        }));
        void refreshAuditTrail(res.access_token);
        return res.access_token;
      } catch {
        setSessions((prev) => ({
          ...prev,
          [perspective]: {
            ...prev[perspective],
            authLoading: false,
            authError: "服务连接暂时不可用，请稍后点击重试",
          },
        }));
        return null;
      }
    },
    [refreshAuditTrail, sessions]
  );

  useEffect(() => {
    void ensureLoggedIn(activeTab, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const latestTurn = currentSession.turns[currentSession.turns.length - 1];
  useEffect(() => {
    if (currentSession.turns.length > 0) {
      bottomEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [
    currentSession.turns.length,
    latestTurn?.toolOrder.length,
    latestTurn?.finalAnswer,
    latestTurn?.pendingHitl,
  ]);

  const mutateTurn = useCallback(
    (
      perspective: PerspectiveId,
      turnId: string,
      updater: (turn: ConversationTurn) => ConversationTurn
    ) => {
      setSessions((prev) => ({
        ...prev,
        [perspective]: {
          ...prev[perspective],
          turns: prev[perspective].turns.map((t) =>
            t.id === turnId ? updater(t) : t
          ),
        },
      }));
    },
    []
  );

  const applyIncrementalToolCardData = (
    artifacts: TurnArtifacts,
    toolName: string,
    data: Record<string, unknown> | null
  ): TurnArtifacts => {
    if (!data) return artifacts;
    const next = { ...artifacts };

    if (toolName === "pim__compare_skus") {
      next.sku_comparisons = [
        ...next.sku_comparisons,
        data as unknown as CompareSkusData,
      ];
    } else if (toolName === "pim__hybrid_search") {
      next.search_results = [...next.search_results, data];
    } else if (toolName === "pim__check_stock") {
      next.stock_checks = [
        ...next.stock_checks,
        data as unknown as CheckStockData,
      ];
    } else if (
      toolName === "oms__search_orders" &&
      Array.isArray(data.orders)
    ) {
      const byId = new Map(next.orders.map((o) => [o.order_id, o]));
      for (const item of data.orders as OrderRecord[]) {
        byId.set(item.order_id, item);
      }
      next.orders = Array.from(byId.values());
    } else if (toolName === "logistics__track_pkg") {
      const pkg = data as unknown as LogisticsPackageData;
      next.logistics = [
        ...next.logistics.filter((p) => p.tracking_no !== pkg.tracking_no),
        pkg,
      ];
    } else if (toolName === "oms__create_refund") {
      const rf = data as unknown as CreateRefundData;
      next.refund_results = [...next.refund_results, rf];
      next.orders = next.orders.map((o) =>
        o.order_id === rf.order_id
          ? { ...o, status: "refunded", refund_id: rf.refund_id }
          : o
      );
    } else if (toolName === "oms__update_address") {
      const orderId = String(data.order_id ?? "");
      next.orders = next.orders.map((o) =>
        o.order_id === orderId
          ? {
              ...o,
              buyer_name: String(data.receiver_name ?? o.buyer_name),
              buyer_phone: String(data.receiver_phone ?? o.buyer_phone),
              shipping_address: String(
                data.shipping_address ?? o.shipping_address
              ),
            }
          : o
      );
    }

    return next;
  };

  const handleSendMessage = async (overrideMessage?: string) => {
    const perspective = activeTab;
    const rawMsg = (overrideMessage ?? sessions[perspective].draft).trim();
    if (!rawMsg || isBusy) return;

    const jwtToken = await ensureLoggedIn(perspective, false);
    if (!jwtToken) return;

    const turnId = `turn_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const existingThreadId = sessions[perspective].threadId;

    const newTurn: ConversationTurn = {
      id: turnId,
      userMessage: rawMsg,
      createdAt: new Date().toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
      }),
      threadId: existingThreadId ?? undefined,
      isStreaming: true,
      activeThinking: null,
      thinkingHistory: [],
      toolOrder: [],
      toolsById: {},
      artifacts: createEmptyArtifacts(),
      pendingHitl: null,
      hitlDecisionNote: null,
      finalAnswer: null,
      streamError: null,
    };

    setSessions((prev) => ({
      ...prev,
      [perspective]: {
        ...prev[perspective],
        draft: overrideMessage ? prev[perspective].draft : "",
        turns: [...prev[perspective].turns, newTurn],
      },
    }));

    try {
      const meta = await streamChatTurn({
        token: jwtToken,
        message: rawMsg,
        threadId: existingThreadId,
        handlers: {
          onThinking: (ev) => {
            if (ev.thread_id) {
              setSessions((prev) => ({
                ...prev,
                [perspective]: {
                  ...prev[perspective],
                  threadId: ev.thread_id,
                },
              }));
            }
            mutateTurn(perspective, turnId, (t) => ({
              ...t,
              threadId: ev.thread_id || t.threadId,
              traceId: ev.trace_id || t.traceId,
              activeThinking: ev,
              thinkingHistory: [...t.thinkingHistory, ev],
              artifacts: ev.diagnosis_artifact
                ? { ...t.artifacts, diagnosis_artifact: ev.diagnosis_artifact }
                : t.artifacts,
            }));
          },
          onToolStart: (ev) => {
            mutateTurn(perspective, turnId, (t) => {
              const exists = Boolean(t.toolsById[ev.tool_call_id]);
              return {
                ...t,
                threadId: ev.thread_id || t.threadId,
                traceId: ev.trace_id || t.traceId,
                toolOrder: exists
                  ? t.toolOrder
                  : [...t.toolOrder, ev.tool_call_id],
                toolsById: {
                  ...t.toolsById,
                  [ev.tool_call_id]: {
                    tool_call_id: ev.tool_call_id,
                    tool_name: ev.tool_name,
                    domain: ev.domain,
                    agent: ev.agent,
                    risk: ev.risk,
                    status: "running",
                    arguments: ev.arguments,
                  },
                },
              };
            });
          },
          onToolResult: (ev) => {
            mutateTurn(perspective, turnId, (t) => {
              const exists = Boolean(t.toolsById[ev.tool_call_id]);
              const updatedArtifacts =
                ev.status === "success"
                  ? applyIncrementalToolCardData(
                      t.artifacts,
                      ev.tool_name,
                      ev.data
                    )
                  : t.artifacts;
              return {
                ...t,
                toolOrder: exists
                  ? t.toolOrder
                  : [...t.toolOrder, ev.tool_call_id],
                toolsById: {
                  ...t.toolsById,
                  [ev.tool_call_id]: {
                    tool_call_id: ev.tool_call_id,
                    tool_name: ev.tool_name,
                    domain: ev.domain,
                    agent: ev.agent,
                    risk: ev.risk,
                    status: ev.status,
                    arguments:
                      t.toolsById[ev.tool_call_id]?.arguments ?? {},
                    duration_ms: ev.duration_ms,
                    data: ev.data,
                    error: ev.error,
                  },
                },
                artifacts: updatedArtifacts,
              };
            });
          },
          onHitlInterrupt: (ev) => {
            setSessions((prev) => ({
              ...prev,
              [perspective]: {
                ...prev[perspective],
                threadId: ev.thread_id,
              },
            }));
            mutateTurn(perspective, turnId, (t) => ({
              ...t,
              isStreaming: false,
              activeThinking: null,
              threadId: ev.thread_id,
              traceId: ev.trace_id,
              pendingHitl: ev,
              artifacts: {
                ...t.artifacts,
                diagnosis_artifact:
                  ev.diagnosis_artifact ?? t.artifacts.diagnosis_artifact,
                selection_proposal:
                  ev.selection_proposal ?? t.artifacts.selection_proposal,
              },
            }));
            setActiveHitlModal({
              perspective,
              turnId,
              interrupt: ev,
            });
          },
          onFinalAnswer: (ev) => {
            setSessions((prev) => ({
              ...prev,
              [perspective]: {
                ...prev[perspective],
                threadId: ev.thread_id,
              },
            }));
            mutateTurn(perspective, turnId, (t) => ({
              ...t,
              isStreaming: false,
              activeThinking: null,
              threadId: ev.thread_id,
              traceId: ev.trace_id,
              finalAnswer: ev.answer,
              artifacts: ev.artifacts,
            }));
          },
          onStreamError: (msg) => {
            mutateTurn(perspective, turnId, (t) => ({
              ...t,
              isStreaming: false,
              activeThinking: null,
              streamError: cleanDisplayText(msg) || "服务暂时繁忙，请稍后再试",
            }));
          },
        },
      });

      if (meta.threadId) {
        setSessions((prev) => ({
          ...prev,
          [perspective]: {
            ...prev[perspective],
            threadId: meta.threadId,
          },
        }));
      }
    } catch {
      mutateTurn(perspective, turnId, (t) => ({
        ...t,
        isStreaming: false,
        activeThinking: null,
        streamError: "网络连接波动，请稍后重新发送",
      }));
    } finally {
      void refreshAuditTrail(jwtToken);
    }
  };

  const handleHitlDecision = async (
    decision: "approve" | "reject",
    comment: string
  ) => {
    if (!activeHitlModal) return;
    const { perspective, turnId, interrupt } = activeHitlModal;
    const jwtToken = await ensureLoggedIn(perspective, false);
    if (!jwtToken) return;

    setIsResumingHitl(true);
    mutateTurn(perspective, turnId, (t) => ({
      ...t,
      isStreaming: true,
      pendingHitl: null,
      hitlDecisionNote: { decision, comment },
    }));
    setActiveHitlModal(null);

    try {
      await streamHitlResume({
        token: jwtToken,
        threadId: interrupt.thread_id,
        decision,
        comment,
        handlers: {
          onThinking: (ev) => {
            mutateTurn(perspective, turnId, (t) => ({
              ...t,
              traceId: ev.trace_id || t.traceId,
              activeThinking: ev,
              thinkingHistory: [...t.thinkingHistory, ev],
            }));
          },
          onToolStart: (ev) => {
            mutateTurn(perspective, turnId, (t) => {
              const exists = Boolean(t.toolsById[ev.tool_call_id]);
              return {
                ...t,
                toolOrder: exists
                  ? t.toolOrder
                  : [...t.toolOrder, ev.tool_call_id],
                toolsById: {
                  ...t.toolsById,
                  [ev.tool_call_id]: {
                    tool_call_id: ev.tool_call_id,
                    tool_name: ev.tool_name,
                    domain: ev.domain,
                    agent: ev.agent,
                    risk: ev.risk,
                    status: "running",
                    arguments: ev.arguments,
                  },
                },
              };
            });
          },
          onToolResult: (ev) => {
            mutateTurn(perspective, turnId, (t) => {
              const exists = Boolean(t.toolsById[ev.tool_call_id]);
              const updatedArtifacts =
                ev.status === "success"
                  ? applyIncrementalToolCardData(
                      t.artifacts,
                      ev.tool_name,
                      ev.data
                    )
                  : t.artifacts;
              return {
                ...t,
                toolOrder: exists
                  ? t.toolOrder
                  : [...t.toolOrder, ev.tool_call_id],
                toolsById: {
                  ...t.toolsById,
                  [ev.tool_call_id]: {
                    tool_call_id: ev.tool_call_id,
                    tool_name: ev.tool_name,
                    domain: ev.domain,
                    agent: ev.agent,
                    risk: ev.risk,
                    status: ev.status,
                    arguments:
                      t.toolsById[ev.tool_call_id]?.arguments ?? {},
                    duration_ms: ev.duration_ms,
                    data: ev.data,
                    error: ev.error,
                  },
                },
                artifacts: updatedArtifacts,
              };
            });
          },
          onFinalAnswer: (ev) => {
            mutateTurn(perspective, turnId, (t) => ({
              ...t,
              isStreaming: false,
              activeThinking: null,
              traceId: ev.trace_id || t.traceId,
              finalAnswer: ev.answer,
              artifacts: ev.artifacts,
            }));
          },
          onStreamError: (msg) => {
            mutateTurn(perspective, turnId, (t) => ({
              ...t,
              isStreaming: false,
              activeThinking: null,
              streamError: cleanDisplayText(msg) || "确认提交未完成，请重试",
            }));
          },
        },
      });
    } catch {
      mutateTurn(perspective, turnId, (t) => ({
        ...t,
        isStreaming: false,
        activeThinking: null,
        streamError: "提交确认结果时网络异常，请重试",
      }));
    } finally {
      setIsResumingHitl(false);
      void refreshAuditTrail(jwtToken);
    }
  };

  const handleResetThread = () => {
    if (isBusy) return;
    setSessions((prev) => ({
      ...prev,
      [activeTab]: {
        ...prev[activeTab],
        threadId: null,
        turns: [],
      },
    }));
  };

  return (
    <div
      className={`flex min-h-screen flex-col transition-colors duration-200 ${
        isMerchantView
          ? "bg-[#F6F7F9] text-slate-900"
          : "bg-[#FAF8F5] text-[#1F1D1A]"
      }`}
    >
      {/* 极简品质感顶部导航栏 */}
      <header
        className={`sticky top-0 z-30 border-b backdrop-blur-md transition-colors ${
          isMerchantView
            ? "border-slate-200/90 bg-white/95"
            : "border-[#EAE3DA] bg-[#FAF8F5]/92"
        }`}
      >
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          {/* 左侧：云裳女装品牌名 */}
          <div className="flex items-center gap-3">
            <div
              className={`flex h-9 w-9 items-center justify-center rounded-xl text-sm font-semibold shadow-2xs transition-colors ${
                isMerchantView
                  ? "bg-slate-900 text-white"
                  : "bg-[#8C3B4A] text-white font-editorial text-base"
              }`}
            >
              云
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span
                  className={`text-base font-bold tracking-tight ${
                    isMerchantView ? "text-slate-900" : "font-editorial text-[#1F1D1A]"
                  }`}
                >
                  云裳女装
                </span>
                <span
                  className={`hidden sm:inline-block text-xs ${
                    isMerchantView ? "text-slate-400" : "text-[#9C9184]"
                  }`}
                >
                  ·
                </span>
                <span
                  className={`hidden sm:inline-block text-xs ${
                    isMerchantView ? "text-slate-500" : "text-[#7A7167]"
                  }`}
                >
                  {isMerchantView ? "经营与选品助手" : "智能导购与客服"}
                </span>
              </div>
            </div>
          </div>

          {/* 中部：顾客导购 / 商家经营台 双模式切换胶囊 */}
          <div
            className={`flex items-center rounded-full border p-1 ${
              isMerchantView
                ? "border-slate-200 bg-slate-100/90"
                : "border-[#E6DFD5] bg-[#F2EDE5]/90"
            }`}
          >
            <button
              type="button"
              onClick={() => setActiveTab("customer_01")}
              className={`inline-flex items-center gap-1.5 rounded-full px-4 py-1.5 text-xs font-semibold transition-all cursor-pointer ${
                !isMerchantView
                  ? "bg-white text-[#8C3B4A] shadow-2xs"
                  : "text-slate-600 hover:text-slate-900"
              }`}
            >
              <ShoppingBag className="h-3.5 w-3.5" />
              <span>顾客导购</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("merchant_01")}
              className={`inline-flex items-center gap-1.5 rounded-full px-4 py-1.5 text-xs font-semibold transition-all cursor-pointer ${
                isMerchantView
                  ? "bg-slate-900 text-white shadow-2xs"
                  : "text-[#6E665E] hover:text-[#1F1D1A]"
              }`}
            >
              <Briefcase className="h-3.5 w-3.5" />
              <span>商家经营台</span>
            </button>
          </div>

          {/* 右侧：当前身份问候 + 新建对话 + 低调操作审计小入口 */}
          <div className="flex items-center gap-2">
            {currentSession.account && (
              <span
                className={`hidden lg:inline-block mr-1 text-xs ${
                  isMerchantView ? "text-slate-500" : "text-[#7A7167]"
                }`}
              >
                {currentSession.account.display_name}
              </span>
            )}

            <button
              type="button"
              onClick={handleResetThread}
              disabled={isBusy}
              className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 cursor-pointer ${
                isMerchantView
                  ? "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
                  : "border-[#E5DDD2] bg-white text-[#3D3833] hover:bg-[#F7F3ED]"
              }`}
            >
              <Plus className="h-3.5 w-3.5" />
              <span>新建对话</span>
            </button>

            <button
              type="button"
              onClick={() => {
                setIsAuditOpen(true);
                if (currentSession.token) {
                  void refreshAuditTrail(currentSession.token);
                }
              }}
              title="查看系统操作审计记录"
              className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1.5 text-xs transition-colors cursor-pointer ${
                isMerchantView
                  ? "text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                  : "text-[#9C9184] hover:bg-[#F2EDE5] hover:text-[#4A433C]"
              }`}
            >
              <ClipboardList className="h-3.5 w-3.5" />
              <span className="hidden md:inline">操作审计</span>
            </button>
          </div>
        </div>

        {/* 仅在网关连接异常时温和提示 */}
        {currentSession.authError && (
          <div className="border-t border-rose-200/70 bg-rose-50/80 px-4 py-1.5 text-center text-xs text-rose-700">
            <span>{currentSession.authError}</span>
            <button
              type="button"
              onClick={() => void ensureLoggedIn(activeTab, true)}
              className="ml-2 font-semibold underline cursor-pointer"
            >
              立即重连
            </button>
          </div>
        )}
      </header>

      {/* 主内容区 */}
      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-4 py-6 sm:px-6">
        {currentSession.turns.length === 0 ? (
          /* 空状态：舒展留白 + 3 个清晰场景快捷卡片 */
          <div className="my-auto py-8 sm:py-12">
            <div className="mx-auto max-w-2xl text-center space-y-3">
              <div
                className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${
                  isMerchantView
                    ? "border-slate-200 bg-white text-slate-600"
                    : "border-[#E6DDD1] bg-white/90 text-[#8C3B4A]"
                }`}
              >
                <Sparkles className="h-3.5 w-3.5" />
                <span>
                  {isMerchantView
                    ? "云裳女装旗舰店 · 商品企划与退货诊断助手"
                    : "2026 春夏甄选系列 · 专属穿搭与售后服务顾问"}
                </span>
              </div>

              <h1
                className={`text-2xl sm:text-3xl font-bold tracking-tight ${
                  isMerchantView
                    ? "text-slate-900"
                    : "font-editorial text-[#1F1D1A]"
                }`}
              >
                {isMerchantView
                  ? "今天想看哪一类女装的经营与新款企划？"
                  : "您好，今天想挑选哪类女装或查询订单？"}
              </h1>

              <p
                className={`mx-auto max-w-xl text-sm leading-relaxed ${
                  isMerchantView ? "text-slate-500" : "text-[#7A7167]"
                }`}
              >
                {isMerchantView
                  ? "支持近 3 日高退货女装归因、空白价格带机会识别、成衣供应链单件成本与毛利率核算，以及新款一键建档。"
                  : "为您提供连衣裙、衬衫、裤装等尺码颜色与面料对比、成套穿搭建议，也可随时查询顺丰物流、修改地址或申请退款。"}
              </p>
            </div>

            {/* 3 个一目了然的场景快捷卡片 */}
            <div className="mt-9 grid grid-cols-1 md:grid-cols-3 gap-4">
              {SCENARIO_CARDS[activeTab].map((card, idx) => (
                <button
                  key={idx}
                  type="button"
                  disabled={isBusy}
                  onClick={() => void handleSendMessage(card.prompt)}
                  className={`group flex flex-col justify-between rounded-2xl border p-5 text-left transition-all disabled:opacity-50 cursor-pointer ${
                    isMerchantView
                      ? "border-slate-200/90 bg-white hover:border-slate-300 hover:shadow-md"
                      : "border-[#E8E0D5] bg-white hover:border-[#D4B2B8] hover:shadow-[0_8px_24px_-8px_rgba(140,59,74,0.14)]"
                  }`}
                >
                  <div>
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                          isMerchantView
                            ? "bg-slate-100 text-slate-700"
                            : "bg-[#F6EFE8] text-[#8C3B4A]"
                        }`}
                      >
                        {card.category}
                      </span>
                      <ArrowUpRight
                        className={`h-4 w-4 transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5 ${
                          isMerchantView
                            ? "text-slate-400 group-hover:text-slate-800"
                            : "text-[#B5AAA0] group-hover:text-[#8C3B4A]"
                        }`}
                      />
                    </div>

                    <h3
                      className={`mt-3.5 text-[15px] font-semibold leading-snug ${
                        isMerchantView ? "text-slate-900" : "text-[#1F1D1A]"
                      }`}
                    >
                      {card.title}
                    </h3>

                    <p
                      className={`mt-2 text-xs leading-relaxed ${
                        isMerchantView ? "text-slate-500" : "text-[#7A7167]"
                      }`}
                    >
                      {card.description}
                    </p>
                  </div>

                  <div
                    className={`mt-4 pt-3 border-t text-xs font-medium ${
                      isMerchantView
                        ? "border-slate-100 text-slate-700 group-hover:text-slate-900"
                        : "border-[#F2ECE4] text-[#8C3B4A]"
                    }`}
                  >
                    点击直接提问 →
                  </div>
                </button>
              ))}
            </div>
          </div>
        ) : (
          /* 对话与业务卡片流 */
          <div className="space-y-6 pb-6">
            {/* 轻量场景快捷胶囊条 */}
            <div className="flex items-center gap-2 overflow-x-auto pb-1">
              <span
                className={`text-xs shrink-0 ${
                  isMerchantView ? "text-slate-400" : "text-[#9C9184]"
                }`}
              >
                常问场景：
              </span>
              {SCENARIO_CARDS[activeTab].map((item, idx) => (
                <button
                  key={idx}
                  type="button"
                  disabled={isBusy}
                  onClick={() => void handleSendMessage(item.prompt)}
                  className={`shrink-0 rounded-full border px-3.5 py-1 text-xs font-medium transition-colors disabled:opacity-50 cursor-pointer ${
                    isMerchantView
                      ? "border-slate-200 bg-white text-slate-700 hover:border-slate-400"
                      : "border-[#E6DDD1] bg-white text-[#4A433C] hover:border-[#C89EA6] hover:text-[#8C3B4A]"
                  }`}
                >
                  {item.title}
                </button>
              ))}
            </div>

            {currentSession.turns.map((turn) => {
              const orderedTools = turn.toolOrder
                .map((id) => turn.toolsById[id])
                .filter(Boolean);

              const displayComparisons =
                turn.artifacts.sku_comparisons.length > 0
                  ? turn.artifacts.sku_comparisons
                  : !isMerchantView
                  ? buildFallbackComparisonFromSearch(
                      turn.artifacts.search_results
                    )
                  : [];

              const formattedAnswer = formatFinalAnswerText(turn.finalAnswer);

              return (
                <div key={turn.id} className="space-y-4">
                  {/* 用户消息气泡 */}
                  <div className="flex justify-end">
                    <div
                      className={`max-w-2xl rounded-2xl rounded-tr-sm px-4 py-3 text-sm shadow-2xs ${
                        isMerchantView
                          ? "bg-slate-900 text-white"
                          : "bg-[#8C3B4A] text-white"
                      }`}
                    >
                      <p className="leading-relaxed">{turn.userMessage}</p>
                      <div className="mt-1 text-right text-[11px] opacity-70 tabular-nums">
                        {turn.createdAt}
                      </div>
                    </div>
                  </div>

                  {/* 顾问/经营助手回复卡片容器 */}
                  <div
                    className={`rounded-2xl border p-4 sm:p-5 space-y-4 ${
                      isMerchantView
                        ? "border-slate-200/90 bg-white shadow-2xs"
                        : "border-[#EAE2D7] bg-white shadow-[0_2px_16px_-4px_rgba(60,42,33,0.05)]"
                    }`}
                  >
                    {/* 安静可折叠的步骤进度条 */}
                    <ToolCallStatusList
                      isStreaming={turn.isStreaming}
                      isMerchant={isMerchantView}
                      activeThinking={turn.activeThinking}
                      thinkingHistory={turn.thinkingHistory}
                      toolSteps={orderedTools}
                    />

                    {/* 买家：商品推荐与对比卡 */}
                    {displayComparisons.map((comp, idx) => (
                      <ProductComparisonCard
                        key={idx}
                        comparison={comp}
                        stockChecks={turn.artifacts.stock_checks}
                      />
                    ))}

                    {/* 买家：订单与物流状态卡 */}
                    {(turn.artifacts.orders.length > 0 ||
                      turn.artifacts.logistics.length > 0 ||
                      turn.artifacts.refund_results.length > 0) && (
                      <OrderAndLogisticsCard
                        orders={turn.artifacts.orders}
                        logistics={turn.artifacts.logistics}
                        refundResults={turn.artifacts.refund_results}
                      />
                    )}

                    {/* 商家：退货异常诊断卡 */}
                    {turn.artifacts.diagnosis_artifact && (
                      <DiagnosisArtifactCard
                        artifact={turn.artifacts.diagnosis_artifact}
                      />
                    )}

                    {/* 商家：新款开发建议卡 */}
                    {turn.artifacts.selection_proposal && (
                      <SelectionProposalCard
                        proposal={turn.artifacts.selection_proposal}
                        committed={turn.artifacts.committed_proposal}
                        approvalStatus={turn.artifacts.approval_status}
                      />
                    )}

                    {/* 待确认操作温和提示条（关闭弹窗后可再次点开） */}
                    {turn.pendingHitl && (
                      <div
                        className={`flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3 text-xs ${
                          isMerchantView
                            ? "border-amber-200 bg-amber-50/70 text-slate-800"
                            : "border-[#E5C9CE] bg-[#FDF6F7] text-[#3D3833]"
                        }`}
                      >
                        <div>
                          <div className="font-semibold">
                            {turn.pendingHitl.tool_name === "oms__create_refund"
                              ? "请确认您的退款申请"
                              : turn.pendingHitl.tool_name ===
                                "pim__draft_sku_proposal"
                              ? "新款开发方案已生成，请确认是否提交建档"
                              : "请确认您的订单信息修改"}
                          </div>
                          <div className="mt-0.5 text-slate-600">
                            {turn.pendingHitl.tool_name === "oms__create_refund"
                              ? "核对退款商品与金额无误后，点击右侧按钮即可提交原路退款。"
                              : turn.pendingHitl.tool_name ===
                                "pim__draft_sku_proposal"
                              ? "确认通过后将建立新款商品档案，并同步更新至门店导购卖点库。"
                              : "确认后将立即为您更新收货地址。"}
                          </div>
                        </div>
                        <button
                          type="button"
                          onClick={() =>
                            setActiveHitlModal({
                              perspective: activeTab,
                              turnId: turn.id,
                              interrupt: turn.pendingHitl!,
                            })
                          }
                          className={`rounded-xl px-4 py-2 text-xs font-semibold text-white cursor-pointer ${
                            isMerchantView
                              ? "bg-slate-900 hover:bg-slate-800"
                              : "bg-[#8C3B4A] hover:bg-[#76303D]"
                          }`}
                        >
                          {isMerchantView ? "查看并确认" : "立即确认"}
                        </button>
                      </div>
                    )}

                    {/* 确认结果轻量状态条 */}
                    {turn.hitlDecisionNote && (
                      <div
                        className={`flex items-center gap-2 rounded-xl border px-3.5 py-2 text-xs ${
                          turn.hitlDecisionNote.decision === "approve"
                            ? "border-emerald-200/80 bg-emerald-50/60 text-emerald-900"
                            : "border-stone-200 bg-stone-50 text-stone-600"
                        }`}
                      >
                        {turn.hitlDecisionNote.decision === "approve" ? (
                          <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />
                        ) : (
                          <XCircle className="h-4 w-4 text-stone-400 shrink-0" />
                        )}
                        <span>
                          {turn.hitlDecisionNote.decision === "approve"
                            ? isMerchantView
                              ? "已确认通过新款建档"
                              : "已确认提交服务申请"
                            : isMerchantView
                            ? "已退回暂缓本次新款建档"
                            : "已暂缓处理本次申请"}
                          {turn.hitlDecisionNote.comment
                            ? ` · 备注：“${turn.hitlDecisionNote.comment}”`
                            : ""}
                        </span>
                      </div>
                    )}

                    {/* 自然流畅的中文最终回复 */}
                    {formattedAnswer && (
                      <div
                        className={`rounded-xl px-4 py-3.5 text-sm leading-relaxed whitespace-pre-wrap border ${
                          isMerchantView
                            ? "bg-slate-50/75 text-slate-800 border-slate-200/70"
                            : "bg-[#FAF8F5] text-[#2C2825] border-[#EFEAE2]"
                        }`}
                      >
                        {formattedAnswer}
                      </div>
                    )}

                    {/* 异常提示 */}
                    {turn.streamError && (
                      <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50/70 px-4 py-3 text-xs text-rose-800">
                        <AlertCircle className="h-4 w-4 text-rose-600 shrink-0" />
                        <span>{turn.streamError}</span>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
            <div ref={bottomEndRef} />
          </div>
        )}
      </main>

      {/* 底部顺手提问输入栏（支持回车直接发送） */}
      <footer
        className={`sticky bottom-0 z-20 border-t backdrop-blur-md transition-colors ${
          isMerchantView
            ? "border-slate-200/90 bg-white/95"
            : "border-[#EAE3DA] bg-[#FAF8F5]/95"
        }`}
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleSendMessage();
          }}
          className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-3.5 sm:px-6"
        >
          <input
            type="text"
            value={currentSession.draft}
            onChange={(e) =>
              setSessions((prev) => ({
                ...prev,
                [activeTab]: {
                  ...prev[activeTab],
                  draft: e.target.value,
                },
              }))
            }
            disabled={isBusy}
            placeholder={
              isMerchantView
                ? "输入您想诊断的女装品类、退货原因分析或新款毛利测算需求，按回车发送…"
                : "想挑选什么风格或尺码？也可以直接查订单、顺丰物流或申请退款，按回车发送…"
            }
            className={`flex-1 rounded-full border px-5 py-3 text-sm transition-all focus:outline-none disabled:opacity-60 ${
              isMerchantView
                ? "border-slate-300 bg-slate-50/70 text-slate-900 placeholder:text-slate-400 focus:border-slate-900 focus:bg-white"
                : "border-[#DFD6CA] bg-white text-[#1F1D1A] placeholder:text-[#9C9184] focus:border-[#8C3B4A] focus:ring-2 focus:ring-[#8C3B4A]/15"
            }`}
          />

          <button
            type="submit"
            disabled={isBusy || !currentSession.draft.trim()}
            className={`inline-flex items-center gap-2 rounded-full px-6 py-3 text-sm font-semibold text-white shadow-xs transition-colors disabled:opacity-45 cursor-pointer ${
              isMerchantView
                ? "bg-slate-900 hover:bg-slate-800"
                : "bg-[#8C3B4A] hover:bg-[#76303D]"
            }`}
          >
            {isBusy ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>处理中</span>
              </>
            ) : (
              <>
                <Send className="h-4 w-4" />
                <span>发送</span>
              </>
            )}
          </button>
        </form>
      </footer>

      {/* 退款 / 改址 / 新款建档 确认弹窗 */}
      <HitlApprovalModal
        key={activeHitlModal?.interrupt.tool_call_id ?? "closed"}
        interrupt={activeHitlModal?.interrupt ?? null}
        isSubmitting={isResumingHitl}
        onDecision={(decision, comment) =>
          void handleHitlDecision(decision, comment)
        }
      />

      {/* 右上角低调入口打开的操作审计抽屉 */}
      <AuditLogsDrawer
        isOpen={isAuditOpen}
        onClose={() => setIsAuditOpen(false)}
        logs={auditLogs}
        viewer={auditViewer}
        isLoading={auditLoading}
        onRefresh={() => {
          if (currentSession.token) {
            void refreshAuditTrail(currentSession.token);
          }
        }}
      />
    </div>
  );
}
