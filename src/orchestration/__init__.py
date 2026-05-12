"""编排中心模块

负责任务调度、资源管理和执行协调的统一入口。
"""

from src.core.states import ExecutionStatus
from src.orchestration.concurrency_manager import ConcurrencyManager
from src.core.exceptions import (
    OrchestrationError,
    ResourceExhaustedError,
    SubAgentNestingError,
    TaskExecutionError,
    TaskNotFoundError,
)
from src.orchestration.executor_factory import ExecutorFactory
from src.orchestration.phase import PhaseStatus, TaskPhase, TaskPhaseController
from src.orchestration.recovery import (
    RecoveryConfig,
    RecoveryResult,
    TaskRecoveryOrchestrator,
    restore_tasks_on_startup,
)
from src.orchestration.resource_manager import ResourceManager
from src.orchestration.scheduler import (
    Scheduler,
    get_global_scheduler,
    schedule,
    start_global_scheduler,
    stop_global_scheduler,
)
from src.orchestration.task_orchestrator import (
    DependencyResolution,
    TaskOrchestrator,
    get_task_orchestrator,
    stop_task_orchestrator,
)
from src.orchestration.types import (
    AgentLevel,
    ResourceAllocation,
    ResourceQuota,
    TargetType,
    TaskPriority,
    TaskRequest,
    TaskResult,
)

# TaskClient 和 TaskClientFactory 延迟导入以避免循环依赖
# 使用: from src.orchestration.task_client import TaskClient, TaskClientFactory

# AgentExecutor 延迟导入以避免循环依赖（依赖 src.agents.loop）
# 使用: from src.orchestration.agent_executor import AgentExecutor, get_global_executor

__all__ = [
    "AgentLevel",
    "TaskPriority",
    "ExecutionStatus",
    "TargetType",
    "ResourceQuota",
    "TaskRequest",
    "TaskResult",
    "ResourceAllocation",
    "OrchestrationError",
    "TaskNotFoundError",
    "ResourceExhaustedError",
    "TaskExecutionError",
    "SubAgentNestingError",
    "Scheduler",
    "schedule",
    "ExecutorFactory",
    "ResourceManager",
    "ConcurrencyManager",
    "TaskOrchestrator",
    "DependencyResolution",
    "AgentExecutor",
    "get_global_executor",
    "RecoveryConfig",
    "RecoveryResult",
    "TaskRecoveryOrchestrator",
    "restore_tasks_on_startup",
    "PhaseStatus",
    "TaskPhase",
    "TaskPhaseController",
    "get_scheduler",
    "get_global_scheduler",
    "start_global_scheduler",
    "stop_global_scheduler",
    "get_task_orchestrator",
    "stop_task_orchestrator",
]


def get_scheduler():
    """获取调度器实例

    Returns:
        Scheduler: 调度器实例
    """
    return get_global_scheduler()


def __getattr__(name: str):
    """延迟导入 AgentExecutor 和 get_global_executor 以避免循环依赖"""
    if name == "AgentExecutor":
        from src.orchestration.agent_executor import AgentExecutor

        return AgentExecutor
    if name == "get_global_executor":
        from src.orchestration.agent_executor import get_global_executor

        return get_global_executor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
