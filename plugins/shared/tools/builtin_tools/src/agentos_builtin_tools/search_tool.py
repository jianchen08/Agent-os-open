"""增强搜索工具——代码/文件内容搜索。

核心业务逻辑从 0.1 src/tools/builtin/enhanced_search/ 迁移。

工作空间约束与其他文件工具同规（fs_tools 单源）：路径解析/边界校验/
凭据黑名单全部复用 fs_tools，本模块不自持第二套判定。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from agentos_builtin_tools.fs_tools import (
    _check_workspace_path,
    _sensitive_file_reason,
)
from agentos_builtin_tools.result import ToolResult

ENHANCED_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "搜索关键词或正则表达式（字面子串匹配，use_regex=true 才是正则；"
                "不支持 * 通配符，filename 搜索直接用文件名子串，勿带 * 或 ?）"
            ),
        },
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
        "timeout_seconds": {
            "type": "number",
            "description": "遍历墙钟预算（秒），超时返回部分结果",
            "default": 60.0,
        },
    },
    "required": ["query"],
}

# 二进制闸预算：NUL 探测只看文件头；命中扫描有界（防大文件全文进正则）。
_BINARY_SNIFF_BYTES = 64 * 1024
_BINARY_SCAN_BYTES = 4 * 1024 * 1024

# 遍历剪枝名单（按 basename 全深度生效，镜像本仓 .gitignore 重目录 +
# rg 同款默认 .git）：依赖与构建产物树单层精确剪枝（repo_walk_prune 只覆盖
# 仓根一级）拦不住嵌套形态——.zctmp/.wt-* 内的 npm 依赖循环链可达数十万
# 目录，实测本仓仓根无此剪枝 380s 走不到 docs/，有此剪枝 1.5s 命中。
_WALK_SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        ".venv",
        "venv",
        ".venv-hindsight",
        ".zctmp",
        "__pycache__",
        "target",
        "dist",
        "dist-electron",
    }
)
_WALK_SKIP_DIR_NAMES_NORMED = frozenset(
    os.path.normcase(n) for n in _WALK_SKIP_DIR_NAMES
)


def _walk_prune_dirs(root: str, dirs: list[str], prune: set[str]) -> list[str]:
    """walk 剪枝：basename 全深度名单 + .wt- 前缀 + 仓根一级精确集。"""
    kept: list[str] = []
    for d in dirs:
        nd = os.path.normcase(d)
        if nd in _WALK_SKIP_DIR_NAMES_NORMED or nd.startswith(".wt-"):
            continue
        if prune and os.path.normcase(os.path.join(root, d)) in prune:
            continue
        kept.append(d)
    return kept


def _binary_contains(file_path: Path, pattern: re.Pattern[str]) -> bool:
    """二进制文件按字节扫描命中（latin-1 映射保字节，预算内读取）。"""
    try:
        with file_path.open("rb") as fh:
            raw = fh.read(_BINARY_SCAN_BYTES)
    except OSError:
        return False
    return pattern.search(raw.decode("latin-1")) is not None


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
    timeout_seconds: float = 60.0,
    workspace: str | None = None,
    project_root: str | None = None,
    authorized_read_zones: str | None = None,
) -> ToolResult:
    """搜索文件内容或文件名（相对路径以注入根锚定；无注入报错）。

    双闸与其他文件工具同源（fs_tools）：① 路径边界 fail-closed（根外绝对
    路径/``..`` 逃逸拒绝、/workspace 挂载点重映射）；② 目标路径自身命中
    凭据黑名单（.env/私钥等）直接拒绝。

    遍历按 basename 全深度跳过 node_modules/.git/target 等依赖与构建产物
    目录（rg 同款默认；直接以这些目录为 path 起搜仍可进入）。
    """
    import fnmatch

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if use_regex else re.escape(query), flags)

    allowed, reason, resolved = _check_workspace_path(
        path,
        workspace,
        project_root,
        operation="search",
        authorized_read_zones=authorized_read_zones,
    )
    if not allowed or resolved is None:
        return ToolResult.failure_result(reason)
    search_path = Path(resolved)
    if not search_path.exists():
        return ToolResult.failure_result(f"Path not found: {path}")
    truncated_reason = ""
    # 仓库根内遍历剪枝：搜索根本身是仓库根时会顺路走运行时/产物目录
    # （data/config/logs/.ai_workspaces/.git/.venv 等），walk 循环剔除；
    # 根在仓库外（普通工作区）时为空集零开销。延迟导入避免与 fs_tools
    # 的共享根自举产生导入顺序依赖。
    import repo_anchor  # noqa: PLC0415

    prune = repo_anchor.repo_walk_prune(search_path)

    results: list[dict[str, Any]] = []

    def _search_sync() -> None:
        nonlocal truncated_reason
        # 支持单文件路径：os.walk 对「文件」路径产出空迭代（它只遍历目录条目），
        # 导致指向具体文件时结果恒为空。这里显式把单文件构造成一次遍历，复用下方
        # 既有匹配逻辑（file_path = Path(root) / fname 会还原成该文件路径）。
        if search_path.is_file():
            walk_iter: Any = [(search_path.parent, [], [search_path.name])]
        else:
            walk_iter = os_walk_depth(search_path, max_depth)
        deadline = time.monotonic() + timeout_seconds
        for root, dirs, files in walk_iter:
            if time.monotonic() > deadline:
                # 墙钟预算（D6 同源护栏）：daemon 高压/超大树下限时不无限走，
                # 返回已达成的部分结果并如实标注截断。
                nonlocal truncated_reason
                truncated_reason = f"遍历超时（>{timeout_seconds:.0f}s），结果可能不完整"
                return
            dirs[:] = _walk_prune_dirs(root, dirs, prune)
            if search_type == "filename":
                for fname in files:
                    if not fnmatch.fnmatch(fname, file_pattern):
                        continue
                    # 先匹配后敏感判定（纯换序，输出等价）：匹配是纯内存操作，
                    # 敏感判定每文件一次 resolve 系统调用——十万级文件树下
                    # 先判定会把整树遍历从秒级拖到近墙钟预算。
                    if not pattern.search(fname):
                        continue
                    # 凭据类文件不进结果（遍历场景跳过而非整体失败：walk 会
                    # 顺路碰到 .env，跳过才符合"搜索不回传凭据"）
                    if _sensitive_file_reason((Path(root) / fname).resolve()) is not None:
                        continue
                    results.append(
                        {
                            "file_path": str(Path(root) / fname),
                            "line_number": 0,
                            "content": fname,
                            "context_before": [],
                            "context_after": [],
                        }
                    )
                    if len(results) >= max_results:
                        return
            else:
                for fname in files:
                    if not fnmatch.fnmatch(fname, file_pattern):
                        continue
                    file_path = Path(root) / fname
                    if _sensitive_file_reason(file_path.resolve()) is not None:
                        continue
                    # 二进制闸（git 同款 NUL 探测）：二进制不展开内容——此前
                    # errors="replace" 把二进制吞成替换字符垃圾随命中行回传，
                    # 单个文件即可撑出 MB 级结果。命中只报路径与体量，细节由
                    # agent 决定是否 file_read（2026-09-14 用户裁定）。
                    try:
                        with file_path.open("rb") as fh:
                            is_binary = b"\x00" in fh.read(_BINARY_SNIFF_BYTES)
                    except OSError:
                        continue
                    if is_binary:
                        if _binary_contains(file_path, pattern):
                            results.append(
                                {
                                    "file_path": str(file_path),
                                    "line_number": 0,
                                    "content": (
                                        f"[二进制文件命中，{file_path.stat().st_size} 字节，"
                                        "内容不展开——需要细节请用 file_read 读取]"
                                    ),
                                    "context_before": [],
                                    "context_after": [],
                                }
                            )
                            if len(results) >= max_results:
                                return
                        continue
                    try:
                        content = file_path.read_text("utf-8", errors="replace")
                    except OSError:
                        continue
                    lines = content.split("\n")
                    for i, line in enumerate(lines):
                        if pattern.search(line):
                            ctx_start = max(0, i - context_lines)
                            ctx_end = min(len(lines), i + context_lines + 1)
                            results.append(
                                {
                                    "file_path": str(file_path),
                                    "line_number": i + 1,
                                    "content": line,
                                    "context_before": lines[ctx_start:i],
                                    "context_after": lines[i + 1 : ctx_end],
                                }
                            )
                            if len(results) >= max_results:
                                return
                    if len(results) >= max_results:
                        return

    await asyncio.to_thread(_search_sync)

    return ToolResult.success_result(
        {"results": results},
        count=len(results),
        truncated=len(results) >= max_results or bool(truncated_reason),
        message=truncated_reason or None,
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
