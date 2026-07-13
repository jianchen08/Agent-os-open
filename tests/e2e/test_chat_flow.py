"""对话流程 E2E 测试。

验证 WebSocket 对话协议：WS 连接 → 认证 → 心跳交换 → 事件序列。
对应 features.md 场景 1。

由于完整对话流程依赖管道引擎和真实 LLM，本测试聚焦 WebSocket 协议层面：
- 连接建立与认证
- connection_confirmation 事件
- heartbeat / heartbeat_ack 机制
- 消息格式校验

测试用例：
- test_ws_connect_without_token_rejected：无 Token 连接被拒绝
- test_ws_connect_with_invalid_token_rejected：无效 Token 连接被拒绝
- test_ws_connection_confirmation：连接后收到 connection_confirmation
- test_ws_heartbeat_exchange：心跳交互
- test_ws_message_without_thread_id：消息缺少 thread_id 返回 error
- test_ws_multiple_heartbeats：多次心跳保持连接稳定
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.e2e.utils.ws_client import WSTestClient


# ---------------------------------------------------------------------------
# WebSocket 协议测试
# ---------------------------------------------------------------------------

def test_ws_connect_without_token_rejected(ws_test_client: Any) -> None:
    """无 Token 连接 /ws/chat 应被拒绝（关闭码 4001）。

    验证点：
    - 不传 token 参数连接 /ws/chat，连接立即被关闭
    """
    with pytest.raises(WebSocketDisconnect) as exc_info, ws_test_client.websocket_connect("/ws/chat"):
        pass

    assert exc_info.value.code == 4001, (
        f"无 Token 应以 4001 关闭，得到 {exc_info.value.code}"
    )


def test_ws_connect_with_invalid_token_rejected(ws_test_client: Any) -> None:
    """无效 Token 连接 /ws/chat 应被拒绝（关闭码 4001）。

    验证点：
    - 传无效 token 连接 /ws/chat，连接立即被关闭
    """
    with pytest.raises(WebSocketDisconnect) as exc_info, ws_test_client.websocket_connect("/ws/chat?token=invalid.jwt.token"):
        pass

    assert exc_info.value.code == 4001, (
        f"无效 Token 应以 4001 关闭，得到 {exc_info.value.code}"
    )


def test_ws_connection_confirmation(ws_test_client: Any, auth_token: str) -> None:
    """有效 Token 连接后收到 connection_confirmation 事件。

    验证点：
    - 连接成功建立
    - 收到 type=connection_confirmation 事件
    - 事件 data 包含 status=connected
    """
    with WSTestClient(ws_test_client, f"/ws/chat?token={auth_token}") as ws:
        event = ws.wait_for_event_type("connection_confirmation", max_events=5, timeout_seconds=5)

        assert event["type"] == "connection_confirmation"
        data = event.get("data", {})
        assert data.get("status") == "connected", (
            f"status 应为 connected，得到 {data.get('status')}"
        )
        assert "user_id" in data, "data 应包含 user_id"


def test_ws_heartbeat_exchange(ws_test_client: Any, auth_token: str) -> None:
    """心跳交换：发送 heartbeat，收到 heartbeat_ack。

    验证点：
    - 发送 type=heartbeat 消息
    - 收到 type=heartbeat_ack 响应
    - heartbeat_ack 包含 server_time
    """
    with WSTestClient(ws_test_client, f"/ws/chat?token={auth_token}") as ws:
        ws.wait_for_event_type("connection_confirmation", max_events=5, timeout_seconds=5)

        ws.clear_events()
        ws.send_json({"type": "heartbeat"})

        ack = ws.wait_for_event_type("heartbeat_ack", max_events=5, timeout_seconds=5)
        assert ack["type"] == "heartbeat_ack"

        data = ack.get("data", {})
        assert "server_time" in data, "heartbeat_ack 应包含 server_time"


def test_ws_message_without_thread_id(ws_test_client: Any, auth_token: str) -> None:
    """消息缺少 thread_id 时收到 error 事件。

    验证点：
    - 发送 user_input 消息但不含 thread_id
    - 收到 type=error 事件
    - error 事件的 data 包含提示信息
    """
    with WSTestClient(ws_test_client, f"/ws/chat?token={auth_token}") as ws:
        ws.wait_for_event_type("connection_confirmation", max_events=5, timeout_seconds=5)

        ws.clear_events()
        ws.send_json({
            "type": "user_input",
            "data": {"content": "hello"},
        })

        error_event = ws.wait_for_event_type("error", max_events=10, timeout_seconds=5)
        assert error_event["type"] == "error"

        data = error_event.get("data", {})
        assert "message" in data, "error 事件应包含 message 字段"
        assert "thread_id" in data["message"], (
            f"错误消息应提及 thread_id，得到: {data['message']}"
        )


def test_ws_multiple_heartbeats(ws_test_client: Any, auth_token: str) -> None:
    """多次心跳交换保持连接稳定。

    验证点：
    - 连续发送 3 次心跳
    - 每次都收到 heartbeat_ack
    - 连接保持稳定
    """
    with WSTestClient(ws_test_client, f"/ws/chat?token={auth_token}") as ws:
        ws.wait_for_event_type("connection_confirmation", max_events=5, timeout_seconds=5)
        ws.clear_events()

        for i in range(3):
            ws.send_json({"type": "heartbeat"})
            ack = ws.wait_for_event_type("heartbeat_ack", max_events=5, timeout_seconds=5)
            assert ack["type"] == "heartbeat_ack", (
                f"第 {i+1} 次心跳未收到 ack"
            )

        acks = ws.get_events_by_type("heartbeat_ack")
        assert len(acks) >= 3, f"应收到 >= 3 个 ack，得到 {len(acks)}"
