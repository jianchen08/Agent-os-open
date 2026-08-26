# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_feishu stream_client 测试（A5.2 补）。

FeishuStreamClient 的 token 获取 / endpoint 获取 / 消息发送 / 卡片发送 /
WebSocket 接收循环 / 断线重连 / 事件分发。外部依赖（aiohttp 会话、
WebSocket、时钟）全部 mock，协议行为真实断言。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

pytestmark = pytest.mark.unit

from tests.channels.conftest import use_channel

use_channel("feishu")
import stream_client as _stream_client_mod
from stream_client import FeishuStreamClient


def _fake_response(payload: dict[str, Any]) -> MagicMock:
    """构造 aiohttp 响应上下文管理器。"""
    resp = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock()
    return session


def _make_client(**kwargs: Any) -> FeishuStreamClient:
    return FeishuStreamClient(
        app_id="cli_test",
        app_secret="secret",
        base_url="https://open.feishu.cn/",
        **kwargs,
    )


class _FakeWS:
    """可 async for 迭代的假 WebSocket。"""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.closed = False
        self.send_json = AsyncMock()

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for msg in self._messages:
            yield msg


class TestToken:
    """tenant_access_token 获取与缓存。"""

    @pytest.mark.asyncio
    async def test_ensure_token_fetches_and_caches(self, monkeypatch) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response(
            {"tenant_access_token": "tok-1", "expire": 7200}
        )
        fake_now = [1000.0]
        monkeypatch.setattr("stream_client.time.time", lambda: fake_now[0])

        await client._ensure_token()
        assert client._tenant_token == "tok-1"
        assert client._token_expires == 1000.0 + 7200
        url = client._session.post.call_args[0][0]
        assert url.endswith("/open-apis/auth/v3/tenant_access_token/internal")
        body = client._session.post.call_args[1]["json"]
        assert body == {"app_id": "cli_test", "app_secret": "secret"}

        # 缓存命中：token 未过期（距过期 >60s）不再请求
        client._session.post.reset_mock()
        await client._ensure_token()
        client._session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_token_refreshes_when_expiring(self, monkeypatch) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response(
            {"tenant_access_token": "tok-2", "expire": 7200}
        )
        fake_now = [1000.0]
        monkeypatch.setattr("stream_client.time.time", lambda: fake_now[0])

        client._tenant_token = "tok-old"
        client._token_expires = 1000.0 + 30  # 距过期 30s < 60s 阈值
        await client._ensure_token()
        assert client._tenant_token == "tok-2"
        assert client._session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_ensure_token_without_session_raises(self) -> None:
        client = _make_client()
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client._ensure_token()


class TestSendMessage:
    """send_message 协议行为。"""

    @pytest.mark.asyncio
    async def test_send_text_message(self) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 0, "msg": "ok"})
        client._ensure_token = AsyncMock()
        client._tenant_token = "tok"

        result = await client.send_message("ou_1", "hello", "text")
        assert result == {"code": 0, "msg": "ok"}
        url = client._session.post.call_args[0][0]
        assert url == (
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        )
        body = client._session.post.call_args[1]["json"]
        assert body["receive_id"] == "ou_1"
        assert body["msg_type"] == "text"
        assert json.loads(body["content"]) == {"text": "hello"}
        headers = client._session.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_send_interactive_message_passes_content_raw(self) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 0})
        client._tenant_token = "tok"

        card = {"elements": [{"tag": "div"}]}
        await client.send_message("ou_2", json.dumps(card), "interactive")
        body = client._session.post.call_args[1]["json"]
        assert body["msg_type"] == "interactive"
        assert body["content"] == json.dumps(card)

    @pytest.mark.asyncio
    async def test_send_message_error_code_logged_and_returned(self, caplog) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 99991, "msg": "bad"})
        client._tenant_token = "tok"

        with caplog.at_level("ERROR", logger="stream_client"):
            result = await client.send_message("ou_1", "hi")
        assert result == {"code": 99991, "msg": "bad"}
        assert "Feishu send message failed" in caplog.text

    @pytest.mark.asyncio
    async def test_send_message_without_session_raises(self) -> None:
        client = _make_client()
        client._tenant_token = "tok"
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message("ou_1", "hi")

    @pytest.mark.asyncio
    async def test_send_message_session_gone_after_token_raises(self) -> None:
        # token 有效（_ensure_token 早退）但 session 为 None → send_message 自身抛错
        client = _make_client()
        client._tenant_token = "tok"
        client._token_expires = 10**12
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message("ou_1", "hi")


class TestSendCard:
    """send_card 协议行为。"""

    @pytest.mark.asyncio
    async def test_send_card_success(self) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 0, "msg": "ok"})
        client._tenant_token = "tok"

        card = {"elements": [{"tag": "hr"}]}
        result = await client.send_card("ou_1", card)
        assert result == {"code": 0, "msg": "ok"}
        body = client._session.post.call_args[1]["json"]
        assert body["receive_id"] == "ou_1"
        assert body["msg_type"] == "interactive"
        assert body["content"] == json.dumps(card)

    @pytest.mark.asyncio
    async def test_send_card_error_code_logged(self, caplog) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 1, "msg": "fail"})
        client._tenant_token = "tok"

        with caplog.at_level("ERROR", logger="stream_client"):
            result = await client.send_card("ou_1", {"elements": []})
        assert result == {"code": 1, "msg": "fail"}
        assert "Feishu send card failed" in caplog.text

    @pytest.mark.asyncio
    async def test_send_card_without_session_raises(self) -> None:
        client = _make_client()
        client._tenant_token = "tok"
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_card("ou_1", {"elements": []})

    @pytest.mark.asyncio
    async def test_send_card_session_gone_after_token_raises(self) -> None:
        client = _make_client()
        client._tenant_token = "tok"
        client._token_expires = 10**12
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_card("ou_1", {"elements": []})


class TestEndpoint:
    """Stream endpoint 获取。"""

    @pytest.mark.asyncio
    async def test_get_endpoint_success(self) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response(
            {"data": {"endpoint": "wss://stream.feishu.cn/ws"}}
        )
        client._ensure_token = AsyncMock()
        client._tenant_token = "tok"

        endpoint = await client._get_endpoint()
        assert endpoint == "wss://stream.feishu.cn/ws"
        url = client._session.post.call_args[0][0]
        assert url.endswith("/open-apis/callback/ws/endpoint")
        headers = client._session.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_get_endpoint_missing_returns_empty(self, caplog) -> None:
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"data": {}})
        client._tenant_token = "tok"

        with caplog.at_level("ERROR", logger="stream_client"):
            endpoint = await client._get_endpoint()
        assert endpoint == ""
        assert "No stream endpoint" in caplog.text

    @pytest.mark.asyncio
    async def test_get_endpoint_session_gone_after_token_raises(self) -> None:
        client = _make_client()
        client._tenant_token = "tok"
        client._token_expires = 10**12
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client._get_endpoint()


class TestReceiveLoop:
    """WebSocket 接收循环与事件分发。"""

    @staticmethod
    def _ws_msg(msg_type: Any, data: Any = None) -> SimpleNamespace:
        return SimpleNamespace(type=msg_type, data=data)

    @staticmethod
    def _text_msg(data: str) -> SimpleNamespace:
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=data)

    @staticmethod
    def _close_msg() -> SimpleNamespace:
        return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)

    @pytest.mark.asyncio
    async def test_handle_text_event_dispatches_to_callback(self) -> None:
        client = _make_client()
        client._ws = MagicMock()
        client._ws.closed = False
        client._ws.send_json = AsyncMock()
        received: list[dict[str, Any]] = []

        async def _cb(payload: dict[str, Any]) -> None:
            received.append(payload)

        client.on_message = _cb
        event = {
            "schema": "2.0",
            "header": {"event_id": "evt-1"},
            "headers": {"event_type": "im.message.receive_v1"},
            "data": {"message": {"content": "hi"}},
        }
        await client._handle_event(event)
        assert received == [{"message": {"content": "hi"}}]
        # schema 2.0 需回 ACK
        client._ws.send_json.assert_awaited_once_with(
            {"schema": "2.0", "header": {"event_id": "evt-1"}}
        )

    @pytest.mark.asyncio
    async def test_handle_event_no_callback_still_acks(self) -> None:
        # schema 2.0 事件无论有无回调都需回 ACK（协议要求）
        client = _make_client()
        client._ws = _FakeWS([])
        client.on_message = None
        await client._handle_event(
            {"schema": "2.0", "header": {"event_id": "evt-2"}, "headers": {"event_type": "x"}}
        )
        client._ws.send_json.assert_awaited_once_with(
            {"schema": "2.0", "header": {"event_id": "evt-2"}}
        )

    @pytest.mark.asyncio
    async def test_handle_event_unknown_type_skipped(self) -> None:
        client = _make_client()
        client._ws = MagicMock()
        client._ws.closed = False
        client._ws.send_json = AsyncMock()
        called: list[dict[str, Any]] = []

        async def _cb(payload: dict[str, Any]) -> None:
            called.append(payload)

        client.on_message = _cb
        await client._handle_event(
            {"headers": {"event_type": "im.chat.updated"}, "data": {"x": 1}}
        )
        assert called == []

    @pytest.mark.asyncio
    async def test_handle_event_ws_closed_skips_ack(self) -> None:
        client = _make_client()
        ws = MagicMock()
        ws.closed = True
        ws.send_json = AsyncMock()
        client._ws = ws
        client.on_message = None
        await client._handle_event(
            {"schema": "2.0", "header": {}, "headers": {"event_type": "x"}}
        )
        ws.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_event_message_type_no_callback(self) -> None:
        # 消息事件但未注册回调 → 静默跳过
        client = _make_client()
        client._ws = _FakeWS([])
        client.on_message = None
        await client._handle_event(
            {"headers": {"event_type": "im.message.receive_v1"}, "data": {"m": 1}}
        )

    @pytest.mark.asyncio
    async def test_receive_loop_text_and_close(self) -> None:
        client = _make_client()
        client._ws = _FakeWS(
            [
                self._text_msg(json.dumps({"headers": {"event_type": ""}, "data": {"m": 1}})),
                self._close_msg(),
            ]
        )
        received: list[dict[str, Any]] = []

        async def _cb(payload: dict[str, Any]) -> None:
            received.append(payload)

        client.on_message = _cb
        await client._receive_loop()
        assert received == [{"m": 1}]

    @pytest.mark.asyncio
    async def test_receive_loop_bad_json_warns(self, caplog) -> None:
        client = _make_client()
        client._ws = _FakeWS([self._text_msg("{not json")])
        with caplog.at_level("WARNING", logger="stream_client"):
            await client._receive_loop()
        assert "Error handling stream message" in caplog.text

    @pytest.mark.asyncio
    async def test_receive_loop_callback_error_warns(self, caplog) -> None:
        # 回调抛非 JSONDecodeError 异常 → 走 except 的 Exception 分支
        client = _make_client()
        client._ws = _FakeWS(
            [self._text_msg(json.dumps({"headers": {"event_type": ""}, "data": {"m": 1}}))]
        )

        async def _cb(payload: dict[str, Any]) -> None:
            raise ValueError("boom")

        client.on_message = _cb
        with caplog.at_level("WARNING", logger="stream_client"):
            await client._receive_loop()
        assert "Error handling stream message" in caplog.text

    @pytest.mark.asyncio
    async def test_receive_loop_no_ws_returns(self) -> None:
        client = _make_client()
        client._ws = None
        await client._receive_loop()  # 不抛

    @pytest.mark.asyncio
    async def test_receive_loop_unknown_msg_type_continues(self) -> None:
        # 非 TEXT/CLOSED/ERROR 消息类型（如 BINARY）→ 跳过并继续循环
        client = _make_client()
        client._ws = _FakeWS(
            [
                SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"x"),
                self._close_msg(),
            ]
        )
        await client._receive_loop()  # 不抛

    @pytest.mark.asyncio
    async def test_receive_loop_running_logs_reconnect(self, caplog) -> None:
        client = _make_client()
        client._ws = _FakeWS([self._close_msg()])
        client._running = True
        with caplog.at_level("INFO", logger="stream_client"):
            await client._receive_loop()
        assert "will reconnect" in caplog.text


class TestConnectDisconnect:
    """连接 / 重连 / 断开。"""

    @pytest.mark.asyncio
    async def test_connect_success_path(self, monkeypatch) -> None:
        client = _make_client(max_retries=2)
        session = _fake_session()
        monkeypatch.setattr(_stream_client_mod.aiohttp, "ClientSession", lambda: session)
        client._ensure_token = AsyncMock()
        client._get_endpoint = AsyncMock(return_value="wss://stream.feishu.cn/ws")
        ws = MagicMock()
        ws.closed = False
        session.ws_connect = AsyncMock(return_value=ws)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _blocking_receive_loop() -> None:
            # 真实 receive_loop 会阻塞到连接关闭；mock 阻塞在事件上，
            # 避免 connect 的 while 循环空转
            entered.set()
            await release.wait()

        client._receive_loop = _blocking_receive_loop
        task = asyncio.create_task(client.connect())
        await asyncio.wait_for(entered.wait(), timeout=5)
        session.ws_connect.assert_awaited_once_with("wss://stream.feishu.cn/ws")
        assert client.is_connected is True
        # 模拟断开：_running 置 False 后释放接收循环，connect 退出
        client._running = False
        release.set()
        await task

    @pytest.mark.asyncio
    async def test_connect_retries_then_gives_up(self, monkeypatch) -> None:
        client = _make_client(max_retries=2, base_delay=0.01)
        session = _fake_session()
        monkeypatch.setattr(_stream_client_mod.aiohttp, "ClientSession", lambda: session)
        client._ensure_token = AsyncMock()
        client._get_endpoint = AsyncMock(return_value="")
        sleeps: list[float] = []
        original_sleep = asyncio.sleep

        def _fake_sleep(delay: float) -> Any:
            sleeps.append(delay)
            return original_sleep(0)

        monkeypatch.setattr(_stream_client_mod.asyncio, "sleep", _fake_sleep)

        await client.connect()
        # 2 次重试：第 1 次失败后 sleep，第 2 次失败后退出
        assert client._get_endpoint.await_count == 2
        assert sleeps == [0.01]

    @pytest.mark.asyncio
    async def test_start_receive_loop_spawns_background_task(self) -> None:
        client = _make_client()
        client.connect = AsyncMock()
        await client.start_receive_loop()
        assert client._receive_task is not None
        await client._receive_task
        client.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_closes_ws_and_session(self) -> None:
        client = _make_client()
        ws = MagicMock()
        ws.closed = False
        ws.close = AsyncMock()
        session = _fake_session()
        session.close = AsyncMock()
        client._ws = ws
        client._session = session
        client._running = True

        await client.disconnect()
        assert client._running is False
        ws.close.assert_awaited_once()
        session.close.assert_awaited_once()
        assert client._ws is None
        assert client._session is None

    @pytest.mark.asyncio
    async def test_disconnect_cancels_receive_task(self) -> None:
        client = _make_client()
        task = asyncio.create_task(asyncio.sleep(10))
        client._receive_task = task
        client._ws = None
        client._session = None

        await client.disconnect()
        assert task.cancelled() is True
        assert client._receive_task is None

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self) -> None:
        client = _make_client()
        client._ws = None
        client._session = None
        await client.disconnect()  # 不抛
        await client.disconnect()  # 再次断开不抛
