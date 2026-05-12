"""
用户输入消息处理器

处理用户发送的聊天消息

重构说明：
- 将消息验证、持久化、流式处理拆分为独立组件
- Handler 只负责编排各组件的调用
"""

import logging
from typing import Any

from src.api.websocket.handlers.base import BaseHandler, HandlerContext
from src.api.websocket.handlers.components import (
    MessagePersistence,
    MessageValidator,
    StreamProcessor,
)
from src.api.websocket.message_bus import SourceType, get_message_bus
from src.api.websocket.message_factory import MessageFactory
from src.api.websocket.message_timing_fix import (
    mark_message_saved,
    mark_message_saving,
)

logger = logging.getLogger(__name__)


class UserInputHandler(BaseHandler):
    """
    用户输入消息处理器

    职责：编排消息处理流程
    - 使用 MessageValidator 验证消息格式
    - 使用 MessagePersistence 保存消息
    - 使用 StreamProcessor 处理流式输出
    """

    def __init__(self):
        """初始化处理器及其组件"""
        self.validator = MessageValidator()
        self.persistence = MessagePersistence()
        self.stream_processor = StreamProcessor()

    def can_handle(self, message_type: str) -> bool:
        """判断是否能处理该消息类型"""
        return message_type == "user_input"

    async def handle(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        处理用户输入消息

        流程：
        1. 验证消息格式
        2. 发送并保存用户消息
        3. 发送 stream_start
        4. 执行 Agent 流式处理
        5. 发送并保存 AI 消息
        6. 发送 stream_end
        """
        thread_id = ctx.thread_id
        import time

        handle_start_time = time.time()

        logger.info(
            f"[UserInputHandler] 开始处理 | "
            f"thread_id={thread_id} | "
            f"user_id={ctx.user_id} | "
            f"data={data}"
        )

        # 1. 验证消息格式
        logger.debug(f"[UserInputHandler] 步骤1: 验证消息格式 | thread_id={thread_id}")
        validation = self.validator.validate_user_input(data)
        if not validation.is_valid:
            logger.error(
                f"[UserInputHandler] 验证失败 | "
                f"thread_id={thread_id} | "
                f"error_code={validation.error_code} | "
                f"error={validation.error_message}"
            )
            error_msg = MessageFactory.create_error_message(
                thread_id=thread_id,
                error_code=validation.error_code,
                message=validation.error_message,
            )
            await get_message_bus().emit(
                thread_id, error_msg, source_type=SourceType.SYSTEM, source_id="handler"
            )
            return None

        content = validation.content
        enable_thinking = validation.enable_thinking

        logger.info(
            f"[UserInputHandler] 消息验证通过 | "
            f"thread_id={thread_id} | "
            f"content_length={len(content)} | "
            f"enable_thinking={enable_thinking}"
        )
        logger.debug(
            f"[UserInputHandler] 消息内容 | "
            f"thread_id={thread_id} | "
            f"content={content[:100]}..."
        )

        # 2. 先保存用户消息到数据库（先存后发策略）
        logger.debug(f"[UserInputHandler] 步骤2: 保存用户消息 | thread_id={thread_id}")
        from src.utils.message_id_helper import generate_execution_record_id

        # 尝试保存消息，最多重试 3 次（处理 ID 冲突）
        max_retries = 3
        user_message_id = None
        save_success = False

        for attempt in range(max_retries):
            user_message_id = await generate_execution_record_id(ctx.db, thread_id)
            mark_message_saving(user_message_id)

            # 先保存到数据库（parent_record_id=None 表示顶层记录）
            save_success = await self.persistence.save_user_message(
                ctx.db, thread_id, user_message_id, content, ctx.user_id, None
            )

            if save_success:
                break  # 保存成功，退出循环

            # 保存失败，可能是 ID 冲突，记录并重试
            if attempt < max_retries - 1:
                logger.warning(
                    f"[UserInputHandler] 消息保存失败（可能是 ID 冲突）| "
                    f"attempt={attempt + 1}/{max_retries} | "
                    f"message_id={user_message_id} | "
                    f"thread_id={thread_id}"
                )
            else:
                # 最后一次尝试也失败了
                logger.error(
                    f"[UserInputHandler] 用户消息保存失败 | "
                    f"message_id={user_message_id} | "
                    f"thread_id={thread_id} | "
                    f"attempts={max_retries}"
                )

        if not save_success:
            error_msg = MessageFactory.create_error_message(
                thread_id=thread_id,
                error_code="MESSAGE_SAVE_FAILED",
                message="保存消息失败，请重试",
            )
            await get_message_bus().emit(
                thread_id, error_msg, source_type=SourceType.SYSTEM, source_id="handler"
            )
            return None

        logger.debug(
            f"[UserInputHandler] 用户消息保存成功 | message_id={user_message_id}"
        )
        mark_message_saved(user_message_id)

        # 数据库保存成功后再发送
        user_message = MessageFactory.create_new_message(
            thread_id=thread_id,
            message_id=user_message_id,
            role="user",
            content=content,
        )
        await get_message_bus().emit(
            thread_id, user_message, source_type=SourceType.MAIN, source_id="handler"
        )
        logger.debug(
            f"[UserInputHandler] 用户消息已发送 | message_id={user_message_id}"
        )

        # 3. 生成AI消息ID并发送 stream_start
        # AI消息将由Agent循环创建，这里只生成ID用于流式跟踪
        logger.debug(f"[UserInputHandler] 步骤3: 生成AI消息ID | thread_id={thread_id}")
        ai_message_id = await generate_execution_record_id(ctx.db, thread_id)
        mark_message_saving(ai_message_id)

        stream_start = MessageFactory.create_stream_message(
            thread_id=thread_id, ai_message_id=ai_message_id, is_start=True
        )
        await get_message_bus().emit(
            thread_id, stream_start, source_type=SourceType.MAIN, source_id="handler"
        )
        logger.debug(
            f"[UserInputHandler] stream_start 已发送 | ai_message_id={ai_message_id}"
        )

        # 4. 在 Agent 流式执行之前检查并压缩上下文
        # 这样可以避免压缩响应被 LangGraph 的 astream 捕获
        logger.info(
            f"[UserInputHandler] 步骤4: 检查并压缩上下文（如果需要） | "
            f"thread_id={thread_id}"
        )
        if hasattr(ctx.agent_loop, '_layered_context_store') and ctx.agent_loop._layered_context_store:
            layered_context_store = ctx.agent_loop._layered_context_store
            if hasattr(layered_context_store, 'check_and_compress'):
                try:
                    await layered_context_store.check_and_compress()
                    logger.info(f"[UserInputHandler] 上下文压缩检查完成 | thread_id={thread_id}")
                except Exception as e:
                    logger.warning(f"[UserInputHandler] 上下文压缩失败: {e}")

        # 5. 执行 Agent 流式处理
        logger.info(
            f"[UserInputHandler] 步骤5: 执行 Agent 流式处理 | "
            f"thread_id={thread_id} | "
            f"enable_thinking={enable_thinking}"
        )
        stream_start_time = time.time()

        stream_result = await self.stream_processor.process(
            thread_id=thread_id,
            agent_loop=ctx.agent_loop,
            content=content,
            enable_thinking=enable_thinking,
            message_id=ai_message_id,
        )

        stream_duration_ms = int((time.time() - stream_start_time) * 1000)
        logger.info(
            f"[UserInputHandler] Agent 流式处理完成 | "
            f"thread_id={thread_id} | "
            f"duration_ms={stream_duration_ms} | "
            f"has_error={stream_result.has_error}"
        )

        # 5. 发送最终消息和 stream_end
        # 注意：AI消息现在由Agent循环创建，WebSocket层只负责推送事件到前端
        logger.debug(f"[UserInputHandler] 步骤5: 发送最终消息 | thread_id={thread_id}")

        # 确定最终消息ID和内容
        final_message_id = stream_result.second_ai_message_id or ai_message_id
        final_content = stream_result.final_content
        final_tool_calls = [] if stream_result.second_ai_message_id else stream_result.tool_calls

        # BUG-FIX: 同步流式累积的内容到数据库
        # 问题根因: _create_ai_execution_record 使用 LLM 原始响应的 content，
        #          当 LLM 只返回 tool_calls 时 content 为空
        # 修复方案: 流式处理结束后，更新数据库中的 content 字段
        if final_content:
            try:
                from sqlalchemy import select
                from sqlalchemy.orm.attributes import flag_modified

                from src.db.connection import get_async_session
                from src.db.models import ExecutionRecord

                async for db_session in get_async_session():
                    result = await db_session.execute(
                        select(ExecutionRecord).where(ExecutionRecord.id == final_message_id)
                    )
                    record = result.scalar_one_or_none()
                    if record:
                        record.message_data["content"] = final_content
                        flag_modified(record, "message_data")
                        await db_session.commit()
                        logger.info(
                            f"[UserInputHandler] 同步流式内容到数据库 | "
                            f"record_id={final_message_id} | content_len={len(final_content)}"
                        )
                    break
            except Exception as e:
                logger.warning(
                    f"[UserInputHandler] 同步流式内容失败 | record_id={final_message_id} | error={e}"
                )

        # BUG-FIX: 同步第一条AI消息的内容（包含 tool_calls 的消息）
        # 当有第二条消息时，第一条消息的内容可能是流式累积的文本
        if stream_result.second_ai_message_id and stream_result.first_ai_message_content:
            try:
                from sqlalchemy import select
                from sqlalchemy.orm.attributes import flag_modified

                from src.db.connection import get_async_session
                from src.db.models import ExecutionRecord

                async for db_session in get_async_session():
                    result = await db_session.execute(
                        select(ExecutionRecord).where(ExecutionRecord.id == ai_message_id)
                    )
                    record = result.scalar_one_or_none()
                    if record:
                        record.message_data["content"] = stream_result.first_ai_message_content
                        flag_modified(record, "message_data")
                        await db_session.commit()
                        logger.info(
                            f"[UserInputHandler] 同步第一条AI消息内容到数据库 | "
                            f"record_id={ai_message_id} | content_len={len(stream_result.first_ai_message_content)}"
                        )
                    break
            except Exception as e:
                logger.warning(
                    f"[UserInputHandler] 同步第一条AI消息内容失败 | record_id={ai_message_id} | error={e}"
                )

        # 发送 assistant 消息到前端
        await self._send_assistant_message(
            ctx,
            final_message_id,
            final_content,
            stream_result.has_error,
            stream_result.error_detail,
            stream_result.thinking_content,
            final_tool_calls,
        )
        logger.debug(
            f"[UserInputHandler] assistant 消息已发送 | message_id={final_message_id}"
        )

        # 6. 发送 stream_end
        logger.debug(
            f"[UserInputHandler] 步骤6: 发送 stream_end | thread_id={thread_id}"
        )
        stream_end = MessageFactory.create_stream_message(
            thread_id=thread_id,
            ai_message_id=final_message_id,
            is_end=True,
            final_message_id=final_message_id,
        )
        await get_message_bus().emit(
            thread_id, stream_end, source_type=SourceType.MAIN, source_id="handler"
        )

        total_duration_ms = int((time.time() - handle_start_time) * 1000)
        logger.info(
            f"[UserInputHandler] 消息处理完成 | "
            f"thread_id={thread_id} | "
            f"ai_message_id={ai_message_id} | "
            f"total_duration_ms={total_duration_ms}"
        )
        return None

    async def _send_assistant_message(
        self,
        ctx: HandlerContext,
        message_id: str,
        content: str,
        has_error: bool,
        error_detail: str | None,
        thinking: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        """发送助手消息事件"""
        msg = MessageFactory.create_new_message(
            thread_id=ctx.thread_id,
            message_id=message_id,
            role="assistant",
            content=content,
            thinking=thinking,
            tool_calls=tool_calls,
            has_error=has_error,
            error_detail=error_detail if has_error else None,
        )
        await get_message_bus().emit(
            ctx.thread_id, msg, source_type=SourceType.MAIN, source_id="handler"
        )
