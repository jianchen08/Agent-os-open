"""WebSocket 事件服务。

提供事件服务单例，用于向用户推送执行生命周期事件
（执行开始、执行完成等）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class EventService:
    """WebSocket 事件推送服务。

    封装执行事件的推送逻辑，委托 WebSocketManager 完成实际发送。
    """

    def __init__(self) -> None:
        self._manager: Any = None

    def _get_manager(self) -> Any:
        """延迟获取 WebSocketManager 实例。"""
        if self._manager is None:
            from src.websocket.handler import WebSocketManager

            self._manager = WebSocketManager()
        return self._manager

    async def send_execution_start(
        self,
        *,
        user_id: str,
        execution_id: str,
        execution_type: str,
        name: str | None = None,
        description: str | None = None,
        parent_id: str | None = None,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """发送执行开始事件。

        Args:
            user_id: 目标用户 ID
            execution_id: 执行 ID
            execution_type: 执行类型（如 agent、pipeline）
            name: 执行名称
            description: 执行描述
            parent_id: 父级 ID
            input_data: 输入数据
            metadata: 附加元数据

        Returns:
            是否发送成功
        """
        try:
            manager = self._get_manager()
            event: dict[str, Any] = {
                "type": "execution_start",
                "data": {
                    "execution_id": execution_id,
                    "execution_type": execution_type,
                },
            }
            if name is not None:
                event["data"]["name"] = name
            if description is not None:
                event["data"]["description"] = description
            if parent_id is not None:
                event["data"]["parent_id"] = parent_id
            if input_data is not None:
                event["data"]["input_data"] = input_data
            if metadata is not None:
                event["data"]["metadata"] = metadata

            return await manager.send_to_user(user_id, event)
        except Exception as exc:
            logger.error(
                "[EventService] send_execution_start 失败 | "
                "user=%s | execution_id=%s | error=%s",
                user_id[:12] if user_id else "",
                execution_id[:12] if execution_id else "",
                exc,
            )
            return False

    async def send_execution_done(
        self,
        *,
        user_id: str,
        execution_id: str,
        success: bool,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        summary: str | None = None,
    ) -> bool:
        """发送执行完成事件。

        Args:
            user_id: 目标用户 ID
            execution_id: 执行 ID
            success: 是否成功
            output: 输出结果
            error: 错误信息
            duration_ms: 执行耗时（毫秒）
            summary: 执行摘要

        Returns:
            是否发送成功
        """
        try:
            manager = self._get_manager()
            event: dict[str, Any] = {
                "type": "execution_done",
                "data": {
                    "execution_id": execution_id,
                    "success": success,
                },
            }
            if output is not None:
                event["data"]["output"] = output
            if error is not None:
                event["data"]["error"] = error
            if duration_ms is not None:
                event["data"]["duration_ms"] = duration_ms
            if summary is not None:
                event["data"]["summary"] = summary

            return await manager.send_to_user(user_id, event)
        except Exception as exc:
            logger.error(
                "[EventService] send_execution_done 失败 | "
                "user=%s | execution_id=%s | error=%s",
                user_id[:12] if user_id else "",
                execution_id[:12] if execution_id else "",
                exc,
            )
            return False


_service_instance: EventService | None = None


def get_event_service() -> EventService:
    """获取事件服务单例。

    Returns:
        EventService 实例
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = EventService()
    return _service_instance
