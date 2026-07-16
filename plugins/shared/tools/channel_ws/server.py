#!/usr/bin/env python3
"""WebSocket 通道适配器 MCP 服务端。

作为边车服务提供 WebSocket 前端通信能力。
核心业务逻辑参考 0.1 src/channels/websocket/。

[来源: docs/tasks/task_10_system_plugins.md AC-09-6]
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from lingxi_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("channel_websocket")

# 消息队列（模拟 WebSocket 连接的消息缓冲）
_outbound: list[dict[str, Any]] = []
_inbound: list[dict[str, Any]] = []
# 连接的客户端（模拟）
_connected_clients: set[str] = set()


@plugin.tool(
    name="channel.send_message",
    schema={
        "type": "object",
        "properties": {
            "client_id": {"type": "string"},
            "message": {"type": "object"},
            "type": {"type": "string", "default": "text"},
        },
        "required": ["client_id", "message"],
    },
    description="Send a message to a specific WebSocket client",
)
async def send_message(
    client_id: str,
    message: dict[str, Any],
    type: str = "text",
) -> dict[str, Any]:
    """Send a message to a specific connected client."""
    if client_id not in _connected_clients:
        return {"error": "client not connected", "client_id": client_id}

    entry = {
        "id": f"msg_{uuid.uuid4().hex[:8]}",
        "client_id": client_id,
        "message": message,
        "type": type,
        "direction": "outbound",
        "timestamp": time.time(),
    }
    _outbound.append(entry)
    return {"msg_id": entry["id"], "sent": True}


@plugin.tool(
    name="channel.receive",
    schema={
        "type": "object",
        "properties": {
            "client_id": {"type": "string"},
            "timeout": {"type": "number", "default": 30},
        },
    },
    description="Receive pending messages from WebSocket clients (non-blocking snapshot)",
)
async def receive(
    client_id: str | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """Receive pending inbound messages.

    Non-blocking snapshot: returns all currently buffered messages.
    The timeout parameter is reserved for future blocking-mode implementation.

    Args:
        client_id: Filter by client (None = all clients).
        timeout: Max wait time in seconds (reserved for future blocking mode).
    """
    # DEBT: 阻塞等待模式未实现。ceiling: 当前仅非阻塞快照。
    # upgrade: 集成真实 WebSocket 服务后实现 asyncio.wait_for 阻塞等待。
    await asyncio.sleep(0)  # Yield to event loop (placeholder for future blocking wait)

    messages = []
    remaining = []
    for msg in _inbound:
        if client_id is None or msg.get("client_id") == client_id:
            messages.append(msg)
        else:
            remaining.append(msg)
    _inbound.clear()
    _inbound.extend(remaining)

    return {"messages": messages, "count": len(messages)}


@plugin.tool(
    name="channel.broadcast",
    schema={
        "type": "object",
        "properties": {
            "message": {"type": "object"},
            "type": {"type": "string", "default": "event"},
        },
        "required": ["message"],
    },
    description="Broadcast a message to all connected WebSocket clients",
)
async def broadcast(
    message: dict[str, Any],
    type: str = "event",
) -> dict[str, Any]:
    """Broadcast a message to all connected clients."""
    sent_count = 0
    for client_id in _connected_clients:
        entry = {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "client_id": client_id,
            "message": message,
            "type": type,
            "direction": "broadcast",
            "timestamp": time.time(),
        }
        _outbound.append(entry)
        sent_count += 1

    return {"sent_count": sent_count, "total_clients": len(_connected_clients)}


@plugin.on_load
async def on_load(params: dict[str, Any]) -> None:
    """Initialize WebSocket adapter on load."""
    # DEBT: WebSocket 服务器启动未实现。ceiling: 当前无真实 WS 连接。
    # upgrade: 集成 uvicorn + websockets 后启动 WS 服务。
    pass


@plugin.on_unload
async def on_unload(params: dict[str, Any]) -> None:
    """Cleanup WebSocket connections."""
    _connected_clients.clear()
    _outbound.clear()
    _inbound.clear()


if __name__ == "__main__":
    plugin.run()
