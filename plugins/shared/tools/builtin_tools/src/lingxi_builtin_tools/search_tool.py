"""增强搜索工具——代码/文件内容搜索。

核心业务逻辑从 0.1 src/tools/builtin/enhanced_search/ 迁移。
使用 ripgrep（如有）或 Python 实现。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from lingxi_builtin_tools.result import ToolResult

ENHANCED_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词或正则表达式"},
        "path": {"type": "string", "description": "搜索起始路径", "default": "."},
        "search_type": {
            "type": "string",
            "enum": ["text", "filename"],
            "description": "搜索类型：text=内容搜索，filename=文件名搜索",
            "default": "text",
        },
        "file_pattern": {"type": "string", "description": "文件过滤模式（如 *.py）"},
        "case_sensitive": {"type": "boolean", "description": "是否区分大小写", "default": False},
        "use_regex": {"type": "boolean", "description": "是否使用正则表达式", "default": False},
        "context_lines": {"type": "integer", "description": "上下文行数", "default": 2},
        "max_results": {"type": "integer", "description": "最大结果数", "default": 100},
        "max_depth": {"type": "integer", "description": "最大递归深度", "default": 20},
    },
    "required": ["query"],
}


async def enhanced_search(
    query: str,
    path: str = ".",
    search_type: str = "text",
    file_pattern: str = "*",
    case_sensitive: bool = False,
    use_regex: bool = False,
    context_lines: int = 2,
    max_results: int = 100,
    max_depth: int = 20,
) -> ToolResult:
    """搜索文件内容或文件名。"""
    import fnmatch

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if use_regex else re.escape(query), flags)

    search_path = Path(path)
    if not search_path.exists():
        return ToolResult.failure_result(f"Path not found: {path}")

    results: list[dict[str, Any]] = []

    def _search_sync() -> None:
        for root, dirs, files in os_walk_depth(search_path, max_depth):
            if search_type == "filename":
                for fname in files:
                    if not fnmatch.fnmatch(fname, file_pattern):
                        continue
                    if pattern.search(fname):
                        results.append({
                            "file_path": str(Path(root) / fname),
                            "line_number": 0,
                            "content": fname,
                            "context_before": [],
                            "context_after": [],
                        })
                        if len(results) >= max_results:
                            return
            else:
                for fname in files:
                    if not fnmatch.fnmatch(fname, file_pattern):
                        continue
                    file_path = Path(root) / fname
                    try:
                        content = file_path.read_text("utf-8", errors="replace")
                    except OSError:
                        continue
                    lines = content.split("\n")
                    for i, line in enumerate(lines):
                        if pattern.search(line):
                            ctx_start = max(0, i - context_lines)
                            ctx_end = min(len(lines), i + context_lines + 1)
                            results.append({
                                "file_path": str(file_path),
                                "line_number": i + 1,
                                "content": line,
                                "context_before": lines[ctx_start:i],
                                "context_after": lines[i + 1 : ctx_end],
                            })
                            if len(results) >= max_results:
                                return
                    if len(results) >= max_results:
                        return

    await asyncio.to_thread(_search_sync)

    return ToolResult.success_result(
        {"results": results},
        count=len(results),
        truncated=len(results) >= max_results,
    )


def os_walk_depth(root: Path, max_depth: int):
    """限制递归深度的 os.walk。"""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts) if str(rel) != "." else 0
        if depth >= max_depth:
            dirnames.clear()
        yield dirpath, dirnames, filenames
