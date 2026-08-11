"""
执行状态枚举定义

暴露接口：
- is_terminal(self) -> bool：is_terminal功能
- is_active(self) -> bool：is_active功能
- is_waiting(self) -> bool：is_waiting功能
- is_success(self) -> bool：is_success功能
- is_failure(self) -> bool：is_failure功能
- ExecutionStatus：ExecutionStatus类
"""

from enum import Enum


class ExecutionStatus(str, Enum):
    """
    统一执行状态

    用于 Task、Agent、Workflow 等执行实体的状态管理。
    提供状态属性判断（终态、活跃、等待、成功、失败）。

    状态说明:
        - PENDING: 待执行，任务已创建但尚未开始
        - SCHEDULED: 已调度，任务已安排执行时间
        - RUNNING: 执行中，任务正在执行
        - EVALUATING: 评估中，任务正在评估执行结果
        - SUSPENDED: 暂停，任务被暂停等待恢复
        - BLOCKED: 阻塞，任务被阻塞等待依赖解决
        - COMPLETED: 已完成，任务执行成功
        - FAILED: 失败，任务执行失败
        - CANCELLED: 已取消，任务被取消
        - TIMEOUT: 超时，任务执行超时

    Example:
        >>> status = ExecutionStatus.RUNNING
        >>> status.is_active
        True
        >>> status.is_terminal
        False
    """

    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    EVALUATING = "evaluating"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        """是否为终态"""
        return self in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        }

    @property
    def is_active(self) -> bool:
        """是否为活跃状态（正在执行）"""
        return self in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.EVALUATING,
            ExecutionStatus.SCHEDULED,
        }

    @property
    def is_waiting(self) -> bool:
        """是否为等待状态"""
        return self in {
            ExecutionStatus.PENDING,
            ExecutionStatus.SUSPENDED,
            ExecutionStatus.BLOCKED,
        }

    @property
    def is_success(self) -> bool:
        """是否为成功终态"""
        return self == ExecutionStatus.COMPLETED

    @property
    def is_failure(self) -> bool:
        """是否为失败终态"""
        return self in {
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        }


# 执行状态转换规则
# 定义每个状态可以合法转换到的目标状态列表
EXECUTION_TRANSITIONS: dict[ExecutionStatus, list[ExecutionStatus]] = {
    ExecutionStatus.PENDING: [
        ExecutionStatus.SCHEDULED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.SCHEDULED: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.RUNNING: [
        ExecutionStatus.EVALUATING,
        ExecutionStatus.SUSPENDED,
        ExecutionStatus.BLOCKED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.TIMEOUT,
    ],
    ExecutionStatus.EVALUATING: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.BLOCKED,
    ],
    ExecutionStatus.SUSPENDED: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.BLOCKED: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.COMPLETED: [],
    ExecutionStatus.FAILED: [
        ExecutionStatus.PENDING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.CANCELLED: [],
    ExecutionStatus.TIMEOUT: [
        ExecutionStatus.PENDING,
        ExecutionStatus.CANCELLED,
    ],
}
