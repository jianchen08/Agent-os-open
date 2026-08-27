"""增强搜索工具——代码/文件内容搜索。

核心业务逻辑从 0.1 src/tools/builtin/enhanced_search/ 迁移。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from agentos_builtin_tools.result import ToolResult

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
    workspace: str | None = None,
    project_root: str | None = None,
) -> ToolResult:
    """搜索文件内容或文件名（相对路径以注入根锚定；无注入报错）。"""
    import fnmatch

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if use_regex else re.escape(query), flags)

    root_str = project_root or workspace
    if not root_str:
        return ToolResult.failure_result(
            f"workspace/project_root 未注入，无法锚定搜索路径（相对路径禁止以进程 cwd 解析）：{path}"
        )
    root = Path(root_str).resolve()
    if path == "/workspace" or path.startswith("/workspace/"):
        path = str(root) + path[len("/workspace"):]
    target = Path(path)
    search_path = target.resolve() if target.is_absolute() else (root / target).resolve()
    if not search_path.exists():
        return ToolResult.failure_result(f"Path not found: {path}")

    results: list[dict[str, Any]] = []

    def _search_sync() -> None:
        # 支持单文件路径：os.walk 对「文件」路径产出空迭代（它只遍历目录条目），
        # 导致指向具体文件时结果恒为空。这里显式把单文件构造成一次遍历，复用下方
        # 既有匹配逻辑（file_path = Path(root) / fname 会还原成该文件路径）。
        if search_path.is_file():
            walk_iter: Any = [(search_path.parent, [], [search_path.name])]
        else:
            walk_iter = os_walk_depth(search_path, max_depth)
        for root, dirs, files in walk_iter:
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
