"""
WebSocket 消息去重器

防止重复处理相同的消息，提高系统稳定性和效率
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class MessageDeduplicator:
    """
    消息去重器

    功能：
    1. 记录已处理的消息 ID
    2. 检测重复消息
    3. 自动清理过期记录
    4. 提供统计信息

    使用场景：
    - WebSocket 消息去重
    - API 请求去重
    - 任务执行去重
    """

    def __init__(self, ttl_seconds: int = 300, cleanup_interval: int = 60):
        """
        初始化去重器

        Args:
            ttl_seconds: 消息记录过期时间（秒），默认 5 分钟
            cleanup_interval: 清理过期记录的间隔（秒），默认 1 分钟
        """
        self._processed_messages: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._ttl_seconds = ttl_seconds
        self._cleanup_interval = cleanup_interval

        # 统计信息
        self._stats = {
            "total_received": 0,  # 总接收消息数
            "duplicate_detected": 0,  # 检测到的重复消息数
            "processed": 0,  # 处理的唯一消息数
            "expired_cleaned": 0,  # 清理的过期记录数
        }

        # 启动后台清理任务
        self._cleanup_task: asyncio.Task | None = None
        self._start_cleanup_task()

    async def is_duplicate(self, message_id: str) -> bool:
        """
        检查消息是否重复

        Args:
            message_id: 消息 ID

        Returns:
            是否为重复消息（True 表示重复，False 表示首次处理）
        """
        if not message_id:
            # 空消息 ID 视为有效，但需要处理
            message_id = "__empty__"

        async with self._lock:
            self._stats["total_received"] += 1

            # 检查是否已处理
            if message_id in self._processed_messages:
                self._stats["duplicate_detected"] += 1
                logger.debug(f"[DEDUP] 重复消息检测 | message_id={message_id}")
                return True

            # 标记为已处理
            self._processed_messages[message_id] = datetime.utcnow()
            self._stats["processed"] += 1
            logger.debug(f"[DEDUP] 新消息记录 | message_id={message_id}")
            return False

    async def mark_processed(self, message_id: str) -> None:
        """
        手动标记消息为已处理

        Args:
            message_id: 消息 ID
        """
        if not message_id:
            message_id = "__empty__"

        async with self._lock:
            self._processed_messages[message_id] = datetime.utcnow()
            logger.debug(f"[DEDUP] 手动标记已处理 | message_id={message_id}")

    async def remove(self, message_id: str) -> None:
        """
        移除消息记录（允许重新处理）

        Args:
            message_id: 消息 ID
        """
        if not message_id:
            message_id = "__empty__"

        async with self._lock:
            if message_id in self._processed_messages:
                del self._processed_messages[message_id]
                logger.debug(f"[DEDUP] 移除消息记录 | message_id={message_id}")

    async def clear(self) -> None:
        """清空所有记录"""
        async with self._lock:
            count = len(self._processed_messages)
            self._processed_messages.clear()
            self._stats = {
                "total_received": 0,
                "duplicate_detected": 0,
                "processed": 0,
                "expired_cleaned": 0,
            }
            logger.info(f"[DEDUP] 清空所有记录 | count={count}")

    def get_stats(self) -> dict[str, Any]:
        """
        获取统计信息

        Returns:
            统计信息字典
        """
        return self._stats.copy()

    async def get_processed_count(self) -> int:
        """
        获取已处理消息数

        Returns:
            当前记录的消息数量
        """
        async with self._lock:
            return len(self._processed_messages)

    async def is_processed(self, message_id: str) -> bool:
        """
        检查消息是否已处理（不影响统计）

        Args:
            message_id: 消息 ID

        Returns:
            是否已处理
        """
        if not message_id:
            message_id = "__empty__"

        async with self._lock:
            return message_id in self._processed_messages

    async def _cleanup_expired(self) -> int:
        """
        清理过期的消息记录（内部方法）

        Returns:
            清理的记录数量
        """
        now = datetime.utcnow()
        expired_count = 0

        async with self._lock:
            expired_ids = [
                msg_id
                for msg_id, timestamp in self._processed_messages.items()
                if (now - timestamp).total_seconds() > self._ttl_seconds
            ]

            for msg_id in expired_ids:
                del self._processed_messages[msg_id]
                expired_count += 1

            self._stats["expired_cleaned"] += expired_count

        if expired_count > 0:
            logger.info(f"[DEDUP] 清理过期记录 | count={expired_count}")

        return expired_count

    def _start_cleanup_task(self) -> None:
        """启动后台清理任务"""
        if self._cleanup_task is None or self._cleanup_task.done():

            async def cleanup_loop():
                """定期清理过期记录"""
                try:
                    while True:
                        await asyncio.sleep(self._cleanup_interval)
                        await self._cleanup_expired()
                except asyncio.CancelledError:
                    logger.debug("[DEDUP] 清理任务已取消")

            self._cleanup_task = asyncio.create_task(cleanup_loop())
            logger.debug("[DEDUP] 后台清理任务已启动")

    async def stop(self) -> None:
        """停止去重器，取消后台任务"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.debug("[DEDUP] 后台清理任务已停止")

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.stop()


# 全局去重器实例（按需创建）
_global_deduplicator: MessageDeduplicator | None = None


def get_message_deduplicator(
    ttl_seconds: int = 300, cleanup_interval: int = 60
) -> MessageDeduplicator:
    """
    获取全局消息去重器实例

    Args:
        ttl_seconds: 消息记录过期时间
        cleanup_interval: 清理间隔

    Returns:
        全局去重器实例
    """
    global _global_deduplicator

    if _global_deduplicator is None:
        _global_deduplicator = MessageDeduplicator(
            ttl_seconds=ttl_seconds, cleanup_interval=cleanup_interval
        )
        logger.info("[DEDUP] 全局去重器已创建")

    return _global_deduplicator


async def reset_global_deduplicator() -> None:
    """重置全局去重器"""
    global _global_deduplicator

    if _global_deduplicator is not None:
        await _global_deduplicator.stop()
        _global_deduplicator = None
        logger.info("[DEDUP] 全局去重器已重置")
