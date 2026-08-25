"""
任务状态机模块 - 提供通用的简单状态机实现。

- 统一为 SimpleStateMachine（所有状态转换逻辑集中在此模块）
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
