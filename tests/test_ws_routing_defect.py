"""WS 推送路由验证测试（单连接架构：_global_connections + _thread_user_map）。

架构变更后路由模型：
  - 连接唯一真相之源：_global_connections（user_id → ws，每用户一条）
  - thread→user 逻辑映射：_thread_user_map（在用户发消息时建立）
  - notify_request：按 record.message_data.user_id 精确路由（与 task_notifier 一致）
  - send_to_thread（流式事件）：按 _thread_user_map[thread_id] 反查 user_id → send_to_user

本测试验证：
  1. 仅 register_global（未建 thread→user 映射）时，notify_request 仍能按 user_id 送达
  2. send_to_thread 在有 thread→user 映射时精确路由
  3. 无任何连接时返回 False（消息不丢入黑洞）
  4. 多会话不串台
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from src.channels.websocket.ws_handler import WebSocketInteractionNotifier


class MockWebSocket:
    """记录 send_text 的轻量 WebSocket mock（从已删的 src/websocket/handler.py 迁移）。"""

    def __init__(self, user_id: str = "test_user") -> None:
        self.user_id = user_id
        self.sent_messages: list[str] = []
        self._closed = False

    async def send_text(self, message: str) -> None:
        if self._closed:
            raise RuntimeError("WebSocket is closed")
        self.sent_messages.append(message)

    async def close(self) -> None:
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed


def _make_request(
    thread_id: str, request_id: str = "req-1", user_id: str = ""
) -> dict[str, Any]:
    """构造 interaction_request record（与 service._make_request_record 同构）。"""
    return {
        "id": request_id,
        "session_id": thread_id,
        "type": "interaction_request",
        "message_data": {
            "interaction_mode": "choice",
            "title": "测试交互",
            "description": "desc",
            "thread_id": thread_id,
            "tab_id": "",
            "agent_id": "agent-1",
            "pipeline_id": thread_id,
            "user_id": user_id,
            "options": [{"id": "approve", "label": "同意"}],
        },
    }


class TestWSRoutingByUserId:
    """单连接架构：notify_request 按 user_id 精确路由。"""

    @pytest.mark.asyncio
    async def test_有user_id时精确路由送达(self) -> None:
        """连接 register_global，notify_request 带正确 user_id 应精确送达。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)

        request = _make_request("thread-A", user_id="user-1")
        sent = await notifier.notify_request(request)

        assert sent is True
        assert len(ws.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_user_id为空但有线程映射时回退送达(self) -> None:
        """notify_request 无 user_id，但有 thread→user 映射时应回退送达。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)
        notifier.register_thread_user("thread-A", "user-1")

        request = _make_request("thread-A", user_id="")
        sent = await notifier.notify_request(request)

        assert sent is True
        assert len(ws.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_无任何连接时返回False(self) -> None:
        """既无连接时，notify_request 应返回 False（消息丢失）。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        request = _make_request("thread-A", user_id="user-1")
        sent = await notifier.notify_request(request)
        assert sent is False


class TestSendToThreadViaMap:
    """send_to_thread（流式事件）按 _thread_user_map 反查路由。"""

    @pytest.mark.asyncio
    async def test_有线程映射时精确路由(self) -> None:
        """register_thread_user 后，send_to_thread 应按映射送达对应 user。"""
        notifier = WebSocketInteractionNotifier()
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)
        notifier.register_thread_user("thread-A", "user-1")

        ok = await notifier.send_to_thread(
            "thread-A", {"type": "stream_chunk", "data": {"content": "x"}}
        )

        assert ok is True
        assert len(ws.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_无线程映射但有全局连接时广播兜底(self) -> None:
        """无 thread→user 映射但存在全局连接时，应广播兜底（兼容历史）。"""
        notifier = WebSocketInteractionNotifier()
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)
        # 不调 register_thread_user

        ok = await notifier.send_to_thread(
            "thread-A", {"type": "stream_chunk", "data": {"content": "x"}}
        )

        assert ok is True

    @pytest.mark.asyncio
    async def test_无连接时返回False(self) -> None:
        """无任何连接时 send_to_thread 应返回 False。"""
        notifier = WebSocketInteractionNotifier()
        ok = await notifier.send_to_thread(
            "thread-A", {"type": "stream_chunk", "data": {"content": "x"}}
        )
        assert ok is False


class TestWSRoutingMultiSession:
    """多会话场景：不同 user 不串台（单连接架构核心保证）。"""

    @pytest.mark.asyncio
    async def test_不同用户不串台(self) -> None:
        """user-1 和 user-2 各有连接，user-1 的推送不应到达 user-2。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        notifier.register_global("user-1", ws1)
        notifier.register_global("user-2", ws2)

        request = _make_request("thread-A", user_id="user-1")
        sent = await notifier.notify_request(request)

        assert sent is True
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 0, "user-2 不应收到 user-1 的消息"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
