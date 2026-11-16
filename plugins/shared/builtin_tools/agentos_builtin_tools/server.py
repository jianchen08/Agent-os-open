"""MCP 服务端——将 7 个内置工具封装为 MCP 服务。

使用 agentos-plugin-sdk 的 AgentOSPlugin 注册全部工具，
通过 stdin/stdout JSON-RPC 与 Rust 内核 McpClient 对接。
"""

from __future__ import annotations

from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

from agentos_builtin_tools.fs_tools import (
    COPY_FILE_SCHEMA,
    CREATE_DIRECTORY_SCHEMA,
    DELETE_FILE_SCHEMA,
    FILE_READ_OUTPUT_SCHEMA,
    FILE_READ_SCHEMA,
    FILE_WRITE_OUTPUT_SCHEMA,
    FILE_WRITE_SCHEMA,
    MOVE_FILE_SCHEMA,
    copy_file,
    create_directory,
    delete_file,
    file_read,
    file_write,
    move_file,
)
from agentos_builtin_tools.search_tool import ENHANCED_SEARCH_SCHEMA, enhanced_search

# LLM 可见描述（与 plugin.json 同文镜像；改动须两处同刀）
FILE_READ_DESCRIPTION = (
    "读取文件内容；路径为目录时返回该目录条目列表（名称/类型/大小）。"
    "查看工作空间或目录结构直接用本工具，读文件/列目录不要用 Shell 命令代替"
)


def create_plugin() -> AgentOSPlugin:
    """创建包含全部 7 个工具的 AgentOSPlugin 实例。"""
    plugin = AgentOSPlugin("agentos-builtin-tools")

    plugin.register_tool(
        "file_read", FILE_READ_SCHEMA, file_read, FILE_READ_DESCRIPTION,
        output_schema=FILE_READ_OUTPUT_SCHEMA,
    )
    plugin.register_tool(
        "file_write", FILE_WRITE_SCHEMA, file_write, "写入/编辑文件",
        output_schema=FILE_WRITE_OUTPUT_SCHEMA,
    )
    plugin.register_tool(
        "enhanced_search", ENHANCED_SEARCH_SCHEMA, enhanced_search, "代码/文件搜索"
    )
    plugin.register_tool(
        "create_directory", CREATE_DIRECTORY_SCHEMA, create_directory, "创建目录"
    )
    plugin.register_tool("copy_file", COPY_FILE_SCHEMA, copy_file, "复制文件")
    plugin.register_tool("move_file", MOVE_FILE_SCHEMA, move_file, "移动文件")
    plugin.register_tool("delete_file", DELETE_FILE_SCHEMA, delete_file, "删除文件")

    @plugin.on_load
    async def _on_load(_params: dict[str, Any]) -> None:
        """注入授权名单服务调用方（T6a：project.load_zones，project_manager 直连）。"""
        from agentos_builtin_tools import fs_tools

        async def _zones_caller(method: str, args: dict[str, Any]) -> dict[str, Any]:
            handle = plugin.get_capability("project")
            return await handle.call(method, args)

        fs_tools.set_zones_service_caller(_zones_caller)

    @plugin.on_unload
    async def _on_unload(_params: dict[str, Any]) -> None:
        """摘除授权名单服务调用方（防卸载窗口期残留句柄）。"""
        from agentos_builtin_tools import fs_tools

        fs_tools.set_zones_service_caller(None)

    return plugin


def run() -> None:
    """启动 MCP 服务端。"""
    create_plugin().run()


# 工具注册表——供测试直接调用
TOOL_REGISTRY = {
    "file_read": (FILE_READ_SCHEMA, file_read),
    "file_write": (FILE_WRITE_SCHEMA, file_write),
    "enhanced_search": (ENHANCED_SEARCH_SCHEMA, enhanced_search),
    "create_directory": (CREATE_DIRECTORY_SCHEMA, create_directory),
    "copy_file": (COPY_FILE_SCHEMA, copy_file),
    "move_file": (MOVE_FILE_SCHEMA, move_file),
    "delete_file": (DELETE_FILE_SCHEMA, delete_file),
}
