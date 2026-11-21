#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
GATEWAY_PORT="${GATEWAY_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
RUNTIME_DIR="${ROOT_DIR}/.demo-runtime"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[ERROR] 未找到虚拟环境 Python: ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ "${1:-}" == "--daemon" ]] || [[ "${1:-}" == "-d" ]]; then
  mkdir -p "${RUNTIME_DIR}"
  "${PYTHON_BIN}" - <<PY
import os
import subprocess
from pathlib import Path

root = Path("${ROOT_DIR}")
rt = Path("${RUNTIME_DIR}")
rt.mkdir(parents=True, exist_ok=True)

gw_log = open(rt / "gateway.log", "ab", buffering=0)
gw_env = {
    **os.environ,
    "GATEWAY_CORS_ORIGINS": "http://localhost:${WEB_PORT},http://127.0.0.1:${WEB_PORT}",
}
gw_proc = subprocess.Popen(
    [
        str(root / ".venv/bin/python"),
        "-m",
        "uvicorn",
        "gateway.main:app",
        "--host",
        "${GATEWAY_HOST}",
        "--port",
        "${GATEWAY_PORT}",
    ],
    cwd=str(root),
    env=gw_env,
    stdin=subprocess.DEVNULL,
    stdout=gw_log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
(rt / "gateway.pid").write_text(f"{gw_proc.pid}\n", encoding="utf-8")

web_log = open(rt / "web.log", "ab", buffering=0)
web_env = {
    **os.environ,
    "NEXT_PUBLIC_API_BASE_URL": "http://${GATEWAY_HOST}:${GATEWAY_PORT}",
    "NODE_ENV": "production",
    "PORT": "${WEB_PORT}",
    "HOSTNAME": "0.0.0.0",
}
standalone_entry = root / "web/.next/standalone/server.js"
if standalone_entry.exists():
    import shutil

    src_static = root / "web/.next/static"
    dst_static = root / "web/.next/standalone/.next/static"
    if src_static.exists():
        dst_static.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_static, dst_static, dirs_exist_ok=True)
    src_public = root / "web/public"
    dst_public = root / "web/.next/standalone/public"
    if src_public.exists():
        shutil.copytree(src_public, dst_public, dirs_exist_ok=True)
    web_cmd = ["node", str(standalone_entry)]
    web_cwd = str(root / "web/.next/standalone")
else:
    web_cmd = [
        str(root / "web/node_modules/.bin/next"),
        "start",
        "--port",
        "${WEB_PORT}",
    ]
    web_cwd = str(root / "web")

web_proc = subprocess.Popen(
    web_cmd,
    cwd=web_cwd,
    env=web_env,
    stdin=subprocess.DEVNULL,
    stdout=web_log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
(rt / "web.pid").write_text(f"{web_proc.pid}\n", encoding="utf-8")

print(f"[DAEMON] Gateway started: PID={gw_proc.pid}, log={rt / 'gateway.log'}")
print(f"[DAEMON] Next.js started: PID={web_proc.pid}, log={rt / 'web.log'}")
PY
  exit 0
fi

GATEWAY_PID=""
WEB_PID=""

cleanup() {
  echo ""
  echo "[INFO] 正在优雅关闭前端与网关（含内管 5 域 MCP Server 集群）..."
  if [[ -n "${WEB_PID}" ]] && kill -0 "${WEB_PID}" 2>/dev/null; then
    kill "${WEB_PID}" 2>/dev/null || true
    wait "${WEB_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
  echo "[INFO] 所有演示进程已清理完毕。"
}

trap cleanup EXIT INT TERM

echo "[1/2] 启动 FastAPI 网关 (http://${GATEWAY_HOST}:${GATEWAY_PORT}) 及内管 5 域 MCP 集群..."
(
  cd "${ROOT_DIR}"
  exec "${PYTHON_BIN}" -m uvicorn gateway.main:app \
    --host "${GATEWAY_HOST}" \
    --port "${GATEWAY_PORT}"
) &
GATEWAY_PID=$!

for _ in {1..60}; do
  if curl -fsS "http://${GATEWAY_HOST}:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
    echo "[OK] FastAPI 网关与 5 域 MCP 集群已就绪: http://${GATEWAY_HOST}:${GATEWAY_PORT}/health"
    break
  fi
  if ! kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    echo "[ERROR] FastAPI 网关启动异常退出" >&2
    exit 1
  fi
  sleep 0.25
done

echo "[2/2] 启动 Next.js 双视角全栈工作台 (http://localhost:${WEB_PORT})..."
(
  cd "${ROOT_DIR}/web"
  NEXT_PUBLIC_API_BASE_URL="http://${GATEWAY_HOST}:${GATEWAY_PORT}" \
    exec npm run dev -- --port "${WEB_PORT}"
) &
WEB_PID=$!

echo ""
echo "=================================================================="
echo "  电商多 Agent 全栈演示工作台已启动"
echo "  - 前端界面: http://localhost:${WEB_PORT}"
echo "  - 网关接口: http://${GATEWAY_HOST}:${GATEWAY_PORT}"
echo "  - 按 Ctrl+C 一键停止全部服务与 MCP 子进程"
echo "=================================================================="

wait "${GATEWAY_PID}" "${WEB_PID}"
