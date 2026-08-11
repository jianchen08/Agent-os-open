"""轻量级进程内事件总线。

支持 emit / subscribe / unsubscribe 模式，
用于管道间异步通信（如子管道完成通知父管道恢复）。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    """轻量级进程内事件总线。

    Usage::

        bus = EventBus()

        async def on_completed(data: dict) -> None:
            print(f"Pipeline {data['pipeline_id']} completed")

        bus.subscribe("pipeline_completed", on_completed)
        await bus.emit("pipeline_completed", {"pipeline_id": "p-1"})
        bus.unsubscribe("pipeline_completed", on_completed)
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., Coroutine[Any, Any, None]]]] = defaultdict(list)

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """发射事件，通知所有订阅者。

        按订阅顺序依次调用回调，某个回调异常不影响后续回调执行。

        Args:
            event_type: 事件类型标识
            data: 事件数据字典
        """
        callbacks = self._subscribers.get(event_type, [])
        if not callbacks:
            logger.debug("No subscribers for event: %s", event_type)
            return

        logger.debug("Emitting event %s to %d subscribers", event_type, len(callbacks))
        for callback in callbacks:
            try:
                result = callback(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error("Event callback error for %s: %s", event_type, exc, exc_info=True)

    def subscribe(self, event_type: str, callback: Callable[..., Coroutine[Any, Any, None]]) -> None:
        """订阅事件。

        Args:
            event_type: 事件类型标识
            callback: 异步回调函数，接收 dict 参数
        """
        self._subscribers[event_type].append(callback)
        logger.debug("Subscribed to %s: %s", event_type, callback.__name__)

    def unsubscribe(self, event_type: str, callback: Callable[..., Coroutine[Any, Any, None]]) -> None:
        """取消订阅。

        Args:
            event_type: 事件类型标识
            callback: 要移除的回调函数
        """
        if event_type in self._subscribers:
            self._subscribers[event_type] = [cb for cb in self._subscribers[event_type] if cb is not callback]
            logger.debug("Unsubscribed from %s: %s", event_type, callback.__name__)

    def has_subscribers(self, event_type: str) -> bool:
        """检查事件是否有订阅者。

        Args:
            event_type: 事件类型标识

        Returns:
            是否存在订阅者
        """
        return bool(self._subscribers.get(event_type))
