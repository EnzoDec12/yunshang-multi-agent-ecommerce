"use client";

import React, { useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Loader2,
} from "lucide-react";
import type { ThinkingEvent, ToolStepRecord } from "@/types/agent";
import { describeThinkingStep, describeToolStep } from "@/lib/presenters";

interface Props {
  isStreaming: boolean;
  isMerchant?: boolean;
  activeThinking?: ThinkingEvent | null;
  thinkingHistory: ThinkingEvent[];
  toolSteps: ToolStepRecord[];
}

export function ToolCallStatusList({
  isStreaming,
  isMerchant = false,
  activeThinking,
  thinkingHistory,
  toolSteps,
}: Props) {
  const [expanded, setExpanded] = useState(false);

  const hasSteps = toolSteps.length > 0;
  const hasThinking = Boolean(activeThinking) || thinkingHistory.length > 0;

  if (!hasSteps && !hasThinking) {
    return null;
  }

  const runningStep = toolSteps.find((s) => s.status === "running");
  const blockedCount = toolSteps.filter((s) => s.status === "error").length;
  const pendingConfirmCount = toolSteps.filter(
    (s) => s.status === "hitl_required"
  ).length;
  const completedCount = toolSteps.filter((s) => s.status === "success").length;

  // 流式执行中：只展示安静的一行当前进度文案
  if (isStreaming) {
    const liveLabel = runningStep
      ? describeToolStep(runningStep).runningText
      : describeThinkingStep(
          activeThinking ?? thinkingHistory[thinkingHistory.length - 1]
        );

    return (
      <div
        className={`flex items-center justify-between gap-3 rounded-xl px-3.5 py-2 text-xs transition-colors ${
          isMerchant
            ? "bg-slate-50 text-slate-600 border border-slate-200/70"
            : "bg-[#FBF9F6] text-[#6E665E] border border-[#ECE5DC]"
        }`}
      >
        <div className="flex items-center gap-2.5 min-w-0">
          <Loader2
            className={`h-3.5 w-3.5 animate-spin shrink-0 ${
              isMerchant ? "text-slate-700" : "text-[#8C3B4A]"
            }`}
          />
          <span className="truncate font-medium">{liveLabel}</span>
        </div>
        {completedCount > 0 && (
          <span className="shrink-0 text-[11px] opacity-75 tabular-nums">
            已完成 {completedCount} 项
          </span>
        )}
      </div>
    );
  }

  // 执行结束后：若没有任何工具步骤则不展示多余条，保持回复干净
  if (!hasSteps) {
    return null;
  }

  const summaryLabel =
    blockedCount > 0
      ? `已核对服务规则（${blockedCount} 项未满足办理条件，点击查看过程）`
      : pendingConfirmCount > 0
      ? `已完成前置核对，等待您确认提交（点击查看过程）`
      : isMerchant
      ? `已完成 ${toolSteps.length} 项经营分析与成本测算（点击查看过程）`
      : `已完成 ${toolSteps.length} 项查询与核对（点击查看过程）`;

  return (
    <div
      className={`rounded-xl border text-xs transition-colors ${
        isMerchant
          ? "border-slate-200/75 bg-slate-50/70 text-slate-600"
          : "border-[#ECE5DC] bg-[#FBF9F6] text-[#6E665E]"
      }`}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-3.5 py-2 text-left hover:opacity-90 cursor-pointer"
      >
        <div className="flex items-center gap-2 min-w-0">
          {blockedCount > 0 ? (
            <AlertCircle className="h-3.5 w-3.5 text-rose-600 shrink-0" />
          ) : pendingConfirmCount > 0 ? (
            <Clock className="h-3.5 w-3.5 text-amber-600 shrink-0" />
          ) : (
            <Check
              className={`h-3.5 w-3.5 shrink-0 ${
                isMerchant ? "text-emerald-600" : "text-[#2E6B56]"
              }`}
            />
          )}
          <span className="truncate">{summaryLabel}</span>
        </div>
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 opacity-60 shrink-0" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 opacity-60 shrink-0" />
        )}
      </button>

      {expanded && (
        <div
          className={`space-y-1.5 border-t px-3.5 py-2.5 ${
            isMerchant ? "border-slate-200/70" : "border-[#ECE5DC]"
          }`}
        >
          {toolSteps.map((step, idx) => {
            const desc = describeToolStep(step);
            return (
              <div
                key={`${step.tool_call_id}-${idx}`}
                className="flex items-start gap-2 text-xs leading-relaxed"
              >
                {desc.tone === "blocked" ? (
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 text-rose-500 shrink-0" />
                ) : desc.tone === "warning" ? (
                  <Clock className="mt-0.5 h-3.5 w-3.5 text-amber-600 shrink-0" />
                ) : (
                  <Check
                    className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${
                      isMerchant ? "text-emerald-600" : "text-[#2E6B56]"
                    }`}
                  />
                )}
                <span
                  className={
                    desc.tone === "blocked"
                      ? "text-rose-700"
                      : desc.tone === "warning"
                      ? "text-amber-800"
                      : isMerchant
                      ? "text-slate-700"
                      : "text-[#3D3833]"
                  }
                >
                  {desc.doneText}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
