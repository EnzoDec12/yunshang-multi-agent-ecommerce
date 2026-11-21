import type {
  AuditLogsResponse,
  FinalAnswerEvent,
  HITLInterruptEvent,
  LoginResponse,
  PerspectiveId,
  ThinkingEvent,
  ToolResultEvent,
  ToolStartEvent,
} from "@/types/agent";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ||
  "http://127.0.0.1:8000";

export interface SSEHandlers {
  onThinking?: (ev: ThinkingEvent) => void;
  onToolStart?: (ev: ToolStartEvent) => void;
  onToolResult?: (ev: ToolResultEvent) => void;
  onHitlInterrupt?: (ev: HITLInterruptEvent) => void;
  onFinalAnswer?: (ev: FinalAnswerEvent) => void;
  onStreamError?: (msg: string) => void;
}

export async function loginPresetAccount(
  accountId: PerspectiveId
): Promise<LoginResponse> {
  const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ account_id: accountId }),
  });

  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`登录失败 (${res.status}): ${errText}`);
  }

  return (await res.json()) as LoginResponse;
}

export async function fetchAuditLogs(
  token: string,
  params?: {
    traceId?: string;
    status?: string;
    limit?: number;
  }
): Promise<AuditLogsResponse> {
  const query = new URLSearchParams();
  if (params?.traceId) query.set("trace_id", params.traceId);
  if (params?.status) query.set("status", params.status);
  query.set("limit", String(params?.limit ?? 120));

  const res = await fetch(`${API_BASE_URL}/api/audit/logs?${query.toString()}`, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });

  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`查询审计日志失败 (${res.status}): ${errText}`);
  }

  return (await res.json()) as AuditLogsResponse;
}

async function consumeSSEStream(
  response: Response,
  handlers: SSEHandlers
): Promise<void> {
  if (!response.ok) {
    let errDetail = `请求失败 (${response.status})`;
    try {
      const errJson = (await response.json()) as { detail?: string };
      if (errJson.detail) errDetail = `${errDetail}: ${errJson.detail}`;
    } catch {
      const txt = await response.text();
      if (txt) errDetail = `${errDetail}: ${txt}`;
    }
    throw new Error(errDetail);
  }

  if (!response.body) {
    throw new Error("浏览器未返回可读 SSE ReadableStream");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const dispatchFrame = (rawFrame: string) => {
    const trimmed = rawFrame.trim();
    if (!trimmed) return;

    let eventName = "message";
    const dataLines: string[] = [];

    for (const line of trimmed.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).trim());
      }
    }

    if (dataLines.length === 0) return;

    const payload = JSON.parse(dataLines.join("\n"));

    switch (eventName) {
      case "thinking":
        handlers.onThinking?.(payload as ThinkingEvent);
        break;
      case "tool_start":
        handlers.onToolStart?.(payload as ToolStartEvent);
        break;
      case "tool_result":
        handlers.onToolResult?.(payload as ToolResultEvent);
        break;
      case "hitl_interrupt":
        handlers.onHitlInterrupt?.(payload as HITLInterruptEvent);
        break;
      case "final_answer":
        handlers.onFinalAnswer?.(payload as FinalAnswerEvent);
        break;
      case "error":
        handlers.onStreamError?.(
          String(
            (payload as { message?: string }).message ?? "流式编排发生未知错误"
          )
        );
        break;
      default:
        break;
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

    let boundaryIdx = buffer.indexOf("\n\n");
    while (boundaryIdx !== -1) {
      const rawFrame = buffer.slice(0, boundaryIdx);
      buffer = buffer.slice(boundaryIdx + 2);
      dispatchFrame(rawFrame);
      boundaryIdx = buffer.indexOf("\n\n");
    }
  }

  const tail = decoder.decode();
  if (tail) {
    buffer += tail.replace(/\r\n/g, "\n");
  }
  if (buffer.trim()) {
    dispatchFrame(buffer);
  }
}

export async function streamChatTurn(params: {
  token: string;
  message: string;
  threadId?: string | null;
  handlers: SSEHandlers;
  signal?: AbortSignal;
}): Promise<{ threadId: string | null; traceId: string | null }> {
  const body: { message: string; thread_id?: string } = {
    message: params.message,
  };
  if (params.threadId) {
    body.thread_id = params.threadId;
  }

  const response = await fetch(`${API_BASE_URL}/api/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      Authorization: `Bearer ${params.token}`,
    },
    body: JSON.stringify(body),
    signal: params.signal,
  });

  const resolvedThreadId = response.headers.get("X-Thread-Id");
  const resolvedTraceId = response.headers.get("X-Trace-Id");

  await consumeSSEStream(response, params.handlers);
  return { threadId: resolvedThreadId, traceId: resolvedTraceId };
}

export async function streamHitlResume(params: {
  token: string;
  threadId: string;
  decision: "approve" | "reject";
  comment: string;
  handlers: SSEHandlers;
  signal?: AbortSignal;
}): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/hitl/resume`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      Authorization: `Bearer ${params.token}`,
    },
    body: JSON.stringify({
      thread_id: params.threadId,
      decision: params.decision,
      comment: params.comment,
    }),
    signal: params.signal,
  });

  await consumeSSEStream(response, params.handlers);
}
