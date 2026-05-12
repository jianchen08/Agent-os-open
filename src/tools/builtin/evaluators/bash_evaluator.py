"""
Bash 评估器

检查命令执行结果
"""

import asyncio
import time
from typing import Any

from src.tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class BashEvaluator:
    """Bash 评估器"""

    DEFAULT_TIMEOUT = 60  # 默认超时时间（秒）

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="bash_evaluator",
            description="Bash 评估器：检查命令执行结果",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令",
                    },
                    "check": {
                        "type": "string",
                        "enum": ["success", "time"],
                        "description": "检查类型",
                        "default": "success",
                    },
                    "max_seconds": {
                        "type": "number",
                        "description": "最大执行时间（秒）",
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "工作目录",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "命令超时时间（秒）",
                        "default": 60,
                    },
                },
                "required": ["command"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            requires_approval=True,  # 命令执行需要审批
            tags=["evaluator", "bash", "command"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行命令检查"""
        command = inputs.get("command")
        check = inputs.get("check", "success")

        if not command:
            return create_failure_result(
                error="命令不能为空",
                error_code="MISSING_COMMAND",
            )

        if check == "success":
            return await self._check_success(command, inputs)
        if check == "time":
            return await self._check_time(command, inputs)

        return create_failure_result(error=f"不支持的检查类型: {check}")

    async def _check_success(
        self,
        command: str,
        inputs: dict[str, Any],
    ) -> ToolResult:
        """检查命令是否成功执行"""
        working_dir = inputs.get("working_dir")
        timeout = inputs.get("timeout", self.DEFAULT_TIMEOUT)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )

            passed = process.returncode == 0
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")

            return create_success_result(
                data={
                    "passed": passed,
                    "score": 100 if passed else 0,
                    "feedback": (
                        "命令执行成功"
                        if passed
                        else f"命令执行失败，返回码: {process.returncode}"
                    ),
                    "details": {
                        "return_code": process.returncode,
                        "stdout": stdout_text[-2000:],  # 截取最后 2000 字符
                        "stderr": stderr_text[-1000:],
                    },
                }
            )
        except TimeoutError:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"命令执行超时（{timeout}s）",
                }
            )
        except FileNotFoundError:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": "命令不存在或工作目录无效",
                }
            )

    async def _check_time(
        self,
        command: str,
        inputs: dict[str, Any],
    ) -> ToolResult:
        """检查命令执行时间"""
        max_seconds = inputs.get("max_seconds")
        if max_seconds is None:
            return create_failure_result(error="max_seconds 不能为空")

        working_dir = inputs.get("working_dir")
        timeout = inputs.get("timeout", max(self.DEFAULT_TIMEOUT, max_seconds * 2))

        start_time = time.time()

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )

            execution_time = time.time() - start_time
            passed = execution_time <= max_seconds

            return create_success_result(
                data={
                    "passed": passed,
                    "score": (
                        100
                        if passed
                        else max(0, int(100 * max_seconds / execution_time))
                    ),
                    "feedback": (
                        f"执行时间 {execution_time:.2f}s，"
                        f"{'符合' if passed else '超过'}限制 {max_seconds}s"
                    ),
                    "details": {
                        "execution_time": execution_time,
                        "max_seconds": max_seconds,
                        "return_code": process.returncode,
                    },
                }
            )
        except TimeoutError:
            execution_time = time.time() - start_time
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"命令执行超时（{execution_time:.2f}s）",
                    "details": {
                        "execution_time": execution_time,
                        "max_seconds": max_seconds,
                    },
                }
            )
