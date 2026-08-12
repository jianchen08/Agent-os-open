"""TaskService 获取的统一入口。

M3（2026-08-01）：插件自包含实例化 TaskService，不再依赖 channel_api 进程的
ServiceProvider 单例。TaskService 所需的 mixin（_task_cleanup/_task_crud/_task_state）
已复制到本插件包，event_bus 经核实是装饰性（赋值后从不读取），故 event_bus=None 安全。

公共接口：
- get_task_service() -> Any: 获取 TaskService 实例（进程内单例，懒加载）
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["get_task_service"]

# 进程内单例（替代原 ServiceProvider.get_or_create 的缓存语义）。
_task_service_instance: Any = None


def get_task_service() -> Any:
    """获取 TaskService 实例（进程内单例，懒加载）。

    M3 后插件自包含：直接实例化 tasks.service.TaskService（mixin 已在本包），
    不再调 infrastructure.service_provider。event_bus=None（装饰性，从不读取）。

    Returns:
        TaskService 实例，初始化失败时返回 None
    """
    global _task_service_instance  # noqa: PLW0603
    if _task_service_instance is not None:
        return _task_service_instance
    try:
        # 显式按 tasks 包限定，避免裸 `from service import` 在 channel_api 进程中被
        # sys.path 上的 tools/human/service.py 误解析（后者 `from models import Priority`
        # 会撞上 channel_api/models.py 无 Priority → ImportError → TaskService 永远为 None）。
        # 另：tasks.service 及其 mixin 使用平铺兄弟导入（_task_cleanup/state_machine/...），
        # 需 tasks/ 本身也在 sys.path 上；channel_api 仅把 system/ 入列，故此处补齐本目录。
        import os
        import sys
        _tasks_dir = os.path.dirname(os.path.abspath(__file__))
        if _tasks_dir not in sys.path:
            sys.path.insert(0, _tasks_dir)
        from tasks.service import TaskService  # noqa: PLC0415

        _task_service_instance = TaskService()
        return _task_service_instance
    except Exception as exc:
        logger.warning(
            "get_task_service: TaskService 初始化失败，将返回 None | error=%s",
            exc,
        )
        return None
