"""
事件总线工厂

提供事件总线的创建和全局单例管理
"""

import logging
from enum import Enum

from src.core.event_bus.base import EventBusBase
from src.core.event_bus.memory import InMemoryEventBus
from src.core.event_bus.redis_streams import RedisStreamsEventBus

logger = logging.getLogger(__name__)


class EventBusType(str, Enum):
    """事件总线类型"""

    REDIS_STREAMS = "redis_streams"
    MEMORY = "memory"


# 全局单例
_event_bus_instance: EventBusBase | None = None


def create_event_bus(
    bus_type: EventBusType = EventBusType.REDIS_STREAMS,
    redis_url: str | None = None,
    **kwargs,
) -> EventBusBase:
    """
    创建事件总线实例

    Args:
        bus_type: 事件总线类型
        redis_url: Redis 连接 URL（仅 Redis Streams 模式需要）
        **kwargs: 其他配置参数

    Returns:
        事件总线实例
    """
    if bus_type == EventBusType.REDIS_STREAMS:
        # 获取 Redis URL
        if redis_url is None:
            from src.config.settings import get_settings  # noqa: PLC0415

            settings = get_settings()
            redis_url = settings.redis_url

        return RedisStreamsEventBus(
            redis_url=redis_url,
            **kwargs,
        )

    if bus_type == EventBusType.MEMORY:
        return InMemoryEventBus(**kwargs)

    raise ValueError(f"不支持的事件总线类型: {bus_type}")


def get_event_bus(
    bus_type: EventBusType | None = None,
    redis_url: str | None = None,
    **kwargs,
) -> EventBusBase:
    """
    获取全局事件总线单例

    首次调用时创建实例，后续调用返回同一实例

    Args:
        bus_type: 事件总线类型（仅首次调用有效）
        redis_url: Redis 连接 URL（仅首次调用有效）
        **kwargs: 其他配置参数（仅首次调用有效）

    Returns:
        事件总线实例
    """
    global _event_bus_instance  # noqa: PLW0603

    if _event_bus_instance is None:
        from src.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()

        # 确定事件总线类型
        if bus_type is None:
            # 优先使用配置文件中的设置
            config_type = settings.event_bus_type.lower()
            if config_type == "memory":
                bus_type = EventBusType.MEMORY
            elif config_type == "redis_streams":
                bus_type = EventBusType.REDIS_STREAMS
            # 根据环境决定类型
            elif settings.environment == "test":
                bus_type = EventBusType.MEMORY
            else:
                bus_type = EventBusType.REDIS_STREAMS

        # 尝试创建 Redis 事件总线，失败则降级到内存模式
        if bus_type == EventBusType.REDIS_STREAMS:
            try:
                bus = create_event_bus(
                    bus_type=bus_type,
                    redis_url=redis_url,
                    **kwargs,
                )
                # 尝试连接
                import asyncio  # noqa: PLC0415

                try:
                    asyncio.get_running_loop()
                    # 如果能获取到运行中的循环，尝试连接
                    asyncio.create_task(bus.connect())
                    _event_bus_instance = bus
                    logger.info("[EventBus] Redis Streams 事件总线已创建")
                except RuntimeError:
                    # 没有运行中的事件循环，使用内存模式
                    _event_bus_instance = InMemoryEventBus(**kwargs)
                    logger.warning("[EventBus] 无事件循环，使用内存事件总线")
            except Exception as e:
                logger.warning(f"[EventBus] Redis 连接失败，降级到内存事件总线 | error={str(e)}")
                _event_bus_instance = InMemoryEventBus(**kwargs)
        else:
            _event_bus_instance = create_event_bus(
                bus_type=bus_type,
                redis_url=redis_url,
                **kwargs,
            )

        logger.info(f"创建全局事件总线: {bus_type.value}")

    return _event_bus_instance


def reset_event_bus() -> None:
    """
    重置全局事件总线（用于测试）
    """
    global _event_bus_instance  # noqa: PLW0603
    _event_bus_instance = None
    logger.debug("全局事件总线已重置")


async def shutdown_event_bus() -> None:
    """
    关闭全局事件总线
    """
    global _event_bus_instance  # noqa: PLW0603

    if _event_bus_instance is not None:
        await _event_bus_instance.disconnect()
        _event_bus_instance = None
        logger.info("全局事件总线已关闭")
