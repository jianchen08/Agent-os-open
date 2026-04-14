"""
任务状态机

暴露接口：
- get_task_state_machine() -> TaskStateMachine：get_task_state_machine功能
- can_transition(self, from_status: str, to_status: str) -> bool：can_transition功能
- get_valid_transitions(self, status: str) -> list[str]：get_valid_transitions功能
- is_terminal_state(self, status: str) -> bool：is_terminal_state功能
- get_status_description(self, status: str) -> str：get_status_description功能
- get_next_logical_status(self, current_status: str) -> str | None：get_next_logical_status功能
- TaskStateMachine：TaskStateMachine类
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import update

from core.states import ExecutionStatus

if TYPE_CHECKING:
    from db.models import Task

logger = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """非法状态转换异常。

    当任务状态转换不符合预定义规则时抛出。

    Attributes:
        from_status: 当前状态
        to_status: 目标状态
        message: 错误描述
    """

    def __init__(self, from_status: str, to_status: str, message: str = "") -> None:
        """初始化异常。

        Args:
            from_status: 当前状态
            to_status: 目标状态
            message: 自定义错误信息
        """
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            message or f"不允许从 {from_status} 转换到 {to_status}"
        )


class TaskStateMachine:
    """
    任务状态机

    统一管理状态转换，确保状态变更符合业务规则。

    核心功能：
    1. 验证状态转换是否合法
    2. 执行状态转换并记录原因
    3. 提供有效转换列表

    Example:
        >>> state_machine = TaskStateMachine()
        >>> state_machine.can_transition("pending", "running")
        True
        >>> state_machine.can_transition("completed", "running")
        False
    """

    # 状态转换规则定义
    # key: 当前状态, value: 允许转换到的状态列表
    # 使用 ExecutionStatus 的 value 值
    # 重要：任务状态变更为 completed 只能通过评估服务 (EvaluationService.complete_task_after_evaluation)
    # 正常流程: pending -> running -> evaluating -> completed
    # 长期任务: pending -> evaluating -> completed (直接评估)
    # 长期任务部分通过: evaluating -> pending (回到等待状态)
    # AC重试耗尽后，任务进入 failed 状态
    # blocked 状态保留，以后用于人工审批场景
    TRANSITIONS: dict[str, list[str]] = {
        "pending": ["scheduled", "running", "evaluating", "suspended", "cancelled"],
        "scheduled": ["running", "suspended", "cancelled"],
        "running": [
            "evaluating",
            "suspended",
            "blocked",
            "completed",
            "failed",
            "cancelled",
        ],
        "evaluating": ["running", "completed", "failed", "blocked", "pending"],
        "suspended": ["running", "cancelled"],
        "blocked": ["running", "completed", "cancelled"],
        "completed": [],
        "failed": ["pending", "cancelled"],
        "cancelled": [],
        "timeout": ["pending", "cancelled"],
    }

    TERMINAL_STATES = {"completed", "cancelled", "timeout"}

    def can_transition(self, from_status: str, to_status: str) -> bool:
        """检查状态转换是否合法"""
        valid_transitions = self.TRANSITIONS.get(from_status, [])
        return to_status in valid_transitions

    def get_valid_transitions(self, status: str) -> list[str]:
        """获取指定状态的所有有效转换"""
        return self.TRANSITIONS.get(status, []).copy()

    def is_terminal_state(self, status: str) -> bool:
        """检查是否为终态"""
        return status in self.TERMINAL_STATES

    async def transition(
        self,
        task: "Task",
        to_status: str,
        reason: str | None = None,
        session=None,
    ) -> "Task":
        """执行状态转换"""
        from_status = task.status

        # 验证转换是否合法
        if not self.can_transition(from_status, to_status):
            valid = self.get_valid_transitions(from_status)
            raise ValueError(
                f"非法状态转换: {from_status} -> {to_status}。"
                f"当前状态可转换为: {valid}"
            )

        # 记录转换日志
        logger.info(
            f"[TaskStateMachine] 状态转换 | "
            f"task_id={task.id} | "
            f"from={from_status} | to={to_status} | "
            f"reason={reason or 'N/A'}"
        )

        # 更新任务状态
        task.status = to_status
        task.updated_at = datetime.now(UTC)

        # 更新元数据中的状态变更记录
        if task.task_metadata is None:
            task.task_metadata = {}

        # 特殊状态处理
        if to_status == ExecutionStatus.COMPLETED.value:
            task.completed_at = datetime.now(UTC)
        elif to_status == ExecutionStatus.CANCELLED.value:
            task.task_metadata["cancelled_at"] = datetime.now(UTC).isoformat()
            if reason:
                task.task_metadata["cancel_reason"] = reason

        # 如果提供了 session，执行数据库更新
        if session is not None:
            update_data = {
                "status": to_status,
                "updated_at": task.updated_at,
                "task_metadata": task.task_metadata,
            }

            if to_status == ExecutionStatus.COMPLETED.value:
                update_data["completed_at"] = task.completed_at

            await session.execute(
                update(type(task)).where(type(task).id == task.id).values(**update_data)
            )
            await session.flush()

        return task

    def get_status_description(self, status: str) -> str:
        """获取状态的中文描述"""
        descriptions = {
            "pending": "待执行",
            "scheduled": "已调度",
            "running": "执行中",
            "evaluating": "评估中",
            "suspended": "已暂停",
            "blocked": "阻塞（需人工审批）",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "timeout": "超时",
        }
        return descriptions.get(status, f"未知状态: {status}")

    def get_next_logical_status(self, current_status: str) -> str | None:
        """获取下一个逻辑状态（用于流程推进）"""
        # 定义正常流程的状态推进
        flow_sequence = {
            "pending": "running",
            "running": "evaluating",
            "evaluating": "completed",
        }
        return flow_sequence.get(current_status)


# 全局状态机实例（单例模式）
_state_machine_instance: TaskStateMachine | None = None


def get_task_state_machine() -> TaskStateMachine:
    """获取任务状态机实例（单例）"""
    global _state_machine_instance
    if _state_machine_instance is None:
        _state_machine_instance = TaskStateMachine()
    return _state_machine_instance
