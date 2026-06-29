"""WS 推送路由缺陷验证测试。

复现用户报告："后端已经通知了/已经发消息了，前端 4-5 秒才收到，甚至丢失"。

已通过代码审查确认的链路事实：
  - 前端连 /ws/chat → 后端 register_global(user_id)（仅写 _global_connections）
  - per-session 账本 _active_connections[thread_id] 只在用户发 user_input 时才写入
    （app_factory.py:351，位于 if msg_type == "user_input" 分支内）
  - notify_request（交互通知）路由顺序：
      1. _active_connections.get(thread_id) → 空（刷新/切会话后未发消息）
      2. 遍历 _active_connections.values() → 空
      3. fallback _global_connections → 才送达
  - _send_event_to_thread（流式事件 stream_chunk 等）路由：同样依赖 _active_connections

本测试验证：连接只 register_global、未 register(thread_id) 时，两类推送能否送达、
是否走 fallback、fallback 路径是否稳定。若 fallback 不稳定或丢消息，即为根因。

判据：
  - 若"仅 global"场景下 notify_request / send_to_thread 能稳定送达 → 路由缺陷不成立，
    需另查（事件循环争用 / send_text 阻塞）
  - 若送达失败或行为异常 → 确认 WS 路由缺陷是延迟/丢失的根因
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# 复用现成的轻量 WebSocket mock（记录 send_text 到 sent_messages）
from src.websocket.handler import MockWebSocket
from src.channels.websocket.ws_handler import WebSocketInteractionNotifier


def _make_request(thread_id: str, request_id: str = "req-1") -> dict[str, Any]:
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
            "options": [{"id": "approve", "label": "同意"}],
        },
    }


class TestWSRoutingOnlyGlobal:
    """场景：连接仅 register_global（模拟刷新/切会话后未发 user_input）。"""

    @pytest.mark.asyncio
    async def test_notify_request_仅global连接时能否送达(self) -> None:
        """连接只 register_global，notify_request 是否走 fallback 送达。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)
        # 关键：【不】调 register(thread_id)，模拟刷新后未发消息

        request = _make_request("thread-A")
        sent = await notifier.notify_request(request)

        print(f"[仅global] notify_request 返回 sent={sent}")
        print(f"[仅global] ws.sent_messages 数量={len(ws.sent_messages)}")
        if ws.sent_messages:
            print(f"[仅global] 收到的消息={ws.sent_messages[0][:120]}...")

        # 关键断言：fallback 到 _global_connections 应能送达
        assert sent is True, "仅 global 连接时 notify_request 应通过 fallback 送达"
        assert len(ws.sent_messages) == 1, "应恰好送达一条"

    @pytest.mark.asyncio
    async def test_send_to_thread_仅global连接时能否送达(self) -> None:
        """连接只 register_global，send_to_thread（流式事件路径）是否走 fallback。"""
        notifier = WebSocketInteractionNotifier()
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)

        ok = await notifier.send_to_thread("thread-A", {"type": "stream_chunk", "data": {"content": "x"}})

        print(f"[仅global] send_to_thread 返回 ok={ok}")
        print(f"[仅global] ws.sent_messages 数量={len(ws.sent_messages)}")

        assert ok is True, "仅 global 连接时 send_to_thread 应通过 fallback 送达"

    @pytest.mark.asyncio
    async def test_无任何连接时消息是否丢失(self) -> None:
        """既无 active 也无 global 连接时，notify_request 应返回 False（消息丢失）。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        request = _make_request("thread-A")
        sent = await notifier.notify_request(request)

        print(f"[无连接] notify_request 返回 sent={sent}（无连接应 False）")
        assert sent is False


class TestWSRoutingActiveRegistered:
    """对照组：连接正常 register(thread_id)（用户已发过 user_input）。"""

    @pytest.mark.asyncio
    async def test_已注册thread时直达(self) -> None:
        """连接 register(thread_id) 后，notify_request 应直达 _active_connections。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)
        notifier.register("thread-A", ws)  # 模拟已发 user_input

        request = _make_request("thread-A")
        sent = await notifier.notify_request(request)

        print(f"[已注册] notify_request 返回 sent={sent}, messages={len(ws.sent_messages)}")
        assert sent is True
        assert len(ws.sent_messages) == 1


class TestWSRoutingMultiSession:
    """多会话场景：用户切会话时，旧 thread 的推送会不会串到新会话。"""

    @pytest.mark.asyncio
    async def test_切会话后旧thread推送的路由(self) -> None:
        """用户从 thread-A 切到 thread-B（只 register_global），旧 thread 的推送去哪。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)
        # 用户曾活跃于 thread-A，现切到 thread-B，但 thread-B 未发消息
        notifier.register("thread-A", ws)

        # 后端给 thread-B 推交互请求（用户当前在看 B，但 B 没注册 active）
        request_b = _make_request("thread-B", request_id="req-B")
        sent_b = await notifier.notify_request(request_b)

        print(f"[切会话] thread-B 推送 sent={sent_b}, messages={len(ws.sent_messages)}")
        # thread-B 未注册 active，应 fallback 到 global（同一个 ws）送达
        assert sent_b is True


class TestTraceInjection:
    """验证延迟追踪埋点：__send_ts 注入 + 前端可据此算延迟。"""

    @pytest.mark.asyncio
    async def test_send_to_thread注入__send_ts(self) -> None:
        """send_to_thread 推送的事件应携带 __send_ts（epoch ms）。"""
        notifier = WebSocketInteractionNotifier()
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)

        await notifier.send_to_thread("thread-A", {
            "type": "stream_chunk",
            "data": {"message_id": "msg-1", "content": "x"},
        })

        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert "__send_ts" in parsed, "事件应注入 __send_ts"
        assert isinstance(parsed["__send_ts"], (int, float))
        assert parsed["__send_ts"] > 0

    @pytest.mark.asyncio
    async def test_notify_request注入__send_ts(self) -> None:
        """notify_request 推送的事件应携带 __send_ts。"""
        notifier = WebSocketInteractionNotifier(auto_confirm_delay=9999)
        ws = MockWebSocket()
        notifier.register_global("user-1", ws)

        await notifier.notify_request(_make_request("thread-A", "req-1"))

        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert "__send_ts" in parsed, "交互请求应注入 __send_ts"
        assert parsed["data"]["request_id"] == "req-1"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
