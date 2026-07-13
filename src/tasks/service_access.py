"""TaskService 获取的统一入口。

所有模块通过此公共接口获取 TaskService 实例，
避免在各文件中重复定义 _get_task_service()。

公共接口：
- get_task_service() -> Any: 获取全局 TaskService 实例
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["get_task_service"]


def get_task_service() -> Any:
    """通过 ServiceProvider 获取全局 TaskService 实例。

    使用 get_or_create 支持懒加载创建，
    创建时注入 EventBus 确保 TaskService 功能完整。

    Returns:
        TaskService 实例，服务不可用或创建失败时返回 None
    """
    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415
        from tasks.service import TaskService  # noqa: PLC0415

        provider = get_service_provider()
        return provider.get_or_create(
            "task_service",
            lambda: TaskService(event_bus=provider.get("event_bus")),
        )
    except Exception as exc:
        logger.warning(
            "get_task_service: TaskService 初始化失败，将返回 None | error=%s",
            exc,
        )
        return None
