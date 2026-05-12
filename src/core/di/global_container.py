"""
全局容器管理器

提供全局容器的初始化和访问
"""

import logging

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
    global _global_container

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
    global _global_container
    _global_container = container
    logger.info("Global DI container set")


def reset_global_container() -> None:
    """重置全局容器（主要用于测试）"""
    global _global_container
    _global_container = None
    logger.info("Global DI container reset")


async def dispose_global_container() -> None:
    """销毁全局容器"""
    global _global_container

    if _global_container is not None:
        await _global_container.dispose()
        _global_container = None
        logger.info("Global DI container disposed")
