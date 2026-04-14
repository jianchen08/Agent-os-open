"""WebSocket 会话管理器。

管理 WebSocket 连接的生命周期，包括：
- 连接注册与注销
- 会话 ID 与 WebSocket 的映射
- 消息发送（单播 / 广播）
- 会话恢复（断线重连时通过 session_id 恢复）
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class WebSocketConnection(Protocol):
    """WebSocket 连接协议。

    定义 WebSocket 连接对象需要实现的最小接口，
    兼容 aiohttp WebSocketResponse。
    """

    async def send_str(self, data: str) -> None:
        """发送文本消息。"""
        ...

    @property
    def closed(self) -> bool:
        """连接是否已关闭。"""
        ...


@dataclass
class SessionInfo:
    """会话信息数据类。

    Attributes:
        session_id: 会话唯一标识
        ws: WebSocket 连接对象
        thread_id: 线程 ID（前端传入，用于重连恢复）
        connected_at: 连接时间戳
        last_active_at: 最后活跃时间戳
        metadata: 附加元数据
    """

    session_id: str
    ws: Any  # WebSocket 连接对象，类型为 WebSocketConnection
    thread_id: str = ""
    connected_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """更新最后活跃时间戳。"""
        self.last_active_at = time.time()


class SessionManager:
    """WebSocket 会话管理器。

    负责管理所有活跃的 WebSocket 连接，提供：
    - 连接注册与注销
    - 按 session_id 查找连接
    - 按 thread_id 查找连接（支持重连恢复）
    - 消息发送（单播 / 广播）
    - 会话超时清理

    Example::

        manager = SessionManager()
        session_id = await manager.register(ws, thread_id="thread-123")
        await manager.send_to(session_id, '{"type": "stream_chunk", ...}')
        await manager.unregister(session_id)
    """

    def __init__(self, session_timeout: float = 3600.0) -> None:
        """初始化会话管理器。

        Args:
            session_timeout: 会话超时时间（秒），默认 1 小时。
                超时后 cleanup_stale 会清理该会话。
        """
        self._sessions: dict[str, SessionInfo] = {}
        self._thread_to_session: dict[str, str] = {}
        self._session_timeout = session_timeout

    async def register(
        self,
        ws: Any,
        thread_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """注册新的 WebSocket 连接。

        为新连接生成 session_id，如果提供了 thread_id 且存在旧会话，
        则先注销旧会话再注册新会话（重连恢复场景）。

        Args:
            ws: WebSocket 连接对象
            thread_id: 线程 ID（可选，用于重连恢复）
            metadata: 附加元数据

        Returns:
            新生成的 session_id
        """
        session_id = str(uuid.uuid4())

        # 重连恢复：如果 thread_id 已有会话，注销旧会话
        if thread_id and thread_id in self._thread_to_session:
            old_session_id = self._thread_to_session[thread_id]
            if old_session_id in self._sessions:
                logger.info(
                    "Reconnecting session: thread_id=%s, old=%s, new=%s",
                    thread_id, old_session_id, session_id,
                )
                await self._force_unregister(old_session_id)

        session_info = SessionInfo(
            session_id=session_id,
            ws=ws,
            thread_id=thread_id,
            metadata=metadata or {},
        )
        self._sessions[session_id] = session_info

        if thread_id:
            self._thread_to_session[thread_id] = session_id

        logger.info(
            "Session registered: session_id=%s, thread_id=%s",
            session_id, thread_id,
        )
        return session_id

    async def unregister(self, session_id: str) -> None:
        """注销 WebSocket 连接。

        清理会话数据，不主动关闭 WebSocket（由服务器层负责）。

        Args:
            session_id: 要注销的会话 ID
        """
        await self._force_unregister(session_id)

    async def _force_unregister(self, session_id: str) -> None:
        """强制注销会话，清理所有映射。

        Args:
            session_id: 要注销的会话 ID
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        # 清理 thread_id 映射
        if session.thread_id and self._thread_to_session.get(session.thread_id) == session_id:
            del self._thread_to_session[session.thread_id]

        logger.info("Session unregistered: session_id=%s", session_id)

    async def send_to(self, session_id: str, message: str) -> bool:
        """向指定会话发送文本消息。

        Args:
            session_id: 目标会话 ID
            message: 要发送的文本消息（通常为 JSON 字符串）

        Returns:
            发送成功返回 True，会话不存在或连接已关闭返回 False
        """
        session = self._sessions.get(session_id)
        if session is None:
            logger.warning("Send to unknown session: %s", session_id)
            return False

        try:
            if session.ws.closed:
                logger.warning("WebSocket closed for session: %s", session_id)
                await self._force_unregister(session_id)
                return False

            await session.ws.send_str(message)
            session.touch()
            return True
        except Exception as exc:
            logger.error("Failed to send to session %s: %s", session_id, exc)
            await self._force_unregister(session_id)
            return False

    async def broadcast(self, message: str) -> int:
        """向所有活跃会话广播消息。

        Args:
            message: 要广播的文本消息

        Returns:
            成功发送的会话数量
        """
        session_ids = list(self._sessions.keys())
        success_count = 0

        for session_id in session_ids:
            if await self.send_to(session_id, message):
                success_count += 1

        return success_count

    def get_session(self, session_id: str) -> SessionInfo | None:
        """获取会话信息。

        Args:
            session_id: 会话 ID

        Returns:
            SessionInfo 实例，不存在则返回 None
        """
        return self._sessions.get(session_id)

    def get_session_by_thread(self, thread_id: str) -> SessionInfo | None:
        """通过 thread_id 获取会话信息。

        用于重连恢复场景。

        Args:
            thread_id: 线程 ID

        Returns:
            SessionInfo 实例，不存在则返回 None
        """
        session_id = self._thread_to_session.get(thread_id)
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    @property
    def active_count(self) -> int:
        """当前活跃会话数量。"""
        return len(self._sessions)

    async def cleanup_stale(self) -> int:
        """清理超时的会话。

        移除超过 session_timeout 未活跃的会话。

        Returns:
            清理的会话数量
        """
        now = time.time()
        stale_ids = [
            sid
            for sid, session in self._sessions.items()
            if now - session.last_active_at > self._session_timeout
        ]

        for sid in stale_ids:
            await self._force_unregister(sid)
            logger.info("Cleaned up stale session: %s", sid)

        return len(stale_ids)
