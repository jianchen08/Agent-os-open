"""
事件流模块

提供事件流和事件处理功能。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from collections import defaultdict
import asyncio


@dataclass
class StreamEvent:
    """流事件"""
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    source: str | None = None


class EventStream:
    """事件流管理器"""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._event_queue: asyncio.Queue = asyncio.Queue()

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """订阅事件"""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """取消订阅"""
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
            except ValueError:
                pass

    async def emit(self, event: StreamEvent) -> None:
        """发送事件"""
        await self._event_queue.put(event)
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception:
                pass

    async def get_event(self) -> StreamEvent | None:
        """获取事件"""
        try:
            return await self._event_queue.get()
        except Exception:
            return None


_event_stream: EventStream | None = None


def init_event_stream() -> EventStream:
    """初始化事件流"""
    global _event_stream
    _event_stream = EventStream()
    return _event_stream


def get_event_stream() -> EventStream:
    """获取事件流实例"""
    global _event_stream
    if _event_stream is None:
        _event_stream = EventStream()
    return _event_stream


async def emit_event(event_type: str, data: dict[str, Any], source: str | None = None) -> None:
    """发送事件的便捷函数"""
    event = StreamEvent(event_type=event_type, data=data, source=source)
    stream = get_event_stream()
    await stream.emit(event)
