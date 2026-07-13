"""
目录创建工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- CreateDirectoryTool：目录创建工具类
"""

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


class CreateDirectoryTool(BuiltinTool, WorkspaceAwareMixin):
    """
    目录创建工具

    支持创建目录及其父目录，可选择是否处理目录已存在的情况。
    """

    def __init__(self, base_path: str | None = None):
        """初始化目录创建工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="create_directory",
            description="创建新目录。适用场景：需要创建新的工作目录、创建项目文件夹、整理文件时新建目录。"
            "不适用场景：需要创建文件（使用 file_write）、需要复制文件（使用 copy_file）。",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要创建的目录路径（相对路径或绝对路径）",
                    },
                    "parents": {
                        "type": "boolean",
                        "description": "是否创建父目录（即创建多层嵌套目录），默认 true",
                        "default": True,
                    },
                    "exist_ok": {
                        "type": "boolean",
                        "description": "当目录已存在时是否报错，默认 true（会报错）。设为 false 则不报错",
                        "default": True,
                    },
                },
                "required": ["path"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE_SYSTEM,
            level=ToolLevel.USER,
            tags=["directory", "create", "mkdir", "folder"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]):  # noqa: PLR0911
        """执行目录创建操作"""
        self._init_workspace(inputs)
        self._init_agent_level(inputs)

        try:
            path_str = inputs.get("path")
            parents = inputs.get("parents", True)
            exist_ok = inputs.get("exist_ok", True)

            if not path_str:
                return create_failure_result(
                    error="目录路径不能为空",
                    error_code="MISSING_PATH",
                )

            path = self.resolve_path(path_str)
            display_path = self._format_output_path(path, path_str)

            # 路径越界校验：创建目录是写操作，必须落在 workspace 允许范围内
            agent_level = getattr(self, "_agent_level", None)
            ok, err = self.check_path_allowed(str(path), "write", agent_level)
            if not ok:
                return create_failure_result(
                    error=f"路径越界拒绝: {err}",
                    error_code="PATH_NOT_ALLOWED",
                )

            # 检查目录是否已存在
            if path.exists():
                if not exist_ok:
                    # exist_ok=False 且目录存在，不报错但返回信息
                    return create_success_result(
                        data={
                            "path": display_path,
                            "created": False,
                            "existed": True,
                            "message": "目录已存在",
                        },
                        metadata={"action": "create_directory"},
                    )
                if path.is_dir():
                    return create_failure_result(
                        error=f"目录已存在: {display_path}",
                        error_code="DIRECTORY_EXISTS",
                    )
                return create_failure_result(
                    error=f"路径已存在但不是目录: {display_path}",
                    error_code="PATH_EXISTS_NOT_DIRECTORY",
                )

            # 创建目录
            path.mkdir(parents=parents, exist_ok=exist_ok)

            return create_success_result(
                data={
                    "path": display_path,
                    "created": True,
                    "existed": False,
                },
                metadata={"action": "create_directory"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限不足，无法创建目录: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"创建目录失败: {str(e)}",
                error_code="CREATE_FAILED",
            )
