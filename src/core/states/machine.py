"""
统一状态机实现

暴露接口：
- can_transition(self, from_state: T, to_state: T) -> bool：can_transition功能
- get_valid_transitions(self, state: T) -> list[T]：get_valid_transitions功能
- is_terminal(self, state: T) -> bool：is_terminal功能
- StateMachineConfig：StateMachineConfig类
- StateMachine：StateMachine类
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from core.states.base import StateTransition
from core.states.events import StateEvent

T = TypeVar("T")


@dataclass
class StateMachineConfig(Generic[T]):
    """
    状态机配置

    配置状态机的转换规则和生命周期钩子。

    Attributes:
        transitions: 状态转换规则映射表
        on_enter: 进入状态时的回调函数映射
        on_exit: 退出状态时的回调函数映射
        on_transition: 状态转换时的回调函数

    Example:
        >>> async def on_running(event: StateEvent) -> None:
        ...     print(f"进入运行状态: {event}")
        >>> config = StateMachineConfig(
        ...     transitions=EXECUTION_TRANSITIONS,
        ...     on_enter={ExecutionStatus.RUNNING: on_running},
        ... )
    """

    transitions: dict[T, list[T]]
    on_enter: dict[T, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    on_exit: dict[T, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    on_transition: Callable[[StateEvent], Awaitable[None]] | None = None


class StateMachine(Generic[T]):
    """
    通用状态机

    支持状态转换验证、生命周期钩子、事件通知。

    功能特性:
        - 状态转换合法性验证
        - 进入/退出状态的生命周期钩子
        - 状态转换事件通知
        - 详细的日志记录

    Example:
        >>> config = StateMachineConfig(transitions=EXECUTION_TRANSITIONS)
        >>> machine = StateMachine(config)
        >>> machine.can_transition(ExecutionStatus.PENDING, ExecutionStatus.RUNNING)
        True
        >>> event = await machine.transition(
        ...     entity_type="task",
        ...     entity_id="task-123",
        ...     from_state=ExecutionStatus.PENDING,
        ...     to_state=ExecutionStatus.RUNNING,
        ...     reason="开始执行",
        ... )
    """

    def __init__(self, config: StateMachineConfig[T]) -> None:
        """初始化状态机"""
        self._config = config
        self._transition_rules = StateTransition(config.transitions)
        self._logger = logging.getLogger(__name__)

    def can_transition(self, from_state: T, to_state: T) -> bool:
        """检查状态转换是否合法"""
        return self._transition_rules.can_transition(from_state, to_state)

    def get_valid_transitions(self, state: T) -> list[T]:
        """获取指定状态的有效转换列表"""
        return self._transition_rules.get_valid_transitions(state)

    def is_terminal(self, state: T) -> bool:
        """检查状态是否为终态"""
        return self._transition_rules.is_terminal(state)

    async def transition(
        self,
        entity_type: str,
        entity_id: str,
        from_state: T,
        to_state: T,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StateEvent:
        """执行状态转换"""
        # 验证转换合法性
        if not self.can_transition(from_state, to_state):
            valid = self.get_valid_transitions(from_state)
            raise ValueError(
                f"非法状态转换: {from_state.value} -> {to_state.value}。当前状态可转换为: {[s.value for s in valid]}"
            )

        # 创建状态变更事件
        event = StateEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            from_state=from_state.value,  # type: ignore
            to_state=to_state.value,  # type: ignore
            reason=reason,
            metadata=metadata or {},
        )

        # 记录日志
        self._logger.info(
            f"[StateMachine] 状态转换 | "
            f"{entity_type}={entity_id} | "
            f"{from_state.value} -> {to_state.value} | "  # type: ignore
            f"reason={reason or 'N/A'}"
        )

        # 执行退出状态钩子
        if self._config.on_exit and from_state in self._config.on_exit:
            await self._config.on_exit[from_state](event)

        # 执行进入状态钩子
        if self._config.on_enter and to_state in self._config.on_enter:
            await self._config.on_enter[to_state](event)

        # 执行转换回调
        if self._config.on_transition:
            await self._config.on_transition(event)

        return event
