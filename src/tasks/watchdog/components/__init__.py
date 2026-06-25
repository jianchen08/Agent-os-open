"""
Watchdog 组件模块

提供任务监控、触发、超时处理、失败处理和项目控制等组件。
"""

from src.tasks.watchdog.components.monitor import TaskMonitor
from src.tasks.watchdog.components.trigger import TaskTrigger

# 延迟导入：以下组件可能不存在
try:
    from src.tasks.watchdog.components.failure_handler import (
        FailureHandler,
        FailureReason,
    )
except ImportError:
    FailureHandler = None  # type: ignore
    FailureReason = None  # type: ignore

try:
    from src.tasks.watchdog.components.project_controller import ProjectController
except ImportError:
    ProjectController = None  # type: ignore

try:
    from src.tasks.watchdog.components.timeout_handler import (
        SubtaskActivityStatus,
        TaskActivityStatus,
        TimeoutHandler,
    )
except ImportError:
    TimeoutHandler = None  # type: ignore
    TaskActivityStatus = None  # type: ignore
    SubtaskActivityStatus = None  # type: ignore

__all__ = [
    "TaskMonitor",
    "TaskTrigger",
    "TimeoutHandler",
    "FailureHandler",
    "FailureReason",
    "ProjectController",
    "TaskActivityStatus",
    "SubtaskActivityStatus",
]
