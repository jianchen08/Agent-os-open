"""
事件总线模块

基于 Redis Streams 的分布式事件总线，支持：
- 跨进程/跨实例事件通信
- 消费者组（负载均衡）
- 消息持久化和历史回溯
- 消息确认机制
"""

from src.core.event_bus.base import EventBusBase
from src.core.event_bus.factory import (
    EventBusType,
    create_event_bus,
    get_event_bus,
    reset_event_bus,
    shutdown_event_bus,
)
from src.core.event_bus.memory import InMemoryEventBus
from src.core.event_bus.redis_streams import RedisStreamsEventBus
from src.core.event_bus.types import (
    EventFilter,
    EventPriority,
    EventType,
    ExecutionEvent,
)

__all__ = [
    # 类型
    "EventType",
    "ExecutionEvent",
    "EventFilter",
    "EventPriority",
    # 实现
    "EventBusBase",
    "RedisStreamsEventBus",
    "InMemoryEventBus",
    # 工厂
    "create_event_bus",
    "get_event_bus",
    "reset_event_bus",
    "shutdown_event_bus",
    "EventBusType",
]
