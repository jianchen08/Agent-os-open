"""IDE 工具——从 0.1 提取核心逻辑为纯函数。

[来源: src/tools/builtin/ide_get_selection/tool.py, ide_open_file/tool.py, ide_show_diff/tool.py]

0.2 无 connectors 运行时注入（_registry 无 setter 恒 None），IDE 连接器分支
已删除；三个工具均返回降级提示/文本 diff 语义。
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


async def ide_get_selection(**kwargs: Any) -> dict[str, Any]:
    """获取当前 IDE 中的上下文信息（无活跃连接器时返回降级提示）。"""
    return {
        "message": "无活跃连接器，请手动提供上下文信息",
        "active_file": None,
        "selected_text": None,
        "cursor_position": None,
    }


async def ide_open_file(file_path: str, line: int | None = None, column: int | None = None, **kwargs: Any) -> dict[str, Any]:
    """在 IDE 中打开指定文件（无活跃连接器时返回降级提示）。"""
    if not file_path:
        return {"error": "file_path 参数不能为空"}

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
    """在 IDE 中显示文件差异（无活跃连接器时降级为文本 diff）。"""
    if not file_path:
        return {"error": "file_path 参数不能为空"}

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
