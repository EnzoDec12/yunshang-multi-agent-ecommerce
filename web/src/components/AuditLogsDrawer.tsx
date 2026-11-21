"use client";

import React, { useMemo, useState } from "react";
import {
  ClipboardList,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import type { AuditLogEntry, RoleType } from "@/types/agent";
import {
  cleanDisplayText,
  describeAuditOperation,
  describeAuditRisk,
  describeAuditRole,
  describeAuditStatus,
} from "@/lib/presenters";

interface Props {
  isOpen: boolean;
  onClose: () => void;
  logs: AuditLogEntry[];
  viewer?: {
    tenant_id: string;
    user_id: string;
    role: RoleType;
  } | null;
  isLoading: boolean;
  onRefresh: () => void;
}

export function AuditLogsDrawer({
  isOpen,
  onClose,
  logs,
  viewer,
  isLoading,
  onRefresh,
}: Props) {
  const [filterMode, setFilterMode] = useState<
    "all" | "intercepted" | "success"
  >("all");
  const [keyword, setKeyword] = useState("");

  const filteredLogs = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return logs.filter((item) => {
      if (filterMode === "intercepted" && !item.intercepted) return false;
      if (filterMode === "success" && item.status !== "success") return false;
      if (kw) {
        const opLabel = describeAuditOperation(item.tool_name);
        const hay = `${opLabel} ${item.guard_reason ?? ""} ${item.trace_id}`.toLowerCase();
        if (!hay.includes(kw)) return false;
      }
      return true;
    });
  }, [logs, filterMode, keyword]);

  if (!isOpen) return null;

  const interceptedCount = logs.filter((l) => l.intercepted).length;

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30 backdrop-blur-[1px]">
      <div className="flex h-full w-full max-w-2xl flex-col border-l border-stone-200 bg-white shadow-2xl">
        {/* 抽屉头部 */}
        <div className="flex items-center justify-between border-b border-stone-200 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-stone-100 text-stone-700">
              <ClipboardList className="h-4 w-4" />
            </div>
            <div>
              <h3 className="text-sm font-bold text-stone-900">
                操作审计记录
              </h3>
              <p className="text-[11px] text-stone-500">
                {viewer?.role === "merchant"
                  ? "当前展示云裳女装旗舰店全店服务与风控记录"
                  : "当前仅展示您本人会话下的服务操作记录"}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onRefresh}
              className="inline-flex items-center gap-1 rounded-lg border border-stone-200 bg-stone-50 px-2.5 py-1.5 text-xs font-medium text-stone-700 hover:bg-stone-100 cursor-pointer"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${isLoading ? "animate-spin" : ""}`}
              />
              刷新
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1.5 text-stone-400 hover:bg-stone-100 hover:text-stone-700 cursor-pointer"
              aria-label="关闭操作审计抽屉"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* 筛选与搜索栏 */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-200 bg-stone-50/70 px-5 py-3">
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => setFilterMode("all")}
              className={`rounded-lg px-2.5 py-1 text-xs font-medium cursor-pointer ${
                filterMode === "all"
                  ? "bg-stone-900 text-white"
                  : "bg-white text-stone-600 border border-stone-200"
              }`}
            >
              全部 ({logs.length})
            </button>
            <button
              type="button"
              onClick={() => setFilterMode("intercepted")}
              className={`rounded-lg px-2.5 py-1 text-xs font-medium cursor-pointer ${
                filterMode === "intercepted"
                  ? "bg-rose-600 text-white"
                  : "bg-white text-rose-700 border border-rose-200"
              }`}
            >
              待确认 / 已拦截 ({interceptedCount})
            </button>
            <button
              type="button"
              onClick={() => setFilterMode("success")}
              className={`rounded-lg px-2.5 py-1 text-xs font-medium cursor-pointer ${
                filterMode === "success"
                  ? "bg-emerald-600 text-white"
                  : "bg-white text-emerald-700 border border-emerald-200"
              }`}
            >
              已完成 ({logs.length - interceptedCount})
            </button>
          </div>

          <div className="relative flex-1 min-w-[180px]">
            <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-stone-400" />
            <input
              type="text"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="搜索操作事项或说明…"
              className="w-full rounded-lg border border-stone-200 bg-white pl-8 pr-2.5 py-1 text-xs text-stone-800 placeholder:text-stone-400 focus:border-stone-400 focus:outline-none"
            />
          </div>
        </div>

        {/* 中文审计表格列表 */}
        <div className="flex-1 overflow-y-auto p-5">
          {filteredLogs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-center text-xs text-stone-400">
              <ClipboardList className="h-8 w-8 text-stone-300 mb-2" />
              <p>暂无操作审计记录</p>
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-stone-200">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-stone-200 bg-stone-50 text-[11px] font-semibold text-stone-600">
                  <tr>
                    <th className="px-3.5 py-2.5">时间</th>
                    <th className="px-3.5 py-2.5">操作事项</th>
                    <th className="px-3.5 py-2.5">操作身份</th>
                    <th className="px-3.5 py-2.5">操作类型</th>
                    <th className="px-3.5 py-2.5">处理结果</th>
                    <th className="px-3.5 py-2.5 text-right">响应耗时</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-100 bg-white">
                  {filteredLogs.map((log, idx) => {
                    const riskInfo = describeAuditRisk(log.risk);
                    const statusInfo = describeAuditStatus(log);
                    return (
                      <tr
                        key={`${log.trace_id}-${log.tool_call_id}-${idx}`}
                        className="align-top hover:bg-stone-50/70"
                      >
                        <td className="px-3.5 py-2.5 whitespace-nowrap text-stone-500 tabular-nums">
                          {new Date(log.timestamp).toLocaleTimeString("zh-CN")}
                        </td>
                        <td className="px-3.5 py-2.5">
                          <div className="font-medium text-stone-900">
                            {describeAuditOperation(log.tool_name)}
                          </div>
                          {log.guard_reason && (
                            <div className="mt-1 text-[11px] leading-relaxed text-stone-500">
                              说明：{cleanDisplayText(log.guard_reason)}
                            </div>
                          )}
                        </td>
                        <td className="px-3.5 py-2.5 whitespace-nowrap text-stone-600">
                          {describeAuditRole(log.role)}
                        </td>
                        <td className="px-3.5 py-2.5 whitespace-nowrap">
                          <span
                            className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] ${riskInfo.cls}`}
                          >
                            {riskInfo.label}
                          </span>
                        </td>
                        <td className="px-3.5 py-2.5 whitespace-nowrap">
                          <span
                            className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-medium ${statusInfo.cls}`}
                          >
                            {statusInfo.label}
                          </span>
                        </td>
                        <td className="px-3.5 py-2.5 whitespace-nowrap text-right text-stone-500 tabular-nums">
                          {Math.round(log.duration_ms)} ms
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
