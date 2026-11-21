#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/.demo-runtime"
GATEWAY_PID_FILE="${RUNTIME_DIR}/gateway.pid"
WEB_PID_FILE="${RUNTIME_DIR}/web.pid"

stop_pid_file() {
  local label="$1"
  local pid_file="$2"

  if [[ ! -f "${pid_file}" ]]; then
    echo "[INFO] ${label} PID 文件不存在 (${pid_file})，跳过。"
    return 0
  fi

  local pid
  pid="$(cat "${pid_file}" 2>/dev/null || true)"
  if [[ -z "${pid}" ]]; then
    rm -f "${pid_file}"
    return 0
  fi

  if kill -0 "${pid}" 2>/dev/null; then
    echo "[STOP] 正在优雅停止 ${label} (PID=${pid})..."
    kill -TERM "${pid}" 2>/dev/null || true
    for _ in {1..40}; do
      if ! kill -0 "${pid}" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    if kill -0 "${pid}" 2>/dev/null; then
      echo "[WARN] ${label} (PID=${pid}) 未在预期时间内退出，发送 SIGKILL..."
      kill -KILL "${pid}" 2>/dev/null || true
    fi
    echo "[OK] ${label} 已停止。"
  else
    echo "[INFO] ${label} 进程 (PID=${pid}) 已不在线。"
  fi

  rm -f "${pid_file}"
}

stop_pid_file "Next.js 前端工作台" "${WEB_PID_FILE}"
stop_pid_file "FastAPI 网关（含内管 5 域 MCP 集群）" "${GATEWAY_PID_FILE}"

echo "[DONE] 后台常驻 Demo 服务清理完成。"
