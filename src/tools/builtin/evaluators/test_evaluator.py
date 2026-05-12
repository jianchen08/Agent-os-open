"""
测试评估器

检查测试是否通过、覆盖率是否达标
"""

import asyncio
import re
from typing import Any

from src.tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class TestEvaluator:
    """测试评估器"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="test_evaluator",
            description="测试评估器：检查测试通过率和覆盖率",
            input_schema={
                "type": "object",
                "properties": {
                    "check": {
                        "type": "string",
                        "enum": ["passed", "coverage"],
                        "default": "passed",
                    },
                    "path": {"type": "string", "description": "测试文件或目录路径"},
                    "command": {"type": "string", "default": "pytest"},
                    "min_coverage": {"type": "number", "default": 80},
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            requires_approval=False,
            tags=["evaluator", "test", "pytest"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行测试检查"""
        check = inputs.get("check", "passed")
        path = inputs.get("path", "")
        command = inputs.get("command", "pytest")

        try:
            if check == "passed":
                return await self._check_test_passed(command, path)
            elif check == "coverage":
                min_coverage = inputs.get("min_coverage", 80)
                return await self._check_coverage(command, path, min_coverage)
            else:
                return create_failure_result(error=f"不支持的检查类型: {check}")
        except Exception as e:
            return create_failure_result(error=f"测试检查失败: {str(e)}")

    async def _check_test_passed(self, command: str, path: str) -> ToolResult:
        """检查测试是否通过"""
        cmd = f"{command} {path} -v --tb=short" if path else f"{command} -v --tb=short"

        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            output = stdout.decode("utf-8", errors="replace")

            # 解析测试结果
            passed = process.returncode == 0

            # 尝试提取测试统计
            stats = self._parse_pytest_output(output)

            return create_success_result(
                data={
                    "passed": passed,
                    "score": 100 if passed else 0,
                    "feedback": f"测试{'全部通过' if passed else '存在失败'}",
                    "details": {
                        "return_code": process.returncode,
                        "stats": stats,
                        "output": output[-2000:] if len(output) > 2000 else output,
                    },
                }
            )
        except TimeoutError:
            return create_success_result(
                data={"passed": False, "score": 0, "feedback": "测试执行超时"}
            )
        except Exception as e:
            return create_failure_result(error=f"执行测试失败: {str(e)}")

    async def _check_coverage(
        self, command: str, path: str, min_coverage: float
    ) -> ToolResult:
        """检查测试覆盖率"""
        cmd = (
            f"{command} {path} --cov --cov-report=term"
            if path
            else f"{command} --cov --cov-report=term"
        )

        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            output = stdout.decode("utf-8", errors="replace")

            # 解析覆盖率
            coverage = self._parse_coverage(output)
            passed = coverage >= min_coverage if coverage is not None else False

            return create_success_result(
                data={
                    "passed": passed,
                    "score": coverage if coverage else 0,
                    "feedback": f"覆盖率 {coverage}%，{'达标' if passed else '未达标'}（要求 {min_coverage}%）",
                    "details": {
                        "coverage": coverage,
                        "min_coverage": min_coverage,
                    },
                }
            )
        except TimeoutError:
            return create_success_result(
                data={"passed": False, "score": 0, "feedback": "覆盖率检查超时"}
            )
        except Exception as e:
            return create_failure_result(error=f"覆盖率检查失败: {str(e)}")

    def _parse_pytest_output(self, output: str) -> dict[str, int]:
        """解析 pytest 输出"""
        stats = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}

        # 匹配 "X passed, Y failed, Z skipped"
        match = re.search(r"(\d+) passed", output)
        if match:
            stats["passed"] = int(match.group(1))

        match = re.search(r"(\d+) failed", output)
        if match:
            stats["failed"] = int(match.group(1))

        match = re.search(r"(\d+) skipped", output)
        if match:
            stats["skipped"] = int(match.group(1))

        match = re.search(r"(\d+) error", output)
        if match:
            stats["error"] = int(match.group(1))

        return stats

    def _parse_coverage(self, output: str) -> float:
        """解析覆盖率输出"""
        # 匹配 "TOTAL ... XX%"
        match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
        if match:
            return float(match.group(1))

        # 匹配其他格式
        match = re.search(r"(\d+(?:\.\d+)?)%\s*$", output, re.MULTILINE)
        if match:
            return float(match.group(1))

        return None
