"""MCP 服务端——将 10 个内置工具封装为 MCP 服务。

使用 agentos-plugin-sdk 的 AgentOSPlugin 注册全部工具，
通过 stdin/stdout JSON-RPC 与 Rust 内核 McpClient 对接。
"""

from __future__ import annotations

from agentos_plugin_sdk import AgentOSPlugin

from agentos_builtin_tools.bash_tool import BASH_EXECUTE_OUTPUT_SCHEMA, BASH_EXECUTE_SCHEMA, bash_execute
from agentos_builtin_tools.fs_tools import (
    COPY_FILE_SCHEMA,
    CREATE_DIRECTORY_SCHEMA,
    DELETE_FILE_SCHEMA,
    FILE_READ_OUTPUT_SCHEMA,
    FILE_READ_SCHEMA,
    FILE_WRITE_OUTPUT_SCHEMA,
    FILE_WRITE_SCHEMA,
    LIST_DIRECTORY_OUTPUT_SCHEMA,
    LIST_DIRECTORY_SCHEMA,
    MOVE_FILE_SCHEMA,
    copy_file,
    create_directory,
    delete_file,
    file_read,
    file_write,
    list_directory,
    move_file,
)
from agentos_builtin_tools.search_tool import ENHANCED_SEARCH_SCHEMA, enhanced_search
from agentos_builtin_tools.web_tool import WEB_OPERATE_SCHEMA, web_operate


def create_plugin() -> AgentOSPlugin:
    """创建包含全部 10 个工具的 AgentOSPlugin 实例。"""
    plugin = AgentOSPlugin("agentos-builtin-tools")

    plugin.register_tool(
        "file_read", FILE_READ_SCHEMA, file_read, "读取文件内容",
        output_schema=FILE_READ_OUTPUT_SCHEMA,
    )
    plugin.register_tool(
        "file_write", FILE_WRITE_SCHEMA, file_write, "写入/编辑文件",
        output_schema=FILE_WRITE_OUTPUT_SCHEMA,
    )
    plugin.register_tool(
        "bash_execute", BASH_EXECUTE_SCHEMA, bash_execute, "执行 Shell 命令",
        output_schema=BASH_EXECUTE_OUTPUT_SCHEMA,
    )
    plugin.register_tool(
        "enhanced_search", ENHANCED_SEARCH_SCHEMA, enhanced_search, "代码/文件搜索"
    )
    plugin.register_tool(
        "list_directory", LIST_DIRECTORY_SCHEMA, list_directory, "目录列举",
        output_schema=LIST_DIRECTORY_OUTPUT_SCHEMA,
    )
    plugin.register_tool(
        "create_directory", CREATE_DIRECTORY_SCHEMA, create_directory, "创建目录"
    )
    plugin.register_tool("copy_file", COPY_FILE_SCHEMA, copy_file, "复制文件")
    plugin.register_tool("move_file", MOVE_FILE_SCHEMA, move_file, "移动文件")
    plugin.register_tool("delete_file", DELETE_FILE_SCHEMA, delete_file, "删除文件")
    plugin.register_tool("web_operate", WEB_OPERATE_SCHEMA, web_operate, "Web 操作")

    return plugin


def run() -> None:
    """启动 MCP 服务端。"""
    create_plugin().run()


# 工具注册表——供测试直接调用
TOOL_REGISTRY = {
    "file_read": (FILE_READ_SCHEMA, file_read),
    "file_write": (FILE_WRITE_SCHEMA, file_write),
    "bash_execute": (BASH_EXECUTE_SCHEMA, bash_execute),
    "enhanced_search": (ENHANCED_SEARCH_SCHEMA, enhanced_search),
    "list_directory": (LIST_DIRECTORY_SCHEMA, list_directory),
    "create_directory": (CREATE_DIRECTORY_SCHEMA, create_directory),
    "copy_file": (COPY_FILE_SCHEMA, copy_file),
    "move_file": (MOVE_FILE_SCHEMA, move_file),
    "delete_file": (DELETE_FILE_SCHEMA, delete_file),
    "web_operate": (WEB_OPERATE_SCHEMA, web_operate),
}
