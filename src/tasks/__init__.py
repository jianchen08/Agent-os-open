"""
任务管理模块

提供任务执行闭环的核心功能：
- TaskStateMachine: 任务状态机
- TaskProgressTracker: 进度追踪器
- TimerManager: 计时器管理器

注意：
- ACEvaluator 已合并到 EvaluationService，请使用：
    from tasks.services.evaluation import EvaluationService
- AutoExecuteWatchdog 已移除，请使用统一决策引擎：
    from agents.decision import DecisionEngineFactory
    engine = DecisionEngineFactory.create_engine()
"""

from .progress import L3ProgressManager, L3Subtask, TaskProgressTracker
from .state_machine import TaskStateMachine, get_task_state_machine

# TimerManager 延迟导入 — 依赖 src.config.system_config，可能不存在
def __getattr__(name: str):
    if name in ("TimerManager", "TimerState", "TimerStatus"):
        from . import timer_manager
        return getattr(timer_manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "TaskStateMachine",
    "get_task_state_machine",
    "TaskProgressTracker",
    "L3Subtask",
    "L3ProgressManager",
    "TimerManager",
    "TimerState",
    "TimerStatus",
]
