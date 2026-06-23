"""
全局容器管理器

提供全局容器的初始化和访问
"""

import logging
from typing import Any

from src.core.di.container import Container

logger = logging.getLogger(__name__)

# 全局容器实例
_global_container: Container | None = None


def get_global_container() -> Container:
    """
    获取全局容器实例

    如果容器不存在，会创建一个新的容器

    Returns:
        全局容器实例
    """
    global _global_container  # noqa: PLW0603

    if _global_container is None:
        _global_container = Container()
        logger.info("Global DI container initialized")

    return _global_container


def set_global_container(container: Container) -> None:
    """
    设置全局容器实例

    Args:
        container: 容器实例
    """
    global _global_container  # noqa: PLW0603
    _global_container = container
    logger.info("Global DI container set")


def reset_global_container() -> None:
    """重置全局容器（主要用于测试）"""
    global _global_container  # noqa: PLW0603
    _global_container = None
    logger.info("Global DI container reset")


def get_service(name: str, default: Any = None) -> Any:
    """便捷获取服务实例，封装 ServiceProvider 样板代码。

    封装 get_service_provider() + provider.get(name) + try/except，
    使调用方无需编写重复的异常处理代码。

    Args:
        name: 服务名称（如 "maintenance_service"）
        default: 服务不存在或异常时的返回值

    Returns:
        服务实例，未找到时返回 default
    """
    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        result = provider.get(name)
        return result if result is not None else default
    except (ImportError, AttributeError, KeyError) as exc:
        logger.warning("[DI] get_service('%s') 失败，返回默认值: %s", name, exc)
        return default


async def dispose_global_container() -> None:
    """销毁全局容器"""
    global _global_container  # noqa: PLW0603

    if _global_container is not None:
        await _global_container.dispose()
        _global_container = None
        logger.info("Global DI container disposed")
