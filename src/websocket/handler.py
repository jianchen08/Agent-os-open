"""WebSocket 连接管理器。

管理 WebSocket 连接的建立、心跳、消息推送和断开清理。
支持全局连接和按 thread_id 分组的会话连接。

FEATURE-20260521-ws-queue:
设计决策:
  - 引入 asyncio.Queue 作为发送缓冲区，避免同步等待 ws.send_text() 阻塞事件循环
  - 独立后台任务 _sender_loop 批量消费队列，发送失败时自动清理失效连接
  - send_to_user / send_to_thread 改为非阻塞入队，立即返回，提升高并发下的响应性
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


class _SendItem:
    """发送队列项，封装目标类型和消息数据。"""

    __slots__ = ("target_type", "target_id", "payload")

    def __init__(self, target_type: str, target_id: str, payload: str) -> None:
        self.target_type = target_type  # "user" | "thread"
        self.target_id = target_id
        self.payload = payload


class WebSocketManager:
    """WebSocket 连接管理器。

    管理全局连接和按会话分组的连接，支持心跳检测和消息推送。
    通过后台发送队列解耦消息入队和实际发送，避免高并发时阻塞事件循环。
    """

    HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
    HEARTBEAT_TIMEOUT = 60   # 心跳超时（秒）
    SEND_QUEUE_MAXSIZE = 5000  # 发送队列最大长度，防止内存无限增长

    def __init__(self) -> None:
        self._global_connections: dict[str, MockWebSocket] = {}
        self._active_connections: dict[str, list[MockWebSocket]] = {}
        self._heartbeat_timestamps: dict[str, float] = {}

        # FEATURE-20260521-ws-queue: 发送队列和后台任务
        self._send_queue: asyncio.Queue[_SendItem] = asyncio.Queue(
            maxsize=self.SEND_QUEUE_MAXSIZE,
        )
        self._sender_task: asyncio.Task | None = None

    def _ensure_sender(self) -> None:
        """确保后台发送任务已启动。"""
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(
                self._sender_loop(), name="ws_sender_loop",
            )

    async def _sender_loop(self) -> None:
        """后台发送循环，批量消费队列并实际发送消息。

        独立运行，不阻塞调用方的事件循环调度。
        """
        while True:
            try:
                item = await self._send_queue.get()
            except asyncio.CancelledError:
                break

            if item.target_type == "user":
                await self._do_send_to_user(item.target_id, item.payload)
            elif item.target_type == "thread":
                await self._do_send_to_thread(item.target_id, item.payload)

    async def _do_send_to_user(self, user_id: str, payload: str) -> bool:
        """实际执行向用户发送（在后台循环中调用）。"""
        ws = self._global_connections.get(user_id)
        if ws is None:
            return False
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
            return True
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error("[WS] 推送失败: user=%s err=%s", user_id[:12], exc)
            self.unregister_global(user_id)
            return False

    async def _do_send_to_thread(self, thread_id: str, payload: str) -> bool:
        """实际执行向会话发送（在后台循环中调用）。"""
        conns = self._active_connections.get(thread_id, [])
        if conns:
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
                await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
                return True
            except (asyncio.TimeoutError, Exception):
                self._global_connections.pop(user_id, None)

        return False

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
        """向指定用户推送事件（通过全局连接）。

        FEATURE-20260521-ws-queue: 改为非阻塞入队，由后台任务实际发送。
        """
        self._ensure_sender()
        ws = self._global_connections.get(user_id)
        if ws is None:
            return False
        payload = json.dumps(event, ensure_ascii=False, default=str)
        try:
            self._send_queue.put_nowait(_SendItem("user", user_id, payload))
            return True
        except asyncio.QueueFull:
            logger.warning("[WS] 发送队列已满，丢弃消息: user=%s", user_id[:12])
            return False

    async def send_to_thread(self, thread_id: str, event_data: dict[str, Any]) -> bool:
        """向指定会话推送事件。

        FEATURE-20260521-ws-queue: 改为非阻塞入队，由后台任务实际发送。
        """
        self._ensure_sender()
        payload = json.dumps(event_data, ensure_ascii=False)
        try:
            self._send_queue.put_nowait(_SendItem("thread", thread_id, payload))
            return True
        except asyncio.QueueFull:
            logger.warning("[WS] 发送队列已满，丢弃消息: thread=%s", thread_id[:12])
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
