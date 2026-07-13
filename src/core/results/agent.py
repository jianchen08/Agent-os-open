"""
Agent 执行结果

暴露接口：
- to_dict(self) -> dict[str, Any]：to_dict功能
- total_tool_calls(self) -> int：total_tool_calls功能
- successful_tool_calls(self) -> int：successful_tool_calls功能
- AgentExecutionResult：AgentExecutionResult类
"""

from typing import Any

from pydantic import Field

from core.results.base import ExecutionResult
from core.results.tool_call import ToolCallRecord


class AgentExecutionResult(ExecutionResult[str]):
    """Agent 执行结果

    继承自 ExecutionResult 基类，添加 Agent 特有字段。

    特有字段：
    - iterations: 迭代次数
    - tool_calls: 工具调用记录
    - reasoning: 推理过程（可选）
    - agent_id: Agent ID
    - agent_name: Agent 名称

    Attributes:
        iterations: 迭代次数
        tool_calls: 工具调用记录列表
        reasoning: 推理过程（思考模式）
        agent_id: Agent ID
        agent_name: Agent 名称
    """

    # Agent 特有字段
    iterations: int = Field(default=0, ge=0, description="迭代次数")
    tool_calls: list[ToolCallRecord] = Field(default_factory=list, description="工具调用记录")
    reasoning: str | None = Field(default=None, description="推理过程（思考模式）")

    # Agent 标识
    agent_id: str | None = Field(default=None, description="Agent ID")
    agent_name: str | None = Field(default=None, description="Agent 名称")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        result = super().to_dict()
        result["iterations"] = self.iterations

        if self.tool_calls:
            result["tool_calls"] = [tc.model_dump() for tc in self.tool_calls]

        if self.reasoning:
            result["reasoning"] = self.reasoning

        if self.agent_id:
            result["agent_id"] = self.agent_id
        if self.agent_name:
            result["agent_name"] = self.agent_name

        return result

    @property
    def total_tool_calls(self) -> int:
        """工具调用总次数"""
        return len(self.tool_calls)

    @property
    def successful_tool_calls(self) -> int:
        """成功的工具调用次数"""
        return sum(1 for tc in self.tool_calls if tc.success)
