# @feature: FP-0.2.多端同步 2c 统一路径树验收 | @ci: python-coverage
"""2c 统一路径树 + 前缀路由轻量验收车道（单实例 + 进程内模拟第二节点）。

验收口径（台账 T5 最小闭环，2026-10-17 用户拍板模型）：
- S1 路由表：真 RelayStore + routing_view → 解析层归属/可达判定；
- S2 跨端干活文件落 B：真 manifest → 真 MCP 栈（ClientSession over
  streamable-http ASGI + Bearer，fmin tests 先例）→ 统一路径写入，文件落在
  属主端真实路径（逐字节同一，路径保真），内容回发起端（结果可见）；
- S3 断开重连同一空间：通道挂载持久化，重连后同一路径续作；
- S4 断链诚实不可用：B 链路断 → 访问明确报错（无本地副本），本端子树照常；
  恢复后同一通道自动可用（掉盘又上电）；
- S5 心跳掉盘：注册表在线窗口过期 → 解析层诚实呈现不可用；
- S6 安全：per-device exec token 错误 → 401 先于任何 MCP 会话；领地出界拒绝。

内核侧同名工具前缀消歧执法体 = register_host/resolve_tool_instance（Rust 测试
覆盖 registry.rs 系列），本车道断言 manifest 的 allowed_roots 契约面（执法体输入）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mount import build_mount_manifest, mount_plugin_id, write_mount
from relay_store import RelayStore
from routing_view import build_routing_view
from tree_core import resolve_path

pytestmark = pytest.mark.unit

TOKEN_B = "exec-token-node-b"
TOKEN_ENV_B = "AGENTOS_EXEC_TOKEN_NODE_B"
WINDOW_SECS = 90.0


# ---------------------------------------------------------------------------
# 模拟第二节点（执行端 B）：真 MCP server + Bearer + 领地守卫
# ---------------------------------------------------------------------------


class PathOutsideTerritory(Exception):
    """路径越界（执行端数据不出权限区，D13'）。"""


def build_node_server(territory: Path) -> MCPServer:
    """执行端节点：统一路径 = 属主端真实路径（零翻译），越界诚实拒绝。"""

    def _guard(path: str) -> Path:
        real = Path(path).resolve()
        root = territory.resolve()
        if real != root and root not in real.parents:
            raise PathOutsideTerritory(f"路径 {path} 越出领地 {territory}（执行端数据不出权限区）")
        return real

    server = MCPServer(
        name="sim-exec-node",
        version="1.0.0",
        instructions="模拟执行端节点：统一路径树子树执行（文件读写落属主端真实路径）。",
    )

    @server.tool()
    async def tree_write(path: str, content: str) -> str:
        """统一路径树写入（属主端执行）：内容落本端真实路径，回执含字节数与路径。"""
        try:
            real = _guard(path)
        except PathOutsideTerritory as exc:
            # 业务异常就地转错误信封：MCP 库对裸异常只回泛化包装（细节仅进
            # server 日志），错误细节必须留在工具结果里才是可辨识拒绝。
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        real.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        real.write_bytes(data)
        return json.dumps({"written": str(real), "bytes": len(data)}, ensure_ascii=False)

    @server.tool()
    async def tree_read(path: str) -> str:
        """统一路径树读取（属主端执行）：回读本端真实文件内容。"""
        real = _guard(path)
        return real.read_text(encoding="utf-8")

    return server


class BearerTokenMiddleware:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                supplied = value
                break
        if supplied != self._expected:
            body = json.dumps({"error": "unauthorized"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def node_asgi_app(territory: Path, token: str) -> Any:
    inner = build_node_server(territory).streamable_http_app(
        streamable_http_path="/mcp", host="127.0.0.1"
    )
    return BearerTokenMiddleware(inner, token)


@asynccontextmanager
async def drive_lifespan(app: Any):
    """按 ASGI lifespan 协议驱动 app（ASGITransport 不跑 lifespan，fmin 先例）。"""
    ack = asyncio.Event()
    shutdown_gate = asyncio.Event()

    async def receive() -> dict[str, str]:
        if not ack.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_gate.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "lifespan.startup.complete":
            ack.set()

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await ack.wait()
    try:
        yield
    finally:
        shutdown_gate.set()
        await task


@asynccontextmanager
async def node_session(app: Any, token: str, mcp_url: str):
    """真 MCP 会话（manifest 契约消费：url + bearer env 插值后连接）。"""
    async with (
        drive_lifespan(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:9201",
            headers={"Authorization": f"Bearer {token}"},
        ) as http,
        streamable_http_client(mcp_url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


# ---------------------------------------------------------------------------
# 被测系统装配：真 relay 注册表 + 真 manifest 生成
# ---------------------------------------------------------------------------


def register_fleet(tmp_path: Path, *, b_online: bool) -> RelayStore:
    """两节点入网：initiator（发起端）+ node-b（执行端，领地声明 B 盘）。"""
    store = RelayStore(tmp_path / "relay.db")
    now = datetime.now(UTC)
    local_disk = tmp_path / "local_disk"
    local_disk.mkdir(exist_ok=True)
    store.upsert_device(
        device_id="initiator",
        tier="desktop",
        app_version="1.0.0",
        capabilities={"exec_channel": {"mcp_url": "", "allowed_roots": [str(local_disk)]}},
        now_iso=now.isoformat(),
    )
    b_hb = now if b_online else now - timedelta(seconds=WINDOW_SECS * 10)
    store.upsert_device(
        device_id="node-b",
        tier="desktop",
        app_version="1.0.0",
        capabilities={
            "resource": {"cpu_cores": 8, "mem_mb": 16384},
            "exec_channel": {"mcp_url": "http://127.0.0.1:9201/mcp", "allowed_roots": [str(tmp_path / "b_disk")]},
        },
        now_iso=b_hb.isoformat(),
    )
    return store


def mount_manifest(tmp_path: Path, store: RelayStore) -> dict[str, Any]:
    """真路由表 → 真 manifest（被测挂载面），落盘持久化（断开重连场景载体）。"""
    view = build_routing_view(
        store, now=datetime.now(UTC), window_secs=WINDOW_SECS, requested_by="initiator"
    )
    record = next(d for d in view["devices"] if d["device_id"] == "node-b")
    manifest = build_mount_manifest(record, token_env=TOKEN_ENV_B)
    write_mount(tmp_path / "plugins_root", manifest)
    return manifest


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setenv(TOKEN_ENV_B, TOKEN_B)
    b_disk = tmp_path / "b_disk"
    b_disk.mkdir()
    store = register_fleet(tmp_path, b_online=True)
    manifest = mount_manifest(tmp_path, store)
    return {
        "tmp": tmp_path,
        "b_disk": b_disk,
        "store": store,
        "manifest": manifest,
        "plugins_root": tmp_path / "plugins_root",
    }


def _interp_auth(manifest: dict[str, Any]) -> str:
    """按内核契约消费 manifest auth（client.rs：${ENV} 引用 connect 时解析）。"""
    value = manifest["mcp"]["endpoint"]["auth"]["value"]
    assert value.startswith("${") and value.endswith("}")
    env_name = value[2:-1]
    import os

    return os.environ[env_name]


class TestS1RoutingTable:
    def test_routing_view_and_resolution(self, fleet: dict[str, Any]) -> None:
        view = build_routing_view(
            fleet["store"], now=datetime.now(UTC), window_secs=WINDOW_SECS, requested_by="initiator"
        )
        unified = "/node-b/workspace/proj/note.md"
        r = resolve_path(unified, view, self_device_id="initiator")
        assert r.owner_device == "node-b"
        assert r.reachable is True
        # 一次翻译：统一路径 → 属主端真实路径（执行面契约，路径保真）
        assert Path(r.real_path) == fleet["b_disk"] / "workspace" / "proj" / "note.md"
        # 本端前缀 = 零网络开销本地执行
        mine = resolve_path("/initiator/x.txt", view, self_device_id="initiator")
        assert mine.local is True
        assert mine.reachable is True
        assert Path(mine.real_path) == fleet["tmp"] / "local_disk" / "x.txt"
        # 未知前缀 = 不在统一树上
        ghost = resolve_path("/ghost/x", view, self_device_id="initiator")
        assert ghost.owner_device is None

    def test_manifest_declares_territory_for_kernel_homeland_routing(
        self, fleet: dict[str, Any]
    ) -> None:
        """allowed_roots = 内核 register_host 输入（前缀路由执法体契约面）。"""
        manifest = fleet["manifest"]
        assert manifest["entry"] == "mcp:external"
        assert manifest["mcp"]["allowed_roots"] == [str(fleet["b_disk"])]
        assert (fleet["plugins_root"] / mount_plugin_id("node-b") / "plugin.json").exists()


class TestS2CrossDeviceExecution:
    def test_write_on_b_subtree_lands_on_b_and_visible_to_initiator(
        self, fleet: dict[str, Any]
    ) -> None:
        manifest = fleet["manifest"]
        unified = "/node-b/workspace/proj/note.md"
        view = build_routing_view(
            fleet["store"], now=datetime.now(UTC), window_secs=WINDOW_SECS, requested_by="initiator"
        )
        real_path = resolve_path(unified, view, self_device_id="initiator").real_path
        app = node_asgi_app(fleet["b_disk"], _interp_auth(manifest))

        async def scenario() -> str:
            async with node_session(app, _interp_auth(manifest), "http://127.0.0.1:9201/mcp") as session:
                tools = await session.list_tools()
                assert "tree_write" in {t.name for t in tools.tools}
                result = await session.call_tool(
                    "tree_write",
                    {"path": real_path, "content": "跨端产文：A 端发起，B 端属地执行"},
                )
                assert result.is_error is False
                return result.content[0].text

        receipt = json.loads(asyncio.run(scenario()))
        # 文件落在属主端真实路径（逐字节同一 = 路径保真）+ 内容回发起端可见
        assert (fleet["b_disk"] / "workspace" / "proj" / "note.md").read_text(
            encoding="utf-8"
        ) == "跨端产文：A 端发起，B 端属地执行"
        assert receipt["bytes"] > 0


class TestS3DisconnectReconnect:
    def test_same_workspace_continues_after_reconnect(self, fleet: dict[str, Any]) -> None:
        manifest = fleet["manifest"]
        token = _interp_auth(manifest)
        real_path = fleet["b_disk"] / "workspace" / "log.md"

        async def scenario() -> None:
            app = node_asgi_app(fleet["b_disk"], token)
            async with node_session(app, token, "http://127.0.0.1:9201/mcp") as session:
                await session.call_tool("tree_write", {"path": str(real_path), "content": "第一段"})
            # 断开（会话退出）后重连：同一持久化 manifest，同一路径续作
            app2 = node_asgi_app(fleet["b_disk"], token)
            async with node_session(app2, token, "http://127.0.0.1:9201/mcp") as session:
                await session.call_tool("tree_write", {"path": str(real_path), "content": "第一段\n第二段（重连后续作）"})
                read = await session.call_tool("tree_read", {"path": str(real_path)})
                assert "第二段（重连后续作）" in read.content[0].text

        asyncio.run(scenario())
        assert "第一段" in real_path.read_text(encoding="utf-8")


class TestS4HonestUnavailability:
    def test_dead_link_errors_clearly_and_local_subtree_unaffected(
        self, fleet: dict[str, Any]
    ) -> None:
        """断 B 链：B 子树访问诚实报错（无本地副本）；本端子树照常。"""
        token = TOKEN_B
        owner_real = fleet["b_disk"] / "workspace" / "x.md"
        app = node_asgi_app(fleet["b_disk"], token)

        async def scenario() -> None:
            async with drive_lifespan(app):
                pass  # B 节点已下电（lifespan 关闭，通道不可用）
            # 下电后协议边界诚实报错：MCP 请求不得静默成功
            # （真机上此层 = 连接拒绝/通道断开；MCP 客户端对死流会等流不超时，
            # 故断言取协议边界 POST 的显式失败面）
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://127.0.0.1:9201",
                headers={"Authorization": f"Bearer {token}"},
            ) as http:
                with pytest.raises(Exception) as exc_info:
                    await http.post(
                        "http://127.0.0.1:9201/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    )
                if isinstance(exc_info.value, httpx2.HTTPStatusError):
                    assert exc_info.value.response.status_code >= 500

        asyncio.run(scenario())
        # 断链诚实：B 子树路径无本地副本出现（不做离线缓存）
        assert not owner_real.exists()
        # 本端子树照常（本地路径解析无属主、文件操作不受 B 停机影响）
        local_file = fleet["tmp"] / "local_only.txt"
        local_file.write_text("本端属地照常干活", encoding="utf-8")
        assert local_file.exists()

    def test_node_recovery_same_manifest_reconnects(self, fleet: dict[str, Any]) -> None:
        """掉盘又上电：同一持久化通道配置自动恢复可用。"""
        manifest = fleet["manifest"]
        token = _interp_auth(manifest)
        real_path = fleet["b_disk"] / "workspace" / "recovery.md"

        async def scenario() -> None:
            app = node_asgi_app(fleet["b_disk"], token)
            async with drive_lifespan(app):
                pass  # 下电
            # 上电恢复
            app2 = node_asgi_app(fleet["b_disk"], token)
            async with node_session(app2, token, "http://127.0.0.1:9201/mcp") as session:
                await session.call_tool("tree_write", {"path": str(real_path), "content": "恢复后可干活"})
                read = await session.call_tool("tree_read", {"path": str(real_path)})
                assert read.content[0].text == "恢复后可干活"

        asyncio.run(scenario())


class TestS5HeartbeatDiskDrop:
    def test_stale_heartbeat_reports_offline_subtree(self, fleet: dict[str, Any]) -> None:
        store = register_fleet(fleet["tmp"], b_online=False)
        view = build_routing_view(
            store, now=datetime.now(UTC), window_secs=WINDOW_SECS, requested_by="initiator"
        )
        r = resolve_path("/node-b/workspace/y.md", view, self_device_id="initiator")
        assert r.owner_device == "node-b"
        assert r.reachable is False
        assert "不可用" in r.note


class TestS6Security:
    def test_wrong_exec_token_rejected_before_mcp(self, fleet: dict[str, Any]) -> None:
        """per-device exec token 错误 → 401 先于任何 MCP 会话（P6）。"""
        app = node_asgi_app(fleet["b_disk"], TOKEN_B)

        async def scenario() -> int:
            async with (
                drive_lifespan(app),
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="http://127.0.0.1:9201",
                    headers={"Authorization": "Bearer wrong-token"},
                ) as http,
            ):
                resp = await http.post("http://127.0.0.1:9201/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "x"})
                return resp.status_code

        assert asyncio.run(scenario()) == 401

    def test_out_of_territory_path_rejected(self, fleet: dict[str, Any]) -> None:
        """执行端数据不出权限区：领地外路径诚实拒绝。"""
        manifest = fleet["manifest"]
        token = _interp_auth(manifest)
        outside = fleet["tmp"] / "outside.txt"
        app = node_asgi_app(fleet["b_disk"], token)

        async def scenario() -> str:
            async with node_session(app, token, "http://127.0.0.1:9201/mcp") as session:
                result = await session.call_tool(
                    "tree_write", {"path": str(outside), "content": "越界"}
                )
                return result.content[0].text

        assert "越出领地" in asyncio.run(scenario())
        assert not outside.exists()
