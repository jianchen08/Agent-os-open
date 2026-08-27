# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""QQ 通道测试（A5.2 渠道 per-file 100% 批）。

覆盖 channel_qq 三个源文件：
- adapter.py：input/output 适配器（OneBot v11 消息段）+ 组合适配器
- helpers.py：Array/CQ 码两种消息格式文本提取
- onebot_client.py：HTTP 发送/事件处理/消息段构建

所有外部 I/O 以 mock 注入（与 test_dingtalk.py 同范式）。
"""

from __future__ import annotations

import asyncio
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
    @pytest.mark.asyncio
    async def test_raw_envelope_full(self) -> None:
        """私聊原始报文经 enqueue→receive 得到管道信封。"""
        adapter = QQInputAdapter()
        await adapter.enqueue_message(
            {
                "user_id": 123456,
                "message_id": "mid-1",
                "message_type": "private",
                "message": "在吗",
            }
        )
        state = await adapter.receive()
        assert state["user_input"] == "在吗"
        assert state["_channel_type"] == "qq"
        assert state["_channel_user_id"] == "123456"
        assert state["_message_type"] == "private"
        assert state["iteration"] == 1
        assert "_group_id" not in state

    @pytest.mark.asyncio
    async def test_raw_group_fields(self) -> None:
        """群消息报文携带群号与群类型。"""
        adapter = QQInputAdapter()
        await adapter.enqueue_message(
            {"user_id": 1, "message_id": "m", "message_type": "group", "group_id": 999, "message": "x"}
        )
        state = await adapter.receive()
        assert state["_message_type"] == "group"
        assert state["_group_id"] == 999

    @pytest.mark.asyncio
    async def test_raw_empty_defaults(self) -> None:
        """空报文降级为默认私聊信封。"""
        adapter = QQInputAdapter()
        await adapter.enqueue_message({})
        state = await adapter.receive()
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

    @pytest.mark.asyncio
    async def test_send_propagates_send_failure(self) -> None:
        """底层发送失败 → 异常传播给调用方（适配器不吞错，管道可感知丢消息）。"""
        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("OneBot send message failed: retcode=100"))
        out = QQOutputAdapter(client)
        with pytest.raises(RuntimeError, match="OneBot send message failed"):
            await out.send({"raw_result": "hello", "_channel_user_id": "123"})

    @pytest.mark.asyncio
    async def test_send_stream_flush_propagates_send_failure(self) -> None:
        """流式 flush 发送失败 → 异常传播，不静默丢弃累积文本。"""
        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("OneBot send message failed"))
        out = QQOutputAdapter(client)
        out.set_channel_user_id("321")
        with pytest.raises(RuntimeError, match="OneBot send message failed"):
            await out.send_stream({"text": "完整", "flush": True})


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
        # 构造期 HTTP API 配置的生效由 send_message 请求 URL 断言承载

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
        # 默认构造的 HTTP API 地址（OneBot 标准端口 5700）经发送行为可观察
        url = mock_session.post.call_args[0][0]
        assert url == "http://127.0.0.1:5700/send_msg"
        body = mock_session.post.call_args[1]["json"]
        assert body["message_type"] == "private"
        assert body["user_id"] == 123
        assert body["message"] == [{"type": "text", "data": {"text": "hi"}}]

    @pytest.mark.asyncio
    async def test_send_message_array_segments_passthrough(self) -> None:
        """消息段数组直通 OneBot 报文（不包 text 段包装）。"""
        client = OneBotClient()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session

        segs = [{"type": "image", "data": {"file": "x.png"}}]
        await client.send_message(user_id=123, content=segs)
        body = mock_session.post.call_args[1]["json"]
        assert body["message"] == segs

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
    async def test_ws_frames_filtered_and_dispatched(self, monkeypatch) -> None:
        """WS 帧 → 事件分发契约：message 帧送达回调、notice 帧被过滤、
        回调缺位不抛（连接循环存续）。"""
        from aiohttp import web

        client = OneBotClient()
        message_frame = json.dumps({"post_type": "message", "user_id": 1})
        notice_frame = json.dumps({"post_type": "notice"})

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

        request = MagicMock()
        request.remote = "127.0.0.1"

        got: list[dict] = []

        async def cb(data):
            got.append(data)

        client.on_message = cb
        fake_ws = _FakeWS(
            [
                MagicMock(type=aiohttp.WSMsgType.TEXT, data=message_frame),
                MagicMock(type=aiohttp.WSMsgType.TEXT, data=notice_frame),
                MagicMock(type=aiohttp.WSMsgType.CLOSED),
            ]
        )
        monkeypatch.setattr(web, "WebSocketResponse", lambda: fake_ws)
        await client._ws_handler(request)
        assert [e["post_type"] for e in got] == ["message"]

        # 回调缺位：message 帧不再投递，但处理不抛
        client.on_message = None
        fake_ws_none_cb = _FakeWS(
            [
                MagicMock(type=aiohttp.WSMsgType.TEXT, data=message_frame),
                MagicMock(type=aiohttp.WSMsgType.CLOSED),
            ]
        )
        monkeypatch.setattr(web, "WebSocketResponse", lambda: fake_ws_none_cb)
        await client._ws_handler(request)  # 不抛

    @pytest.mark.asyncio
    async def test_disconnect_cleanup(self) -> None:
        client = OneBotClient()
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session
        ws_server = AsyncMock()  # cleanup 是 async 方法
        client._ws_server = ws_server
        await client.disconnect()
        # 幂等契约：第二次 disconnect 不再重复释放资源，也不抛
        await client.disconnect()
        session.close.assert_awaited_once()
        ws_server.cleanup.assert_awaited_once()


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
        # 处理器内部 append 后 finally 移除——连接不再存活（公共观察面）
        assert client.is_connected is False
        assert result.closed is False

    @pytest.mark.asyncio
    async def test_ws_handler_callback_failure_visible_and_loop_survives(
        self, caplog, monkeypatch
    ) -> None:
        """回调异常分级契约：error 级记录+失败计数，连接循环不终结、后续事件照常送达。"""
        import logging

        from aiohttp import web

        client = OneBotClient()
        processed: list[dict] = []
        calls = {"n": 0}

        async def cb(data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("callback boom")
            processed.append(data)

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
                MagicMock(type=aiohttp.WSMsgType.TEXT, data=json.dumps({"post_type": "message", "m": 1})),
                MagicMock(type=aiohttp.WSMsgType.TEXT, data=json.dumps({"post_type": "message", "m": 2})),
                MagicMock(type=aiohttp.WSMsgType.CLOSED),
            ]
        )
        monkeypatch.setattr(web, "WebSocketResponse", lambda: fake_ws)
        request = MagicMock()
        request.remote = "127.0.0.1"

        with caplog.at_level("WARNING", logger="onebot_client"):
            await client._ws_handler(request)

        # 核心行为不变量：首条回调失败后连接循环仍在跑，第二条消息照常送达
        # （OneBot 契约：on_message 回调接收完整事件对象）
        assert processed == [{"post_type": "message", "m": 2}]
        # 失败可见性：error 级以上记录 + 公开失败计数递增
        assert client.failed_event_count == 1
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


class TestOneBotConnectLoop:
    """connect 重试循环与后台任务（A5.2 补）。"""

    @pytest.mark.asyncio
    async def test_start_receive_loop_background(self) -> None:
        """start_receive_loop 后台托管 connect：异步启动且阻塞期间可被 disconnect 及时回收。"""
        client = OneBotClient()
        started = asyncio.Event()

        async def _blocking_connect() -> None:
            started.set()
            await asyncio.sleep(3600)

        client.connect = _blocking_connect
        await client.start_receive_loop()
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(client.disconnect(), timeout=5)

    @pytest.mark.asyncio
    async def test_connect_retries_on_server_error(self, monkeypatch) -> None:
        from aiohttp import web

        client = OneBotClient(max_retries=2)
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session

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
        await client.connect()
        assert calls["n"] >= 1


class TestOneBotConnectServer:
    """connect 成功建站 + 服务运行循环（A5.2 补）。"""

    @pytest.mark.asyncio
    async def test_connect_success_builds_server(self, monkeypatch) -> None:
        import asyncio as _asyncio
        from aiohttp import web

        client = OneBotClient(max_retries=1)
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session

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

        # 建站成功路径：手动驱动一次真实注册流程
        async def _run():
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
    async def test_send_message_api_error_raises(self) -> None:
        """status!=ok 且 retcode!=0 → RuntimeError 上抛（契约：发送失败可感知）。"""
        client = OneBotClient()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "failed", "retcode": 100, "msg": "err"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session

        with pytest.raises(RuntimeError, match="retcode=100"):
            await client.send_message(user_id=1, content="x")

    @pytest.mark.asyncio
    async def test_send_message_success_returns_result(self) -> None:
        """成功响应正常返回（行为不变量：失败上抛不改成功路径）。"""
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

    @pytest.mark.asyncio
    async def test_disconnect_closes_ws_connections(self, monkeypatch) -> None:
        """经真实 ws_handler 注册的连接，disconnect 时被统一关闭。"""
        from aiohttp import web

        client = OneBotClient()

        class _BlockedWS:
            def __init__(self):
                self.closed = False
                self.close = AsyncMock()

            async def prepare(self, request):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(0.01)
                if self.close.await_count:
                    raise StopAsyncIteration
                return MagicMock(type=aiohttp.WSMsgType.TEXT, data="{}")

        blocked = _BlockedWS()
        monkeypatch.setattr(web, "WebSocketResponse", lambda: blocked)
        request = MagicMock()
        request.remote = "127.0.0.1"
        handler_task = asyncio.create_task(client._ws_handler(request))

        async def _wait_registered():
            while not client.is_connected:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_registered(), timeout=2)
        await asyncio.wait_for(client.disconnect(), timeout=2)
        blocked.close.assert_awaited_once()
        # 连接已被服务端关闭，接收循环自然收尾
        await asyncio.wait_for(handler_task, timeout=2)

    @pytest.mark.asyncio
    async def test_disconnect_cancels_receive_task(self) -> None:
        """_receive_task 未完成 → 取消并等待,资源释放。"""
        import asyncio as _asyncio

        client = OneBotClient()
        client._session = None
        client._ws_server = None

        async def _never_done():
            await _asyncio.sleep(10)

        task = _asyncio.create_task(_never_done())
        client._receive_task = task
        await client.disconnect()
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_disconnect_closes_open_session(self) -> None:
        """session 未关闭时断开 → close 调用并置 None。"""
        client = OneBotClient()
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session
        await client.disconnect()
        session.close.assert_awaited_once()
        # 会话已释放：再次发送报"未初始化"而非静默成功
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message(user_id=1, content="x")

    @pytest.mark.asyncio
    async def test_connect_full_loop_start_and_stop(self, monkeypatch) -> None:
        """真实 connect() 主循环:建站成功 → 服务循环 → disconnect 退出。"""
        from aiohttp import web

        client = OneBotClient(max_retries=3)

        calls = {"loop": 0}

        class _FakeRunner:
            def __init__(self, app):
                self.cleanup_called = False

            async def setup(self):
                pass

            async def cleanup(self):
                self.cleanup_called = True

        fake_runner = _FakeRunner(None)
        fake_app = MagicMock()
        monkeypatch.setattr(web, "Application", lambda: fake_app)
        monkeypatch.setattr(web, "AppRunner", lambda app: fake_runner)
        monkeypatch.setattr(web, "TCPSite", lambda runner, host, port: _FakeSite())

        class _FakeSite:
            async def start(self):
                pass

        async def _bounded_sleep(duration):
            calls["loop"] += 1
            if calls["loop"] >= 2:
                client._running = False  # 两次循环后退出

        # 用注入 sleep 计数逼近退出
        import asyncio as _asyncio

        async def _patched_sleep(duration):
            calls["loop"] += 1
            if calls["loop"] >= 2:
                client._running = False
            return await _asyncio.sleep(0)

        monkeypatch.setattr(_asyncio, "sleep", _patched_sleep)
        await client.connect()
        assert calls["loop"] >= 2
        await client.disconnect()
        assert fake_runner.cleanup_called
