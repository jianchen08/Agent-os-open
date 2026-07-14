"""WebSocket 推送失败处理回归测试。

BUG-FIX-20260713_ws_fake_offline:
send_to_user 此前把超时和真异常捆在一个 except 里（line 273-275），任何一次
send 超时就 pop 整个 user 连接，导致 TCP 仍存活的连接在内存表里被清掉 →
后续所有推送都判 "用户不在线"，直到客户端重建连接。

正确语义：
- 超时：连接可能仍存活（背压/调度抖动），只返回 False 让 sink 失败计数累积，
  不踢连接；权威清理由接收循环 WebSocketDisconnect → unregister_global。
- 真异常（连接已坏）：才 pop，且要校验身份，避免删掉重连后的新连接。

同步覆盖 send_to_thread / notify_request 两处广播兜底分支的相同缺陷。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestSendToUserTimeoutKeepsConnection:
    """超时不应踢连接 —— 本次 '用户不在线' 假离线的核心修复点。"""

    @pytest.mark.asyncio
    async def test_timeout_does_not_pop_connection(self) -> None:
        """send 超时后，连接必须仍留在 _global_connections，否则后续推送误判不在线。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=asyncio.TimeoutError)
        notifier._global_connections["u1"] = ws

        ok = await notifier.send_to_user("u1", {"type": "test"})

        assert ok is False
        # 修复后：超时保留连接（不 pop）
        assert "u1" in notifier._global_connections
        assert notifier._global_connections["u1"] is ws

    @pytest.mark.asyncio
    async def test_timeout_repeatable_until_real_disconnect(self) -> None:
        """连续超时不应累积清空连接 —— sink 自己的熔断器会处理，send 路径不该越权。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=asyncio.TimeoutError)
        notifier._global_connections["u1"] = ws

        for _ in range(3):
            await notifier.send_to_user("u1", {"type": "test"})

        # 三次超时后连接仍在
        assert notifier._global_connections.get("u1") is ws


class TestSendToUserRealExceptionPopsWithIdentityCheck:
    """真异常才 pop，且要校验身份，避免删掉重连后的新连接。"""

    @pytest.mark.asyncio
    async def test_connection_error_pops_stale_connection(self) -> None:
        """连接已坏（真异常）应 pop，避免后续无谓重试。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=ConnectionError("closed"))
        notifier._global_connections["u1"] = ws

        ok = await notifier.send_to_user("u1", {"type": "test"})

        assert ok is False
        assert "u1" not in notifier._global_connections

    @pytest.mark.asyncio
    async def test_real_exception_does_not_pop_replaced_connection(self) -> None:
        """身份校验：send 期间用户重连（表里 ws 被换），旧 ws 的异常不应删新连接。

        与 unregister_global:254-257 的 'ws is current' 保护语义一致。
        当前代码无条件 pop(user_id) 会误删新连接，修复后须校验身份。
        """
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        old_ws = MagicMock()
        new_ws = MagicMock()
        notifier._global_connections["u1"] = old_ws

        async def _send_text_then_replace(*_args, **_kwargs):
            # 模拟 old_ws send 期间用户重连，表被换成 new_ws
            notifier._global_connections["u1"] = new_ws
            raise ConnectionError("old closed")

        old_ws.send_text = AsyncMock(side_effect=_send_text_then_replace)

        ok = await notifier.send_to_user("u1", {"type": "test"})

        assert ok is False
        # 旧 ws 的异常不应删掉新连接（身份校验）
        assert notifier._global_connections.get("u1") is new_ws


class TestSendToThreadBroadcastFallbackTimeoutKeepsConnection:
    """send_to_thread 广播兜底（无 thread→user 映射）应与主路径语义一致。

    修复前 line 308: 广播兜底里 send 超时也 pop 连接，同样的假离线缺陷。
    """

    @pytest.mark.asyncio
    async def test_broadcast_timeout_keeps_connection(self) -> None:
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=asyncio.TimeoutError)
        notifier._global_connections["u1"] = ws
        # 无 thread→user 映射，走广播兜底
        assert "t-no-map" not in notifier._thread_user_map

        ok = await notifier.send_to_thread("t-no-map", {"type": "test"})

        assert ok is False
        # 修复后：广播兜底超时同样保留连接
        assert notifier._global_connections.get("u1") is ws

    @pytest.mark.asyncio
    async def test_broadcast_real_exception_pops_with_identity_check(self) -> None:
        """广播兜底里真异常应 pop，但只 pop 抛异常的那个 ws（身份校验）。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        bad_ws = MagicMock()
        bad_ws.send_text = AsyncMock(side_effect=ConnectionError("closed"))
        good_ws = MagicMock()
        good_ws.send_text = AsyncMock(return_value=None)
        notifier._global_connections["u1"] = bad_ws
        notifier._global_connections["u2"] = good_ws

        await notifier.send_to_thread("t-no-map", {"type": "test"})

        # 坏连接被清，好连接保留
        assert "u1" not in notifier._global_connections
        assert notifier._global_connections.get("u2") is good_ws


class TestNotifyRequestBroadcastFallbackTimeoutKeepsConnection:
    """notify_request 广播兜底（line 113-120）同样的假离线缺陷。

    notify_request 只接收单个 request dict（ws_handler.py:76），无 user_id 且
    无 thread→user 映射时走广播兜底。
    """

    @pytest.mark.asyncio
    async def test_notify_broadcast_timeout_keeps_connection(self) -> None:
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=asyncio.TimeoutError)
        notifier._global_connections["u1"] = ws

        # 构造无 user_id、无 thread_id 的 request，走广播兜底
        request = {
            "id": "r1",
            "message_data": {"thread_id": "", "interaction_mode": "choice"},
        }

        await notifier.notify_request(request)

        # 修复后：广播兜底超时同样保留连接
        assert notifier._global_connections.get("u1") is ws
