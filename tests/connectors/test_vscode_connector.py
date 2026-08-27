# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: none-local
"""VSCode 连接器的单元测试。

测试 VSCodeConnector 的生命周期（connect/disconnect）、上下文获取、操作执行和连接失败场景。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from connector_types import ConnectorAction, ConnectorState
from vscode.channel import VSCodeChannel
from vscode.connector import VSCodeConnector

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试


@pytest.fixture
def connector() -> VSCodeConnector:
    """创建未连接的 VSCode 连接器实例。"""
    return VSCodeConnector(host="localhost", port=9999, timeout=1.0)


@pytest.fixture
def connected_connector(connector: VSCodeConnector) -> VSCodeConnector:
    """创建已连接的 VSCode 连接器实例。"""
    connector._set_state(ConnectorState.CONNECTED)
    return connector


class TestConnect:
    """连接测试。"""

    @pytest.mark.asyncio
    async def test_connect_success(self, connector: VSCodeConnector) -> None:
        """测试连接成功。"""
        connector.channel.is_available = MagicMock(return_value=True)
        await connector.connect()
        assert connector.state == ConnectorState.CONNECTED

    @pytest.mark.asyncio
    async def test_connect_failure_sets_error_state(self, connector: VSCodeConnector) -> None:
        """测试连接失败后状态为 ERROR。"""
        connector.channel.is_available = MagicMock(return_value=False)
        with pytest.raises(ConnectionError, match="连接失败"):
            await connector.connect()
        assert connector.state == ConnectorState.ERROR

    @pytest.mark.asyncio
    async def test_connect_when_already_connected(self, connected_connector: VSCodeConnector) -> None:
        """测试已连接时重复调用 connect 不抛异常。"""
        await connected_connector.connect()
        assert connected_connector.state == ConnectorState.CONNECTED


class TestDisconnect:
    """断开连接测试。"""

    @pytest.mark.asyncio
    async def test_disconnect_from_connected(self, connected_connector: VSCodeConnector) -> None:
        """测试从已连接状态断开。"""
        await connected_connector.disconnect()
        assert connected_connector.state == ConnectorState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_when_already_disconnected(self, connector: VSCodeConnector) -> None:
        """测试从已断开状态调用 disconnect 不抛异常。"""
        await connector.disconnect()
        assert connector.state == ConnectorState.DISCONNECTED


class TestGetContext:
    """获取上下文测试。"""

    @pytest.mark.asyncio
    async def test_get_context_when_connected(self, connected_connector: VSCodeConnector) -> None:
        """测试已连接时获取上下文。"""
        from connector_types import ConnectorContext, CursorPosition

        expected = ConnectorContext(
            active_file="test.py",
            selected_text="hello",
            cursor_position=CursorPosition(line=1, column=0),
        )
        connected_connector.channel.listen_for_context = AsyncMock(return_value=expected)

        ctx = await connected_connector.get_context()
        assert ctx.active_file == "test.py"
        assert ctx.selected_text == "hello"
        assert connected_connector.state == ConnectorState.ACTIVE

    @pytest.mark.asyncio
    async def test_get_context_when_disconnected(self, connector: VSCodeConnector) -> None:
        """测试未连接时获取上下文返回空对象。"""
        ctx = await connector.get_context()
        assert ctx.active_file is None
        assert ctx.selected_text is None


class TestContextErrorPropagation:
    """上下文获取失败必须上抛——内部链路禁止伪造空上下文。

    行为契约："无上下文"（正常的空选区）与"获取失败"（连接断/超时）必须可区分；
    降级是调用方（工具面）的决策，通道/连接器层一律传播错误。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "match"),
        [
            (ConnectionError("VSCode 扩展连接失败"), "连接失败"),
            (TimeoutError("VSCode 扩展请求超时"), "超时"),
        ],
        ids=["conn-error", "timeout"],
    )
    async def test_channel_listen_for_context_propagates(
        self, exc: Exception, match: str
    ) -> None:
        """通道层 listen_for_context 不吞 ConnectionError/TimeoutError。"""
        channel = VSCodeChannel(host="localhost", port=9741)
        channel.send_request = AsyncMock(side_effect=exc)  # type: ignore[method-assign]
        with pytest.raises(type(exc), match=match):
            await channel.listen_for_context()

    @pytest.mark.asyncio
    async def test_channel_success_still_parses_context(self) -> None:
        """传播语义不得破坏成功路径：正常响应仍解析出上下文字段。"""
        channel = VSCodeChannel(host="localhost", port=9741)
        payload = {
            "active_file": "a.py",
            "selected_text": "x",
            "cursor_position": {"line": 1, "column": 0},
            "open_files": ["a.py"],
            "metadata": {},
        }
        channel.send_request = AsyncMock(return_value=payload)  # type: ignore[method-assign]
        ctx = await channel.listen_for_context()
        # 断行为（字段透传解析），断类型副本会与多插件裸名模块治理hook相互干扰
        assert ctx.active_file == "a.py"
        assert ctx.selected_text == "x"
        assert ctx.cursor_position is not None
        assert ctx.cursor_position.line == 1
        assert ctx.open_files == ["a.py"]
        assert ctx.metadata == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [ConnectionError("conn down"), TimeoutError("slow")],
        ids=["conn-error", "timeout"],
    )
    async def test_connector_get_context_propagates(
        self, connected_connector: VSCodeConnector, exc: Exception
    ) -> None:
        """连接器层 get_context 同样上抛，且不再返回伪造空上下文。"""
        connected_connector.channel.listen_for_context = AsyncMock(side_effect=exc)  # type: ignore[method-assign]
        with pytest.raises(type(exc)):
            await connected_connector.get_context()


class TestSendRequestAsync:
    """send_request 异步化：阻塞 IO 必须移出事件循环，超时语义不变。"""

    @pytest.fixture
    def http_server(self):
        """真实本地 HTTP 服务：POST 延迟应答（可控延迟注入）。"""
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        holder: dict[str, float] = {"delay": 0.0}

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 —— 基类协议命名
                import time

                if holder["delay"]:
                    time.sleep(holder["delay"])
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                body = _json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, holder
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @staticmethod
    def _channel_for(server) -> VSCodeChannel:
        host, port = server.server_address[:2]
        return VSCodeChannel(host=str(host), port=int(port), timeout=5.0)

    @pytest.mark.asyncio
    async def test_request_succeeds_and_event_loop_stays_schedulable(self, http_server) -> None:
        """请求窗口内事件循环必须持续可调度（阻塞实现下 tick≈0）。"""
        server, holder = http_server
        holder["delay"] = 0.3
        channel = self._channel_for(server)

        ticks = 0
        async def _count() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        counter = asyncio.create_task(_count())
        resp = await asyncio.wait_for(channel.send_request("/context", {}), timeout=10)
        counter.cancel()
        assert resp == {"ok": True}
        assert ticks >= 5, (
            f"事件循环在请求窗口内仅被调度 {ticks} 次——urlopen 阻塞了事件循环"
        )

    @pytest.mark.asyncio
    async def test_timeout_semantics_preserved(self, http_server) -> None:
        """异步化后客户端 timeout 仍生效并翻译为 TimeoutError。"""
        server, holder = http_server
        holder["delay"] = 2.0
        host, port = server.server_address[:2]
        channel = VSCodeChannel(host=str(host), port=int(port), timeout=0.25)
        with pytest.raises(TimeoutError):
            await channel.send_request("/context", {})

    @pytest.mark.asyncio
    async def test_connection_refused_translates_to_connection_error(self) -> None:
        """URLError 翻译契约不变：拒连 → ConnectionError。"""
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()

        channel = VSCodeChannel(host="127.0.0.1", port=closed_port, timeout=1.0)
        with pytest.raises(ConnectionError):
            await channel.send_request("/context", {})


class TestExecuteAction:
    """执行操作测试。"""

    @pytest.mark.asyncio
    async def test_execute_action_success(self, connected_connector: VSCodeConnector) -> None:
        """测试执行操作成功。"""
        connected_connector.channel.send_request = AsyncMock(
            return_value={"success": True, "data": {"opened": True}}
        )
        action = ConnectorAction(action_type="open_file", parameters={"file_path": "a.py"})
        result = await connected_connector.execute_action(action)
        assert result.success is True
        assert result.data == {"opened": True}

    @pytest.mark.asyncio
    async def test_execute_action_failure_response(self, connected_connector: VSCodeConnector) -> None:
        """测试操作返回失败响应。"""
        connected_connector.channel.send_request = AsyncMock(
            return_value={"success": False, "error": "文件不存在"}
        )
        action = ConnectorAction(action_type="open_file", parameters={"file_path": "x.py"})
        result = await connected_connector.execute_action(action)
        assert result.success is False
        assert "文件不存在" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_action_when_disconnected(self, connector: VSCodeConnector) -> None:
        """测试未连接时执行操作返回失败。"""
        action = ConnectorAction(action_type="open_file")
        result = await connector.execute_action(action)
        assert result.success is False
        assert "未连接" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_action_connection_error_sets_error_state(
        self, connected_connector: VSCodeConnector
    ) -> None:
        """测试连接异常导致状态变为 ERROR。"""
        connected_connector.channel.send_request = AsyncMock(side_effect=ConnectionError("断连"))
        action = ConnectorAction(action_type="open_file")
        result = await connected_connector.execute_action(action)
        assert result.success is False
        assert connected_connector.state == ConnectorState.ERROR

    @pytest.mark.asyncio
    async def test_execute_action_assigns_action_id_if_empty(
        self, connected_connector: VSCodeConnector
    ) -> None:
        """测试空 action_id 时自动分配 UUID。"""
        connected_connector.channel.send_request = AsyncMock(
            return_value={"success": True, "data": None}
        )
        action = ConnectorAction(action_type="open_file", action_id="")
        await connected_connector.execute_action(action)
        assert len(action.action_id) > 0


class TestConnectorInfo:
    """连接器信息测试。"""

    def test_connector_type(self, connector: VSCodeConnector) -> None:
        """测试连接器类型为 vscode。"""
        assert connector.connector_type == "vscode"

    def test_get_info(self, connector: VSCodeConnector) -> None:
        """测试 get_info 返回正确的信息。"""
        info = connector.get_info()
        assert info.connector_type == "vscode"
        assert info.display_name == "Visual Studio Code"
        assert "open_file" in info.capabilities
        assert "show_diff" in info.capabilities
        assert info.priority == 10
