"""
工具执行结果

暴露接口：
- data(self) -> Any | None：data功能
- duration(self) -> float：duration功能
- result(self) -> Any | None：result功能
- to_dict(self) -> dict[str, Any]：to_dict功能
- ToolExecutionResult：ToolExecutionResult类
"""

from typing import Any

from pydantic import Field

from core.results.base import ExecutionResult


class ToolExecutionResult(ExecutionResult[Any]):
    """工具执行结果

    继承自 ExecutionResult 基类，添加工具特有字段。

    特有字段：
    - tool_name: 工具名称
    - tool_id: 工具 ID
    - input_params: 输入参数

    兼容属性（用于向后兼容旧的 ToolResult）：
    - data: 等同于 output
    - duration: duration_ms 转换为秒
    - result: 等同于 output（废弃）

    Attributes:
        tool_name: 工具名称
        tool_id: 工具 ID
        input_params: 输入参数
    """

    # 工具标识
    tool_name: str | None = Field(default=None, description="工具名称")
    tool_id: str | None = Field(default=None, description="工具 ID")

    # 输入参数
    input_params: dict[str, Any] = Field(
        default_factory=dict,
        description="输入参数"
    )

    # === 兼容属性（向后兼容旧的 ToolResult）===

    @property
    def data(self) -> Any | None:
        """兼容旧字段名，等同于 output"""
        return self.output

    @property
    def duration(self) -> float:
        """兼容旧字段名，将 duration_ms 转换为秒"""
        if self.duration_ms is not None:
            return self.duration_ms / 1000.0
        return 0.0

    @property
    def result(self) -> Any | None:
        """兼容旧字段名（已废弃），等同于 output"""
        return self.output

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        result = super().to_dict()

        # 添加向后兼容字段
        if self.output is not None:
            result["data"] = self.output

        if self.tool_name:
            result["tool_name"] = self.tool_name
        if self.tool_id:
            result["tool_id"] = self.tool_id
        if self.input_params:
            result["input_params"] = self.input_params

        return result
