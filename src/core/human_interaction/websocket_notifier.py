"""
WebSocket 交互通知器

通过 WebSocket 将交互请求推送到前端
"""

import logging

from src.api.websocket.message_bus import SourceType, get_message_bus
from src.api.websocket.message_types import (
    create_interaction_cancelled_message,
    create_interaction_request_message,
)
from src.core.human_interaction.interfaces import IInteractionNotifier
from src.core.human_interaction.models import InteractionRequest

logger = logging.getLogger(__name__)


class WebSocketInteractionNotifier(IInteractionNotifier):
    """
    WebSocket 交互通知器

    通过 WebSocket 消息总线将交互请求推送到前端
    """

    def __init__(self):
        self._message_bus = get_message_bus()

    async def notify_request(self, request: InteractionRequest) -> bool:
        try:
            message = create_interaction_request_message(
                thread_id=request.thread_id,
                request_id=request.request_id,
                interaction_type=request.interaction_type.value,
                mode=request.mode.value,
                title=request.title,
                description=request.description,
                priority=request.priority.value,
                timeout=request.timeout,
                approval_options=[opt.to_dict() for opt in request.approval_options]
                if request.approval_options
                else None,
                context=request.context.to_dict() if request.context else None,
                conversation_context=request.conversation_context.to_dict()
                if request.conversation_context
                else None,
                agent_id=request.agent_id,
            )

            await self._message_bus.emit(
                request.thread_id,
                message,
                source_type=SourceType.SYSTEM,
                source_id="human_interaction",
            )

            logger.info(
                f"[WebSocketNotifier] 交互请求已推送 | "
                f"request_id={request.request_id} | "
                f"type={request.interaction_type.value} | "
                f"thread_id={request.thread_id}"
            )

            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 推送交互请求失败 | "
                f"request_id={request.request_id} | error={e}"
            )
            return False

    async def notify_cancel(self, request_id: str, reason: str | None = None) -> bool:
        try:
            create_interaction_cancelled_message(
                thread_id="",
                request_id=request_id,
                reason=reason,
            )

            logger.info(
                f"[WebSocketNotifier] 交互取消通知已发送 | "
                f"request_id={request_id}"
            )

            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 发送取消通知失败 | "
                f"request_id={request_id} | error={e}"
            )
            return False

    async def notify_timeout(self, request_id: str) -> bool:
        try:
            logger.info(
                f"[WebSocketNotifier] 交互超时通知 | request_id={request_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"[WebSocketNotifier] 发送超时通知失败 | "
                f"request_id={request_id} | error={e}"
            )
            return False
