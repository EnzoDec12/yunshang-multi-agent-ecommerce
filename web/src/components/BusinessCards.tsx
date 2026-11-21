"use client";

import React, { useState } from "react";
import {
  AlertCircle,
  ArrowUpRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  MapPin,
  Package,
  Sparkles,
  Star,
  TrendingUp,
  Truck,
  XCircle,
} from "lucide-react";
import type {
  CheckStockData,
  CommittedProposalData,
  CompareSkusData,
  CreateRefundData,
  DiagnosisArtifact,
  LogisticsPackageData,
  OrderRecord,
  SelectionProposal,
} from "@/types/agent";
import {
  METRIC_NAME_LABELS,
  ORDER_STATUS_LABELS,
  cleanDisplayText,
  formatCurrency,
  resolveColorSwatch,
} from "@/lib/presenters";

function splitSizeBadges(rawSizes?: string): string[] {
  if (!rawSizes) return [];
  return rawSizes
    .split(/[\/、,，]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function ProductComparisonCard({
  comparison,
  stockChecks,
}: {
  comparison: CompareSkusData;
  stockChecks: CheckStockData[];
}) {
  const stockBySku = new Map(stockChecks.map((s) => [s.sku_id, s]));
  const gridColsClass =
    comparison.entries.length >= 3
      ? "grid-cols-1 lg:grid-cols-3"
      : "grid-cols-1 md:grid-cols-2";

  return (
    <div className="overflow-hidden rounded-2xl border border-[#E8E0D5] bg-white shadow-[0_2px_14px_-4px_rgba(60,42,33,0.06)]">
      {/* 卡片顶栏 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#EFEAE2] bg-[#FCF9F5] px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-[#8C3B4A]/10 text-[#8C3B4A]">
            <Sparkles className="h-3.5 w-3.5" />
          </span>
          <div>
            <h4 className="text-sm font-semibold text-[#1F1D1A]">
              {comparison.entries.length > 1
                ? "女装款式对比与尺码推荐"
                : "精选女装款式推荐"}
            </h4>
            <p className="text-xs text-[#7A7167]">
              已为您核对当季在售颜色、尺码、面料与实时库存
            </p>
          </div>
        </div>
        <span className="rounded-full border border-[#E6DDD1] bg-white px-3 py-1 text-xs font-medium text-[#5C5349] tabular-nums">
          共 {comparison.compared_count} 款 · {formatCurrency(comparison.min_price)}
          {comparison.max_price > comparison.min_price
            ? ` – ${formatCurrency(comparison.max_price)}`
            : ""}
        </span>
      </div>

      {/* 款式卡片网格 */}
      <div className={`grid ${gridColsClass} gap-4 p-4 sm:p-5`}>
        {comparison.entries.map((sku, idx) => {
          const stockDetail = stockBySku.get(sku.sku_id);
          const stockQty = stockDetail?.available_stock ?? sku.available_stock;
          const colorName = sku.color || sku.specs?.color || "";
          const sizesRaw = sku.sizes || sku.specs?.sizes || "";
          const sizeList = splitSizeBadges(sizesRaw);
          const fabricText = cleanDisplayText(
            sku.fabric || sku.specs?.fabric || ""
          );
          const fitText = cleanDisplayText(sku.specs?.fit || "");
          const careText = cleanDisplayText(sku.specs?.care || "");
          const stylingText = cleanDisplayText(sku.specs?.styling_match || "");

          const stockStatus =
            stockQty <= 0
              ? {
                  label: "已售罄",
                  cls: "bg-stone-100 text-stone-500 border-stone-200",
                }
              : stockQty <= 10
              ? {
                  label: `仅剩 ${stockQty} 件`,
                  cls: "bg-amber-50 text-amber-800 border-amber-200",
                }
              : {
                  label: "现货充足",
                  cls: "bg-[#EDF5F1] text-[#2E6B56] border-[#CBE3D8]",
                };

          return (
            <div
              key={`${sku.name}-${idx}`}
              className={`flex flex-col justify-between rounded-2xl border p-4 transition-shadow ${
                idx === 0
                  ? "border-[#D8B8BE] bg-[#FDFBFA] shadow-[0_2px_10px_-3px_rgba(140,59,74,0.08)]"
                  : "border-[#ECE5DC] bg-[#FCFAF8]/60"
              }`}
            >
              <div>
                {/* 顶部标签行：品类 + 店长主推 + 好评度 */}
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <div className="flex items-center gap-1.5">
                    <span className="rounded-full bg-[#F3EDE4] px-2.5 py-0.5 text-[11px] font-medium text-[#5E554B]">
                      {sku.category}
                    </span>
                    {idx === 0 && (
                      <span className="rounded-full bg-[#8C3B4A] px-2.5 py-0.5 text-[11px] font-medium text-white">
                        主推优选
                      </span>
                    )}
                  </div>
                  <span className="inline-flex items-center gap-1 text-xs font-medium text-[#8C6D46] tabular-nums">
                    <Star className="h-3.5 w-3.5 fill-[#D4A359] text-[#D4A359]" />
                    {sku.rating.toFixed(1)} 好评
                  </span>
                </div>

                {/* 商品中文名 */}
                <h5 className="mt-2.5 text-[15px] font-semibold leading-snug text-[#1F1D1A]">
                  {cleanDisplayText(sku.name)}
                </h5>

                {/* 价格与现货状态 */}
                <div className="mt-3 flex items-baseline justify-between gap-2 border-b border-[#EFEAE2] pb-3">
                  <div className="text-xl font-bold tracking-tight text-[#8C3B4A] tabular-nums">
                    {formatCurrency(sku.retail_price)}
                  </div>
                  <span
                    className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${stockStatus.cls}`}
                  >
                    {stockStatus.label}
                  </span>
                </div>

                {/* 颜色色板 + 可选尺码标签 */}
                <div className="mt-3 space-y-2 text-xs">
                  {colorName && (
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[#7A7167]">颜色</span>
                      <span className="inline-flex items-center gap-1.5 font-medium text-[#2C2825]">
                        <span
                          className="h-3 w-3 rounded-full border border-black/15"
                          style={{
                            backgroundColor: resolveColorSwatch(colorName),
                          }}
                        />
                        {colorName}
                      </span>
                    </div>
                  )}

                  {sizeList.length > 0 && (
                    <div className="flex items-start justify-between gap-2">
                      <span className="text-[#7A7167] shrink-0 pt-0.5">
                        可选尺码
                      </span>
                      <div className="flex flex-wrap justify-end gap-1">
                        {sizeList.map((sz) => (
                          <span
                            key={sz}
                            className="rounded-md border border-[#E5DDD2] bg-white px-2 py-0.5 text-[11px] font-medium text-[#3B3530]"
                          >
                            {sz}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>

                {/* 面料、版型、穿搭与洗护信息 */}
                <div className="mt-3 space-y-2 rounded-xl bg-white p-3 border border-[#EFEAE2] text-xs leading-relaxed">
                  {fabricText && (
                    <div>
                      <span className="font-medium text-[#6E665E]">面料材质：</span>
                      <span className="text-[#262320]">{fabricText}</span>
                    </div>
                  )}
                  {fitText && (
                    <div>
                      <span className="font-medium text-[#6E665E]">版型剪裁：</span>
                      <span className="text-[#262320]">{fitText}</span>
                    </div>
                  )}
                  {stylingText && (
                    <div>
                      <span className="font-medium text-[#6E665E]">穿搭建议：</span>
                      <span className="text-[#262320]">{stylingText}</span>
                    </div>
                  )}
                  {careText && (
                    <div>
                      <span className="font-medium text-[#6E665E]">洗护方式：</span>
                      <span className="text-[#5C5349]">{careText}</span>
                    </div>
                  )}
                </div>
              </div>

              {/* 款式亮点 */}
              {sku.selling_points.length > 0 && (
                <div className="mt-3.5 pt-3 border-t border-[#EFEAE2]">
                  <div className="mb-1.5 text-[11px] font-medium text-[#7A7167]">
                    款式亮点
                  </div>
                  <ul className="space-y-1.5">
                    {sku.selling_points.map((pt, i) => (
                      <li
                        key={i}
                        className="flex items-start gap-1.5 text-xs leading-relaxed text-[#3D3833]"
                      >
                        <Check className="mt-0.5 h-3.5 w-3.5 text-[#8C3B4A] shrink-0" />
                        <span>{cleanDisplayText(pt)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function OrderAndLogisticsCard({
  orders,
  logistics,
  refundResults,
}: {
  orders: OrderRecord[];
  logistics: LogisticsPackageData[];
  refundResults: CreateRefundData[];
}) {
  const [expandedTracking, setExpandedTracking] = useState<
    Record<string, boolean>
  >({});

  const logisticsByOrder = new Map(logistics.map((pkg) => [pkg.order_id, pkg]));
  const refundByOrder = new Map(refundResults.map((r) => [r.order_id, r]));

  if (
    orders.length === 0 &&
    logistics.length === 0 &&
    refundResults.length === 0
  ) {
    return null;
  }

  const toggleLogistics = (trackingNo: string) => {
    setExpandedTracking((prev) => ({
      ...prev,
      [trackingNo]: !prev[trackingNo],
    }));
  };

  return (
    <div className="overflow-hidden rounded-2xl border border-[#E8E0D5] bg-white shadow-[0_2px_14px_-4px_rgba(60,42,33,0.06)]">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#EFEAE2] bg-[#FCF9F5] px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-[#8C3B4A]/10 text-[#8C3B4A]">
            <Package className="h-3.5 w-3.5" />
          </span>
          <div>
            <h4 className="text-sm font-semibold text-[#1F1D1A]">
              我的订单与物流状态
            </h4>
            <p className="text-xs text-[#7A7167]">
              仅展示您本人的订单记录，收件人信息已做隐私保护
            </p>
          </div>
        </div>
        {orders.length > 0 && (
          <span className="text-xs text-[#7A7167] tabular-nums">
            共 {orders.length} 笔订单
          </span>
        )}
      </div>

      <div className="divide-y divide-[#F0EBE3]">
        {orders.map((ord) => {
          const refundRec = refundByOrder.get(ord.order_id);
          const effectiveStatus = refundRec ? "refunded" : ord.status;
          const statusMeta = ORDER_STATUS_LABELS[effectiveStatus] ?? {
            label: "处理中",
            badgeClass: "bg-stone-100 text-stone-700 border-stone-200",
            dotClass: "bg-stone-400",
          };
          const pkg = logisticsByOrder.get(ord.order_id);
          const reversedCheckpoints = pkg
            ? [...pkg.checkpoints].reverse()
            : [];
          const latestCkpt = reversedCheckpoints[0];
          const isFullTrackOpen = pkg
            ? Boolean(expandedTracking[pkg.tracking_no])
            : false;

          return (
            <div key={ord.order_id} className="p-4 sm:p-5 space-y-3">
              {/* 订单头部：编号、日期与中文状态 */}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2 text-xs text-[#7A7167]">
                  <span>订单编号 {ord.order_id}</span>
                  <span>·</span>
                  <span>下单时间 {ord.created_at}</span>
                </div>
                <div className="flex items-center gap-2">
                  {statusMeta.hint && effectiveStatus === "paid" && (
                    <span className="text-[11px] text-[#8C6D46]">
                      {statusMeta.hint}
                    </span>
                  )}
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${statusMeta.badgeClass}`}
                  >
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${statusMeta.dotClass}`}
                    />
                    {statusMeta.label}
                  </span>
                </div>
              </div>

              {/* 商品与实付金额 */}
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[#EFEAE2] bg-[#FAF8F5] px-4 py-3">
                <div className="min-w-0">
                  <div className="text-sm font-semibold text-[#1F1D1A]">
                    {cleanDisplayText(ord.sku_name)}
                  </div>
                  <div className="mt-0.5 text-xs text-[#7A7167]">
                    数量：{ord.quantity} 件
                  </div>
                </div>
                <div className="text-right tabular-nums">
                  <div className="text-[11px] text-[#7A7167]">实付金额</div>
                  <div className="text-base font-bold text-[#1F1D1A]">
                    ¥{ord.total_amount.toFixed(2)}
                  </div>
                </div>
              </div>

              {/* 收件信息 */}
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[#6E665E]">
                <span className="inline-flex items-center gap-1">
                  <MapPin className="h-3.5 w-3.5 text-[#9B9186]" />
                  收件人：
                  <strong className="font-medium text-[#2C2825]">
                    {ord.buyer_name}
                  </strong>
                </span>
                <span className="tabular-nums">{ord.buyer_phone}</span>
                <span>收货地址：{ord.shipping_address}</span>
              </div>

              {/* 退款成功提示 */}
              {(refundRec || ord.refund_id) && (
                <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-rose-200/80 bg-rose-50/60 px-4 py-2.5 text-xs text-rose-900">
                  <div className="flex items-center gap-2">
                    <CheckCircle2 className="h-4 w-4 text-rose-600 shrink-0" />
                    <span>
                      退款申请已通过，款项将原路退回您的支付账户（服务单号{" "}
                      {refundRec?.refund_id ?? ord.refund_id}）
                    </span>
                  </div>
                  {refundRec && (
                    <span className="font-semibold text-rose-700 tabular-nums">
                      退回 ¥{refundRec.refund_amount.toFixed(2)}
                    </span>
                  )}
                </div>
              )}

              {/* 快递与最新一条物流动态（完整轨迹可展开） */}
              {pkg && latestCkpt && (
                <div className="rounded-xl border border-[#DCE8E3] bg-[#F6FAF8] p-3.5">
                  <div className="flex flex-wrap items-center justify-between gap-2 pb-2.5 border-b border-[#DCE8E3]">
                    <div className="flex items-center gap-2 text-xs font-semibold text-[#234E3F]">
                      <Truck className="h-4 w-4 text-[#2E6B56]" />
                      <span>{pkg.carrier}</span>
                      <span className="font-normal text-[#4C7365] tabular-nums">
                        运单号 {pkg.tracking_no}
                      </span>
                    </div>
                    <span className="text-[11px] font-medium text-[#2E6B56]">
                      预计送达：{pkg.estimated_delivery}
                    </span>
                  </div>

                  {/* 默认展示最新一条动态 */}
                  {!isFullTrackOpen ? (
                    <div className="mt-2.5 flex flex-wrap items-start justify-between gap-3 text-xs">
                      <div className="space-y-0.5 max-w-xl">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="inline-flex items-center rounded bg-[#2E6B56] px-1.5 py-0.5 text-[10px] font-medium text-white">
                            最新动态
                          </span>
                          <span className="font-semibold text-[#1F1D1A]">
                            {latestCkpt.location}
                          </span>
                          <span className="text-[11px] text-[#7A7167] tabular-nums">
                            {latestCkpt.timestamp}
                          </span>
                        </div>
                        <p className="pt-0.5 text-[#3D3833] leading-relaxed">
                          {latestCkpt.status}
                        </p>
                      </div>

                      {reversedCheckpoints.length > 1 && (
                        <button
                          type="button"
                          onClick={() => toggleLogistics(pkg.tracking_no)}
                          className="inline-flex items-center gap-1 text-xs font-medium text-[#2E6B56] hover:underline shrink-0 cursor-pointer"
                        >
                          <span>
                            查看完整轨迹（{reversedCheckpoints.length} 站）
                          </span>
                          <ChevronDown className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="mt-3 space-y-2.5">
                      {reversedCheckpoints.map((ckpt, idx) => (
                        <div key={idx} className="flex gap-3 text-xs">
                          <div className="flex flex-col items-center">
                            <span
                              className={`mt-1 h-2 w-2 rounded-full ${
                                idx === 0
                                  ? "bg-[#2E6B56] ring-4 ring-[#DCE8E3]"
                                  : "bg-stone-300"
                              }`}
                            />
                            {idx < reversedCheckpoints.length - 1 && (
                              <span className="mt-1 h-full w-px bg-[#DCE8E3]" />
                            )}
                          </div>
                          <div className="pb-1.5">
                            <div className="flex flex-wrap items-center gap-2">
                              <span
                                className={`font-semibold ${
                                  idx === 0
                                    ? "text-[#234E3F]"
                                    : "text-[#5C5349]"
                                }`}
                              >
                                {ckpt.location}
                              </span>
                              <span className="text-[11px] text-[#8C8277] tabular-nums">
                                {ckpt.timestamp}
                              </span>
                            </div>
                            <p
                              className={`mt-0.5 leading-relaxed ${
                                idx === 0
                                  ? "font-medium text-[#1F1D1A]"
                                  : "text-[#6E665E]"
                              }`}
                            >
                              {ckpt.status}
                            </p>
                          </div>
                        </div>
                      ))}

                      <button
                        type="button"
                        onClick={() => toggleLogistics(pkg.tracking_no)}
                        className="inline-flex items-center gap-1 text-xs font-medium text-[#2E6B56] hover:underline cursor-pointer"
                      >
                        <span>收起完整轨迹</span>
                        <ChevronUp className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}

        {/* 单独退款回执兜底展示 */}
        {orders.length === 0 &&
          refundResults.map((rf) => (
            <div key={rf.refund_id} className="p-4 sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50/60 px-4 py-3 text-xs text-emerald-900">
                <div>
                  <div className="font-semibold">
                    退款申请已受理（服务单号 {rf.refund_id}）
                  </div>
                  <div className="mt-0.5 text-emerald-700">
                    订单编号 {rf.order_id} · 退款说明：{rf.reason}
                  </div>
                </div>
                <div className="text-base font-bold text-emerald-800 tabular-nums">
                  ¥{rf.refund_amount.toFixed(2)}
                </div>
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}

export function DiagnosisArtifactCard({
  artifact,
}: {
  artifact: DiagnosisArtifact;
}) {
  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200/90 bg-white shadow-xs">
      {/* 标题栏 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-slate-50/80 px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-rose-50 text-rose-600 border border-rose-200/70">
            <TrendingUp className="h-4 w-4" />
          </span>
          <div>
            <h4 className="text-sm font-semibold text-slate-900">
              {artifact.problem_category}退货异常诊断
            </h4>
            <p className="text-xs text-slate-500">
              近 3 日门店成交表现、退货原因分布与价格带机会总结
            </p>
          </div>
        </div>
        <span className="rounded-full border border-rose-200 bg-rose-50 px-3 py-0.5 text-xs font-semibold text-rose-700">
          重点关注品类 · {artifact.problem_category}
        </span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 p-4 sm:p-5">
        {/* 左侧：异常指标与空白价格带机会 */}
        <div className="space-y-3.5">
          {artifact.anomalous_metrics.map((m, idx) => {
            const metricTitle =
              METRIC_NAME_LABELS[m.metric_name] ?? "核心经营指标";
            return (
              <div
                key={idx}
                className="rounded-xl border border-rose-200/75 bg-rose-50/35 p-4"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-rose-800">
                    {metricTitle}偏高预警
                  </span>
                  <span className="inline-flex items-center gap-0.5 rounded-full bg-rose-600 px-2.5 py-0.5 text-[11px] font-semibold text-white tabular-nums">
                    <ArrowUpRight className="h-3 w-3" />
                    超出基准{" "}
                    {m.delta_pct > 0
                      ? `+${m.delta_pct.toFixed(1)}%`
                      : `${m.delta_pct.toFixed(1)}%`}
                  </span>
                </div>

                <div className="mt-3 flex items-baseline gap-6 tabular-nums">
                  <div>
                    <div className="text-[11px] text-slate-500">当前实测值</div>
                    <div className="mt-0.5 text-2xl font-bold text-rose-600">
                      {(m.current_value * 100).toFixed(2)}%
                    </div>
                  </div>
                  <div className="border-l border-rose-200/70 pl-6">
                    <div className="text-[11px] text-slate-500">
                      女装健康警戒线
                    </div>
                    <div className="mt-0.5 text-lg font-semibold text-slate-700">
                      {(m.benchmark_value * 100).toFixed(2)}%
                    </div>
                  </div>
                </div>

                <p className="mt-2.5 text-xs leading-relaxed text-slate-700">
                  {cleanDisplayText(m.summary)}
                </p>
              </div>
            );
          })}

          {artifact.price_band_gaps.map((gap, idx) => (
            <div
              key={idx}
              className="rounded-xl border border-amber-200/80 bg-amber-50/35 p-4"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold text-amber-900">
                  价格带空白机会结论
                </span>
                <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-[11px] font-semibold text-amber-900 tabular-nums">
                  消费者需求热度 {gap.demand_index.toFixed(1)} 倍
                </span>
              </div>
              <div className="mt-1.5 text-lg font-bold text-slate-900 tabular-nums">
                ¥{gap.min_price.toFixed(0)} – ¥{gap.max_price.toFixed(0)} 价格带
              </div>
              <p className="mt-1 text-xs leading-relaxed text-slate-700">
                {cleanDisplayText(gap.gap_description)}
              </p>
            </div>
          ))}
        </div>

        {/* 右侧：Top 3 退货原因条形占比 */}
        <div className="flex flex-col justify-between rounded-xl border border-slate-200/85 bg-slate-50/50 p-4">
          <div>
            <div className="mb-3.5 flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-900">
                主要退货原因分布（Top {artifact.top_return_reasons.length}）
              </span>
              <span className="text-[11px] text-slate-500">
                近 3 日退货单量与占比
              </span>
            </div>

            <div className="space-y-3.5">
              {artifact.top_return_reasons.map((item, idx) => {
                const pct = Math.round(item.share * 1000) / 10;
                return (
                  <div key={idx} className="space-y-1.5">
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span className="font-medium text-slate-800">
                        {idx + 1}. {item.reason}
                      </span>
                      <span className="shrink-0 text-slate-600 tabular-nums">
                        {item.order_count} 单 · {pct.toFixed(1)}%
                      </span>
                    </div>
                    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200/80">
                      <div
                        className={`h-full rounded-full transition-all ${
                          idx === 0
                            ? "bg-rose-500"
                            : idx === 1
                            ? "bg-amber-500"
                            : "bg-slate-500"
                        }`}
                        style={{ width: `${Math.min(100, Math.max(8, pct))}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="mt-4 rounded-lg bg-white px-3 py-2 text-[11px] text-slate-600 border border-slate-200/70">
            诊断建议：优先围绕排名靠前的「防透内衬」与「梨形腰臀剪裁」痛点开发升级面料款，切入高需求空白价格带。
          </div>
        </div>
      </div>
    </div>
  );
}

export function SelectionProposalCard({
  proposal,
  committed,
  approvalStatus,
}: {
  proposal: SelectionProposal;
  committed?: CommittedProposalData | null;
  approvalStatus?: "approved" | "rejected" | null;
}) {
  const leadSku = proposal.candidate_skus[0];

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200/90 bg-white shadow-xs">
      {/* 顶栏 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-slate-50/80 px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600 border border-emerald-200/70">
            <Sparkles className="h-4 w-4" />
          </span>
          <div>
            <h4 className="text-sm font-semibold text-slate-900">
              新款开发建议
            </h4>
            <p className="text-xs text-slate-500">
              结合退货痛点改良方案与成衣供应链成本、毛利率测算
            </p>
          </div>
        </div>

        {approvalStatus === "approved" && committed ? (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-0.5 text-xs font-semibold text-emerald-700">
            <CheckCircle2 className="h-3.5 w-3.5" />
            已确认建档 · 导购卖点库已同步
          </span>
        ) : approvalStatus === "rejected" ? (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-100 px-3 py-0.5 text-xs font-medium text-slate-600">
            <XCircle className="h-3.5 w-3.5 text-slate-500" />
            已退回暂缓 · 未建立档案
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-3 py-0.5 text-xs font-semibold text-amber-800">
            <AlertCircle className="h-3.5 w-3.5" />
            等待确认建档
          </span>
        )}
      </div>

      <div className="p-4 sm:p-5 space-y-4">
        {leadSku && (
          <div className="rounded-xl border border-slate-200/85 bg-slate-50/50 p-4 space-y-3.5">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200/75 pb-3">
              <div>
                <div className="text-[11px] font-medium text-slate-500">
                  建议开发款式
                </div>
                <h5 className="mt-0.5 text-base font-bold text-slate-900">
                  {cleanDisplayText(leadSku.sku_name)}
                </h5>
              </div>
              {committed?.initial_order_qty ? (
                <span className="rounded-lg border border-slate-200 bg-white px-3 py-1 text-xs font-semibold text-slate-700 tabular-nums">
                  首批备货 {committed.initial_order_qty} 件
                </span>
              ) : null}
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 tabular-nums">
              <div className="rounded-xl border border-slate-200/80 bg-white p-3.5">
                <div className="text-xs text-slate-500">建议售价</div>
                <div className="mt-1 text-xl font-bold text-slate-900">
                  ¥{leadSku.target_price.toFixed(2)}
                </div>
              </div>

              <div className="rounded-xl border border-slate-200/80 bg-white p-3.5">
                <div className="text-xs text-slate-500">单件预估成本</div>
                <div className="mt-1 text-xl font-bold text-slate-700">
                  ¥{leadSku.estimated_unit_cost.toFixed(2)}
                </div>
              </div>

              <div className="rounded-xl border border-emerald-200 bg-emerald-50/60 p-3.5">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-emerald-800">
                    预估毛利率
                  </span>
                  <span className="text-[10px] font-medium text-emerald-700">
                    高于 15% 安全线
                  </span>
                </div>
                <div className="mt-1 text-2xl font-extrabold text-emerald-700">
                  {(proposal.estimated_gross_margin * 100).toFixed(2)}%
                </div>
              </div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="rounded-xl border border-emerald-200/70 bg-emerald-50/25 p-4">
            <div className="mb-2.5 text-xs font-semibold text-emerald-900">
              核心卖点清单
            </div>
            <ul className="space-y-2">
              {proposal.core_selling_points.map((pt, idx) => (
                <li
                  key={idx}
                  className="flex items-start gap-2 text-xs leading-relaxed text-slate-800"
                >
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 text-emerald-600 shrink-0" />
                  <span>{cleanDisplayText(pt)}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="rounded-xl border border-amber-200/75 bg-amber-50/25 p-4">
            <div className="mb-2.5 text-xs font-semibold text-amber-900">
              供应链与试销风险提醒
            </div>
            <ul className="space-y-2">
              {proposal.risk_warnings.map((warn, idx) => (
                <li
                  key={idx}
                  className="flex items-start gap-2 text-xs leading-relaxed text-slate-700"
                >
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 text-amber-600 shrink-0" />
                  <span>{cleanDisplayText(warn)}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {committed && approvalStatus === "approved" && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50/70 px-4 py-2.5 text-xs text-emerald-900">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />
              <span>
                新款建档已完成（单号 {committed.proposal_id}），改良卖点已同步更新至门店导购卖点库，顾客咨询时可直接引用推荐。
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
