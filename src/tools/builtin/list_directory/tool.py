"""
目录列表工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- ListDirectoryTool：目录列表工具类
"""

import fnmatch
from pathlib import Path
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.builtin.workspace_aware import WorkspaceAwareMixin
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class ListDirectoryTool(BuiltinTool, WorkspaceAwareMixin):
    """
    目录列表工具

    列出单层目录内容，支持隐藏文件过滤和文件名模式匹配。
    仅展示直接子项，Agent 应逐层探索子目录。
    """

    def __init__(self, base_path: str | None = None):
        """初始化目录列表工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="list_directory",
            description="列出目录的直接子项（文件和目录），包括名称、类型和大小。"
            "仅展示一层，需浏览子目录内容请再次调用并指定子目录路径。",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径（相对路径或绝对路径）",
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "是否包含隐藏文件（以.开头），默认 false",
                        "default": False,
                    },
                    "pattern": {
                        "type": "string",
                        "description": "文件名匹配模式（支持 glob 语法，如 *.py, test_*.txt）",
                    },
                },
                "required": ["path"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE_SYSTEM,
            level=ToolLevel.USER,
            tags=["file", "directory", "list", "browse"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行目录列表操作"""
        self._init_workspace(inputs)

        try:
            path_str = inputs.get("path")
            if not path_str:
                return create_failure_result(
                    error="目录路径不能为空",
                    error_code="MISSING_PATH",
                )

            include_hidden = inputs.get("include_hidden", False)
            pattern = inputs.get("pattern")

            path = self.resolve_path(path_str)
            display_path = self._format_output_path(path, path_str)

            if not path.exists():
                return create_failure_result(
                    error=f"目录不存在: {display_path}",
                    error_code="DIRECTORY_NOT_FOUND",
                )

            if not path.is_dir():
                return create_failure_result(
                    error=f"路径不是目录: {display_path}",
                    error_code="NOT_A_DIRECTORY",
                )

            items = self._list_directory(path, include_hidden, pattern)

            return create_success_result(
                data={
                    "items": items,
                },
                metadata={"action": "list_directory"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限不足: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"列出目录失败: {str(e)}",
                error_code="LIST_FAILED",
            )

    def _list_directory(
        self,
        directory: Path,
        include_hidden: bool,
        pattern: str | None,
    ) -> list[dict[str, Any]]:
        """列出目录的直接子项"""
        items = []
        try:
            for entry in sorted(directory.iterdir()):
                if not include_hidden and entry.name.startswith("."):
                    continue

                if pattern and not fnmatch.fnmatch(entry.name, pattern):
                    continue

                items.append(self._get_item_info(entry))
        except PermissionError:
            pass

        return items

    def _get_item_info(self, path: Path) -> dict[str, Any]:
        """获取目录项信息"""
        try:
            stat = path.stat()
            return {
                "name": path.name,
                "type": "directory" if path.is_dir() else "file",
                "size": self._format_size(stat.st_size),
            }
        except Exception:
            return {
                "name": path.name,
                "type": "directory" if path.is_dir() else "file",
                "size": "0B",
            }

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f}MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"
