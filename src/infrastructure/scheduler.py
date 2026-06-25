"""简化版调度器 — asyncio.Queue + 优先级排序。

精简原则：
- 去掉 priority^1.5 公式（过度设计）
- 去掉事件驱动+0.5s轮询 → 纯 asyncio.Queue
- 去掉公平算法 → 简单优先级排序
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(order=True)
class PriorityItem:
    """优先级队列项。

    数值越小优先级越高。item 不参与排序比较。

    Attributes:
        priority: 优先级数值，越小越先执行
        item: 队列项的实际数据
    """

    priority: int
    item: Any = field(compare=False)


class SchedulerStrategy(ABC):
    """调度策略接口。

    定义从等待队列中选择下一个执行项的策略。
    策略可插拔替换，默认实现为按优先级排序、同优先级 FIFO。
    """

    @abstractmethod
    def pick_next(self, waiting: list[PriorityItem]) -> PriorityItem | None:
        """从等待队列选择下一个执行项。

        Args:
            waiting: 当前等待队列

        Returns:
            被选中的项，队列为空时返回 None
        """


class DefaultSchedulerStrategy(SchedulerStrategy):
    """默认调度策略 — 按优先级排序，同优先级 FIFO。

    优先级数值越小越先执行；相同优先级按入队顺序（FIFO）返回。
    """

    def pick_next(self, waiting: list[PriorityItem]) -> PriorityItem | None:
        """按优先级排序后返回最优先项。

        Args:
            waiting: 当前等待队列

        Returns:
            优先级最高的项，队列为空时返回 None
        """
        if not waiting:
            return None
        waiting.sort(key=lambda x: x.priority)
        return waiting[0]


class Scheduler:
    """简化版调度器 — asyncio.PriorityQueue + 策略模式。

    使用 asyncio.PriorityQueue 管理待调度项，
    通过 SchedulerStrategy 决定选取策略。

    Attributes:
        _queue: 异步优先级队列
        _strategy: 调度策略实例
    """

    def __init__(self, strategy: SchedulerStrategy | None = None) -> None:
        """初始化调度器。

        Args:
            strategy: 调度策略，默认使用 DefaultSchedulerStrategy
        """
        self._queue: asyncio.PriorityQueue[PriorityItem] = asyncio.PriorityQueue()
        self._strategy = strategy or DefaultSchedulerStrategy()

    async def submit(self, item: Any, priority: int = 5) -> None:
        """提交待调度项。

        Args:
            item: 待调度的数据
            priority: 优先级数值，越小越先执行，默认 5（NORMAL）
        """
        await self._queue.put(PriorityItem(priority=priority, item=item))

    async def pick_next(self) -> Any | None:
        """获取下一个待执行项（非阻塞）。

        Returns:
            优先级最高的项数据，队列为空时返回 None
        """
        try:
            pitem = self._queue.get_nowait()
            return pitem.item
        except asyncio.QueueEmpty:
            return None

    @property
    def pending_count(self) -> int:
        """等待中的项目数。"""
        return self._queue.qsize()
