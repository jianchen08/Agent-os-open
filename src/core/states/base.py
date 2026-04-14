"""
基础状态协议和状态转换规则

暴露接口：
- value(self) -> str：value功能
- is_terminal(self) -> bool：is_terminal功能
- is_active(self) -> bool：is_active功能
- can_transition(self, from_state: T, to_state: T) -> bool：can_transition功能
- get_valid_transitions(self, state: T) -> list[T]：get_valid_transitions功能
- is_terminal(self, state: T) -> bool：is_terminal功能
- StateProtocol：StateProtocol类
- StateTransition：StateTransition类
"""

from enum import Enum
from typing import Generic, Protocol, TypeVar

T = TypeVar("T", bound=Enum)


class StateProtocol(Protocol[T]):
    """
    状态协议

    所有状态枚举应实现此协议，提供统一的状态属性访问接口。
    """

    @property
    def value(self) -> str:
        """状态的字符串值"""
        ...

    @property
    def is_terminal(self) -> bool:
        """是否为终态"""
        ...

    @property
    def is_active(self) -> bool:
        """是否为活跃状态"""
        ...


class StateTransition(Generic[T]):
    """
    状态转换规则

    管理状态之间的合法转换关系，提供转换验证和查询功能。

    Attributes:
        _transitions: 状态转换映射表，键为当前状态，值为可转换的目标状态列表
        _terminal_states: 终态集合，没有后续转换的状态

    Example:
        >>> transitions = {
        ...     Status.PENDING: [Status.RUNNING, Status.CANCELLED],
        ...     Status.RUNNING: [Status.COMPLETED, Status.FAILED],
        ...     Status.COMPLETED: [],
        ... }
        >>> rules = StateTransition(transitions)
        >>> rules.can_transition(Status.PENDING, Status.RUNNING)
        True
        >>> rules.is_terminal(Status.COMPLETED)
        True
    """

    def __init__(self, transitions: dict[T, list[T]]) -> None:
        """初始化状态转换规则"""
        self._transitions = transitions
        self._terminal_states: set[T] = set()

        # 识别终态：没有后续转换的状态
        for state, next_states in transitions.items():
            if not next_states:
                self._terminal_states.add(state)

    def can_transition(self, from_state: T, to_state: T) -> bool:
        """检查状态转换是否合法"""
        return to_state in self._transitions.get(from_state, [])

    def get_valid_transitions(self, state: T) -> list[T]:
        """获取指定状态的有效转换列表"""
        return self._transitions.get(state, []).copy()

    def is_terminal(self, state: T) -> bool:
        """检查状态是否为终态"""
        return state in self._terminal_states
