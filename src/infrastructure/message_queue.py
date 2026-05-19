"""消息队列 — 管道间消息传递基础设施。

用于 L1 Agent 向运行中的子任务管道定向注入指令。
消息按 pipeline_id 隔离，每个管道只能取到发给自己的消息。

暴露接口：
- Message: 消息数据类
- MessageQueue: 消息队列
- create_message_id: 消息 ID 工厂函数
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """队列消息。

    Attributes:
        id: 消息唯一标识
        pipeline_id: 目标管道 ID（投递地址）
        target_id: 目标实体 ID（如任务 ID）
        content: 消息内容
        priority: 优先级（数字越大越优先）
        created_at: 创建时间
        expires_at: 过期时间，None 表示不过期
        metadata: 扩展元数据
    """

    id: str
    pipeline_id: str
    target_id: str
    content: str
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        """检查消息是否已过期。"""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at


class MessageQueue:
    """异步安全的消息队列。

    管理待注入的消息，支持：
    - 按 pipeline_id 分组存储消息
    - 消息优先级排序（高优先级先出）— 使用 bisect.insort O(log n) 插入
    - 消息过期自动清理
    - asyncio.Lock 保护（不阻塞事件循环）

    使用示例::

        queue = MessageQueue()
        msg = Message(
            id=create_message_id(),
            pipeline_id="abc123",
            target_id="task_001",
            content="请检查任务状态",
            priority=10,
        )
        await queue.push(msg)
        popped = await queue.pop("abc123")

    Attributes:
        _queues: 按 pipeline_id 分组的消息列表
        _max_queue_size: 单个管道的最大队列长度
        _default_ttl: 消息默认过期秒数
        _lock: asyncio 异步锁
    """

    DEFAULT_MAX_QUEUE_SIZE = 100
    DEFAULT_MESSAGE_TTL = 3600  # 1 小时

    def __init__(
        self,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        default_ttl: int = DEFAULT_MESSAGE_TTL,
    ) -> None:
        """初始化消息队列。

        Args:
            max_queue_size: 单个管道队列最大长度，超出后移除最早消息
            default_ttl: 消息默认过期秒数
        """
        self._queues: dict[str, list[Message]] = defaultdict(list)
        self._max_queue_size = max_queue_size
        self._default_ttl = default_ttl
        self._lock = asyncio.Lock()

    async def push(self, message: Message) -> bool:
        """添加消息到队列。

        队列满时自动移除最低优先级的消息。消息无 expires_at 时
        使用 default_ttl 设置默认过期时间。使用 bisect.insort
        按 priority 降序插入（O(log n)）。

        Args:
            message: 要添加的消息

        Returns:
            添加成功返回 True
        """
        async with self._lock:
            pipeline_id = message.pipeline_id
            queue = self._queues[pipeline_id]

            if len(queue) >= self._max_queue_size:
                queue.pop(0)
                logger.warning(
                    "[MessageQueue] 队列已满，移除最早消息 | pipeline_id=%s",
                    pipeline_id,
                )

            if message.expires_at is None:
                message.expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=self._default_ttl
                )

            bisect.insort(queue, message, key=lambda m: -m.priority)

            logger.debug(
                "[MessageQueue] 消息已添加 | "
                "message_id=%s | pipeline_id=%s | "
                "priority=%d | queue_size=%d",
                message.id, pipeline_id, message.priority, len(queue),
            )

            return True

    async def pop(self, pipeline_id: str, target_id: str | None = None) -> Message | None:
        """弹出最高优先级的消息。

        支持 target_id 精确匹配。不指定 target_id 时弹出队首消息。

        Args:
            pipeline_id: 管道 ID
            target_id: 目标实体 ID，None 时弹出队首

        Returns:
            弹出的消息，无可用消息返回 None
        """
        async with self._lock:
            queue = self._queues.get(pipeline_id)
            if not queue:
                return None

            self._cleanup_expired(pipeline_id)

            queue = self._queues.get(pipeline_id)
            if not queue:
                return None

            if target_id is not None:
                target_index = None
                for i, msg in enumerate(queue):
                    if msg.target_id == target_id:
                        target_index = i
                        break
                if target_index is None:
                    return None
                message = queue.pop(target_index)
            else:
                message = queue.pop(0)

            logger.debug(
                "[MessageQueue] 消息已弹出 | "
                "message_id=%s | pipeline_id=%s | "
                "target_id=%s | remaining=%d",
                message.id, pipeline_id, target_id, len(queue),
            )

            return message

    async def peek(self, pipeline_id: str, target_id: str | None = None) -> Message | None:
        """查看最高优先级的消息（不移除）。

        支持 target_id 精确匹配。

        Args:
            pipeline_id: 管道 ID
            target_id: 目标实体 ID，None 时查看队首

        Returns:
            消息实例，无可用消息返回 None
        """
        async with self._lock:
            queue = self._queues.get(pipeline_id)
            if not queue:
                return None

            self._cleanup_expired(pipeline_id)

            queue = self._queues.get(pipeline_id)
            if not queue:
                return None

            if target_id is not None:
                for msg in queue:
                    if msg.target_id == target_id:
                        return msg
                return None

            return queue[0]

    async def get_all(self, pipeline_id: str, target_id: str | None = None) -> list[Message]:
        """获取管道的所有消息（不移除）。

        支持 target_id 过滤。

        Args:
            pipeline_id: 管道 ID
            target_id: 目标实体 ID，None 时返回全部

        Returns:
            消息列表
        """
        async with self._lock:
            self._cleanup_expired(pipeline_id)
            messages = list(self._queues.get(pipeline_id, []))
            if target_id is not None:
                messages = [m for m in messages if m.target_id == target_id]
            return messages

    async def clear(self, pipeline_id: str) -> int:
        """清空管道的消息队列。

        Args:
            pipeline_id: 管道 ID

        Returns:
            清除的消息数量
        """
        async with self._lock:
            if pipeline_id not in self._queues:
                return 0

            count = len(self._queues[pipeline_id])
            del self._queues[pipeline_id]

            logger.debug(
                "[MessageQueue] 队列已清空 | pipeline_id=%s | cleared=%d",
                pipeline_id, count,
            )

            return count

    async def size(self, pipeline_id: str) -> int:
        """获取管道的消息数量。

        Args:
            pipeline_id: 管道 ID

        Returns:
            有效消息数量
        """
        async with self._lock:
            self._cleanup_expired(pipeline_id)
            return len(self._queues.get(pipeline_id, []))

    async def get_statistics(self) -> dict[str, Any]:
        """获取队列统计信息。"""
        async with self._lock:
            total_messages = sum(len(q) for q in self._queues.values())
            pipelines = list(self._queues.keys())

            return {
                "total_pipelines": len(pipelines),
                "total_messages": total_messages,
                "pipelines": {
                    pid: len(queue) for pid, queue in self._queues.items()
                },
                "max_queue_size": self._max_queue_size,
                "default_ttl": self._default_ttl,
            }

    def _cleanup_expired(self, pipeline_id: str) -> int:
        """清理指定管道的过期消息。

        必须在持有锁的上下文中调用。

        Args:
            pipeline_id: 管道 ID

        Returns:
            清理的过期消息数量
        """
        if pipeline_id not in self._queues:
            return 0

        queue = self._queues[pipeline_id]
        original_size = len(queue)

        self._queues[pipeline_id] = [m for m in queue if not m.is_expired()]

        cleaned = original_size - len(self._queues[pipeline_id])

        if cleaned > 0:
            logger.debug(
                "[MessageQueue] 清理过期消息 | pipeline_id=%s | cleaned=%d",
                pipeline_id, cleaned,
            )

        return cleaned


def create_message_id() -> str:
    """创建消息 ID。"""
    return f"msg_{uuid.uuid4().hex[:12]}"
