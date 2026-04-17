"""
MCP 工具适配器

暴露接口：
- mcp_tool_to_runnable(tool: Tool, handler: ToolHandler) -> ToolRunnable：mcp_tool_to_runnable功能
- runnable_to_mcp_tool(runnable: ToolRunnable, source: ToolSource, category: ToolCategory | None, level: ToolLevel) -> Tool：runnable_to_mcp_tool功能
- batch_convert_mcp_tools(tools: list[Tool], handlers: dict[str, ToolHandler]) -> list[ToolRunnable]：batch_convert_mcp_tools功能
- runnable(self) -> ToolRunnable：runnable功能
- get_mcp_format(self) -> dict[str, Any]：get_mcp_format功能
- get_llm_format(self) -> dict[str, Any]：get_llm_format功能
- MCPToolAdapter：MCPToolAdapter类
"""

from collections.abc import Callable, Coroutine
from typing import Any

from core.runnable import ToolRunnable
from tools.types import Tool, ToolCategory, ToolLevel, ToolSource

# 工具处理函数类型
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class MCPToolAdapter:
    """
    MCP 工具适配器

    将 MCP 格式的 Tool 转换为 ToolRunnable，同时保持 MCP 格式兼容性。

    数据流：
    input: MCP Tool 定义 (标准格式)
      ↓
    adapter: MCPToolAdapter (适配层)
      ↓
    output: LangChain Runnable (执行层)
    """

    def __init__(
        self,
        tool: Tool,
        handler: ToolHandler,
    ):
        """初始化适配器"""
        self.tool = tool
        self._handler = handler
        self._runnable: ToolRunnable | None = None

    @property
    def runnable(self) -> ToolRunnable:
        """获取 ToolRunnable 实例"""
        if self._runnable is None:
            self._runnable = self._create_runnable()
        return self._runnable

    def _create_runnable(self) -> ToolRunnable:
        """创建 ToolRunnable"""
        return ToolRunnable(
            name=self.tool.name,
            description=self.tool.description,
            handler=self._handler,
            input_schema=self.tool.input_schema,
            output_schema=self.tool.output_schema,
            metadata={
                "source": self.tool.source.value,
                "category": self.tool.category.value if self.tool.category else None,
                "level": self.tool.level.value,
                "version": self.tool.version,
                "tags": self.tool.tags,
                **self.tool.metadata,
            },
        )

    def get_mcp_format(self) -> dict[str, Any]:
        """获取 MCP 格式定义"""
        return {
            "name": self.tool.name,
            "description": self.tool.description,
            "inputSchema": self.tool.input_schema,
        }

    def get_llm_format(self) -> dict[str, Any]:
        """获取 LLM 格式定义"""
        return self.runnable.to_llm_format()


def mcp_tool_to_runnable(
    tool: Tool,
    handler: ToolHandler,
) -> ToolRunnable:
    """将 MCP 工具转换为 ToolRunnable"""
    adapter = MCPToolAdapter(tool=tool, handler=handler)
    return adapter.runnable


def runnable_to_mcp_tool(
    runnable: ToolRunnable,
    source: ToolSource = ToolSource.CODE,
    category: ToolCategory | None = None,
    level: ToolLevel = ToolLevel.USER,
) -> Tool:
    """将 ToolRunnable 转换为 MCP 工具格式"""
    return Tool(
        name=runnable.name,
        description=runnable.description,
        input_schema=runnable.tool_input_schema,
        output_schema=runnable.tool_output_schema,
        source=source,
        category=category,
        level=level,
        metadata=runnable._metadata,
    )


def batch_convert_mcp_tools(
    tools: list[Tool],
    handlers: dict[str, ToolHandler],
) -> list[ToolRunnable]:
    """批量转换 MCP 工具为 ToolRunnable"""
    runnables = []
    for tool in tools:
        handler = handlers.get(tool.name)
        if handler:
            runnable = mcp_tool_to_runnable(tool, handler)
            runnables.append(runnable)
    return runnables
