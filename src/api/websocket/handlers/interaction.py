"""
交互响应消息处理器

处理客户端提交的交互响应
"""

import logging
from typing import Any

from src.api.websocket.handlers.base import BaseHandler, HandlerContext
from src.core.human_interaction.models import (
    InteractionResponse,
    ResponseType,
)
from src.core.human_interaction.service import get_human_interaction_service

logger = logging.getLogger(__name__)


class InteractionResponseHandler(BaseHandler):
    """
    交互响应消息处理器

    处理客户端提交的交互响应（审批/对话）
    """

    def can_handle(self, message_type: str) -> bool:
        return message_type == "interaction_response"

    async def handle(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_id = data.get("request_id")
        response_type_str = data.get("response_type")

        if not request_id or not response_type_str:
            logger.warning(
                f"[InteractionResponseHandler] 缺少必要字段 | "
                f"request_id={request_id} | response_type={response_type_str}"
            )
            return {"error": "缺少 request_id 或 response_type"}

        try:
            response_type = ResponseType(response_type_str)
        except ValueError:
            logger.warning(
                f"[InteractionResponseHandler] 无效的响应类型 | "
                f"response_type={response_type_str}"
            )
            return {"error": f"无效的响应类型: {response_type_str}"}

        service = get_human_interaction_service()

        request = await service.get_request(request_id)
        if not request:
            logger.warning(
                f"[InteractionResponseHandler] 请求不存在 | request_id={request_id}"
            )
            return {"error": f"请求不存在: {request_id}"}

        if request.thread_id != ctx.thread_id:
            logger.warning(
                f"[InteractionResponseHandler] 线程ID不匹配 | "
                f"request_thread={request.thread_id} | ctx_thread={ctx.thread_id}"
            )
            return {"error": "线程ID不匹配"}

        response = InteractionResponse(
            request_id=request_id,
            response_type=response_type,
            selected_option_id=data.get("selected_option_id"),
            modified_data=data.get("modified_data"),
            reason=data.get("reason"),
            conversation_result=data.get("conversation_result"),
            conversation_messages=data.get("conversation_messages", []),
            user_id=ctx.user_id,
        )

        success = await service.submit_response(request_id, response)

        logger.info(
            f"[InteractionResponseHandler] 响应已处理 | "
            f"request_id={request_id} | "
            f"response_type={response_type.value} | "
            f"success={success}"
        )

        return {
            "request_id": request_id,
            "success": success,
            "response_type": response_type.value,
        }


class ConversationMessageHandler(BaseHandler):
    """
    对话消息处理器

    处理对话模式下的用户消息
    """

    def can_handle(self, message_type: str) -> bool:
        return message_type == "conversation_message"

    async def handle(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_id = data.get("request_id")
        message_content = data.get("message")

        if not request_id or not message_content:
            logger.warning(
                f"[ConversationMessageHandler] 缺少必要字段 | "
                f"request_id={request_id}"
            )
            return {"error": "缺少 request_id 或 message"}

        service = get_human_interaction_service()

        request = await service.get_request(request_id)
        if not request:
            logger.warning(
                f"[ConversationMessageHandler] 请求不存在 | request_id={request_id}"
            )
            return {"error": f"请求不存在: {request_id}"}

        if request.thread_id != ctx.thread_id:
            logger.warning(
                f"[ConversationMessageHandler] 线程ID不匹配 | "
                f"request_thread={request.thread_id} | ctx_thread={ctx.thread_id}"
            )
            return {"error": "线程ID不匹配"}

        if request.conversation_context:
            request.conversation_context.history.append(
                {
                    "role": "user",
                    "content": message_content,
                    "timestamp": ctx.websocket.app.state.get("current_time", ""),
                }
            )

        logger.info(
            f"[ConversationMessageHandler] 对话消息已记录 | "
            f"request_id={request_id} | "
            f"message_len={len(message_content)}"
        )

        return {
            "request_id": request_id,
            "success": True,
            "history_count": len(request.conversation_context.history)
            if request.conversation_context
            else 0,
        }
