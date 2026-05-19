"""WebSocket 连接管理器。

管理 WebSocket 连接的建立、心跳、消息推送和断开清理。
支持全局连接和按 thread_id 分组的会话连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class MockWebSocket:
    """用于测试的 WebSocket 模拟对象。"""

    def __init__(self, user_id: str = "test_user") -> None:
        self.user_id = user_id
        self.sent_messages: list[str] = []
        self._closed = False

    async def send_text(self, message: str) -> None:
        """模拟发送文本消息。"""
        if self._closed:
            raise RuntimeError("WebSocket is closed")
        self.sent_messages.append(message)

    async def close(self) -> None:
        """模拟关闭连接。"""
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed


class WebSocketManager:
    """WebSocket 连接管理器。

    管理全局连接和按会话分组的连接，支持心跳检测和消息推送。
    """

    HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
    HEARTBEAT_TIMEOUT = 60   # 心跳超时（秒）

    def __init__(self) -> None:
        self._global_connections: dict[str, MockWebSocket] = {}
        self._active_connections: dict[str, list[MockWebSocket]] = {}
        self._heartbeat_timestamps: dict[str, float] = {}

    def register_global(self, user_id: str, ws: MockWebSocket) -> None:
        """注册全局 WebSocket 连接。"""
        self._global_connections[user_id] = ws
        self._heartbeat_timestamps[user_id] = time.time()
        logger.info("[WS] 全局连接注册: user=%s", user_id[:12])

    def unregister_global(self, user_id: str) -> None:
        """注销全局 WebSocket 连接。"""
        self._global_connections.pop(user_id, None)
        self._heartbeat_timestamps.pop(user_id, None)

    def register_session(self, thread_id: str, ws: MockWebSocket) -> None:
        """注册会话 WebSocket 连接。"""
        if thread_id not in self._active_connections:
            self._active_connections[thread_id] = []
        self._active_connections[thread_id].append(ws)
        logger.info("[WS] 会话连接注册: thread=%s", thread_id[:12])

    def unregister_session(self, thread_id: str, ws: MockWebSocket) -> None:
        """注销会话 WebSocket 连接。"""
        conns = self._active_connections.get(thread_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self._active_connections.pop(thread_id, None)

    async def send_to_user(self, user_id: str, event: dict[str, Any]) -> bool:
        """向指定用户推送事件（通过全局连接）。"""
        ws = self._global_connections.get(user_id)
        if ws is None:
            return False
        try:
            await asyncio.wait_for(
                ws.send_text(json.dumps(event, ensure_ascii=False, default=str)),
                timeout=5.0,
            )
            return True
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error("[WS] 推送失败: user=%s err=%s", user_id[:12], exc)
            self.unregister_global(user_id)
            return False

    async def send_to_thread(self, thread_id: str, event_data: dict[str, Any]) -> bool:
        """向指定会话推送事件。"""
        conns = self._active_connections.get(thread_id, [])
        if conns:
            payload = json.dumps(event_data, ensure_ascii=False)
            stale: list[MockWebSocket] = []
            for ws in conns:
                try:
                    await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
                    return True
                except (asyncio.TimeoutError, Exception):
                    stale.append(ws)
            if stale:
                self._active_connections[thread_id] = [
                    c for c in conns if c not in stale
                ]

        for user_id, ws in list(self._global_connections.items()):
            try:
                await asyncio.wait_for(
                    ws.send_text(json.dumps(event_data, ensure_ascii=False)),
                    timeout=5.0,
                )
                return True
            except (asyncio.TimeoutError, Exception):
                self._global_connections.pop(user_id, None)

        return False

    def update_heartbeat(self, user_id: str) -> None:
        """更新心跳时间戳。"""
        self._heartbeat_timestamps[user_id] = time.time()

    def check_heartbeats(self) -> list[str]:
        """检查超时的连接，返回超时的 user_id 列表。"""
        now = time.time()
        timed_out = []
        for user_id, ts in list(self._heartbeat_timestamps.items()):
            if now - ts > self.HEARTBEAT_TIMEOUT:
                timed_out.append(user_id)
        for user_id in timed_out:
            self.unregister_global(user_id)
        return timed_out

    @property
    def global_connection_count(self) -> int:
        """当前全局连接数。"""
        return len(self._global_connections)

    @property
    def session_connection_count(self) -> int:
        """当前会话连接数。"""
        return sum(len(v) for v in self._active_connections.values())

    def get_global_websocket(self, user_id: str) -> MockWebSocket | None:
        """获取指定用户的全局 WebSocket 连接。"""
        return self._global_connections.get(user_id)
