"""
文件删除工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- DeleteFileTool：文件删除工具类
"""

import contextlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from tools.builtin.base import BuiltinTool
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


class DeleteFileTool(BuiltinTool, WorkspaceAwareMixin):
    """
    文件删除工具

    支持文件和目录的删除操作，可选择递归删除和强制删除（包括只读文件）。
    """

    def __init__(self, base_path: str | None = None):
        """初始化文件删除工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="delete_file",
            description="删除文件或目录。适用场景：清理临时文件、删除不需要的文件、清理工作目录。"
            "注意：删除操作不可恢复，请谨慎使用。递归删除会删除整个目录树。",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的文件或目录路径（相对路径或绝对路径），与 paths 二选一",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "批量删除文件路径列表（与 path 二选一，优先使用 paths）",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归删除目录，默认 false（仅删除空目录）",
                        "default": False,
                    },
                    "force": {
                        "type": "boolean",
                        "description": "是否强制删除（包括只读文件、系统文件），默认 false",
                        "default": False,
                    },
                },
                "required": [],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE_SYSTEM,
            level=ToolLevel.USER,
            tags=["file", "delete", "remove", "cleanup"],
            injected_params=["workspace"],
            dangerous_operations=["delete:recursive", "delete:force", "delete:protected"],
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行文件删除操作"""
        self._init_workspace(inputs)

        # 优先使用 paths 批量参数
        paths = inputs.get("paths")
        if paths and isinstance(paths, list):
            return await self._delete_files(inputs, paths)

        # 单文件模式
        return await self._delete_single(inputs)

    async def _delete_files(self, inputs: dict[str, Any], paths: list[str]) -> ToolResult:
        """批量删除文件，每个文件独立返回结果"""
        results = []
        recursive = inputs.get("recursive", False)
        force = inputs.get("force", False)

        for path_str in paths:
            file_inputs = {
                "path": path_str,
                "recursive": recursive,
                "force": force,
            }
            result = await self._delete_single(file_inputs)
            results.append(
                {
                    "path": path_str,
                    "success": result.success,
                    "data": result.output if result.success else None,
                    "error": result.error if not result.success else None,
                }
            )

        success_count = sum(1 for r in results if r["success"])
        failed_count = len(results) - success_count

        return create_success_result(
            data={
                "results": results,
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": failed_count,
                },
            },
            metadata={"action": "batch_delete_files"},
        )

    async def _delete_single(self, inputs: dict[str, Any]):
        """删除单个文件"""
        try:
            path_str = inputs.get("path")
            recursive = inputs.get("recursive", False)
            force = inputs.get("force", False)

            if not path_str:
                return create_failure_result(
                    error="删除路径不能为空",
                    error_code="MISSING_PATH",
                )

            path = self.resolve_path(path_str)
            display_path = self._format_output_path(path, path_str)

            # 检查路径是否存在
            if not path.exists():
                return create_failure_result(
                    error=f"文件/目录不存在: {display_path}",
                    error_code="NOT_FOUND",
                )

            # 执行删除
            if path.is_dir():
                await self._delete_directory(path, recursive, force)
            else:
                await self._delete_file(path, force)

            return create_success_result(
                data={
                    "path": display_path,
                    "deleted": True,
                    "type": "directory" if path.is_dir() else "file",
                },
                metadata={"action": "delete_file"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限不足，无法删除: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"删除失败: {str(e)}",
                error_code="DELETE_FAILED",
            )

    async def _delete_file(self, path: Path, force: bool):
        """删除单个文件"""
        if force:
            # 移除只读属性
            os.chmod(path, stat.S_IWRITE)  # noqa: PTH101
        path.unlink()

    async def _delete_directory(self, path: Path, recursive: bool, force: bool):
        """删除目录"""
        if recursive:
            if force:
                # 强制递归删除：先移除所有文件的只读属性
                self._make_writable(path)
            shutil.rmtree(path)
        else:
            # 非递归删除：只删除空目录
            path.rmdir()

    def _make_writable(self, path: Path):
        """递归设置目录和文件为可写状态"""
        if path.is_dir():
            with contextlib.suppress(Exception):
                os.chmod(path, stat.S_IWRITE | stat.S_IXUSR)  # noqa: PTH101
            for child in path.iterdir():
                self._make_writable(child)
        else:
            with contextlib.suppress(Exception):
                os.chmod(path, stat.S_IWRITE)  # noqa: PTH101
