"""
WebSocket 交互通知器

暴露接口：
- WebSocketInteractionNotifier：WebSocketInteractionNotifier类
"""

import logging

from src.api.websocket.message_bus import SourceType, get_message_bus
from src.api.websocket.message_types import (
    create_interaction_cancelled_message,
    create_interaction_request_message,
    create_interaction_timeout_message,
)
from src.core.human_interaction.interfaces import IInteractionNotifier
from src.db.models import ExecutionRecord

logger = logging.getLogger(__name__)


class WebSocketInteractionNotifier(IInteractionNotifier):
    """
    WebSocket 交互通知器

    通过 WebSocket 消息总线将交互请求推送到前端
    """

    def __init__(self):
        self._message_bus = get_message_bus()

    async def notify_request(self, request: ExecutionRecord) -> bool:
        """推送交互请求通知"""
        try:
            message_data = request.message_data
            thread_id = message_data.get("thread_id", "")

            message = create_interaction_request_message(
                thread_id=thread_id,
                request_id=request.id,
                interaction_type=message_data.get("interaction_mode", "choice"),
                mode=message_data.get("interaction_mode", "choice"),
                title=message_data.get("title", ""),
                description=message_data.get("description", ""),
                priority=message_data.get("priority", "normal"),
                timeout=message_data.get("timeout_seconds", 300),
                approval_options=message_data.get("options"),
                agent_id=message_data.get("agent_id"),
            )

            await self._message_bus.emit(
                thread_id,
                message,
                source_type=SourceType.SYSTEM,
                source_id="human_interaction",
            )

            logger.info(
                f"[WebSocketNotifier] 交互请求已推送 | "
                f"request_id={request.id} | "
                f"mode={message_data.get('interaction_mode')} | "
                f"thread_id={thread_id}"
            )

            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 推送交互请求失败 | "
                f"request_id={request.id} | error={e}"
            )
            return False

    async def notify_cancel(
        self, request_id: str, reason: str | None = None, thread_id: str = ""
    ) -> bool:
        """推送取消通知"""
        try:
            message = create_interaction_cancelled_message(
                thread_id=thread_id,
                request_id=request_id,
                reason=reason,
            )

            await self._message_bus.emit(
                thread_id,
                message,
                source_type=SourceType.SYSTEM,
                source_id="human_interaction",
            )

            logger.info(
                f"[WebSocketNotifier] 交互取消通知已发送 | " f"request_id={request_id}"
            )

            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 发送取消通知失败 | "
                f"request_id={request_id} | error={e}"
            )
            return False

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        """推送超时通知"""
        try:
            message = create_interaction_timeout_message(
                thread_id=thread_id,
                request_id=request_id,
            )

            await self._message_bus.emit(
                thread_id,
                message,
                source_type=SourceType.SYSTEM,
                source_id="human_interaction",
            )

            logger.info(
                f"[WebSocketNotifier] 交互超时通知已发送 | " f"request_id={request_id}"
            )

            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 发送超时通知失败 | "
                f"request_id={request_id} | error={e}"
            )
            return False

    async def notify_timeout_reminder(
        self,
        request_id: str,
        remaining_seconds: int,
        thread_id: str = "",
        *,
        title: str = "",
        mode: str = "",
        options: list[dict] | None = None,
        questions: list[str] | None = None,
    ) -> bool:
        """推送超时提醒"""
        try:
            data = {
                "request_id": request_id,
                "remaining_seconds": remaining_seconds,
                "title": title,
                "mode": mode,
            }
            if options:
                data["options"] = options
            if questions:
                data["questions"] = questions

            message = {
                "type": "interaction_timeout_reminder",
                "data": data,
            }

            await self._message_bus.emit(
                thread_id,
                message,
                source_type=SourceType.SYSTEM,
                source_id="human_interaction",
            )

            logger.info(
                f"[WebSocketNotifier] 超时提醒已发送 | "
                f"request_id={request_id} | remaining={remaining_seconds}s"
            )

            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 发送超时提醒失败 | "
                f"request_id={request_id} | error={e}"
            )
            return False

    async def notify_conversation_start(
        self,
        thread_id: str,
        tab_id: str,
        title: str,
        request_id: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
    ) -> bool:
        """推送对话模式开始通知"""
        try:
            data = {
                "thread_id": thread_id,
                "tab_id": tab_id,
                "title": title,
                "initial_message": initial_message,
                "suggestions": suggestions,
            }
            if request_id:
                data["request_id"] = request_id

            message = {
                "type": "conversation_start",
                "data": data,
            }

            await self._message_bus.emit(
                thread_id,
                message,
                source_type=SourceType.SYSTEM,
                source_id="human_interaction",
            )

            logger.info(
                f"[WebSocketNotifier] 对话模式通知已发送 | "
                f"thread_id={thread_id} | tab_id={tab_id}"
            )

            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 发送对话模式通知失败 | "
                f"thread_id={thread_id} | error={e}"
            )
            return False
