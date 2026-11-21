"use client";

import React, { useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  MapPin,
  RefreshCw,
  ShoppingBag,
  Sparkles,
} from "lucide-react";
import type { HITLInterruptEvent } from "@/types/agent";
import { ORDER_STATUS_LABELS, cleanDisplayText } from "@/lib/presenters";

interface Props {
  interrupt: HITLInterruptEvent | null;
  isSubmitting: boolean;
  onDecision: (decision: "approve" | "reject", comment: string) => void;
}

export function HitlApprovalModal({
  interrupt,
  isSubmitting,
  onDecision,
}: Props) {
  const [comment, setComment] = useState("");

  if (!interrupt) return null;

  const isCustomerRefund = interrupt.tool_name === "oms__create_refund";
  const isAddressUpdate =
    interrupt.tool_name === "oms__update_address" ||
    interrupt.gate_type === "address_confirmation";
  const isMerchantProposal = interrupt.tool_name === "pim__draft_sku_proposal";

  const refund = interrupt.refund_details;
  const proposal = interrupt.selection_proposal;
  const leadSku = proposal?.candidate_skus?.[0];

  const modalTitle = isCustomerRefund
    ? "请确认您的退款申请"
    : isAddressUpdate
    ? "请确认修改收货地址"
    : "确认提交新款建档";

  const modalSubtitle = isCustomerRefund
    ? "核对订单与退款金额无误后，我们将立即为您提交原路退款"
    : isAddressUpdate
    ? "订单尚未发出，确认后将为您更新收件人及收货地址"
    : "请核对建议售价、预估毛利率与改良卖点，确认后将正式建档";

  const approveLabel = isMerchantProposal ? "确认通过" : "确认提交";
  const rejectLabel = isMerchantProposal ? "退回暂缓" : "暂不处理";

  const defaultNote = isCustomerRefund
    ? "确认办理原路退款"
    : isAddressUpdate
    ? "确认修改为新收货地址"
    : "双层防透内衬与梨形剪裁切中退货痛点，毛利达标，同意建档试销";

  const orderStatusLabel = refund?.order_status
    ? ORDER_STATUS_LABELS[refund.order_status]?.label ?? "待处理"
    : "待发货";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-[2px]">
      <div className="w-full max-w-xl overflow-hidden rounded-2xl border border-stone-200/90 bg-white shadow-2xl">
        {/* 温和清晰的弹窗头部 */}
        <div
          className={`border-b px-6 py-4 ${
            isMerchantProposal
              ? "border-slate-200 bg-slate-50/90"
              : "border-[#EFEAE2] bg-[#FCF9F5]"
          }`}
        >
          <div className="flex items-center gap-3">
            <div
              className={`flex h-10 w-10 items-center justify-center rounded-xl ${
                isMerchantProposal
                  ? "bg-slate-900 text-white"
                  : "bg-[#8C3B4A] text-white"
              }`}
            >
              {isMerchantProposal ? (
                <Sparkles className="h-5 w-5" />
              ) : isAddressUpdate ? (
                <MapPin className="h-5 w-5" />
              ) : (
                <ShoppingBag className="h-5 w-5" />
              )}
            </div>
            <div>
              <h3 className="text-base font-bold text-slate-900">
                {modalTitle}
              </h3>
              <p className="mt-0.5 text-xs text-slate-500">{modalSubtitle}</p>
            </div>
          </div>
        </div>

        {/* 弹窗主体 */}
        <div className="max-h-[70vh] overflow-y-auto p-6 space-y-4">
          {/* 1. 买家售后退款确认详情 */}
          {isCustomerRefund && refund && (
            <div className="rounded-xl border border-[#ECE5DC] bg-[#FAF8F5] p-4 space-y-3.5">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#EAE3D9] pb-3">
                <div>
                  <div className="text-xs text-[#7A7167]">申请商品</div>
                  <div className="mt-0.5 text-sm font-semibold text-[#1F1D1A]">
                    {cleanDisplayText(refund.sku_name ?? "已购女装商品")}
                    {refund.quantity ? ` × ${refund.quantity} 件` : ""}
                  </div>
                </div>
                <div className="text-right tabular-nums">
                  <div className="text-xs text-[#7A7167]">预计退回金额</div>
                  <div className="mt-0.5 text-xl font-bold text-[#8C3B4A]">
                    ¥{Number(refund.refund_amount).toFixed(2)}
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 text-xs">
                <div className="rounded-lg bg-white p-3 border border-[#EFEAE2]">
                  <span className="text-[#8C8277]">订单编号</span>
                  <div className="mt-0.5 font-medium text-[#1F1D1A] tabular-nums">
                    {refund.order_id}
                  </div>
                </div>
                <div className="rounded-lg bg-white p-3 border border-[#EFEAE2]">
                  <span className="text-[#8C8277]">当前订单状态</span>
                  <div className="mt-0.5 font-medium text-[#1F1D1A]">
                    {orderStatusLabel} · 原路退回原支付账户
                  </div>
                </div>
              </div>

              <div className="rounded-lg bg-white px-3.5 py-2.5 border border-[#EFEAE2] text-xs">
                <span className="text-[#8C8277]">退款原因：</span>
                <span className="font-medium text-[#1F1D1A]">
                  {refund.reason || "个人原因申请售后退款"}
                </span>
              </div>
            </div>
          )}

          {/* 2. 买家修改地址确认详情 */}
          {isAddressUpdate && (
            <div className="rounded-xl border border-[#ECE5DC] bg-[#FAF8F5] p-4 space-y-2.5 text-xs">
              <div className="flex items-center justify-between border-b border-[#EAE3D9] pb-2.5">
                <span className="text-[#7A7167]">待修改订单编号</span>
                <span className="font-semibold text-[#1F1D1A] tabular-nums">
                  {String(interrupt.arguments.order_id ?? "")}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[#7A7167]">新收件人</span>
                <span className="font-medium text-[#1F1D1A]">
                  {String(interrupt.arguments.receiver_name ?? "")}{" "}
                  {String(interrupt.arguments.receiver_phone ?? "")}
                </span>
              </div>
              <div className="pt-1">
                <span className="text-[#7A7167]">新收货地址：</span>
                <div className="mt-1 rounded-lg bg-white p-2.5 font-medium text-[#1F1D1A] border border-[#EFEAE2]">
                  {String(interrupt.arguments.new_shipping_address ?? "")}
                </div>
              </div>
            </div>
          )}

          {/* 3. 商家新款立项确认详情 */}
          {isMerchantProposal && proposal && leadSku && (
            <div className="space-y-3.5">
              <div className="rounded-xl border border-slate-200 bg-slate-50/75 p-4 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200/80 pb-2.5">
                  <div>
                    <div className="text-[11px] text-slate-500">建议立项款式</div>
                    <h4 className="mt-0.5 text-sm font-bold text-slate-900">
                      {cleanDisplayText(leadSku.sku_name)}
                    </h4>
                  </div>
                  <span className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-semibold text-slate-700 tabular-nums">
                    首批计划备货 {String(interrupt.arguments.initial_order_qty ?? 300)} 件
                  </span>
                </div>

                <div className="grid grid-cols-3 gap-2.5 tabular-nums">
                  <div className="rounded-lg bg-white p-3 border border-slate-200/80">
                    <div className="text-[11px] text-slate-500">建议售价</div>
                    <div className="mt-0.5 text-base font-bold text-slate-900">
                      ¥{leadSku.target_price.toFixed(2)}
                    </div>
                  </div>
                  <div className="rounded-lg bg-white p-3 border border-slate-200/80">
                    <div className="text-[11px] text-slate-500">单件预估成本</div>
                    <div className="mt-0.5 text-base font-bold text-slate-700">
                      ¥{leadSku.estimated_unit_cost.toFixed(2)}
                    </div>
                  </div>
                  <div className="rounded-lg bg-emerald-50 p-3 border border-emerald-200">
                    <div className="text-[11px] font-medium text-emerald-800">
                      预估毛利率
                    </div>
                    <div className="mt-0.5 text-base font-extrabold text-emerald-700">
                      {(proposal.estimated_gross_margin * 100).toFixed(2)}%
                    </div>
                  </div>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 text-xs">
                  <div className="rounded-lg bg-white p-3 border border-slate-200/80">
                    <div className="mb-1.5 font-semibold text-slate-800">
                      核心卖点
                    </div>
                    <ul className="space-y-1 text-slate-600 leading-relaxed">
                      {proposal.core_selling_points.map((pt, i) => (
                        <li key={i} className="flex items-start gap-1.5">
                          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 text-emerald-600 shrink-0" />
                          <span>{cleanDisplayText(pt)}</span>
                        </li>
                      ))}
                    </ul>
                  </div>

                  <div className="rounded-lg bg-white p-3 border border-slate-200/80">
                    <div className="mb-1.5 font-semibold text-amber-900">
                      风险提示
                    </div>
                    <ul className="space-y-1 text-slate-600 leading-relaxed">
                      {proposal.risk_warnings.map((w, i) => (
                        <li key={i} className="flex items-start gap-1.5">
                          <AlertCircle className="mt-0.5 h-3.5 w-3.5 text-amber-500 shrink-0" />
                          <span>{cleanDisplayText(w)}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-2 rounded-xl border border-emerald-200/80 bg-emerald-50/50 px-3.5 py-2.5 text-xs text-emerald-900">
                <RefreshCw className="h-3.5 w-3.5 text-emerald-600 shrink-0" />
                <span>确认后将同步更新至门店导购卖点库，方便顾客咨询时直接推荐。</span>
              </div>
            </div>
          )}

          {/* 选填补充说明 */}
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1.5">
              补充说明（选填）
            </label>
            <textarea
              rows={2}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={
                isCustomerRefund
                  ? "可填写您的补充说明（例如：想换个颜色重新下单）"
                  : isMerchantProposal
                  ? "可填写立项备注（例如：同意首单 300 件试销，重点跟进浅色防透反馈）"
                  : "可填写补充备注说明"
              }
              disabled={isSubmitting}
              className="w-full rounded-xl border border-stone-200 bg-white px-3.5 py-2.5 text-xs text-slate-900 placeholder:text-stone-400 focus:border-slate-400 focus:outline-none"
            />
          </div>
        </div>

        {/* 底部按钮 */}
        <div className="flex items-center justify-end gap-2.5 border-t border-stone-200/80 bg-stone-50/70 px-6 py-3.5">
          <button
            type="button"
            disabled={isSubmitting}
            onClick={() =>
              onDecision(
                "reject",
                comment.trim() ||
                  (isMerchantProposal ? "暂缓本次新款建档" : "暂不办理本次申请")
              )
            }
            className="rounded-xl border border-stone-300 bg-white px-4 py-2 text-xs font-medium text-stone-700 hover:bg-stone-100 disabled:opacity-50 cursor-pointer"
          >
            {rejectLabel}
          </button>

          <button
            type="button"
            disabled={isSubmitting}
            onClick={() => onDecision("approve", comment.trim() || defaultNote)}
            className={`inline-flex items-center gap-1.5 rounded-xl px-5 py-2 text-xs font-semibold text-white shadow-xs transition-colors disabled:opacity-50 cursor-pointer ${
              isMerchantProposal
                ? "bg-slate-900 hover:bg-slate-800"
                : "bg-[#8C3B4A] hover:bg-[#76303D]"
            }`}
          >
            {isSubmitting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            <span>{approveLabel}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
