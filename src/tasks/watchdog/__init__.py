"""
Watchdog 模块

提供任务监控和自动执行功能。
"""

from src.tasks.watchdog.components import (
    FailureHandler,
    ProjectController,
    TaskMonitor,
    TaskTrigger,
    TimeoutHandler,
)

# 延迟导入：watchdog 模块可能不存在
try:
    from src.tasks.watchdog.watchdog import AutoExecuteWatchdog
except ImportError:
    AutoExecuteWatchdog = None  # type: ignore

__all__ = [
    "AutoExecuteWatchdog",
    "TaskMonitor",
    "TaskTrigger",
    "TimeoutHandler",
    "FailureHandler",
    "ProjectController",
]
