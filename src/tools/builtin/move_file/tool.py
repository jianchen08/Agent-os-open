"""
文件移动/重命名工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- MoveFileTool：文件移动工具类
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


class MoveFileTool(BuiltinTool, WorkspaceAwareMixin):
    """
    文件移动/重命名工具

    支持文件和目录的移动或重命名操作，可选择是否覆盖已存在目标。
    """

    def __init__(self, base_path: str | None = None):
        """初始化文件移动工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="move_file",
            description="移动文件或目录到目标位置，也可用于重命名。适用场景：需要整理文件、搬迁目录、重命名文件。"
            "不适用场景：需要复制文件（使用 copy_file）、需要创建目录（使用 create_directory）。",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "源文件或源目录路径（相对路径或绝对路径），与 moves 二选一",
                    },
                    "destination": {
                        "type": "string",
                        "description": "目标路径（相对路径或绝对路径），与 moves 二选一",
                    },
                    "moves": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "destination": {"type": "string"},
                            },
                            "required": ["source", "destination"],
                        },
                        "description": "批量移动列表（与 source/destination 二选一，优先使用 moves）",
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
            tags=["file", "move", "rename", "relocate"],
            injected_params=["workspace"],
            dangerous_operations=["move:overwrite"],
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行文件移动操作"""
        self._init_workspace(inputs)

        # 优先使用 moves 批量参数
        moves = inputs.get("moves")
        if moves and isinstance(moves, list):
            return await self._move_files(inputs, moves)

        # 单文件模式
        return await self._move_single(inputs)

    async def _move_files(self, inputs: dict[str, Any], moves: list[dict]) -> ToolResult:
        """批量移动文件，每个独立返回结果"""
        results = []
        overwrite = inputs.get("overwrite", False)

        for move_item in moves:
            source_str = move_item.get("source")
            dest_str = move_item.get("destination")
            file_inputs = {
                "source": source_str,
                "destination": dest_str,
                "overwrite": overwrite,
            }
            result = await self._move_single(file_inputs)
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
            metadata={"action": "batch_move_files"},
        )

    async def _move_single(self, inputs: dict[str, Any]):  # noqa: PLR0911
        """移动单个文件"""
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

            # 执行移动
            if dest.exists() and overwrite:
                if dest.is_dir():
                    import shutil as sh  # noqa: PLC0415

                    sh.rmtree(dest)
                else:
                    dest.unlink()

            shutil.move(str(source), str(dest))

            return create_success_result(
                data={
                    "source": display_source,
                    "destination": display_dest,
                    "moved": True,
                    "type": "directory" if source.is_dir() else "file",
                },
                metadata={"action": "move_file"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限不足: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"移动失败: {str(e)}",
                error_code="MOVE_FAILED",
            )
