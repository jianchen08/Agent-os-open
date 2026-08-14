"""文件系统工具集合。

包含 file_read / file_write / list_directory / create_directory / copy_file / move_file / delete_file。
核心业务逻辑从 0.1 src/tools/builtin/ 迁移，外层用 SDK 封装为 MCP 工具。

[来源: src/tools/builtin/{file_read,file_write,list_directory,create_directory,copy_file,move_file,delete_file}/tool.py]
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from agentos_builtin_tools.result import ToolResult

# 大文件不再拒绝（task_spill_guard.md 任务 2）：大输出兜底由 pipeline 的
# spill_guard 统一负责（原文存档 + 提取 + 定位符），工具只负责"读文件 +
# 返回内容"。行范围（start_line/end_line/tail）是用户显式指定的查询窗口，
# 不属于静默截断，保留。


# ═════════════════════════════════════════════════════════════
# file_read
# ═════════════════════════════════════════════════════════════

FILE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径（相对路径或绝对路径）"},
        "start_line": {"type": "integer", "description": "起始行号（从1开始），仅读取指定行范围", "default": 1},
        "end_line": {"type": "integer", "description": "结束行号（从1开始，包含该行），仅读取指定行范围"},
        "tail": {"type": "integer", "description": "仅读取文件最后 N 行"},
    },
    "required": ["path"],
}

FILE_READ_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "lines": {"type": "integer"},
        "size": {"type": "integer"},
        "content": {"type": "string"},
    },
}


async def file_read(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    tail: int | None = None,
) -> ToolResult:
    """读取文件内容。

    支持行范围读取和尾部读取。
    """
    file_path = Path(path)
    if not file_path.exists():
        return ToolResult.failure_result(f"File not found: {path}")
    if not file_path.is_file():
        return ToolResult.failure_result(f"Not a file: {path}")

    size = file_path.stat().st_size

    try:
        content = await asyncio.to_thread(file_path.read_text, "utf-8")
    except UnicodeDecodeError:
        return ToolResult.failure_result(f"Binary file or encoding issue: {path}")
    except OSError as e:
        return ToolResult.failure_result(f"Read error: {e}")

    all_lines = content.split("\n")
    total = len(all_lines)

    if tail is not None:
        # split 对结尾 "\n" 会产生一个空串元素，tail 按真实行数计算（去掉末尾空串）
        lines = all_lines[:-1] if all_lines and all_lines[-1] == "" else all_lines
        start_idx = max(0, len(lines) - tail)
        selected = lines[start_idx:]
    elif start_line > 1 or end_line is not None:
        start_idx = max(0, start_line - 1)
        end_idx = end_line if end_line is not None else total
        selected = all_lines[start_idx:end_idx]
    else:
        selected = all_lines

    return ToolResult.success_result(
        {
            "file": path,
            "lines": len(selected),
            "size": size,
            "content": "\n".join(selected),
        },
        total_lines=total,
    )


# ═════════════════════════════════════════════════════════════
# file_write
# ═════════════════════════════════════════════════════════════

FILE_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径"},
        "content": {"type": "string", "description": "文件内容"},
        "action": {
            "type": "string",
            "enum": ["write", "search_replace", "insert", "delete_lines", "append"],
            "description": "编辑操作类型",
            "default": "write",
        },
        "start_line": {"type": "integer", "description": "起始行号（write/insert/delete_lines 使用）"},
        "end_line": {"type": "integer", "description": "结束行号（write/delete_lines 使用）"},
        "line": {"type": "integer", "description": "插入位置行号（insert 使用）"},
        "old_str": {"type": "string", "description": "要搜索的原始文本（search_replace 使用）"},
        "new_str": {"type": "string", "description": "替换后的新文本（search_replace 使用）"},
        "create_backup": {"type": "boolean", "description": "是否创建 .bak 备份", "default": True},
    },
    "required": ["path", "action"],
}

FILE_WRITE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "added": {"type": "integer"},
        "removed": {"type": "integer"},
        "backup": {"type": ["string", "null"]},
    },
}


async def file_write(
    path: str,
    action: str = "write",
    content: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    line: int | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    create_backup: bool = True,
) -> ToolResult:
    """写入/编辑文件。"""
    file_path = Path(path)

    try:
        if action == "write":
            backup = _maybe_backup(file_path, create_backup)
            await asyncio.to_thread(file_path.write_text, content, "utf-8")
            added = content.count("\n") + 1 if content else 0
            return ToolResult.success_result(
                {"added": added, "removed": 0, "backup": backup},
            )

        if action == "append":
            backup = _maybe_backup(file_path, create_backup)
            existing = file_path.read_text("utf-8") if file_path.exists() else ""
            file_path.write_text(existing + content, "utf-8")
            return ToolResult.success_result(
                {"added": content.count("\n") + 1 if content else 0, "removed": 0, "backup": backup},
            )

        if action == "search_replace":
            if old_str is None:
                return ToolResult.failure_result("old_str is required for search_replace")
            if not file_path.exists():
                return ToolResult.failure_result(f"File not found: {path}")
            backup = _maybe_backup(file_path, create_backup)
            existing = file_path.read_text("utf-8")
            replacement = new_str if new_str is not None else ""
            count_replace = existing.count(old_str)
            if count_replace == 0:
                return ToolResult.failure_result(
                    f"old_str not found in {path}", backup=backup or "",
                )
            new_content = existing.replace(old_str, replacement)
            file_path.write_text(new_content, "utf-8")
            return ToolResult.success_result(
                {"added": 0, "removed": 0, "backup": backup},
                replacements=count_replace,
            )

        if action == "insert":
            if line is None:
                return ToolResult.failure_result("line is required for insert")
            backup = _maybe_backup(file_path, create_backup)
            existing = file_path.read_text("utf-8") if file_path.exists() else ""
            lines = existing.split("\n")
            insert_idx = min(max(line, 0), len(lines))
            lines.insert(insert_idx, content)
            file_path.write_text("\n".join(lines), "utf-8")
            return ToolResult.success_result(
                {"added": content.count("\n") + 1 if content else 0, "removed": 0, "backup": backup},
            )

        if action == "delete_lines":
            if start_line is None:
                return ToolResult.failure_result("start_line is required for delete_lines")
            if not file_path.exists():
                return ToolResult.failure_result(f"File not found: {path}")
            backup = _maybe_backup(file_path, create_backup)
            existing = file_path.read_text("utf-8")
            lines = existing.split("\n")
            end = end_line if end_line is not None else len(lines)
            del lines[start_line - 1 : end]
            file_path.write_text("\n".join(lines), "utf-8")
            return ToolResult.success_result(
                {"added": 0, "removed": end - start_line + 1, "backup": backup},
            )

        return ToolResult.failure_result(f"Unknown action: {action}")

    except OSError as e:
        return ToolResult.failure_result(f"IO error: {e}")


def _maybe_backup(path: Path, create: bool) -> str | None:
    """创建 .bak 备份。"""
    if not create or not path.exists():
        return None
    bak_path = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak_path)
    return str(bak_path)


# ═════════════════════════════════════════════════════════════
# list_directory
# ═════════════════════════════════════════════════════════════

LIST_DIRECTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目录路径（相对路径或绝对路径）"},
        "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件（以.开头），默认 false", "default": False},
        "pattern": {"type": "string", "description": "文件名匹配模式（支持 glob 语法，如 *.py）"},
    },
    "required": ["path"],
}

LIST_DIRECTORY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["file", "directory"]},
                    "size": {"type": "integer"},
                },
            },
        },
    },
}


async def list_directory(
    path: str,
    include_hidden: bool = False,
    pattern: str | None = None,
) -> ToolResult:
    """列出目录的直接子项。"""
    dir_path = Path(path)
    if not dir_path.exists():
        return ToolResult.failure_result(f"Directory not found: {path}")
    if not dir_path.is_dir():
        return ToolResult.failure_result(f"Not a directory: {path}")

    items: list[dict[str, Any]] = []
    for entry in sorted(dir_path.iterdir(), key=lambda e: e.name):
        name = entry.name
        if not include_hidden and name.startswith("."):
            continue
        if pattern and not _glob_match(name, pattern):
            continue
        entry_type = "directory" if entry.is_dir() else "file"
        size = entry.stat().st_size if entry.is_file() else 0
        items.append({"name": name, "type": entry_type, "size": size})

    return ToolResult.success_result({"items": items}, count=len(items))


def _glob_match(name: str, pattern: str) -> bool:
    """简化的 glob 匹配。"""
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


# ═════════════════════════════════════════════════════════════
# create_directory
# ═════════════════════════════════════════════════════════════

CREATE_DIRECTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目录路径"},
        "parents": {"type": "boolean", "description": "是否创建父目录（默认 true）", "default": True},
    },
    "required": ["path"],
}


async def create_directory(
    path: str,
    parents: bool = True,
) -> ToolResult:
    """创建目录（幂等：目录已存在直接返回成功）。"""
    dir_path = Path(path)
    try:
        await asyncio.to_thread(dir_path.mkdir, parents=parents, exist_ok=True)
    except OSError as e:
        return ToolResult.failure_result(f"Create error: {e}")

    return ToolResult.success_result({"path": str(dir_path.absolute())})


# ═════════════════════════════════════════════════════════════
# copy_file
# ═════════════════════════════════════════════════════════════

COPY_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "源文件或目录路径"},
        "destination": {"type": "string", "description": "目标路径"},
        "copies": {
            "type": "array",
            "items": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}},
            "description": "批量复制列表（与 source/destination 二选一）",
        },
        "overwrite": {"type": "boolean", "description": "是否覆盖已存在的目标（默认 false）", "default": False},
    },
    "required": [],
}


async def copy_file(
    source: str | None = None,
    destination: str | None = None,
    copies: list[dict[str, str]] | None = None,
    overwrite: bool = False,
) -> ToolResult:
    """复制文件或目录。"""
    if copies:
        results: list[dict[str, Any]] = []
        for item in copies:
            r = await copy_file(item["source"], item["destination"], overwrite=overwrite)
            results.append({"source": item["source"], "destination": item["destination"], "success": r.success})
        return ToolResult.success_result({"results": results})

    if not source or not destination:
        return ToolResult.failure_result("source and destination are required (or use copies)")

    src = Path(source)
    dst = Path(destination)
    if not src.exists():
        return ToolResult.failure_result(f"Source not found: {source}")
    if dst.exists() and not overwrite:
        return ToolResult.failure_result(f"Destination already exists: {destination}")

    try:
        if src.is_dir():
            await asyncio.to_thread(shutil.copytree, src, dst, dirs_exist_ok=overwrite)
        else:
            await asyncio.to_thread(shutil.copy2, src, dst)
    except OSError as e:
        return ToolResult.failure_result(f"Copy error: {e}")

    return ToolResult.success_result({"source": source, "destination": destination})


# ═════════════════════════════════════════════════════════════
# move_file
# ═════════════════════════════════════════════════════════════

MOVE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "源路径"},
        "destination": {"type": "string", "description": "目标路径"},
        "moves": {
            "type": "array",
            "items": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}},
            "description": "批量移动列表",
        },
        "overwrite": {"type": "boolean", "description": "是否覆盖已存在的目标（默认 false）", "default": False},
    },
    "required": [],
}


async def move_file(
    source: str | None = None,
    destination: str | None = None,
    moves: list[dict[str, str]] | None = None,
    overwrite: bool = False,
) -> ToolResult:
    """移动或重命名文件/目录。"""
    if moves:
        results: list[dict[str, Any]] = []
        for item in moves:
            r = await move_file(item["source"], item["destination"], overwrite=overwrite)
            results.append({"source": item["source"], "destination": item["destination"], "success": r.success})
        return ToolResult.success_result({"results": results})

    if not source or not destination:
        return ToolResult.failure_result("source and destination are required (or use moves)")

    src = Path(source)
    dst = Path(destination)
    if not src.exists():
        return ToolResult.failure_result(f"Source not found: {source}")
    if dst.exists() and not overwrite:
        return ToolResult.failure_result(f"Destination already exists: {destination}")

    try:
        if overwrite and dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        await asyncio.to_thread(shutil.move, str(src), str(dst))
    except OSError as e:
        return ToolResult.failure_result(f"Move error: {e}")

    return ToolResult.success_result({"source": source, "destination": destination})


# ═════════════════════════════════════════════════════════════
# delete_file
# ═════════════════════════════════════════════════════════════

DELETE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要删除的文件或目录路径"},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "批量删除路径列表"},
        "recursive": {"type": "boolean", "description": "是否递归删除目录（默认 false）", "default": False},
        "force": {"type": "boolean", "description": "是否强制删除（包括只读文件，默认 false）", "default": False},
    },
    "required": [],
}


async def delete_file(
    path: str | None = None,
    paths: list[str] | None = None,
    recursive: bool = False,
    force: bool = False,
) -> ToolResult:
    """删除文件或目录。"""
    target_paths = paths if paths else [path] if path else []
    if not target_paths:
        return ToolResult.failure_result("path or paths is required")

    results: list[dict[str, Any]] = []
    all_success = True

    for p in target_paths:
        target = Path(p)
        if not target.exists():
            results.append({"path": p, "deleted": False, "error": "not found"})
            all_success = False
            continue
        try:
            if target.is_dir():
                if recursive:
                    await asyncio.to_thread(shutil.rmtree, target)
                else:
                    target.rmdir()  # 仅删除空目录
            else:
                if force:
                    target.chmod(0o644)
                target.unlink()
            results.append({"path": p, "deleted": True})
        except OSError as e:
            results.append({"path": p, "deleted": False, "error": str(e)})
            all_success = False

    if all_success:
        return ToolResult.success_result({"results": results})
    return ToolResult(
        success=False,
        output={"results": results},
        error="some deletions failed",
    )
