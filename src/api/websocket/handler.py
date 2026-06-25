"""WebSocket 连接管理器入口。

提供 connection_manager 单例供外部模块广播消息，
委托 WebSocketManager 完成实际的连接管理和消息推送。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ConnectionManager:
    """连接管理器包装。

    提供 broadcast 等高层 API，委托 WebSocketManager
    完成实际的 WebSocket 消息分发。
    """

    def __init__(self) -> None:
        self._manager: Any = None

    def _get_manager(self) -> Any:
        """延迟获取 WebSocketManager 实例。"""
        if self._manager is None:
            from src.websocket.handler import WebSocketManager

            self._manager = WebSocketManager()
        return self._manager

    async def broadcast(self, message: dict[str, Any]) -> None:
        """向所有活跃连接广播消息。

        遍历全局连接和会话连接，逐个推送消息。
        推送失败时仅记录日志，不影响其他连接。

        Args:
            message: 要广播的消息字典
        """
        import json

        manager = self._get_manager()

        # 向全局连接广播
        for user_id, ws in list(manager._global_connections.items()):
            try:
                await ws.send_text(
                    json.dumps(message, ensure_ascii=False, default=str)
                )
            except Exception as exc:
                logger.debug(
                    "[ConnectionManager] 广播到用户失败 | user=%s | error=%s",
                    user_id[:12] if user_id else "",
                    exc,
                )

        # 向会话连接广播
        for thread_id, conns in list(manager._active_connections.items()):
            for ws in conns:
                try:
                    await ws.send_text(
                        json.dumps(message, ensure_ascii=False, default=str)
                    )
                except Exception as exc:
                    logger.debug(
                        "[ConnectionManager] 广播到会话失败 | thread=%s | error=%s",
                        thread_id[:12] if thread_id else "",
                        exc,
                    )


# 模块级单例，供外部直接 from src.api.websocket.handler import connection_manager
connection_manager = ConnectionManager()
