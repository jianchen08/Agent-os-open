"""
重新生成消息处理器

处理用户请求重新生成 AI 回复，支持回滚之前的操作
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

# Note: MessageCache 已移除（简化架构）
# from src.api.views.message_view import message_cache
from src.api.websocket.handlers.base import BaseHandler, HandlerContext
from src.api.websocket.message_bus import SourceType, get_message_bus
from src.api.websocket.message_factory import MessageFactory
from src.api.websocket.message_timing_fix import mark_message_saved, mark_message_saving
from src.db.models import ExecutionRecord

logger = logging.getLogger(__name__)


class RegenerateHandler(BaseHandler):
    """重新生成消息处理器"""

    def can_handle(self, message_type: str) -> bool:
        return message_type == "regenerate"

    async def handle(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """处理重新生成请求"""
        thread_id = ctx.thread_id
        db = ctx.db
        ai_message_id = data.get("content", "")
        # 是否回滚之前的操作（默认开启）
        rollback_operations = data.get("rollback_operations", True)

        if not ai_message_id:
            error_msg = MessageFactory.create_error_message(
                thread_id=thread_id,
                error_code="MISSING_MESSAGE_ID",
                message="重新生成请求缺少消息ID",
            )
            await get_message_bus().emit(
                thread_id,
                error_msg,
                source_type=SourceType.SYSTEM,
                source_id="regenerate",
            )
            return None

        logger.info(f"收到重新生成请求 | ai_message_id={ai_message_id}")

        try:
            # 查找原 AI 消息
            ai_record = await self._find_ai_record(db, thread_id, ai_message_id)
            if not ai_record:
                error_msg = MessageFactory.create_error_message(
                    thread_id=thread_id,
                    error_code="MESSAGE_NOT_FOUND",
                    message=f"未找到消息: {ai_message_id}",
                )
                await get_message_bus().emit(
                    thread_id,
                    error_msg,
                    source_type=SourceType.SYSTEM,
                    source_id="regenerate",
                )
                return None

            # 查找对应的用户消息
            user_record = await self._find_user_record(db, thread_id, ai_record.id)
            if not user_record:
                error_msg = MessageFactory.create_error_message(
                    thread_id=thread_id,
                    error_code="USER_MESSAGE_NOT_FOUND",
                    message="未找到对应的用户消息",
                )
                await get_message_bus().emit(
                    thread_id,
                    error_msg,
                    source_type=SourceType.SYSTEM,
                    source_id="regenerate",
                )
                return None

            # 回滚之前的操作（如果启用）
            if rollback_operations:
                await self._rollback_previous_operations(thread_id, ai_message_id)

            content = user_record.message_data.get("content", "")
            from src.utils.message_id_helper import generate_execution_record_id

            new_message_id = await generate_execution_record_id(db, thread_id)
            mark_message_saving(new_message_id)

            # 为新消息创建检查点
            await self._create_checkpoint(thread_id, new_message_id)

            # 发送 stream_start（包含原消息 ID，用于前端删除旧消息）
            stream_start = MessageFactory.create_stream_message(
                thread_id=thread_id,
                ai_message_id=new_message_id,
                is_start=True,
                parent_message_id=ai_message_id,  # 传递原消息 ID
                is_retry=True,  # 标记为重试
            )
            await get_message_bus().emit(
                thread_id,
                stream_start,
                source_type=SourceType.SYSTEM,
                source_id="regenerate",
            )

            # 流式执行
            has_error, error_detail, final_content = await self._execute_stream(
                ctx, content, new_message_id
            )

            # 发送 new_message
            await self._send_new_message(
                ctx, new_message_id, final_content, has_error, error_detail
            )

            # 保存到数据库
            await self._save_regenerated_message(
                db,
                thread_id,
                new_message_id,
                final_content,
                ctx.agent_config,
                has_error,
                error_detail,
                ai_message_id,
            )
            mark_message_saved(new_message_id)

            # 发送 stream_end
            stream_end = MessageFactory.create_stream_message(
                thread_id=thread_id,
                ai_message_id=new_message_id,
                is_end=True,
                final_message_id=new_message_id,
            )
            await get_message_bus().emit(
                thread_id,
                stream_end,
                source_type=SourceType.SYSTEM,
                source_id="regenerate",
            )

            logger.info(f"重新生成完成 | new_message_id={new_message_id}")

        except Exception as e:
            logger.error(f"重新生成失败 | error={e}", exc_info=True)
            error_msg = MessageFactory.create_error_message(
                thread_id=thread_id,
                error_code="REGENERATE_ERROR",
                message=f"重新生成失败：{str(e)}",
            )
            await get_message_bus().emit(
                thread_id,
                error_msg,
                source_type=SourceType.SYSTEM,
                source_id="regenerate",
            )

        return None

    async def _find_ai_record(self, db, thread_id: str, message_id: str):
        """查找 AI 消息记录"""
        result = await db.execute(
            select(ExecutionRecord).where(
                ExecutionRecord.id == message_id,
                ExecutionRecord.session_id == thread_id,
            )
        )
        return result.scalar_one_or_none()

    async def _find_user_record(self, db, thread_id: str, ai_message_id: str):
        """查找 AI 消息之前的用户消息"""
        result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == thread_id,
                ExecutionRecord.message_data["type"] == "human",
                ExecutionRecord.id < ai_message_id,
            )
            .order_by(ExecutionRecord.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _execute_stream(
        self, ctx: HandlerContext, content: str, message_id: str
    ) -> tuple:
        """执行流式生成"""
        has_error = False
        error_detail = None
        final_content = ""
        processed_id_strs = []
        last_hash = None

        try:
            async for event in ctx.agent_loop.stream(
                content, stream_mode="messages", enable_thinking=False
            ):
                msg = (
                    event[0] if isinstance(event, tuple) and len(event) >= 1 else event
                )

                # 检查消息类型，只处理 AI 消息的内容
                msg_type = type(msg).__name__
                if msg_type not in ["AIMessage", "AIMessageChunk"]:
                    logger.debug(
                        f"[RegenerateHandler] 跳过非 AI 消息 | type={msg_type}"
                    )
                    continue

                if hasattr(msg, "content"):
                    text = msg.content
                    if isinstance(text, str) and text:
                        is_dup = False
                        if hasattr(msg, "id") and msg.id:
                            try:
                                import json

                                if isinstance(msg.id, dict):
                                    msg_id_str = json.dumps(msg.id, sort_keys=True)
                                else:
                                    msg_id_str = str(msg.id)

                                if msg_id_str in processed_id_strs:
                                    is_dup = True
                                else:
                                    processed_id_strs.append(msg_id_str)
                            except Exception:
                                pass

                        if not is_dup:
                            h = hash(text)
                            if h == last_hash:
                                is_dup = True
                            else:
                                last_hash = h

                        if not is_dup:
                            final_content += text
                            chunk = MessageFactory.create_stream_message(
                                thread_id=ctx.thread_id,
                                ai_message_id=message_id,
                                chunk=text,
                            )
                            await get_message_bus().emit(
                                ctx.thread_id,
                                chunk,
                                source_type=SourceType.SYSTEM,
                                source_id="regenerate",
                            )

        except TimeoutError:
            has_error = True
            error_detail = "Agent 执行超时"
            error_msg = MessageFactory.create_error_message(
                thread_id=ctx.thread_id,
                error_code="TIMEOUT",
                message="抱歉，处理您的请求超时了。",
            )
            await get_message_bus().emit(
                ctx.thread_id,
                error_msg,
                source_type=SourceType.SYSTEM,
                source_id="regenerate",
            )
            final_content = f"[错误] {error_detail}"

        except Exception as e:
            has_error = True
            error_detail = str(e)
            logger.exception(f"重新生成异常 | error={e}")
            error_msg = MessageFactory.create_error_message(
                thread_id=ctx.thread_id,
                error_code="AGENT_EXECUTION_ERROR",
                message=f"抱歉，处理请求时出错：{error_detail}",
            )
            await get_message_bus().emit(
                ctx.thread_id,
                error_msg,
                source_type=SourceType.SYSTEM,
                source_id="regenerate",
            )

        return has_error, error_detail, final_content

    async def _send_new_message(
        self,
        ctx: HandlerContext,
        message_id: str,
        content: str,
        has_error: bool,
        error_detail: str | None,
    ) -> None:
        """发送新消息事件"""
        version_info = {
            "versions": [
                {
                    "id": message_id,
                    "version": 1,
                    "content": content if has_error else "",
                    "created_at": datetime.utcnow().isoformat(),
                    "is_current": True,
                }
            ],
            "total": 1,
            "current_version": 1,
        }

        msg = MessageFactory.create_new_message(
            thread_id=ctx.thread_id,
            message_id=message_id,
            role="assistant",
            content=content,
            version_info=version_info,
            has_error=has_error,
            error_detail=error_detail if has_error else None,
        )
        await get_message_bus().emit(
            ctx.thread_id, msg, source_type=SourceType.MAIN, source_id="regenerate"
        )

    async def _save_regenerated_message(
        self,
        db,
        thread_id: str,
        message_id: str,
        content: str,
        agent_config: Any,
        has_error: bool,
        error_detail: str | None,
        original_id: str,
    ) -> None:
        """保存重新生成的消息"""
        try:
            from src.db.repositories.execution_record_repo import ExecutionRecordRepository
            from src.utils.id_encoder import parse_nested_id

            parsed = parse_nested_id(message_id)
            parsed["sequences"][-1]

            parent_record_id = parsed.get("parent_id")

            message_data = {
                "type": "ai",
                "record_type": "ai_response",
                "content": content,
                "status": "failed" if has_error else "completed",
                "error": error_detail if has_error else None,
            }

            repo = ExecutionRecordRepository(db)
            await repo.save_execution_record(
                session_id=thread_id,
                message_data=message_data,
                parent_record_id=parent_record_id,
                record_id=message_id,
            )

            logger.info(f"重新生成消息已保存 | message_id={message_id}")
        except Exception as e:
            logger.error(f"保存重新生成消息失败 | error={e}", exc_info=True)
            await db.rollback()

    async def _rollback_previous_operations(
        self, thread_id: str, message_id: str
    ) -> None:
        """
        回滚之前消息执行的操作

        Args:
            thread_id: 会话 ID
            message_id: 原消息 ID
        """
        try:
            from src.rollback import get_rollback_integration

            integration = get_rollback_integration()
            result = await integration.on_regenerate(
                session_id=thread_id,
                original_message_id=message_id,
                rollback_operations=True,
            )

            if result:
                logger.info(
                    f"回滚完成 | thread={thread_id}, message={message_id}, "
                    f"成功={result.rolled_back_count}, "
                    f"跳过={result.skipped_count}"
                )

                # 如果有回滚操作，通知前端
                if result.rolled_back_count > 0:
                    rollback_msg = {
                        "type": "rollback_complete",
                        "thread_id": thread_id,
                        "message_id": message_id,
                        "rolled_back_count": result.rolled_back_count,
                        "skipped_count": result.skipped_count,
                        "operations": result.operations[:5],  # 只返回前5个
                    }
                    await get_message_bus().emit(
                        thread_id,
                        rollback_msg,
                        source_type=SourceType.SYSTEM,
                        source_id="regenerate",
                    )

        except ImportError:
            logger.debug("回滚模块未加载，跳过回滚")
        except Exception as e:
            logger.warning(f"回滚操作失败: {e}")

    async def _create_checkpoint(self, thread_id: str, message_id: str) -> None:
        """
        为新消息创建检查点

        Args:
            thread_id: 会话 ID
            message_id: 新消息 ID
        """
        try:
            from src.rollback import get_rollback_integration

            integration = get_rollback_integration()
            await integration.on_message_start(
                session_id=thread_id,
                message_id=message_id,
            )
        except ImportError:
            logger.debug("回滚模块未加载，跳过检查点创建")
        except Exception as e:
            logger.warning(f"创建检查点失败: {e}")
