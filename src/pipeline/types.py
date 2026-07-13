"""管道核心类型定义。

只包含管道框架自身的类型。Agent 层级（AgentLevel）定义在 agents.types，
任务优先级（TaskPriority）定义在 tasks.types，管道需要时通过参数传入。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


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
    LLM_ERROR_HISTORY = "llm_error_history"
    TASK_COMPLETE = "task_complete"
    SHOULD_STOP = "should_stop"
    APPROVAL_REQUIRED = "approval_required"
    ROUTED_TO = "routed_to"
    WAIT_FOR = "wait_for"
    DELEGATION_RESULT = "delegation_result"
    DELEGATION_SCORE = "delegation_score"
    DELEGATION_ERROR = "delegation_error"
    PIPELINE_ID = "pipeline_id"
    CONVERSATION_MODE = "conversation_mode"
    CONVERSATION_ROUND = "conversation_round"
    ATTACHMENTS = "attachments"


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
        route_type: 路由类型，支持 next_llm / next_tool / end / delegate / wait / decision
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
        StateKeys.AGENT_LEVEL: "L1",
        StateKeys.RAW_RESULT: None,
        StateKeys.RAW_ERROR: None,
        StateKeys.RAW_TOOL_CALLS: [],
        StateKeys.RAW_THINKING: None,
        StateKeys.TOOL_RESULTS: [],
        StateKeys.EXECUTION_STATUS: "pending",
        StateKeys.ERROR_ANALYSIS: None,
        StateKeys.LLM_ERROR_HISTORY: [],
        StateKeys.TASK_COMPLETE: False,
        StateKeys.SHOULD_STOP: False,
        StateKeys.APPROVAL_REQUIRED: False,
        StateKeys.CONVERSATION_MODE: False,
        StateKeys.CONVERSATION_ROUND: 0,
    }
    state.update(overrides)
    return state
