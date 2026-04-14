"""
增强代码搜索工具 - 集成ripgrep

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- EnhancedSearchTool：EnhancedSearchTool类
"""

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from tools.builtin.shared import format_size
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class EnhancedSearchTool:
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
        self.ripgrep_available = self._check_ripgrep()

        if self.ripgrep_available:
            print("[Ripgrep] 检测到ripgrep，已启用高性能模式")
        else:
            print(
                "[Search] ripgrep未安装，使用Python模式（建议安装ripgrep以获得更好性能）"
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
                },
                "required": ["query"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SEARCH,
            level=ToolLevel.USER,
            requires_approval=False,
            dangerous_operations=[],
            tags=["search", "code", "ripgrep", "performance", "filename"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行搜索"""
        workspace = inputs.get("workspace")
        if workspace:
            self.base_path = Path(workspace)

        query = inputs.get("query")
        if not query:
            return create_failure_result(
                error="搜索查询不能为空",
                error_code="MISSING_QUERY",
            )

        search_type = inputs.get("search_type", "text")

        if search_type == "filename":
            # 文件名搜索不支持 ripgrep，使用 Python 实现
            return await self._search_filename(inputs)
        elif search_type == "text":
            # 内容搜索优先使用 ripgrep
            if self.ripgrep_available:
                return await self._search_with_ripgrep(inputs)
            else:
                return await self._search_with_python(inputs)
        else:
            return create_failure_result(
                error=f"不支持的搜索类型: {search_type}",
                error_code="INVALID_SEARCH_TYPE",
            )

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

            # 限制结果数量
            cmd.extend(["--max-count", str(max_results)])

            # 执行搜索
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=Path.cwd(),
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

                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue

                    try:
                        entry = json.loads(line)

                        # 处理不同类型的消息
                        if entry.get("type") == "match":
                            data = entry.get("data", {})
                            file_paths.append(data.get("path", {}).get("text", ""))
                            line_numbers.append(data.get("line_number", 0))
                            contents.append(data.get("lines", {}).get("text", "").strip())

                    except json.JSONDecodeError:
                        # 忽略非JSON行
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

            path = Path(search_path)
            if not path.exists():
                return create_failure_result(
                    error=f"搜索路径不存在: {search_path}",
                    error_code="PATH_NOT_FOUND",
                )

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
            file_paths = []
            line_numbers = []
            contents = []
            search_pattern = file_pattern if file_pattern != "*" else None

            for file_path in path.rglob("*"):
                if search_pattern and not file_path.match(search_pattern):
                    continue

                if not file_path.is_file():
                    continue

                try:
                    # 读取文件
                    content = file_path.read_text(encoding="utf-8", errors="ignore")

                    # 搜索匹配
                    for line_num, line in enumerate(content.splitlines(), 1):
                        if pattern.search(line):
                            file_paths.append(str(file_path.relative_to(path)))
                            line_numbers.append(line_num)
                            contents.append(line.strip())

                            if len(file_paths) >= max_results:
                                break

                except Exception:
                    # 忽略无法读取的文件
                    continue

                if len(file_paths) >= max_results:
                    break

            return create_success_result(
                data={
                    "query": query,
                    "engine": "python",
                    "file_paths": file_paths,
                    "line_numbers": line_numbers,
                    "contents": contents,
                    "count": len(file_paths),
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

    async def _search_filename(self, inputs: dict[str, Any]) -> ToolResult:
        """文件名搜索"""
        try:
            query = inputs.get("query")
            search_path = Path(inputs.get("path", str(self.base_path)))
            case_sensitive = inputs.get("case_sensitive", False)
            max_results = inputs.get("max_results", 100)

            if not search_path.exists():
                return create_failure_result(
                    error=f"搜索路径不存在: {search_path}",
                    error_code="PATH_NOT_FOUND",
                )

            file_names = []
            file_sizes = []
            file_paths = []

            # 如果不区分大小写，转换查询为小写
            search_query = query if case_sensitive else query.lower()

            # 递归搜索
            for file_path in search_path.rglob("*"):
                if len(file_names) >= max_results:
                    break

                if file_path.is_file():
                    file_name = file_path.name
                    compare_name = file_name if case_sensitive else file_name.lower()

                    if search_query in compare_name:
                        try:
                            stat = file_path.stat()
                            file_names.append(file_name)
                            file_sizes.append(format_size(stat.st_size))
                            file_paths.append(str(file_path.relative_to(search_path)))
                        except Exception:
                            continue

            return create_success_result(
                data={
                    "query": query,
                    "search_type": "filename",
                    "h": ["file_name", "file_size", "file_path"],
                    "d": [[file_names[i], file_sizes[i], file_paths[i]] for i in range(len(file_names))],
                    "c": len(file_names),
                },
                metadata={"action": "search_filename"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"文件名搜索失败: {str(e)}",
                error_code="SEARCH_FAILED",
            )
