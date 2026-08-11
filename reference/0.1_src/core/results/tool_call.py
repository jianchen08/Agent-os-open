"""
工具调用记录

暴露接口：
- ToolCallRecord：ToolCallRecord类
"""

from typing import Any

from pydantic import BaseModel, Field


class ToolCallRecord(BaseModel):
    """工具调用记录

    记录 Agent 执行过程中调用的工具信息。

    Attributes:
        tool_name: 工具名称
        inputs: 输入参数
        output: 输出结果
        success: 是否成功
        error: 错误信息
        duration_ms: 执行时间（毫秒）
    """

    tool_name: str = Field(..., description="工具名称")
    inputs: dict[str, Any] = Field(default_factory=dict, description="输入参数")
    output: Any | None = Field(None, description="输出结果")
    success: bool = Field(..., description="是否成功")
    error: str | None = Field(None, description="错误信息")
    duration_ms: int = Field(0, description="执行时间（毫秒）")
