"""
MCP Filesystem Server 适配器

暴露接口：
- get_tool_definition(self) -> dict[str, Any]：get_tool_definition功能
- execute(self, arguments: dict[str, Any]) -> ToolExecutionResult：execute功能
- MCPFilesystemAdapter：MCPFilesystemAdapter类
"""

from typing import Any

from core.results import ToolExecutionResult
from tools.types import Tool

# 模拟 MCP 客户端上下文，实际使用时需注入真实的 MCP 客户端
# 这里假设有一个全局的 MCP Client 管理器


class MCPFilesystemAdapter(Tool):
    """
    MCP Filesystem Server 的工具适配器。
    提供对文件系统的只读或读写访问能力，具体取决于 MCP 服务器的配置。
    """

    def __init__(self, allowed_path: str = "/tmp/kiro_allowed_files"):
        self.tool_name = "mcp_filesystem"
        self.description = "通过 MCP 协议访问文件系统，支持读取、写入、搜索和列出目录。"
        self.server_name = "filesystem"
        self.allowed_path = allowed_path

    def get_tool_definition(self) -> dict[str, Any]:
        """
        返回工具的定义，用于向系统注册该工具。
        注意：MCP 工具通常动态发现，这里定义的是适配器本身的元数据。
        """
        return {
            "name": self.tool_name,
            "description": self.description,
            "mcpServer": self.server_name,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "read_file",
                            "write_file",
                            "list_directory",
                            "create_directory",
                            "search_files",
                        ],
                        "description": "要执行的文件系统操作类型",
                    },
                    "path": {
                        "type": "string",
                        "description": "文件或目录的路径，相对于允许的根目录",
                    },
                    "content": {
                        "type": "string",
                        "description": "写入文件时的内容 (仅 write_file 需要)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "搜索文件时的 glob 模式 (仅 search_files 需要)",
                    },
                },
                "required": ["operation", "path"],
            },
        }

    def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """
        执行工具逻辑。此适配器主要作为 MCP 客户端的代理。
        实际执行由底层的 MCP 传输层处理。
        """
        operation = arguments.get("operation")
        path = arguments.get("path")

        if not operation or not path:
            return ToolExecutionResult.create_failed(
                error="Missing required arguments: 'operation' and 'path'"
            )

        # 这里只是逻辑示意，实际调用需通过 MCP Client 发送 JSON-RPC 请求

        try:
            # 模拟执行结果
            return ToolExecutionResult.create_completed(
                output=f"Executed {operation} on {path} via MCP server."
            )
        except Exception as e:
            return ToolExecutionResult.create_failed(
                error=f"MCP Execution failed: {str(e)}"
            )
