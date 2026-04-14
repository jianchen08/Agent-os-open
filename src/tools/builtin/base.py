"""
内置工具基类

暴露接口：
- register_builtin_tool(tool_instance: BuiltinTool, registry: Any) -> str：register_builtin_tool功能
- get_tool_definition() -> Tool：get_tool_definition功能
- to_runnable(self) -> 'ToolRunnable'：to_runnable功能
- to_mcp_format(self) -> dict[str, Any]：to_mcp_format功能
- to_llm_format(self) -> dict[str, Any]：to_llm_format功能
- BuiltinTool：BuiltinTool类
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from core.results import ToolExecutionResult
from tools.types import Tool

if TYPE_CHECKING:
    from core.runnable import ToolRunnable


class BuiltinTool(ABC):
    """
    内置工具基类

    所有内置工具应继承此类，实现：
    - get_tool_definition(): 返回工具定义
    - execute(): 执行工具
    """

    @staticmethod
    @abstractmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""

    @abstractmethod
    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""

    def to_runnable(self) -> "ToolRunnable":
        """转换为 ToolRunnable"""
        tool = self.get_tool_definition()
        return tool.to_runnable(self.execute)

    def to_mcp_format(self) -> dict[str, Any]:
        """转换为 MCP 格式"""
        tool = self.get_tool_definition()
        return tool.to_mcp_format()

    def to_llm_format(self) -> dict[str, Any]:
        """转换为 LLM 格式"""
        tool = self.get_tool_definition()
        return tool.to_llm_format()


def register_builtin_tool(
    tool_instance: BuiltinTool,
    registry: Any,
) -> str:
    """注册内置工具到注册表"""
    tool = tool_instance.get_tool_definition()
    return registry.register_with_handler(
        tool=tool,
        handler=tool_instance.execute,
    )
