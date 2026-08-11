"""
统一执行结果模块

提供统一的执行结果类型定义，所有执行结果继承自 ExecutionResult 基类。

主要类型：
- ExecutionResult: 执行结果基类
- AgentExecutionResult: Agent 执行结果
- ToolExecutionResult: 工具执行结果
- EvaluationExecutionResult: 评估执行结果
- ToolCallRecord: 工具调用记录
"""

from core.results.agent import AgentExecutionResult
from core.results.base import ExecutionResult
from core.results.evaluation import EvaluationExecutionResult, EvaluationStatus
from core.results.tool import ToolExecutionResult
from core.results.tool_call import ToolCallRecord

__all__ = [
    # 基类
    "ExecutionResult",
    # Agent 执行结果
    "AgentExecutionResult",
    # 工具执行结果
    "ToolExecutionResult",
    # 评估执行结果
    "EvaluationExecutionResult",
    "EvaluationStatus",
    # 工具调用记录
    "ToolCallRecord",
]
