"""本地开发专用 5 域 MCP Server 子进程托管器（与网关路由层物理拆分解耦）。

仅当 `settings.MCP_SPAWN_LOCAL_SUBPROCESS=True`（本地开发或单机集成回归）时按需启用；
生产/Docker 模式下网关绝不调用本模块，直接通过 HTTP 连接独立运行的 5 个远程 MCP 容器服务。
"""

from __future__ import annotations

import asyncio
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

DOMAIN_SERVER_MODULES: dict[str, str] = {
    "pim": "mcp_servers.pim_server",
    "oms": "mcp_servers.oms_server",
    "logistics": "mcp_servers.logistics_server",
    "bi": "mcp_servers.bi_server",
    "market_erp": "mcp_servers.market_erp_server",
}


def allocate_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


@dataclass
class ManagedMCPNode:
    domain: str
    module: str
    port: int
    process: asyncio.subprocess.Process

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


async def probe_local_mcp_health(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_s,
        )
        try:
            req = (
                f"GET /health HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(req)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(4096), timeout=timeout_s)
            text = raw.decode("utf-8", errors="replace")
            return "200 OK" in text and '"ok"' in text
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    except Exception:
        return False


class ManagedMCPCluster:
    """本地开发模式下自动拉起并优雅回收 5 个独立域 Streamable HTTP MCP Server 子进程。"""

    def __init__(self) -> None:
        self.nodes: dict[str, ManagedMCPNode] = {}

    async def start(self, *, ready_timeout_s: float = 15.0) -> dict[str, str]:
        used_ports: set[int] = set()
        try:
            for domain, module_path in DOMAIN_SERVER_MODULES.items():
                port = allocate_free_local_port()
                while port in used_ports:
                    port = allocate_free_local_port()
                used_ports.add(port)

                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    module_path,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "error",
                    cwd=str(WORKSPACE_ROOT),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.nodes[domain] = ManagedMCPNode(
                    domain=domain,
                    module=module_path,
                    port=port,
                    process=proc,
                )

            await asyncio.gather(
                *(self._wait_ready(node, ready_timeout_s) for node in self.nodes.values())
            )
            return {domain: node.mcp_url for domain, node in self.nodes.items()}
        except Exception:
            await self.stop()
            raise

    async def _wait_ready(self, node: ManagedMCPNode, timeout_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if node.process.returncode is not None:
                err_bytes = (
                    await node.process.stderr.read()
                    if node.process.stderr is not None
                    else b""
                )
                raise RuntimeError(
                    f"独立域 MCP Server '{node.domain}' 启动失败 (code={node.process.returncode}): "
                    f"{err_bytes.decode('utf-8', errors='replace')}"
                )
            if await probe_local_mcp_health("127.0.0.1", node.port):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"等待独立域 MCP Server '{node.domain}' (port={node.port}) 就绪超时"
        )

    async def stop(self) -> None:
        async def _terminate_one(node: ManagedMCPNode) -> None:
            if node.process.returncode is not None:
                return
            try:
                node.process.terminate()
                await asyncio.wait_for(node.process.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                try:
                    node.process.kill()
                    await asyncio.wait_for(node.process.wait(), timeout=3.0)
                except ProcessLookupError:
                    pass

        await asyncio.gather(
            *(_terminate_one(n) for n in list(self.nodes.values())),
            return_exceptions=True,
        )
        self.nodes.clear()


__all__ = [
    "DOMAIN_SERVER_MODULES",
    "ManagedMCPCluster",
    "ManagedMCPNode",
    "allocate_free_local_port",
    "probe_local_mcp_health",
]
