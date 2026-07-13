"""
文件复制工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- CopyFileTool：文件复制工具类
"""

import shutil
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


class CopyFileTool(BuiltinTool, WorkspaceAwareMixin):
    """
    文件复制工具

    支持文件和目录的复制操作，可选择是否覆盖已存在目标。
    """

    def __init__(self, base_path: str | None = None):
        """初始化文件复制工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="copy_file",
            description="复制文件或目录到目标位置。适用场景：需要备份文件、复制配置文件、创建文件副本。"
            "不适用场景：需要移动文件（使用 move_file）、需要创建目录（使用 create_directory）。",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "源文件或源目录路径（相对路径或绝对路径），与 copies 二选一",
                    },
                    "destination": {
                        "type": "string",
                        "description": "目标路径（相对路径或绝对路径），与 copies 二选一",
                    },
                    "copies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "destination": {"type": "string"},
                            },
                            "required": ["source", "destination"],
                        },
                        "description": "批量复制列表（与 source/destination 二选一，优先使用 copies）",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "是否覆盖已存在的目标文件/目录，默认 false",
                        "default": False,
                    },
                },
                "required": [],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE_SYSTEM,
            level=ToolLevel.USER,
            tags=["file", "copy", "backup", "duplicate"],
            injected_params=["workspace"],
            dangerous_operations=["copy:overwrite"],
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行文件复制操作"""
        self._init_workspace(inputs)

        # 优先使用 copies 批量参数
        copies = inputs.get("copies")
        if copies and isinstance(copies, list):
            return await self._copy_files(inputs, copies)

        # 单文件模式
        return await self._copy_single(inputs)

    async def _copy_files(self, inputs: dict[str, Any], copies: list[dict]) -> ToolResult:
        """批量复制文件，每个独立返回结果"""
        results = []
        overwrite = inputs.get("overwrite", False)

        for copy_item in copies:
            source_str = copy_item.get("source")
            dest_str = copy_item.get("destination")
            file_inputs = {
                "source": source_str,
                "destination": dest_str,
                "overwrite": overwrite,
            }
            result = await self._copy_single(file_inputs)
            results.append(
                {
                    "source": source_str,
                    "destination": dest_str,
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
            metadata={"action": "batch_copy_files"},
        )

    async def _copy_single(self, inputs: dict[str, Any]):  # noqa: PLR0911
        """复制单个文件"""
        try:
            source_str = inputs.get("source")
            dest_str = inputs.get("destination")
            overwrite = inputs.get("overwrite", False)

            if not source_str:
                return create_failure_result(
                    error="源路径不能为空",
                    error_code="MISSING_SOURCE",
                )

            if not dest_str:
                return create_failure_result(
                    error="目标路径不能为空",
                    error_code="MISSING_DESTINATION",
                )

            source = self.resolve_path(source_str)
            dest = self.resolve_path(dest_str)

            display_source = self._format_output_path(source, source_str)
            display_dest = self._format_output_path(dest, dest_str)

            # 检查源路径是否存在
            if not source.exists():
                return create_failure_result(
                    error=f"源文件/目录不存在: {display_source}",
                    error_code="SOURCE_NOT_FOUND",
                )

            # 检查目标是否已存在
            if dest.exists() and not overwrite:
                return create_failure_result(
                    error=f"目标已存在: {display_dest}。如需覆盖，请设置 overwrite=true。",
                    error_code="DESTINATION_EXISTS",
                )

            # 执行复制
            if source.is_dir():
                await self._copy_directory(source, dest, overwrite)
            else:
                await self._copy_file(source, dest, overwrite)

            return create_success_result(
                data={
                    "source": display_source,
                    "destination": display_dest,
                    "copied": True,
                    "type": "directory" if source.is_dir() else "file",
                },
                metadata={"action": "copy_file"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限不足: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"复制失败: {str(e)}",
                error_code="COPY_FAILED",
            )

    async def _copy_file(self, source: Path, dest: Path, overwrite: bool):
        """复制单个文件"""
        # 如果目标存在且需要覆盖，先删除
        if dest.exists():
            if overwrite:
                dest.unlink()
            else:
                raise FileExistsError(f"目标文件已存在: {dest}")

        # 确保目标父目录存在
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)

    async def _copy_directory(self, source: Path, dest: Path, overwrite: bool):
        """复制目录"""
        if dest.exists():
            if not overwrite:
                raise FileExistsError(f"目标目录已存在: {dest}")
            # 覆盖模式下，先删除目标目录
            import shutil as sh  # noqa: PLC0415

            sh.rmtree(dest)

        # 复制目录
        shutil.copytree(source, dest)
