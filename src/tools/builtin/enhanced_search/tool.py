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
import os  # noqa: F401
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 安全常量 ──────────────────────────────────────────────────

# 跳过的目录（性能 + 安全）
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".workbuddy",
    }
)

# 默认最大递归深度
_DEFAULT_MAX_DEPTH: int = 20

# 敏感系统目录黑名单统一由共享模块维护（security_check 与本工具复用同一份）
from isolation.sensitive_paths import is_sensitive_path  # noqa: E402
from tools.builtin.base import BuiltinTool  # noqa: E402
from tools.builtin.shared import format_size  # noqa: E402
from tools.builtin.workspace_aware import WorkspaceAwareMixin  # noqa: E402
from tools.types import (  # noqa: E402
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

    def _check_ripgrep(self) -> bool:
        """检查ripgrep是否可用"""
        try:
            proc = subprocess.run(["rg", "--version"], capture_output=True, timeout=5)  # noqa: PLW1510
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
            injected_params=["workspace", "parent_agent_level"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行搜索"""
        self._init_workspace(inputs)
        self.base_path = self._workspace

        # 解析 agent 层级供后续校验使用
        raw_level = inputs.get("parent_agent_level", 1)
        try:
            self._agent_level = int(str(raw_level).upper().lstrip("L"))
        except (ValueError, TypeError):
            self._agent_level = 1

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
        if search_type == "text":
            if not self._check_ripgrep():
                return create_failure_result(
                    error="ripgrep 未安装，无法执行搜索（请安装 ripgrep 后重试）",
                    error_code="RIPGREP_NOT_AVAILABLE",
                )
            return await self._search_with_ripgrep(inputs)
        return create_failure_result(
            error=f"不支持的搜索类型: {search_type}",
            error_code="INVALID_SEARCH_TYPE",
        )

    def _validate_search_path(self, search_path_str: str, fallback_boundary: Path | None = None) -> ToolResult | None:
        """校验搜索路径安全性。

        返回 None 表示通过；返回 ToolResult 表示校验失败。

        三道底线检查：
        1. 权限范围：路径必须在当前策略允许的可读范围内（由 check_path_allowed 决策）
        2. 路径存在性：给定路径必须真实存在
        3. 敏感系统目录黑名单：禁止搜索 OS 核心目录

        Args:
            search_path_str: 待校验的搜索路径
            fallback_boundary: 历史遗留参数（保留签名兼容）
        """
        search_path = Path(search_path_str).resolve()

        # ── 检查 1：权限范围（统一路径校验，按 agent 层级 + 读权限策略决策） ──
        agent_level = getattr(self, "_agent_level", None)
        ok, err = self.check_path_allowed(str(search_path), "read", agent_level)
        if not ok:
            return create_failure_result(
                error=f"搜索路径超出允许范围: {search_path_str}（{err}）",
                error_code="PATH_NOT_FOUND",
            )

        # ── 检查 2：路径存在性 ──
        if not search_path.exists():
            return create_failure_result(
                error=f"搜索路径不存在: {search_path_str}",
                error_code="PATH_NOT_FOUND",
            )

        # ── 检查 3：敏感系统目录黑名单（共享常量） ──
        hit, matched = is_sensitive_path(str(search_path))
        if hit:
            return create_failure_result(
                error=f"禁止搜索系统目录: {search_path_str}（命中黑名单: {matched}）",
                error_code="SENSITIVE_PATH_BLOCKED",
            )

        return None

    async def _search_with_ripgrep(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0912,PLR0915
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
            max_depth = inputs.get("max_depth", _DEFAULT_MAX_DEPTH)

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

            # 最大递归深度（--max-depth 全称，兼容 rg 13.x；rg 15+ 才支持 -d 短写）
            cmd.extend(["--max-depth", str(max_depth)])

            # 正则表达式 / 字面量搜索
            if use_regex:
                # ripgrep默认就是正则，不需要额外参数
                pass
            else:
                # 字面量搜索
                cmd.append("--fixed-strings")
                # 字面量查询含换行时，需开启 multiline 才能跨行匹配，
                # 否则 rg 会报 "the literal is not allowed in a regex"
                if "\n" in query or "\r" in query:
                    cmd.append("-U")

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

                if process.returncode not in {0, 1}:
                    # 1表示没找到结果，这是正常的
                    error_msg = stderr.decode("utf-8", errors="replace")
                    # rg 正则语法错误（exit 2，stderr 含 "regex parse error"）
                    # 映射为 REGEX_ERROR，与历史错误码语义一致
                    if "regex parse error" in error_msg:
                        return create_failure_result(
                            error=f"正则表达式错误: {error_msg}",
                            error_code="REGEX_ERROR",
                        )
                    return create_failure_result(
                        error=f"搜索失败: {error_msg}",
                        error_code="SEARCH_FAILED",
                    )

                # 解析JSON输出
                # rg --json 流顺序：[begin] (context-before)* match (context-after)* [end] [summary]
                # max_results 限制 match 行数；context 行不计入上限，随匹配行一起返回。
                # 达到上限后仍需 drain 当前 match 的 context-after，否则会丢失上下文。
                file_paths: list[str] = []
                line_numbers: list[int] = []
                contents: list[str] = []
                match_count = 0
                # 达到 max_results 上限后置 True：不再采纳新 match，但继续消费
                # 当前文件的尾部 context，遇到 begin/end/summary 时才安全停止
                stop_accepting = False

                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue

                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    entry_type = entry.get("type")

                    # 已达 match 上限且遇到文件边界/流结束 → 当前 context 已 drain 完
                    if stop_accepting and entry_type in {"begin", "end", "summary"}:
                        break

                    if entry_type == "match":
                        if stop_accepting:
                            # 上限已达，忽略后续 match（含其 context）
                            continue
                        match_count += 1
                        data = entry.get("data", {})
                        file_paths.append(data.get("path", {}).get("text", ""))
                        line_numbers.append(data.get("line_number", 0))
                        contents.append(data.get("lines", {}).get("text", "").strip())
                        # 采纳该 match 后若已达上限，置位但继续 drain 它的 context-after
                        if match_count >= max_results:
                            stop_accepting = True
                    elif entry_type == "context":
                        data = entry.get("data", {})
                        file_paths.append(data.get("path", {}).get("text", ""))
                        line_numbers.append(data.get("line_number", 0))
                        contents.append(data.get("lines", {}).get("text", "").strip())

                return create_success_result(
                    data={
                        "query": query,
                        "engine": "ripgrep",
                        "match_count": match_count,
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
            # ripgrep 不在 PATH 中
            return create_failure_result(
                error="ripgrep 未安装，无法执行搜索（请安装 ripgrep 后重试）",
                error_code="RIPGREP_NOT_AVAILABLE",
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

    async def _search_filename(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0912
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
            _GLOB_CHARS = frozenset("*?[]")  # noqa: N806
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

            # 递归搜索（跳过排除目录 + 深度限制 + 不可访问路径静默跳过）
            for fp in search_path.rglob("*"):
                if len(file_names) >= max_results:
                    break

                if self._should_skip_dir(fp, search_path, max_depth):
                    continue

                try:
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
                except OSError:
                    # 跳过不可访问的路径（跨容器目录权限、死链接等）
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
