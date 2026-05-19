"""
性能监控（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PerformanceMonitor:
    """性能监控器（降级存根）"""

    def __init__(self):
        self._enabled = False

    def record_operation(
        self, operation_type: str, duration_ms: float, **kwargs: Any
    ) -> None:
        """记录操作（空操作）。"""
        pass

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息（返回空字典）。"""
        return {}

    def reset(self) -> None:
        """重置统计（空操作）。"""
        pass


_performance_monitor_instance: PerformanceMonitor | None = None


def get_performance_monitor() -> PerformanceMonitor:
    """获取性能监控器单例。"""
    global _performance_monitor_instance
    if _performance_monitor_instance is None:
        _performance_monitor_instance = PerformanceMonitor()
    return _performance_monitor_instance
