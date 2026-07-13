"""
统一状态模块

提供统一的状态定义、状态转换规则和状态机实现。
用于 Task、Agent、Workflow 等执行实体的状态管理。
"""

from core.states.base import StateProtocol, StateTransition
from core.states.events import StateEvent, StateEventType
from core.states.execution import (
    EXECUTION_TRANSITIONS,
    ExecutionStatus,
)
from core.states.lifecycle import (
    LIFECYCLE_TRANSITIONS,
    LifecycleStatus,
)
from core.states.machine import StateMachine, StateMachineConfig

__all__ = [
    # 基础协议
    "StateProtocol",
    "StateTransition",
    # 执行状态
    "ExecutionStatus",
    "EXECUTION_TRANSITIONS",
    # 生命周期状态
    "LifecycleStatus",
    "LIFECYCLE_TRANSITIONS",
    # 状态事件
    "StateEvent",
    "StateEventType",
    # 状态机
    "StateMachine",
    "StateMachineConfig",
]
