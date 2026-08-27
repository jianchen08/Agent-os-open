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
    """tenant_access_token 获取与缓存（经公共 send_message 流程观察）。"""

    @staticmethod
    def _routing_session(token_payloads: list[dict[str, Any]]) -> MagicMock:
        """按端点路由的假会话：auth 端点依次消费 token_payloads，其余端点回成功。"""
        queue = iter(token_payloads)
        session = _fake_session()

        def _post(url: str, **_kw: Any) -> MagicMock:
            if url.endswith("/auth/v3/tenant_access_token/internal"):
                return _fake_response(next(queue))
            return _fake_response({"code": 0, "msg": "ok"})

        session.post = MagicMock(side_effect=_post)
        return session

    @pytest.mark.asyncio
    async def test_send_message_fetches_and_caches_token(self, monkeypatch) -> None:
        """首次发送取 token（携带应用凭证），token 未过期时第二次发送不再请求。"""
        fake_now = [1000.0]
        monkeypatch.setattr("stream_client.time.time", lambda: fake_now[0])
        client = _make_client()
        session = self._routing_session(
            [{"tenant_access_token": "tok-1", "expire": 7200}]
        )
        client._session = session

        first = await client.send_message("ou_1", "hello", "text")
        assert first == {"code": 0, "msg": "ok"}
        result = await client.send_message("ou_2", "hello again", "text")
        assert result == {"code": 0, "msg": "ok"}

        calls = session.post.call_args_list
        auth_calls = [
            c
            for c in calls
            if c[0][0].endswith("/open-apis/auth/v3/tenant_access_token/internal")
        ]
        assert len(auth_calls) == 1
        # 取 token 请求的端点与凭证体
        auth_url = auth_calls[0][0][0]
        assert auth_url.endswith("/open-apis/auth/v3/tenant_access_token/internal")
        body = auth_calls[0][1]["json"]
        assert body == {"app_id": "cli_test", "app_secret": "secret"}
        # 两次发送均携带同一（缓存的）token
        send_headers = [
            c[1]["headers"]["Authorization"]
            for c in calls
            if "im/v1/messages?" in c[0][0] or c[0][0].endswith("/im/v1/messages")
        ]
        assert send_headers == ["Bearer tok-1", "Bearer tok-1"]

    @pytest.mark.asyncio
    async def test_send_message_refreshes_expiring_token(self, monkeypatch) -> None:
        """expire 推进到过期后，再次发送会重新取 token 并使用新 token。"""
        fake_now = [1000.0]
        monkeypatch.setattr("stream_client.time.time", lambda: fake_now[0])
        client = _make_client()
        session = self._routing_session(
            [{"tenant_access_token": "tok-1", "expire": 7200}, {"tenant_access_token": "tok-2", "expire": 7200}]
        )
        client._session = session
        await client.send_message("ou_1", "hi")

        fake_now[0] += 7200  # 推进到 token 过期
        result = await client.send_message("ou_1", "hi again")
        assert result == {"code": 0, "msg": "ok"}

        calls = session.post.call_args_list
        auth_calls = [
            c
            for c in calls
            if c[0][0].endswith("/open-apis/auth/v3/tenant_access_token/internal")
        ]
        assert len(auth_calls) == 2  # 首次 + 过期后重取
        send_headers = [
            c[1]["headers"]["Authorization"]
            for c in calls
            if "im/v1/messages?" in c[0][0] or c[0][0].endswith("/im/v1/messages")
        ]
        assert send_headers == ["Bearer tok-1", "Bearer tok-2"]

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
    async def test_send_message_api_error_raises_and_logs(self, caplog) -> None:
        """API 错误码 → RuntimeError 上抛且 error 日志记录（发送失败可感知）。"""
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 99991, "msg": "bad"})
        client._tenant_token = "tok"

        with caplog.at_level("ERROR", logger="stream_client"), pytest.raises(
            RuntimeError, match="code=99991"
        ):
            await client.send_message("ou_1", "hi")
        assert "Feishu send message failed" in caplog.text

    @pytest.mark.asyncio
    async def test_send_message_success_code_zero_returns_result(self) -> None:
        """成功码 0 正常返回（行为不变量：失败上抛不改成功路径）。"""
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 0, "msg": "ok"})
        client._tenant_token = "tok"
        result = await client.send_message("ou_1", "hi")
        assert result == {"code": 0, "msg": "ok"}

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
    async def test_send_card_api_error_raises_and_logs(self, caplog) -> None:
        """卡片发送 API 错误码 → RuntimeError 上抛（与 send_message 同契约）。"""
        client = _make_client()
        client._session = _fake_session()
        client._session.post.return_value = _fake_response({"code": 1, "msg": "fail"})
        client._tenant_token = "tok"

        with caplog.at_level("ERROR", logger="stream_client"), pytest.raises(
            RuntimeError, match="code=1"
        ):
            await client.send_card("ou_1", {"elements": []})
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
    """WebSocket 接收循环与事件分发（事件经 WS 帧注入，行为面为回调与 ACK）。"""

    @staticmethod
    def _ws_msg(msg_type: Any, data: Any = None) -> SimpleNamespace:
        return SimpleNamespace(type=msg_type, data=data)

    @staticmethod
    def _text_msg(data: str) -> SimpleNamespace:
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=data)

    @staticmethod
    def _close_msg() -> SimpleNamespace:
        return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)

    @classmethod
    def _event_ws(cls, events: list[dict[str, Any]]) -> _FakeWS:
        """事件序列 → 可迭代的假 WS（每事件一帧 TEXT，末尾 CLOSE 令循环退出）。"""
        msgs = [cls._text_msg(json.dumps(e)) for e in events]
        msgs.append(cls._close_msg())
        return _FakeWS(msgs)

    @pytest.mark.asyncio
    async def test_handle_text_event_dispatches_to_callback(self) -> None:
        """schema 2.0 文本事件：先回 ACK，再把 payload 送达回调。"""
        client = _make_client()
        ws = self._event_ws(
            [
                {
                    "schema": "2.0",
                    "header": {"event_id": "evt-1"},
                    "headers": {"event_type": "im.message.receive_v1"},
                    "data": {"message": {"content": "hi"}},
                }
            ]
        )
        client._ws = ws
        received: list[dict[str, Any]] = []

        async def _cb(payload: dict[str, Any]) -> None:
            received.append(payload)

        client.on_message = _cb
        await client._receive_loop()
        assert received == [{"message": {"content": "hi"}}]
        # schema 2.0 需回 ACK
        ws.send_json.assert_awaited_once_with(
            {"schema": "2.0", "header": {"event_id": "evt-1"}}
        )

    @pytest.mark.asyncio
    async def test_handle_event_no_callback_still_acks(self) -> None:
        # schema 2.0 事件无论有无回调都需回 ACK（协议要求）
        client = _make_client()
        ws = self._event_ws(
            [{"schema": "2.0", "header": {"event_id": "evt-2"}, "headers": {"event_type": "x"}}]
        )
        client._ws = ws
        client.on_message = None
        await client._receive_loop()
        ws.send_json.assert_awaited_once_with(
            {"schema": "2.0", "header": {"event_id": "evt-2"}}
        )

    @pytest.mark.asyncio
    async def test_handle_event_unknown_type_skipped(self) -> None:
        client = _make_client()
        ws = self._event_ws([{"headers": {"event_type": "im.chat.updated"}, "data": {"x": 1}}])
        client._ws = ws
        called: list[dict[str, Any]] = []

        async def _cb(payload: dict[str, Any]) -> None:
            called.append(payload)

        client.on_message = _cb
        await client._receive_loop()
        assert called == []

    @pytest.mark.asyncio
    async def test_handle_event_ws_closed_skips_ack(self) -> None:
        # 连接已关闭：事件照常经循环分发但无法回 ACK（协议降级路径）
        client = _make_client()

        async def _iter_events() -> Any:
            yield self._text_msg(
                json.dumps({"schema": "2.0", "header": {}, "headers": {"event_type": "x"}})
            )
            yield self._close_msg()

        ws = MagicMock()
        ws.closed = True
        ws.__aiter__ = lambda *_a: _iter_events()
        ws.send_json = AsyncMock()
        client._ws = ws
        client.on_message = None
        await client._receive_loop()
        ws.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_event_message_type_no_callback(self) -> None:
        # 消息事件但未注册回调 → 静默跳过
        client = _make_client()
        ws = self._event_ws([{"headers": {"event_type": "im.message.receive_v1"}, "data": {"m": 1}}])
        client._ws = ws
        client.on_message = None
        await client._receive_loop()  # 不抛

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
        assert "Malformed JSON" in caplog.text

    @pytest.mark.asyncio
    async def test_receive_loop_callback_failure_visible_and_loop_survives(
        self, caplog
    ) -> None:
        """回调异常分级契约：error 级记录+失败计数，循环不终结、后续消息照常送达。"""
        import logging

        client = _make_client()
        processed: list[dict[str, Any]] = []
        calls = {"n": 0}

        async def _cb(payload: dict[str, Any]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            processed.append(payload)

        client.on_message = _cb
        client._ws = _FakeWS(
            [
                self._text_msg(json.dumps({"headers": {"event_type": ""}, "data": {"m": 1}})),
                self._text_msg(json.dumps({"headers": {"event_type": ""}, "data": {"m": 2}})),
                self._close_msg(),
            ]
        )
        with caplog.at_level("WARNING", logger="stream_client"):
            await client._receive_loop()

        # 核心行为不变量：首条回调失败后循环仍在跑，第二条消息照常送达
        assert processed == [{"m": 2}]
        # 失败可见性：error 级以上记录 + 公开失败计数递增
        assert client.failed_event_count == 1
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

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
        """端点持续为空 → 按重试预算逐次请求端点后放弃（每次尝试一次端点请求）。"""
        client = _make_client(max_retries=2, base_delay=0.01)
        session = _fake_session()
        session.post.return_value = _fake_response({"data": {}})  # 端点为空
        monkeypatch.setattr(_stream_client_mod.aiohttp, "ClientSession", lambda: session)
        client._ensure_token = AsyncMock()

        sleeps: list[float] = []
        original_sleep = asyncio.sleep

        def _fake_sleep(delay: float) -> Any:
            sleeps.append(delay)
            return original_sleep(0)

        monkeypatch.setattr(_stream_client_mod.asyncio, "sleep", _fake_sleep)

        await client.connect()
        # 2 次尝试各发起 1 次端点请求；仅第 1 次失败后退避
        assert session.post.call_count == 2
        assert sleeps == [0.01]
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_start_receive_loop_spawns_background_task(self) -> None:
        """start_receive_loop 后台托管 connect：异步启动且阻塞期间可被 disconnect 及时回收。"""
        started = asyncio.Event()

        async def _blocking_connect() -> None:
            started.set()
            await asyncio.sleep(3600)

        client = _make_client()
        client.connect = _blocking_connect
        await client.start_receive_loop()
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(client.disconnect(), timeout=5)
        assert client.is_connected is False

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

        await client.disconnect()
        ws.close.assert_awaited_once()
        session.close.assert_awaited_once()
        # 资源释放的公共可观察面：连接标记断开，再发送报"会话未初始化"
        assert client.is_connected is False
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message("ou_1", "hi")

    @pytest.mark.asyncio
    async def test_disconnect_cancels_receive_task(self) -> None:
        client = _make_client()
        task = asyncio.create_task(asyncio.sleep(10))
        client._receive_task = task
        client._ws = None
        client._session = None

        await client.disconnect()
        assert task.cancelled() is True

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self) -> None:
        client = _make_client()
        client._ws = None
        client._session = None
        await client.disconnect()  # 不抛
        await client.disconnect()  # 再次断开不抛
