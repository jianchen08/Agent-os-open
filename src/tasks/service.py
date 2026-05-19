"""
任务服务模块 - 提供任务的业务逻辑处理。

重构说明：
- 状态机逻辑已迁移到 state_machine.py
- 本模块仅包含 TaskService
"""

from __future__ import annotations

from src.tasks.state_machine import SimpleStateMachine


# 默认任务状态转换规则
DEFAULT_TASK_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running"],
    "running": ["completed", "failed", "cancelled"],
    "completed": [],
    "failed": ["pending"],
    "cancelled": [],
}


class TaskService:
    """任务服务类。

    负责任务的创建、状态管理等相关业务逻辑。
    状态转换委托给 SimpleStateMachine 处理。

    Args:
        task_id: 任务唯一标识。
        initial_state: 任务初始状态，默认为 "pending"。
    """

    def __init__(self, task_id: str, initial_state: str = "pending") -> None:
        self.task_id = task_id
        self._state_machine = SimpleStateMachine(
            initial_state=initial_state,
            transitions=DEFAULT_TASK_TRANSITIONS,
        )

    @property
    def state(self) -> str:
        """获取当前任务状态。"""
        return self._state_machine.current_state

    def advance(self, target_state: str) -> None:
        """推进任务到目标状态。

        Args:
            target_state: 目标状态。

        Raises:
            InvalidTransitionError: 当状态转换不被允许时。
        """
        self._state_machine.transition(target_state)
