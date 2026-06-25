"""WebSocket 消息总线。

提供消息总线的单例访问和 SourceType 枚举，
供交互通知等模块向 WebSocket 通道推送消息。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SourceType(str, Enum):
    """消息来源类型。"""

    SYSTEM = "system"
    AGENT = "agent"
    USER = "user"
    TOOL = "tool"


class MessageBus:
    """消息总线。

    将消息推送到指定线程的 WebSocket 连接。
    委托 WebSocketManager 完成实际的推送。
    """

    def __init__(self) -> None:
        self._manager: Any = None

    def _get_manager(self) -> Any:
        """延迟获取 WebSocketManager 实例。"""
        if self._manager is None:
            from src.websocket.handler import WebSocketManager

            self._manager = WebSocketManager()
        return self._manager

    async def emit(
        self,
        thread_id: str,
        message: dict[str, Any],
        *,
        source_type: SourceType = SourceType.SYSTEM,
        source_id: str | None = None,
    ) -> bool:
        """发送消息到指定线程。

        Args:
            thread_id: 目标线程 ID
            message: 消息字典（含 type 和 data）
            source_type: 消息来源类型
            source_id: 来源标识

        Returns:
            是否发送成功
        """
        try:
            manager = self._get_manager()
            event = {
                **message,
                "source_type": source_type.value,
                "source_id": source_id or "",
            }
            return await manager.send_to_thread(thread_id, event)
        except Exception as exc:
            logger.error(
                "[MessageBus] emit 失败 | thread=%s | error=%s",
                thread_id[:12] if thread_id else "",
                exc,
            )
            return False


_bus_instance: MessageBus | None = None


def get_message_bus() -> MessageBus:
    """获取消息总线单例。

    Returns:
        MessageBus 实例
    """
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = MessageBus()
    return _bus_instance
