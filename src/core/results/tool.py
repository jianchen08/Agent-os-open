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

    Attributes:
        tool_name: 工具名称
        tool_id: 工具 ID
        input_params: 输入参数
    """

    # 工具标识
    tool_name: str | None = Field(default=None, description="工具名称")
    tool_id: str | None = Field(default=None, description="工具 ID")

    # 输入参数
    input_params: dict[str, Any] = Field(default_factory=dict, description="输入参数")

    def to_dict(self, slim: bool = False) -> dict[str, Any]:
        """转换为字典

        Args:
            slim: 精简模式，省略 tool_name/tool_id/input_params 等冗余字段
        """
        result = super().to_dict(slim=slim)

        if not slim:
            if self.tool_name:
                result["tool_name"] = self.tool_name
            if self.tool_id:
                result["tool_id"] = self.tool_id
            if self.input_params:
                result["input_params"] = self.input_params

        return result
