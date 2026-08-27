"""TaskService 获取的统一入口。

插件自包含实例化 TaskService（mixin _task_cleanup/_task_crud/_task_state 已
在本插件包内）；event_bus 是装饰性（赋值后从不读取），event_bus=None 安全。

公共接口：
- get_task_service() -> Any: 获取 TaskService 实例（进程内单例，懒加载）
- get_project_registry() -> Any: 获取项目登记簿（进程内单例，懒加载）
- reset_singletons() -> None: 清空单例（测试隔离用）
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["get_task_service", "get_project_registry", "reset_singletons"]

# 进程内单例（懒加载缓存）。
_task_service_instance: Any = None
_project_registry_instance: Any = None


def get_task_service() -> Any:
    """获取 TaskService 实例（进程内单例，懒加载）。

    插件自包含：直接实例化 tasks.service.TaskService（mixin 已在本包）。
    event_bus=None（装饰性，从不读取）。

    Returns:
        TaskService 实例，初始化失败时返回 None
    """
    global _task_service_instance  # noqa: PLW0603
    if _task_service_instance is not None:
        return _task_service_instance
    try:
        # 平铺导入（与 server.py 同款）：tasks 插件代码已平铺到本目录
        # （无 tasks/ 子包），`from service import TaskService` 直接命中。
        # 进程内单例（cache 命中后不再触碰 import，重复调用零开销）。
        from service import TaskService  # noqa: PLC0415
        _task_service_instance = TaskService()
        return _task_service_instance
    except Exception as exc:
        logger.warning(
            "get_task_service: TaskService 初始化失败，将返回 None | error=%s",
            exc,
        )
        return None


def get_project_registry() -> Any:
    """获取 ProjectRegistry 实例（进程内单例，懒加载）。

    Returns:
        ProjectRegistry 实例，初始化失败时返回 None
    """
    global _project_registry_instance  # noqa: PLW0603
    if _project_registry_instance is not None:
        return _project_registry_instance
    try:
        from projects import ProjectRegistry  # noqa: PLC0415

        _project_registry_instance = ProjectRegistry()
        return _project_registry_instance
    except Exception as exc:
        logger.warning(
            "get_project_registry: ProjectRegistry 初始化失败，将返回 None | error=%s",
            exc,
        )
        return None


def reset_singletons() -> None:
    """清空进程内单例（测试隔离用）。"""
    global _task_service_instance, _project_registry_instance  # noqa: PLW0603
    _task_service_instance = None
    _project_registry_instance = None
