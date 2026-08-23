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
