"""
事件模块

提供统一的事件流和事件处理功能。
"""

from src.core.events.event_stream import (
    EventStream,
    StreamEvent,
    emit_event,
    get_event_stream,
    init_event_stream,
)

__all__ = [
    "EventStream",
    "StreamEvent",
    "emit_event",
    "get_event_stream",
    "init_event_stream",
]
