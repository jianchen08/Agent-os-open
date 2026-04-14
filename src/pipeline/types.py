"""管道核心类型定义。

包含所有枚举、常量、数据类和工厂函数，
供管道引擎、插件、路由表等模块共同使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any


class AgentLevel(Enum):
    """Agent 层级枚举。"""

    L1_MAIN = "l1_main"
    L2_SUBTASK = "l2_subtask"
    L3_ATOMIC = "l3_atomic"


class TaskPriority(IntEnum):
    """任务优先级枚举，数值越小优先级越高。"""

    CRITICAL = 1
    HIGH = 3
    NORMAL = 5
    LOW = 7
    BACKGROUND = 9


class TargetType(Enum):
    """核心执行目标类型。"""

    LLM_CALL = "llm_call"
    TOOL_EXECUTE = "tool_execute"


class StateKeys:
    """状态字典字段名常量。

    用于统一引用 state 中的键名，避免硬编码字符串。
    """

    ITERATION = "iteration"
    CORE_TYPE = "core_type"
    ENDED = "ended"
    SESSION_ID = "session_id"
    TASK_ID = "task_id"
    AGENT_LEVEL = "agent_level"
    RAW_RESULT = "raw_result"
    RAW_ERROR = "raw_error"
    RAW_TOOL_CALLS = "raw_tool_calls"
    RAW_THINKING = "raw_thinking"
    TOOL_RESULTS = "tool_results"
    EXECUTION_STATUS = "execution_status"
    ERROR_ANALYSIS = "error_analysis"
    TASK_COMPLETE = "task_complete"
    SHOULD_STOP = "should_stop"
    APPROVAL_REQUIRED = "approval_required"
    ROUTED_TO = "routed_to"
    WAIT_FOR = "wait_for"
    DELEGATION_RESULT = "delegation_result"
    DELEGATION_SCORE = "delegation_score"
    DELEGATION_ERROR = "delegation_error"
    PIPELINE_ID = "pipeline_id"


class ErrorPolicy(Enum):
    """插件错误处理策略。"""

    ABORT = "abort"
    SKIP = "skip"
    RETRY = "retry"
    FALLBACK = "fallback"


@dataclass
class RouteSignal:
    """路由信号数据类。

    由插件产生，经输出路由表仲裁后决定管道下一步走向。

    Attributes:
        route_type: 路由类型，支持 next_llm / next_tool / end / delegate / wait
        target: 路由目标，可为字符串、字符串列表或 None
        reason: 路由原因描述
        payload: 附加数据
    """

    route_type: str
    target: str | list[str] | None = None
    reason: str = ""
    payload: dict[str, Any] | None = None


def create_initial_state(**overrides: Any) -> dict[str, Any]:
    """创建管道初始状态字典。

    Args:
        **overrides: 用于覆盖默认值的关键字参数。

    Returns:
        包含所有必要初始字段的管道状态字典。
    """
    state: dict[str, Any] = {
        StateKeys.ITERATION: 0,
        StateKeys.CORE_TYPE: TargetType.LLM_CALL.value,
        StateKeys.ENDED: False,
        StateKeys.SESSION_ID: "",
        StateKeys.TASK_ID: "",
        StateKeys.AGENT_LEVEL: AgentLevel.L1_MAIN.value,
        StateKeys.RAW_RESULT: None,
        StateKeys.RAW_ERROR: None,
        StateKeys.RAW_TOOL_CALLS: [],
        StateKeys.RAW_THINKING: None,
        StateKeys.TOOL_RESULTS: [],
        StateKeys.EXECUTION_STATUS: "pending",
        StateKeys.ERROR_ANALYSIS: None,
        StateKeys.TASK_COMPLETE: False,
        StateKeys.SHOULD_STOP: False,
        StateKeys.APPROVAL_REQUIRED: False,
    }
    state.update(overrides)
    return state
