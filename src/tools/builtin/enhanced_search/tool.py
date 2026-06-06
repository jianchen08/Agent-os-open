"""
增强代码搜索工具 - 集成ripgrep

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- EnhancedSearchTool：EnhancedSearchTool类
"""

import asyncio
import fnmatch
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 安全常量 ──────────────────────────────────────────────────

# 跳过的目录（性能 + 安全）
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".workbuddy",
})

# 敏感系统目录黑名单（不允许搜索的路径前缀，小写规范形式）
_SENSITIVE_DIRS_WINDOWS: tuple[str, ...] = (
    "c:/windows",
    "c:/windows/system32",
    "c:/windows/syswow64",
    "c:/program files",
    "c:/program files (x86)",
    "c:/$recycle.bin",
    "c:/system volume information",
)

_SENSITIVE_DIRS_LINUX: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/boot",
    "/dev",
    "/run",
)

# 默认最大递归深度
_DEFAULT_MAX_DEPTH: int = 20

from tools.builtin.base import BuiltinTool
from tools.builtin.shared import format_size
from tools.builtin.workspace_aware import WorkspaceAwareMixin
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class EnhancedSearchTool(BuiltinTool, WorkspaceAwareMixin):
    """
    增强代码搜索工具

    提供：
    - 文本搜索（支持正则表达式）
    - 代码搜索
    - 文件名搜索

    优先使用ripgrep，回退到Python实现
    """

    def __init__(self, base_path: str | None = None):
        """初始化搜索工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()
        self._original_base_path = self.base_path  # 永久保存构造时的原始路径
        self.ripgrep_available = self._check_ripgrep()

        if self.ripgrep_available:
            logger.info("[Ripgrep] 检测到ripgrep，已启用高性能模式")
        else:
            logger.warning(
                "[Search] ripgrep未安装，使用Python模式"
                "（建议安装ripgrep以获得更好性能）"
            )

    def _check_ripgrep(self) -> bool:
        """检查ripgrep是否可用"""
        try:
            proc = subprocess.run(["rg", "--version"], capture_output=True, timeout=5)
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="enhanced_search",
            description="在文件中搜索文本、代码或文件名。支持内容搜索（集成ripgrep，性能提升10-100倍）和文件名搜索。适用于查找函数/类/变量定义、TODO注释、特定文件名等场景。默认不区分大小写，结果限制100条。文件名搜索不支持正则表达式。",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或正则表达式。用于内容搜索时支持正则（需设置use_regex=true），用于文件名搜索时仅支持字符串匹配",
                    },
                    "search_type": {
                        "type": "string",
                        "enum": ["text", "filename"],
                        "description": "搜索类型：text=在文件内容中搜索，filename=按文件名搜索",
                        "default": "text",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索的起始路径，默认为当前工作目录",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "文件过滤模式，仅对内容搜索有效。例如：*.py 只搜索Python文件，*.ts 只搜索TypeScript文件",
                        "default": "*",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "是否区分大小写，默认为false（不区分大小写）",
                        "default": False,
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "返回结果时包含的上下文行数，仅内容搜索支持。例如：2表示匹配行前后各2行",
                        "default": 2,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回结果数量，默认为100条",
                        "default": 100,
                    },
                    "use_regex": {
                        "type": "boolean",
                        "description": "是否将query作为正则表达式处理，仅内容搜索支持。默认为false（字面量搜索）",
                        "default": False,
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "最大递归深度，限制搜索目录层级。默认为20，防止在深层目录结构中搜索超时",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SEARCH,
            level=ToolLevel.USER,
            tags=["search", "code", "ripgrep", "performance", "filename"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行搜索"""
        self._init_workspace(inputs)
        self.base_path = self._workspace

        query = inputs.get("query")
        if not query:
            return create_failure_result(
                error="搜索查询不能为空",
                error_code="MISSING_QUERY",
            )

        # ── 安全校验：路径必须在 workspace 内（或原始 base_path 内）+ 不能是敏感系统目录 ──
        search_path_str = inputs.get("path", str(self.base_path))
        err = self._validate_search_path(search_path_str, fallback_boundary=self._original_base_path)
        if err:
            return err

        search_type = inputs.get("search_type", "text")

        if search_type == "filename":
            return await self._search_filename(inputs)
        elif search_type == "text":
            if self.ripgrep_available:
                return await self._search_with_ripgrep(inputs)
            else:
                return await self._search_with_python(inputs)
        else:
            return create_failure_result(
                error=f"不支持的搜索类型: {search_type}",
                error_code="INVALID_SEARCH_TYPE",
            )

    def _validate_search_path(
        self, search_path_str: str, fallback_boundary: Path | None = None
    ) -> ToolResult | None:
        """校验搜索路径安全性。

        返回 None 表示通过；返回 ToolResult 表示校验失败。
        三层检查（按优先级）：
        1. 路径存在性：给定路径必须真实存在
        2. workspace 边界：强制限制在当前 workspace 内（或 fallback_boundary 内）
        3. 敏感系统目录黑名单：禁止搜索 OS 核心目录
        """
        search_path = Path(search_path_str).resolve()
        workspace = self.base_path.resolve()

        # ── 检查 1：路径存在性（优先级最高，不存在就不需要做后续检查） ──
        if not search_path.exists():
            return create_failure_result(
                error=f"搜索路径不存在: {search_path_str}",
                error_code="PATH_NOT_FOUND",
            )

        # ── 检查 2：workspace 边界（主边界 + 回退边界） ──
        sp_str = str(search_path)
        allowed_boundaries: list[Path] = [workspace]
        if fallback_boundary is not None and fallback_boundary.resolve() != workspace:
            allowed_boundaries.append(fallback_boundary.resolve())

        is_allowed = any(
            sp_str == str(b) or sp_str.startswith(str(b) + os.sep)
            for b in allowed_boundaries
        )
        if not is_allowed:
            return create_failure_result(
                error=f"搜索路径超出工作区边界: {search_path_str}（工作区: {workspace}）",
                error_code="PATH_OUTSIDE_WORKSPACE",
            )

        # ── 检查 3：敏感系统目录黑名单 ──
        sp_lower = sp_str.lower().replace("\\", "/")
        sensitive_dirs = (
            _SENSITIVE_DIRS_WINDOWS if os.name == "nt" else _SENSITIVE_DIRS_LINUX
        )
        for forbidden in sensitive_dirs:
            if sp_lower == forbidden or sp_lower.startswith(forbidden + "/"):
                return create_failure_result(
                    error=f"禁止搜索系统目录: {search_path_str}（命中黑名单: {forbidden}）",
                    error_code="SENSITIVE_PATH_BLOCKED",
                )

        return None

    async def _search_with_ripgrep(self, inputs: dict[str, Any]) -> ToolResult:
        """
        使用ripgrep进行搜索（高性能）

        性能：比Python实现快10-100倍
        """
        try:
            query = inputs.get("query")
            search_path = inputs.get("path", str(self.base_path))
            file_pattern = inputs.get("file_pattern", "*")
            case_sensitive = inputs.get("case_sensitive", False)
            context_lines = inputs.get("context_lines", 2)
            max_results = inputs.get("max_results", 100)
            use_regex = inputs.get("use_regex", False)

            # 构建ripgrep命令
            cmd = [
                "rg",
                query,
                search_path,
                "--json",  # JSON格式输出
                "--no-heading",  # 不使用标题模式
                "--line-number",  # 显示行号
            ]

            # 添加上下文
            if context_lines > 0:
                cmd.extend(["-C", str(context_lines)])

            # 大小写敏感
            if not case_sensitive:
                cmd.append("--ignore-case")

            # 文件类型过滤
            if file_pattern and file_pattern != "*":
                cmd.extend(["-g", file_pattern])

            # 正则表达式
            if use_regex:
                # ripgrep默认就是正则，不需要额外参数
                pass
            else:
                # 字面量搜索
                cmd.append("--fixed-strings")

            # BUG-FIX: 移除 --max-count，它限制的是每个文件的匹配数而非总结果数
            # 改为在解析 stdout 时按总结果数截断

            # 执行搜索
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workspace),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=30.0,
                )

                if process.returncode != 0 and process.returncode != 1:
                    # 1表示没找到结果，这是正常的
                    error_msg = stderr.decode("utf-8", errors="replace")
                    return create_failure_result(
                        error=f"搜索失败: {error_msg}",
                        error_code="SEARCH_FAILED",
                    )

                # 解析JSON输出
                file_paths = []
                line_numbers = []
                contents = []
                match_count = 0

                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue

                    # BUG-FIX: 仅对 match 类型计数，context 行不限制
                    if match_count >= max_results:
                        break

                    try:
                        entry = json.loads(line)
                        entry_type = entry.get("type")

                        if entry_type == "match":
                            match_count += 1
                            data = entry.get("data", {})
                            file_paths.append(data.get("path", {}).get("text", ""))
                            line_numbers.append(data.get("line_number", 0))
                            contents.append(data.get("lines", {}).get("text", "").strip())
                        elif entry_type == "context":
                            data = entry.get("data", {})
                            file_paths.append(data.get("path", {}).get("text", ""))
                            line_numbers.append(data.get("line_number", 0))
                            contents.append(data.get("lines", {}).get("text", "").strip())

                    except json.JSONDecodeError:
                        continue

                return create_success_result(
                    data={
                        "query": query,
                        "engine": "ripgrep",
                        "h": ["file_path", "line_number", "content"],
                        "d": [[file_paths[i], line_numbers[i], contents[i]] for i in range(len(file_paths))],
                        "c": len(file_paths),
                    },
                    metadata={
                        "action": "search_ripgrep",
                        "file_pattern": file_pattern,
                        "case_sensitive": case_sensitive,
                    },
                )

            except TimeoutError:
                process.kill()
                await process.wait()
                return create_failure_result(
                    error="搜索超时（30秒）",
                    error_code="TIMEOUT",
                )

        except FileNotFoundError:
            # ripgrep被卸载了，回退到Python模式
            self.ripgrep_available = False
            return await self._search_with_python(inputs)

        except Exception as e:
            logger.warning(f"ripgrep搜索异常，回退到Python模式: {str(e)}")
            self.ripgrep_available = False
            return await self._search_with_python(inputs)

    async def _search_with_python(self, inputs: dict[str, Any]) -> ToolResult:
        """
        使用Python进行搜索（回退方案）

        性能：比ripgrep慢，但功能完整
        """
        try:
            query = inputs.get("query")
            search_path = inputs.get("path", str(self.base_path))
            file_pattern = inputs.get("file_pattern", "*")
            case_sensitive = inputs.get("case_sensitive", False)
            max_results = inputs.get("max_results", 100)
            use_regex = inputs.get("use_regex", False)
            context_lines = inputs.get("context_lines", 2)
            max_depth = inputs.get("max_depth", _DEFAULT_MAX_DEPTH)

            path = Path(search_path)
            # 路径存在性已在 _validate_search_path 中检查，此处不再重复

            # 编译搜索模式
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                if use_regex:
                    pattern = re.compile(query, flags)
                else:
                    pattern = re.compile(re.escape(query), flags)
            except re.error as e:
                return create_failure_result(
                    error=f"正则表达式错误: {str(e)}",
                    error_code="REGEX_ERROR",
                )

            # 搜索文件
            file_paths: list[str] = []
            line_numbers: list[int] = []
            contents: list[str] = []
            search_pattern = file_pattern if file_pattern != "*" else None

            for file_path in path.rglob("*"):
                # 跳过排除目录
                if any(part in _SKIP_DIRS for part in file_path.parts):
                    continue

                # 深度限制
                try:
                    depth = len(file_path.relative_to(path).parts)
                    if depth > max_depth:
                        continue
                except ValueError:
                    continue

                if search_pattern and not file_path.match(search_pattern):
                    continue

                if not file_path.is_file():
                    continue

                if file_path.stat().st_size > 1024 * 1024:
                    continue

                try:
                    # 读取文件
                    content = file_path.read_text(encoding="utf-8", errors="ignore")

                    # 搜索匹配
                    match_count = 0
                    lines = content.splitlines()
                    for line_num, line in enumerate(lines, 1):
                        if pattern.search(line):
                            match_count += 1
                            # 添加匹配行
                            rel_path = str(file_path.relative_to(path))
                            file_paths.append(rel_path)
                            line_numbers.append(line_num)
                            contents.append(line.strip())

                            # BUG-FIX: 收集前后 context_lines 行上下文（不计入 max_results）
                            for offset in range(1, context_lines + 1):
                                if line_num - 1 - offset >= 0:
                                    file_paths.append(rel_path)
                                    line_numbers.append(line_num - offset)
                                    contents.append(lines[line_num - 1 - offset].strip())
                                if line_num - 1 + offset < len(lines):
                                    file_paths.append(rel_path)
                                    line_numbers.append(line_num + offset)
                                    contents.append(lines[line_num - 1 + offset].strip())

                            if match_count >= max_results:
                                break

                except Exception:
                    # 忽略无法读取的文件
                    continue

                if len(file_paths) >= max_results:
                    break

            # BUG-FIX: 返回格式与 ripgrep 一致，使用 h/d/c 紧凑格式
            return create_success_result(
                data={
                    "query": query,
                    "engine": "python",
                    "h": ["file_path", "line_number", "content"],
                    "d": [[file_paths[i], line_numbers[i], contents[i]] for i in range(len(file_paths))],
                    "c": len(file_paths),
                },
                metadata={
                    "action": "search_python",
                    "file_pattern": file_pattern,
                    "case_sensitive": case_sensitive,
                },
            )

        except Exception as e:
            return create_failure_result(
                error=f"搜索失败: {str(e)}",
                error_code="SEARCH_FAILED",
            )

    @staticmethod
    def _should_skip_dir(fp: Path, search_root: Path, max_depth: int) -> bool:
        """判断是否应跳过该路径（排除目录 + 深度超限）。"""
        if any(part in _SKIP_DIRS for part in fp.parts):
            return True
        try:
            if len(fp.relative_to(search_root).parts) > max_depth:
                return True
        except ValueError:
            return True
        return False

    async def _search_filename(self, inputs: dict[str, Any]) -> ToolResult:
        """文件名搜索，支持 glob 通配符 (*, ?, []) 和正则表达式"""
        try:
            query = inputs.get("query")
            search_path = Path(inputs.get("path", str(self.base_path)))
            case_sensitive = inputs.get("case_sensitive", False)
            max_results = inputs.get("max_results", 100)
            use_regex = inputs.get("use_regex", False)
            max_depth = inputs.get("max_depth", _DEFAULT_MAX_DEPTH)

            # 路径存在性已在 _validate_search_path 中检查

            # 确定匹配策略（优先级: regex > glob > substring）
            _GLOB_CHARS = frozenset("*?[]")
            has_glob = any(c in query for c in _GLOB_CHARS)

            if use_regex:
                flags = 0 if case_sensitive else re.IGNORECASE
                try:
                    pattern = re.compile(query, flags)
                except re.error as e:
                    return create_failure_result(
                        error=f"无效的正则表达式: {e}",
                        error_code="INVALID_REGEX",
                    )
                match_mode = "regex"
            elif has_glob:
                match_mode = "glob"
            else:
                match_mode = "substring"

            file_names: list[str] = []
            file_sizes: list[str] = []
            file_paths: list[str] = []

            # 递归搜索（跳过排除目录 + 深度限制）
            for fp in search_path.rglob("*"):
                if len(file_names) >= max_results:
                    break

                if self._should_skip_dir(fp, search_path, max_depth):
                    continue

                if fp.is_file():
                    file_name = fp.name
                    compare_name = file_name if case_sensitive else file_name.lower()

                    matched = False
                    if match_mode == "regex":
                        matched = bool(pattern.search(compare_name))
                    elif match_mode == "glob":
                        matched = fnmatch.fnmatch(compare_name, query if case_sensitive else query.lower())
                    else:  # substring
                        search_query = query if case_sensitive else query.lower()
                        matched = search_query in compare_name

                    if matched:
                        try:
                            stat = fp.stat()
                            file_names.append(file_name)
                            file_sizes.append(format_size(stat.st_size))
                            file_paths.append(str(fp.relative_to(search_path)))
                        except Exception:
                            continue

            return create_success_result(
                data={
                    "query": query,
                    "search_type": "filename",
                    "match_mode": match_mode,
                    "h": ["file_name", "file_size", "file_path"],
                    "d": [[file_names[i], file_sizes[i], file_paths[i]] for i in range(len(file_names))],
                    "c": len(file_names),
                },
                metadata={"action": "search_filename", "match_mode": match_mode},
            )

        except Exception as e:
            return create_failure_result(
                error=f"文件名搜索失败: {str(e)}",
                error_code="SEARCH_FAILED",
            )
