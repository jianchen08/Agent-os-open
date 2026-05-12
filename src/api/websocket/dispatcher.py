"""
WebSocket 消息分发器

使用策略模式统一分发消息到对应的处理器
"""

import logging
from typing import Any

from src.api.websocket.handlers.base import BaseHandler, HandlerContext
from src.api.websocket.handlers.control import ControlHandler
from src.api.websocket.handlers.regenerate import RegenerateHandler
from src.api.websocket.handlers.user_input import UserInputHandler

logger = logging.getLogger(__name__)


class MessageDispatcher:
    """消息分发器"""

    def __init__(self):
        """初始化分发器，注册所有处理器"""
        self._handlers: list[BaseHandler] = [
            UserInputHandler(),
            RegenerateHandler(),
            ControlHandler(),
        ]

    def register_handler(self, handler: BaseHandler) -> None:
        """注册新的处理器"""
        self._handlers.append(handler)

    def get_handler(self, data: dict[str, Any]) -> BaseHandler | None:
        """
        获取能处理该消息的处理器

        Args:
            data: 消息数据

        Returns:
            处理器实例，未找到返回 None
        """
        # 验证消息数据类型
        if not isinstance(data, dict):
            logger.error(f"[Dispatcher] 消息格式错误：期望字典类型，实际收到 {type(data).__name__}")
            return None

        message_type = data.get("type", "")
        for handler in self._handlers:
            if handler.can_handle(message_type):
                return handler
        return None

    async def dispatch(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        分发消息到对应的处理器

        Args:
            ctx: 处理器上下文
            data: 消息数据

        Returns:
            处理结果
        """
        # 验证消息数据类型
        if not isinstance(data, dict):
            logger.error(f"[Dispatcher] 消息格式错误：期望字典类型，实际收到 {type(data).__name__} | thread_id={ctx.thread_id}")
            return None

        message_type = data.get("type", "")
        logger.info(f"[Dispatcher] 开始分发消息 | type={message_type} | thread_id={ctx.thread_id} | handlers_count={len(self._handlers)}")

        for handler in self._handlers:
            handler_name = handler.__class__.__name__
            can_handle = handler.can_handle(message_type)
            logger.debug(f"[Dispatcher] 检查处理器 | handler={handler_name} | can_handle={can_handle} | message_type={message_type}")
            if can_handle:
                import time

                start_time = time.time()
                logger.info(
                    f"[Dispatcher] 找到处理器 | type={message_type} | "
                    f"handler={handler_name} | thread_id={ctx.thread_id}"
                )
                try:
                    result = await handler.handle(ctx, data)
                    duration_ms = int((time.time() - start_time) * 1000)
                    logger.info(
                        f"[Dispatcher] 处理完成 | type={message_type} | "
                        f"handler={handler_name} | duration={duration_ms}ms"
                    )
                    return result
                except Exception as e:
                    duration_ms = int((time.time() - start_time) * 1000)
                    logger.error(
                        f"[Dispatcher] 处理失败 | type={message_type} | "
                        f"handler={handler_name} | error={e} | duration={duration_ms}ms",
                        exc_info=True,
                    )
                    raise

        logger.warning(f"[Dispatcher] 未找到处理器 | type={message_type} | available_handlers={[h.__class__.__name__ for h in self._handlers]}")
        return None


# 全局分发器实例
message_dispatcher = MessageDispatcher()
