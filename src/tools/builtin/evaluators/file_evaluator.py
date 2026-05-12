"""
文件评估器

检查文件/目录的存在性、内容等
"""

import re
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


class FileEvaluator:
    """文件评估器"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="file_evaluator",
            description="文件评估器：检查文件/目录的存在性、大小、内容",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录路径"},
                    "check": {
                        "type": "string",
                        "enum": ["exists", "not_empty", "contains", "is_directory"],
                        "description": "检查类型",
                        "default": "exists",
                    },
                    "pattern": {"type": "string", "description": "内容匹配模式"},
                    "min_size": {
                        "type": "integer",
                        "description": "最小字节数",
                        "default": 1,
                    },
                },
                "required": ["path"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            requires_approval=False,
            tags=["evaluator", "file"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行文件检查"""
        path = inputs.get("path")
        check = inputs.get("check", "exists")

        if not path:
            return create_failure_result(
                error="路径不能为空", error_code="MISSING_PATH"
            )

        try:
            file_path = Path(path)

            if check == "exists":
                return await self._check_exists(file_path)
            elif check == "not_empty":
                return await self._check_not_empty(file_path, inputs.get("min_size", 1))
            elif check == "contains":
                return await self._check_contains(file_path, inputs.get("pattern", ""))
            elif check == "is_directory":
                return await self._check_is_directory(file_path)
            else:
                return create_failure_result(error=f"不支持的检查类型: {check}")

        except Exception as e:
            return create_failure_result(error=f"文件检查失败: {str(e)}")

    async def _check_exists(self, path: Path) -> ToolResult:
        """检查文件/目录是否存在"""
        exists = path.exists()
        return create_success_result(
            data={
                "passed": exists,
                "score": 100 if exists else 0,
                "feedback": f"{'文件存在' if exists else '文件不存在'}: {path}",
                "details": {"path": str(path), "exists": exists},
            }
        )

    async def _check_not_empty(self, path: Path, min_size: int) -> ToolResult:
        """检查文件是否非空"""
        if not path.exists():
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"文件不存在: {path}",
                }
            )

        if path.is_dir():
            # 目录：检查是否有文件
            files = list(path.iterdir())
            passed = len(files) > 0
            return create_success_result(
                data={
                    "passed": passed,
                    "score": 100 if passed else 0,
                    "feedback": f"目录{'非空' if passed else '为空'}，包含 {len(files)} 个项目",
                    "details": {"file_count": len(files)},
                }
            )
        else:
            # 文件：检查大小
            size = path.stat().st_size
            passed = size >= min_size
            return create_success_result(
                data={
                    "passed": passed,
                    "score": 100 if passed else 0,
                    "feedback": f"文件大小 {size} 字节，{'满足' if passed else '不满足'}最小要求 {min_size}",
                    "details": {"size": size, "min_size": min_size},
                }
            )

    async def _check_contains(self, path: Path, pattern: str) -> ToolResult:
        """检查文件是否包含指定内容"""
        if not path.exists():
            return create_success_result(
                data={"passed": False, "score": 0, "feedback": f"文件不存在: {path}"}
            )

        if not pattern:
            return create_failure_result(error="匹配模式不能为空")

        try:
            content = path.read_text(encoding="utf-8")
            # 尝试正则匹配
            try:
                match = re.search(pattern, content)
                passed = match is not None
            except re.error:
                # 普通字符串匹配
                passed = pattern in content

            return create_success_result(
                data={
                    "passed": passed,
                    "score": 100 if passed else 0,
                    "feedback": f"文件{'包含' if passed else '不包含'}指定内容",
                    "details": {"pattern": pattern, "found": passed},
                }
            )
        except Exception as e:
            return create_failure_result(error=f"读取文件失败: {str(e)}")

    async def _check_is_directory(self, path: Path) -> ToolResult:
        """检查是否为目录"""
        exists = path.exists()
        is_dir = path.is_dir() if exists else False

        return create_success_result(
            data={
                "passed": is_dir,
                "score": 100 if is_dir else 0,
                "feedback": f"{'是目录' if is_dir else '不是目录或不存在'}: {path}",
                "details": {"exists": exists, "is_directory": is_dir},
            }
        )
