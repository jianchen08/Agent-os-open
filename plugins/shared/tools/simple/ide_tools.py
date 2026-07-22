"""IDE 工具——从 0.1 提取核心逻辑为纯函数。

[来源: src/tools/builtin/ide_get_selection/tool.py, ide_open_file/tool.py, ide_show_diff/tool.py]

DEBT: IDE 连接器降级逻辑需要 connectors 模块，当前返回降级提示。ceiling: 无连接器场景。
upgrade: connectors 迁移后恢复完整功能。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

IDE_GET_SELECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

IDE_OPEN_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "要打开的文件路径"},
        "line": {"type": "integer", "description": "跳转到的行号（可选）"},
        "column": {"type": "integer", "description": "跳转到的列号（可选）"},
    },
    "required": ["file_path"],
}

IDE_SHOW_DIFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "文件路径"},
        "original_content": {"type": "string", "description": "原始文件内容"},
        "new_content": {"type": "string", "description": "修改后的文件内容"},
        "title": {"type": "string", "description": "差异视图标题（可选）"},
    },
    "required": ["file_path", "original_content", "new_content"],
}

_registry: Any = None


def set_registry(registry: Any) -> None:
    """设置连接器注册表（由 on_load 注入）。"""
    global _registry
    _registry = registry


def _get_active_connector() -> Any:
    """获取活跃连接器。"""
    if _registry is None:
        return None
    return _registry.get_active_connector()


async def ide_get_selection(**kwargs: Any) -> dict[str, Any]:
    """获取当前 IDE 中的上下文信息。"""
    connector = _get_active_connector()
    if connector is not None:
        try:
            context = await connector.get_context()
            return {
                "active_file": context.active_file,
                "selected_text": context.selected_text,
                "cursor_position": (
                    {"line": context.cursor_position.line, "column": context.cursor_position.column}
                    if context.cursor_position
                    else None
                ),
                "open_files": context.open_files,
                "connector": connector.connector_type,
            }
        except Exception as e:
            logger.warning("连接器获取上下文失败: %s", e)

    return {
        "message": "无活跃连接器，请手动提供上下文信息",
        "active_file": None,
        "selected_text": None,
        "cursor_position": None,
    }


async def ide_open_file(file_path: str, line: int | None = None, column: int | None = None, **kwargs: Any) -> dict[str, Any]:
    """在 IDE 中打开指定文件。"""
    if not file_path:
        return {"error": "file_path 参数不能为空"}

    connector = _get_active_connector()
    if connector is not None:
        try:
            from connectors.types import ConnectorAction  # noqa: PLC0415

            action = ConnectorAction(
                action_type="open_file",
                parameters={"file_path": file_path, "line": line, "column": column},
            )
            result = await connector.execute_action(action)
            if result.success:
                return {
                    "message": f"已在 IDE 中打开文件: {file_path}",
                    "file_path": file_path,
                }
        except Exception as e:
            logger.warning("连接器执行失败: %s", e)

    # DEBT: 降级——无连接器时只返回提示。ceiling: 无连接器。upgrade: connectors 迁移后恢复。
    return {
        "message": f"无活跃连接器，请手动打开文件: {file_path}",
        "file_path": file_path,
    }


async def ide_show_diff(
    file_path: str,
    original_content: str = "",
    new_content: str = "",
    title: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """在 IDE 中显示文件差异。"""
    if not file_path:
        return {"error": "file_path 参数不能为空"}

    connector = _get_active_connector()
    if connector is not None:
        try:
            from connectors.types import ConnectorAction  # noqa: PLC0415

            action = ConnectorAction(
                action_type="show_diff",
                parameters={
                    "file_path": file_path,
                    "original_content": original_content,
                    "new_content": new_content,
                    "title": title,
                },
            )
            result = await connector.execute_action(action)
            if result.success:
                return {
                    "message": f"已在 IDE 中显示差异: {file_path}",
                    "file_path": file_path,
                }
        except Exception as e:
            logger.warning("连接器执行失败: %s", e)

    # 降级：生成 unified diff 文本
    import difflib  # noqa: PLC0415

    diff_lines = list(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"{file_path} (original)",
            tofile=f"{file_path} (modified)",
        )
    )
    return {
        "message": f"无活跃连接器，生成文本 diff: {file_path}",
        "file_path": file_path,
        "diff": "".join(diff_lines),
    }
