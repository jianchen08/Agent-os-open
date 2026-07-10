"""
任务状态机模块 - 提供通用的简单状态机实现。

重构说明：
- 统一为 SimpleStateMachine，移除旧版 TaskStateMachine
- 所有状态转换逻辑集中在此模块
"""

from __future__ import annotations


class InvalidTransitionError(Exception):
    """非法状态转换异常。

    当尝试执行不允许的状态转换时抛出。

    Attributes:
        current_state: 当前状态。
        target_state: 目标状态。
        message: 错误描述信息。
    """

    def __init__(self, current_state: str, target_state: str, message: str = "") -> None:
        self.current_state = current_state
        self.target_state = target_state
        self.message = message or f"不允许从 '{current_state}' 转换到 '{target_state}'"
        super().__init__(self.message)


class SimpleStateMachine:
    """简单状态机。

    用于管理任务等实体的状态转换，支持定义合法转换规则和初始状态。

    Args:
        initial_state: 初始状态。
        transitions: 合法的状态转换规则，格式为 {当前状态: [允许的目标状态列表]}。

    Example::

        sm = SimpleStateMachine(
            initial_state="pending",
            transitions={
                "pending": ["running"],
                "running": ["completed", "failed"],
                "completed": ["pending"],
                "failed": [],
            },
        )
        sm.transition("running")   # pending -> running
        sm.transition("completed") # running -> completed
    """

    def __init__(
        self,
        initial_state: str,
        transitions: dict[str, list[str]],
    ) -> None:
        self._current_state = initial_state
        self._transitions = transitions

    @property
    def current_state(self) -> str:
        """获取当前状态。"""
        return self._current_state

    @property
    def transitions(self) -> dict[str, list[str]]:
        """获取转换规则。"""
        return self._transitions

    def can_transition(self, target_state: str) -> bool:
        """检查是否可以转换到目标状态。

        Args:
            target_state: 目标状态。

        Returns:
            是否允许转换。
        """
        allowed = self._transitions.get(self._current_state, [])
        return target_state in allowed

    def transition(self, target_state: str) -> None:
        """执行状态转换。

        Args:
            target_state: 目标状态。

        Raises:
            InvalidTransitionError: 当转换不被允许时抛出。
        """
        if not self.can_transition(target_state):
            raise InvalidTransitionError(self._current_state, target_state)
        self._current_state = target_state


# ---------------------------------------------------------------------------
# 向后兼容别名 & 工厂函数
# ---------------------------------------------------------------------------

# 旧代码中引用 TaskStateMachine，统一指向 SimpleStateMachine
TaskStateMachine = SimpleStateMachine

# 预定义的任务状态转换规则（7 种状态）
_TASK_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running", "stopped", "completed", "failed"],
    "running": ["evaluating", "completed", "failed", "stopped", "timeout"],
    "evaluating": ["running", "completed", "failed", "stopped"],
    "stopped": ["running", "pending"],
    "completed": ["pending"],
    "failed": ["pending", "running"],
    "timeout": ["running", "pending", "failed"],
}


def get_task_state_machine() -> SimpleStateMachine:
    """获取预配置的任务状态机实例。"""
    return SimpleStateMachine(
        initial_state="pending",
        transitions=_TASK_TRANSITIONS,
    )
