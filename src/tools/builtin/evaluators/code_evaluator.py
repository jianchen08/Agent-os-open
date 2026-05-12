"""
代码评估器

检查代码的 Lint、语法、类型等
"""

import asyncio
from pathlib import Path
from typing import Any

from src.tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class CodeEvaluator:
    """代码评估器"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="code_evaluator",
            description="代码评估器：检查 Lint、语法、类型",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "代码文件或目录路径",
                    },
                    "check": {
                        "type": "string",
                        "enum": ["lint", "syntax", "type"],
                        "description": "检查类型",
                        "default": "lint",
                    },
                    "tool": {
                        "type": "string",
                        "description": "检查工具（lint=ruff, type=mypy）",
                    },
                    "language": {
                        "type": "string",
                        "description": "编程语言",
                        "default": "python",
                    },
                },
                "required": ["path"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            requires_approval=False,
            tags=["evaluator", "code", "lint", "syntax", "type"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行代码检查"""
        path = inputs.get("path")
        check = inputs.get("check", "lint")
        language = inputs.get("language", "python")

        if not path:
            return create_failure_result(
                error="路径不能为空",
                error_code="MISSING_PATH",
            )

        file_path = Path(path)
        if not file_path.exists():
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"文件不存在: {path}",
                }
            )

        if check == "lint":
            return await self._check_lint(file_path, language, inputs)
        if check == "syntax":
            return await self._check_syntax(file_path, language)
        if check == "type":
            return await self._check_type(file_path, language, inputs)

        return create_failure_result(error=f"不支持的检查类型: {check}")

    async def _check_lint(
        self,
        path: Path,
        language: str,
        inputs: dict[str, Any],
    ) -> ToolResult:
        """Lint 检查"""
        tool = inputs.get("tool", "ruff")

        if language == "python":
            return await self._run_python_lint(path, tool)

        return create_failure_result(error=f"暂不支持 {language} 的 Lint 检查")

    async def _run_python_lint(self, path: Path, tool: str) -> ToolResult:
        """运行 Python Lint 检查"""
        if tool == "ruff":
            cmd = f"ruff check {path} --output-format=json"
        else:
            cmd = f"flake8 {path} --format=json"

        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=60,
            )
            output = stdout.decode("utf-8", errors="replace")

            passed = process.returncode == 0
            issues = self._parse_lint_output(output, tool)

            return create_success_result(
                data={
                    "passed": passed,
                    "score": 100 if passed else max(0, 100 - len(issues) * 5),
                    "feedback": (
                        "Lint 检查通过" if passed else f"发现 {len(issues)} 个问题"
                    ),
                    "details": {
                        "tool": tool,
                        "issues": issues[:20],  # 最多返回 20 个
                        "total_issues": len(issues),
                    },
                }
            )
        except TimeoutError:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": "Lint 检查超时",
                }
            )
        except FileNotFoundError:
            return create_failure_result(error=f"Lint 工具未安装: {tool}")

    def _parse_lint_output(self, output: str, tool: str) -> list:
        """解析 Lint 输出"""
        import json

        try:
            if tool == "ruff":
                return json.loads(output) if output.strip() else []
            return []
        except json.JSONDecodeError:
            return []

    async def _check_syntax(self, path: Path, language: str) -> ToolResult:
        """语法检查"""
        if language == "python":
            return await self._check_python_syntax(path)

        return create_failure_result(error=f"暂不支持 {language} 的语法检查")

    async def _check_python_syntax(self, path: Path) -> ToolResult:
        """Python 语法检查"""
        import ast

        try:
            content = path.read_text(encoding="utf-8")
            ast.parse(content)

            return create_success_result(
                data={
                    "passed": True,
                    "score": 100,
                    "feedback": "语法检查通过",
                }
            )
        except SyntaxError as e:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"语法错误: {e.msg}",
                    "details": {
                        "line": e.lineno,
                        "offset": e.offset,
                        "text": e.text,
                    },
                }
            )

    async def _check_type(
        self,
        path: Path,
        language: str,
        inputs: dict[str, Any],
    ) -> ToolResult:
        """类型检查"""
        tool = inputs.get("tool", "mypy")

        if language == "python":
            return await self._run_mypy(path, tool)

        return create_failure_result(error=f"暂不支持 {language} 的类型检查")

    async def _run_mypy(self, path: Path, tool: str) -> ToolResult:
        """运行 mypy 类型检查"""
        cmd = f"mypy {path} --no-error-summary"

        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=120,
            )
            output = stdout.decode("utf-8", errors="replace")

            passed = process.returncode == 0
            errors = [
                line for line in output.strip().split("\n") if line and "error:" in line
            ]

            return create_success_result(
                data={
                    "passed": passed,
                    "score": 100 if passed else max(0, 100 - len(errors) * 10),
                    "feedback": (
                        "类型检查通过" if passed else f"发现 {len(errors)} 个类型错误"
                    ),
                    "details": {
                        "tool": tool,
                        "errors": errors[:10],
                        "total_errors": len(errors),
                    },
                }
            )
        except TimeoutError:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": "类型检查超时",
                }
            )
        except FileNotFoundError:
            return create_failure_result(error="mypy 未安装")
