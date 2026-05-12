#!/usr/bin/env python3
"""
消息时序修复模块

解决前端版本查询与后端数据库保存的时序问题
"""

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class MessageTimingManager:
    """消息时序管理器 - 确保数据库保存完成后再允许版本查询"""

    def __init__(self):
        # 记录正在保存的消息ID和保存时间
        self._saving_messages: dict[str, datetime] = {}
        # 保存完成的消息ID
        self._saved_messages: set[str] = set()
        # 保存超时时间（秒）
        self._save_timeout = 5.0

    def mark_message_saving(self, message_id: str) -> None:
        """标记消息开始保存"""
        self._saving_messages[message_id] = datetime.now()
        logger.debug(f"[MessageTiming] 标记消息开始保存 | message_id={message_id}")

    def mark_message_saved(self, message_id: str) -> None:
        """标记消息保存完成"""
        self._saving_messages.pop(message_id, None)
        self._saved_messages.add(message_id)
        logger.debug(f"[MessageTiming] 标记消息保存完成 | message_id={message_id}")

        # 清理过期的已保存记录（避免内存泄漏）
        if len(self._saved_messages) > 1000:
            # 保留最近的500个
            recent_messages = list(self._saved_messages)[-500:]
            self._saved_messages = set(recent_messages)

    def is_message_ready_for_query(self, message_id: str) -> bool:
        """检查消息是否可以进行版本查询"""
        # 如果消息已保存完成，可以查询
        if message_id in self._saved_messages:
            return True

        # 如果消息正在保存中，检查是否超时
        if message_id in self._saving_messages:
            save_start = self._saving_messages[message_id]
            elapsed = (datetime.now() - save_start).total_seconds()

            if elapsed > self._save_timeout:
                # 超时，认为保存失败，移除记录
                logger.warning(
                    f"[MessageTiming] 消息保存超时 | message_id={message_id} | elapsed={elapsed}s"
                )
                self._saving_messages.pop(message_id, None)
                return True  # 允许查询，让API返回404
            else:
                # 还在保存中
                logger.debug(
                    f"[MessageTiming] 消息正在保存中 | message_id={message_id} | elapsed={elapsed}s"
                )
                return False

        # 消息不在任何记录中，可能是旧消息，允许查询
        return True

    async def wait_for_message_ready(
        self, message_id: str, max_wait: float = 2.0
    ) -> bool:
        """等待消息准备就绪"""
        start_time = datetime.now()

        while (datetime.now() - start_time).total_seconds() < max_wait:
            if self.is_message_ready_for_query(message_id):
                return True
            await asyncio.sleep(0.1)

        logger.warning(f"[MessageTiming] 等待消息就绪超时 | message_id={message_id}")
        return False

    def cleanup_expired_records(self) -> None:
        """清理过期记录"""
        now = datetime.now()
        expired_messages = []

        for message_id, save_time in self._saving_messages.items():
            if (now - save_time).total_seconds() > self._save_timeout * 2:
                expired_messages.append(message_id)

        for message_id in expired_messages:
            self._saving_messages.pop(message_id, None)
            logger.debug(f"[MessageTiming] 清理过期保存记录 | message_id={message_id}")


# 全局实例
message_timing_manager = MessageTimingManager()


def mark_message_saving(message_id: str) -> None:
    """标记消息开始保存"""
    message_timing_manager.mark_message_saving(message_id)


def mark_message_saved(message_id: str) -> None:
    """标记消息保存完成"""
    message_timing_manager.mark_message_saved(message_id)


def is_message_ready_for_query(message_id: str) -> bool:
    """检查消息是否可以进行版本查询"""
    return message_timing_manager.is_message_ready_for_query(message_id)


async def wait_for_message_ready(message_id: str, max_wait: float = 2.0) -> bool:
    """等待消息准备就绪"""
    return await message_timing_manager.wait_for_message_ready(message_id, max_wait)
