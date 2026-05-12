"""
消息持久化组件

负责将消息保存到数据库

适配 ExecutionRecord 极简设计：
- 只使用 5 个核心字段：id, session_id, parent_record_id, message_data, created_at
- 所有其他信息存储在 message_data JSON 字段中
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ExecutionRecord
from src.db.repositories.execution_record_repo import ExecutionRecordRepository

logger = logging.getLogger(__name__)


class MessagePersistence:
    """
    消息持久化组件

    负责用户消息和 AI 消息的数据库存储
    """

    async def save_user_message(
        self,
        db: AsyncSession,
        thread_id: str,
        message_id: str,
        content: str,
        user_id: str,
        parent_record_id: str | None = None,
    ) -> bool:
        """
        保存用户消息到数据库

        Args:
            db: 数据库会话
            thread_id: 线程 ID
            message_id: 消息 ID
            content: 消息内容
            user_id: 用户 ID
            parent_record_id: 父记录 ID（可选）

        Returns:
            bool: 是否保存成功
        """
        try:
            from src.utils.message_id_helper import get_sequence_from_id

            sequence = get_sequence_from_id(message_id) or 1

            from src.utils.id_encoder import parse_nested_id

            if parent_record_id:
                try:
                    parent_parsed = parse_nested_id(parent_record_id)
                    depth = parent_parsed.get("depth", 0) + 1
                except Exception:
                    depth = 1
            else:
                try:
                    parsed = parse_nested_id(message_id)
                    depth = parsed.get("depth", 0)
                except Exception:
                    depth = 0

            message_data = {
                "type": "human",
                "record_type": "user_input",
                "content": content,
                "status": "completed",
                "order": {
                    "sequence": sequence,
                    "depth": depth,
                },
                "version": {
                    "is_current": True,
                },
            }

            existing = await db.execute(
                select(ExecutionRecord.id).where(ExecutionRecord.id == message_id)
            )
            if existing.scalar_one_or_none():
                logger.warning(f"[DB] ID 已存在 | id={message_id} | 跳过保存或重新生成")
                return False

            repo = ExecutionRecordRepository(db)
            await repo.save_execution_record(
                session_id=thread_id,
                message_data=message_data,
                parent_record_id=parent_record_id,
                record_id=message_id,
            )

            logger.info(
                "[DB] 保存消息 | id=%s | type=human | thread_id=%s",
                message_id,
                thread_id,
            )
            return True

        except Exception as e:
            logger.error(
                "[DB] 保存失败 | type=human | id=%s | error=%s",
                message_id,
                e,
                exc_info=True,
            )
            await db.rollback()
            return False

    async def save_ai_message(
        self,
        db: AsyncSession,
        thread_id: str,
        message_id: str,
        content: str,
        agent_config: Any,
        has_error: bool = False,
        error_detail: str | None = None,
        tool_calls: list | None = None,
        thinking_content: str | None = None,
        duration_ms: int | None = None,
        parent_record_id: str | None = None,
    ) -> bool:
        """
        保存 AI 消息到数据库

        Args:
            db: 数据库会话
            thread_id: 线程 ID
            message_id: 消息 ID
            content: 消息内容
            agent_config: Agent 配置
            has_error: 是否有错误
            error_detail: 错误详情
            tool_calls: 工具调用列表
            thinking_content: 思考内容
            duration_ms: 执行时长（毫秒）
            parent_record_id: 父记录 ID（可选，用于嵌套子Agent记录）

        Returns:
            bool: 是否保存成功
        """
        try:
            sequence = await self._get_next_sequence(db, thread_id)

            cleaned_content = content

            from src.utils.id_encoder import parse_nested_id

            if parent_record_id:
                try:
                    parent_parsed = parse_nested_id(parent_record_id)
                    depth = parent_parsed.get("depth", 0) + 1
                except Exception:
                    depth = 1
            else:
                try:
                    parsed = parse_nested_id(message_id)
                    depth = parsed.get("depth", 0)
                except Exception:
                    depth = 0

            message_data = {
                "type": "ai",
                "record_type": "ai_response",
                "content": cleaned_content,
                "status": "failed" if has_error else "completed",
                "order": {
                    "sequence": sequence,
                    "depth": depth,
                },
                "version": {
                    "is_current": True,
                },
            }

            if has_error and error_detail:
                message_data["error"] = error_detail

            if tool_calls:
                message_data["tool_calls"] = tool_calls
                logger.info(f"[DB] 保存 tool_calls | count={len(tool_calls)}")

            if thinking_content:
                message_data["thinking"] = thinking_content
                logger.info(f"[DB] 保存 thinking_content | len={len(thinking_content)}")

            if duration_ms is not None:
                message_data["duration_ms"] = duration_ms

            existing = await db.execute(
                select(ExecutionRecord.id).where(ExecutionRecord.id == message_id)
            )
            if existing.scalar_one_or_none():
                logger.warning(f"[DB] ID 已存在 | id={message_id} | 跳过保存或重新生成")
                return False

            repo = ExecutionRecordRepository(db)
            await repo.save_execution_record(
                session_id=thread_id,
                message_data=message_data,
                parent_record_id=parent_record_id,
                record_id=message_id,
            )

            logger.info(
                "[DB] 保存消息 | id=%s | type=ai | thread_id=%s",
                message_id,
                thread_id,
            )
            return True

        except Exception as e:
            logger.error(
                "[DB] 保存失败 | type=ai | id=%s | error=%s",
                message_id,
                e,
                exc_info=True,
            )
            await db.rollback()
            return False

    async def _get_next_sequence(self, db: AsyncSession, thread_id: str) -> int:
        """
        获取下一个序列号

        从 message_data.order.sequence 中获取最大序列号

        Args:
            db: 数据库会话
            thread_id: 线程 ID

        Returns:
            int: 下一个序列号
        """
        # 查询该会话的所有记录
        result = await db.execute(
            select(ExecutionRecord).where(ExecutionRecord.session_id == thread_id)
        )
        records = result.scalars().all()

        # 从 message_data.order.sequence 中提取最大序列号
        max_sequence = 0
        for record in records:
            if record.message_data and "order" in record.message_data:
                seq = record.message_data["order"].get("sequence", 0)
                max_sequence = max(max_sequence, seq)

        return max_sequence + 1

    async def update_message_content(
        self,
        db: AsyncSession,
        message_id: str,
        content: str,
    ) -> bool:
        """
        更新消息内容

        Args:
            db: 数据库会话
            message_id: 消息 ID
            content: 新内容

        Returns:
            bool: 是否更新成功
        """
        try:
            result = await db.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == message_id)
            )
            record = result.scalar_one_or_none()

            if record:
                # 更新 message_data 中的 content
                message_data = record.message_data.copy()
                message_data["content"] = content
                record.message_data = message_data
                await db.commit()

                # Note: MessageCache 已移除（简化架构）
                # message_cache.invalidate(record.session_id)

                # 发送 WebSocket 通知
                from src.api.websocket.enums import SourceType
                from src.api.websocket.message_bus import get_message_bus
                from src.api.websocket.message_factory import MessageFactory
                from src.api.websocket.message_types import MessageTypes

                update_message = MessageFactory.create_message(
                    message_type=MessageTypes.MESSAGE_UPDATED,
                    thread_id=record.session_id,
                    data={
                        "sessionId": record.session_id,
                        "messageId": message_id,
                        "content": content,
                        "updatedFields": ["content"],
                    },
                )

                await get_message_bus().emit(
                    record.session_id,
                    update_message,
                    source_type=SourceType.SYSTEM,
                    source_id="message_persistence",
                )

                logger.info(
                    "[DB] 更新消息 | id=%s | status=content_updated | notification_sent=true",
                    message_id,
                )
                return True

            logger.error("[DB] 消息不存在 | id=%s", message_id)
            return False

        except Exception as e:
            logger.error(
                "[DB] 更新消息失败 | id=%s | error=%s",
                message_id,
                e,
                exc_info=True,
            )
            await db.rollback()
            return False
