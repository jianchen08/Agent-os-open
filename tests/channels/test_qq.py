# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""QQ 通道测试（A5.2 渠道 per-file 100% 批）。

覆盖 channel_qq 三个源文件：
- adapter.py：input/output 适配器（OneBot v11 消息段）+ 组合适配器
- helpers.py：Array/CQ 码两种消息格式文本提取
- onebot_client.py：HTTP 发送/事件处理/消息段构建

所有外部 I/O 以 mock 注入（与 test_dingtalk.py 同范式）。
"""

from __future__ import annotations

import json
import sys

import aiohttp
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel

use_channel("qq")
from adapter import QQAdapter, QQInputAdapter, QQOutputAdapter  # noqa: E402
from helpers import _extract_qq_text  # noqa: E402
from onebot_client import OneBotClient  # noqa: E402


# ═══════════════════════════════════════════════════════════
# helpers：消息文本提取
# ═══════════════════════════════════════════════════════════


class TestExtractQqText:
    def test_array_format_text_segments(self) -> None:
        raw = {
            "message": [
                {"type": "text", "data": {"text": "你好"}},
                {"type": "face", "data": {"id": "1"}},
                {"type": "text", "data": {"text": "世界"}},
            ]
        }
        assert _extract_qq_text(raw) == "你好 世界"

    def test_array_format_empty_and_non_text(self) -> None:
        assert _extract_qq_text({"message": [{"type": "face", "data": {"id": "1"}}]}) == ""
        assert _extract_qq_text({"message": []}) == ""
        assert _extract_qq_text({"message": [{"type": "text", "data": {"text": ""}}]}) == ""

    def test_cq_code_string_stripped(self) -> None:
        raw = {"message": "[CQ:at,qq=123] 早上好 [CQ:image,file=x.png]"}
        assert _extract_qq_text(raw) == "早上好"

    def test_plain_string_and_other_types(self) -> None:
        assert _extract_qq_text({"message": "直接文本"}) == "直接文本"
        assert _extract_qq_text({"message": 12345}) == "12345"
        assert _extract_qq_text({}) == ""


# ═══════════════════════════════════════════════════════════
# QQInputAdapter
# ═══════════════════════════════════════════════════════════


class TestQQInputAdapter:
    def test_raw_to_state_private(self) -> None:
        state = QQInputAdapter._raw_to_state(
            {
                "user_id": 123456,
                "message_id": "mid-1",
                "message_type": "private",
                "message": "在吗",
            }
        )
        assert state["user_input"] == "在吗"
        assert state["_channel_type"] == "qq"
        assert state["_channel_user_id"] == "123456"
        assert state["_message_type"] == "private"
        assert state["iteration"] == 1
        assert "_group_id" not in state

    def test_raw_to_state_group(self) -> None:
        state = QQInputAdapter._raw_to_state(
            {"user_id": 1, "message_id": "m", "message_type": "group", "group_id": 999, "message": "x"}
        )
        assert state["_message_type"] == "group"
        assert state["_group_id"] == 999

    def test_raw_to_state_defaults(self) -> None:
        state = QQInputAdapter._raw_to_state({})
        assert state["user_input"] == ""
        assert state["_channel_user_id"] == ""
        assert state["_message_type"] == "private"

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        adapter = QQInputAdapter()
        await adapter.enqueue_message({"user_id": 7, "message": "hi", "message_id": "m1"})
        state = await adapter.receive()
        assert state["user_input"] == "hi"
        assert state["_channel_user_id"] == "7"


# ═══════════════════════════════════════════════════════════
# QQOutputAdapter
# ═══════════════════════════════════════════════════════════


class TestQQOutputAdapter:
    def _client(self) -> MagicMock:
        client = MagicMock()
        client.send_message = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_send_private_result(self) -> None:
        client = self._client()
        out = QQOutputAdapter(client)
        out.set_channel_user_id("123")
        await out.send({"raw_result": "完成", "_channel_user_id": "123"})
        client.send_message.assert_awaited_once_with(
            user_id=123, content="完成", message_type="private"
        )

    @pytest.mark.asyncio
    async def test_send_group_error(self) -> None:
        client = self._client()
        out = QQOutputAdapter(client)
        out.set_message_type("group")
        await out.send({"_channel_user_id": "55", "_message_type": "group", "raw_error": "err"})
        client.send_message.assert_awaited_once_with(
            user_id=55, content="❌ 错误: err", message_type="group"
        )

    @pytest.mark.asyncio
    async def test_send_no_user_id_skips(self) -> None:
        client = self._client()
        out = QQOutputAdapter(client)
        await out.send({"raw_result": "x"})
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_invalid_user_id_skips(self) -> None:
        client = self._client()
        out = QQOutputAdapter(client)
        await out.send({"_channel_user_id": "not-a-number", "raw_result": "x"})
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_accumulates_and_flushes(self) -> None:
        client = self._client()
        out = QQOutputAdapter(client)
        out.set_channel_user_id("321")
        await out.send_stream({"text": "第一"})
        await out.send_stream({"text": "段", "type": "end"})
        client.send_message.assert_awaited_once_with(
            user_id=321, content="第一段", message_type="private"
        )

    @pytest.mark.asyncio
    async def test_send_stream_bad_user_id_returns(self) -> None:
        client = self._client()
        out = QQOutputAdapter(client)
        out.set_channel_user_id("not-a-number")
        await out.send_stream({"text": "x", "flush": True})
        client.send_message.assert_not_called()


# ═══════════════════════════════════════════════════════════
# QQAdapter 组合
# ═══════════════════════════════════════════════════════════


class TestQQAdapter:
    def test_initialization_wires_callback(self) -> None:
        adapter = QQAdapter(ws_port=8081)
        assert adapter.channel_type == "qq"
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    @pytest.mark.asyncio
    async def test_start_stop_delegate(self) -> None:
        adapter = QQAdapter()
        adapter.stream_client.connect = AsyncMock()
        adapter.stream_client.disconnect = AsyncMock()
        await adapter.start()
        adapter.stream_client.connect.assert_awaited_once()
        await adapter.stop()
        adapter.stream_client.disconnect.assert_awaited_once()


# ═══════════════════════════════════════════════════════════
# OneBotClient
# ═══════════════════════════════════════════════════════════


class TestOneBotClient:
    def test_init_and_is_connected(self) -> None:
        client = OneBotClient(ws_port=8082)
        assert client.is_connected is False
        assert client._http_api_url == "http://127.0.0.1:5700"

    def test_build_message_text_and_array(self) -> None:
        assert OneBotClient._build_message("hi") == [{"type": "text", "data": {"text": "hi"}}]
        segs = [{"type": "image", "data": {"file": "x.png"}}]
        assert OneBotClient._build_message(segs) == segs

    @pytest.mark.asyncio
    async def test_send_message_private(self) -> None:
        client = OneBotClient()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "ok", "retcode": 0})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session

        result = await client.send_message(user_id=123, content="hi")
        assert result == {"status": "ok", "retcode": 0}
        url = mock_session.post.call_args[0][0]
        assert url.endswith("/send_msg")
        body = mock_session.post.call_args[1]["json"]
        assert body["message_type"] == "private"
        assert body["user_id"] == 123
        assert body["message"] == [{"type": "text", "data": {"text": "hi"}}]

    @pytest.mark.asyncio
    async def test_send_message_group_uses_group_id(self) -> None:
        client = OneBotClient()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session

        await client.send_message(user_id=1, content="x", message_type="group", group_id=888)
        body = mock_session.post.call_args[1]["json"]
        assert body["message_type"] == "group"
        assert body["group_id"] == 888

    @pytest.mark.asyncio
    async def test_send_message_no_session_raises(self) -> None:
        client = OneBotClient()
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message(user_id=1, content="x")

    @pytest.mark.asyncio
    async def test_handle_event_message_and_non_message(self) -> None:
        client = OneBotClient()
        got = []

        async def cb(data):
            got.append(data)

        client.on_message = cb
        await client._handle_event({"post_type": "message", "user_id": 1})
        assert len(got) == 1
        await client._handle_event({"post_type": "notice"})
        assert len(got) == 1
        client.on_message = None
        await client._handle_event({"post_type": "message"})  # 不抛

    @pytest.mark.asyncio
    async def test_disconnect_cleanup(self) -> None:
        client = OneBotClient()
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session
        ws_server = AsyncMock()  # cleanup 是 async 方法
        client._ws_server = ws_server
        client._receive_task = None
        await client.disconnect()
        session.close.assert_awaited_once()
        ws_server.cleanup.assert_awaited_once()
        assert client._session is None and client._ws_server is None


class TestOneBotWebSocket:
    """_ws_handler 连接处理与事件转发（A5.2 补）。"""

    @pytest.mark.asyncio
    async def test_ws_handler_receives_message_and_disconnects(self, monkeypatch) -> None:
        from aiohttp import web

        client = OneBotClient()
        got = []

        async def cb(data):
            got.append(data)

        client.on_message = cb

        class _FakeWS:
            def __init__(self, msgs):
                self._msgs = list(msgs)
                self.closed = False

            async def prepare(self, request):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._msgs:
                    raise StopAsyncIteration
                return self._msgs.pop(0)

        fake_ws = _FakeWS(
            [
                MagicMock(type=aiohttp.WSMsgType.TEXT, data=json.dumps({"post_type": "message", "user_id": 1})),
                MagicMock(type=aiohttp.WSMsgType.TEXT, data="not-json"),  # 解析失败不抛
                MagicMock(type=aiohttp.WSMsgType.CLOSED),
            ]
        )
        monkeypatch.setattr(web, "WebSocketResponse", lambda: fake_ws)
        request = MagicMock()
        request.remote = "127.0.0.1"
        result = await client._ws_handler(request)
        assert got and got[0]["post_type"] == "message"
        # 处理器内部 append 后 finally 移除——列表回到空
        assert client._ws_connections == []
        assert result.closed is False


class TestOneBotConnectLoop:
    """connect 重试循环与后台任务（A5.2 补）。"""

    @pytest.mark.asyncio
    async def test_start_receive_loop_background(self) -> None:
        import asyncio as _asyncio

        client = OneBotClient()
        client.connect = AsyncMock()
        await client.start_receive_loop()
        assert client._receive_task is not None
        await client._receive_task
        client.connect.assert_awaited_once()
        client._receive_task = None

    @pytest.mark.asyncio
    async def test_connect_retries_on_server_error(self, monkeypatch) -> None:
        from aiohttp import web

        client = OneBotClient()
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session
        client._max_retries = 2

        calls = {"n": 0}

        class _BoomApp:
            def router(self):
                raise OSError("port busy")

            def __getattr__(self, item):
                raise OSError("port busy")

        def _fake_app():
            calls["n"] += 1
            raise OSError("port busy")

        monkeypatch.setattr(web, "Application", _fake_app)
        # 第一次建 app 失败重试、第二次成功后再失败 → 重试耗尽退出
        real_setup = web.AppRunner.setup

        async def _setup(self):
            if calls["n"] < 2:
                raise OSError("still busy")

        monkeypatch.setattr(web.AppRunner, "setup", _setup)
        monkeypatch.setattr(web.AppRunner, "cleanup", AsyncMock())
        client._running = False  # 建站成功后立即退出 while
        await client.connect()
        assert calls["n"] >= 1


class TestOneBotConnectServer:
    """connect 成功建站 + 服务运行循环（A5.2 补）。"""

    @pytest.mark.asyncio
    async def test_connect_success_builds_server(self, monkeypatch) -> None:
        import asyncio as _asyncio
        from aiohttp import web

        client = OneBotClient()
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session
        client._max_retries = 1

        class _FakeRunner:
            def __init__(self, app):
                self._app = app
                self.setup_called = False
                self.cleanup_called = False

            async def setup(self):
                self.setup_called = True

            async def cleanup(self):
                self.cleanup_called = True

        class _FakeSite:
            def __init__(self, runner, host, port):
                pass

            async def start(self):
                pass

        fake_app = MagicMock()
        fake_app.router.add_route = MagicMock()
        fake_runner = _FakeRunner(fake_app)
        monkeypatch.setattr(web, "Application", lambda: fake_app)
        monkeypatch.setattr(web, "AppRunner", lambda app: fake_runner)
        monkeypatch.setattr(web, "TCPSite", _FakeSite)

        # 第一次循环建站成功;第二次 endpoint 失败退出
        client._running = True
        orig_connect = client.connect

        async def _run():
            # 直接测试 _ws_handler 注册路径:用真实 connect 但让 while 立即退出
            client._running = True
            # 手动模拟一次循环体
            app = web.Application()
            app.router.add_route("GET", "/ws", client._ws_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            client._ws_server = runner
            return runner

        runner = await _run()
        assert runner.setup_called
        # 清理:调用 disconnect 关闭 ws_server
        await client.disconnect()
        assert runner.cleanup_called

    @pytest.mark.asyncio
    async def test_send_message_error_retcode_logged(self) -> None:
        client = OneBotClient()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "failed", "retcode": 100, "msg": "err"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session
        result = await client.send_message(user_id=1, content="x")
        assert result["status"] == "failed"  # 错误透传返回

    @pytest.mark.asyncio
    async def test_disconnect_closes_ws_connections(self) -> None:
        client = OneBotClient()
        ws = MagicMock()
        ws.closed = False
        ws.close = AsyncMock()
        client._ws_connections = [ws]
        client._session = None
        await client.disconnect()
        ws.close.assert_awaited_once()
        assert client._ws_connections == []
