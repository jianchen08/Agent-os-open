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



