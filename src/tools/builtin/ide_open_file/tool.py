"""
IDE 打开文件工具

通过连接器在 IDE 中打开文件，无连接器时降级为读取文件内容。

暴露接口：
- IDEOpenFileTool: IDE 打开文件工具
"""

from __future__ import annotations

import logging
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class IDEOpenFileTool(BuiltinTool):
    """IDE 打开文件工具。

    行为：
    1. 检查是否有活跃连接器
    2. 有连接器 → 通过连接器发送 open_file 指令
    3. 无连接器 → 降级为使用 file_read 读取文件内容并返回
    """

    def __init__(self, registry: Any = None) -> None:
        """初始化工具。

        Args:
            registry: 连接器注册表实例（可选）
        """
        self._registry = registry

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="ide_open_file",
            description="在 IDE 中打开指定文件，并可选跳转到特定位置。无连接器时降级为读取文件内容。",
            when_to_use=["需要在 IDE 中打开文件进行编辑", "需要跳转到文件特定行"],
            when_not_to_use=["仅需读取文件内容（使用 file_read）"],
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要打开的文件路径",
                    },
                    "line": {
                        "type": "integer",
                        "description": "跳转到的行号（可选）",
                    },
                    "column": {
                        "type": "integer",
                        "description": "跳转到的列号（可选）",
                    },
                },
                "required": ["file_path"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE,
            level=ToolLevel.USER,
            tags=["ide", "file", "open"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行打开文件操作。"""
        from connectors.degradation import DegradationManager  # noqa: PLC0415
        from connectors.types import ConnectorAction  # noqa: PLC0415

        file_path = inputs.get("file_path", "")
        if not file_path:
            return create_failure_result(error="file_path 参数不能为空")

        line = inputs.get("line")
        column = inputs.get("column")

        # 尝试通过连接器执行
        connector = self._get_active_connector()
        if connector is not None:
            action = ConnectorAction(
                action_type="open_file",
                parameters={"file_path": file_path, "line": line, "column": column},
            )
            try:
                result = await connector.execute_action(action)
                if result.success:
                    return create_success_result(
                        data={
                            "message": f"已在 IDE 中打开文件: {file_path}",
                            "file_path": file_path,
                            **({"line": line} if line is not None else {}),
                            **({"column": column} if column is not None else {}),
                            "connector": connector.connector_type,
                        },
                    )
                return create_failure_result(
                    error=f"连接器执行失败: {result.error}",
                )
            except Exception as e:
                logger.warning(f"连接器执行失败，降级处理: {e}")

        # 降级处理
        manager = DegradationManager()
        params: dict[str, Any] = {"file_path": file_path}
        if line is not None:
            params["line"] = line
        if column is not None:
            params["column"] = column

        result = manager.execute_with_fallback("open_file", params)
        if result.success:
            return create_success_result(data=result.data)
        return create_failure_result(error=result.error or "降级执行失败")

    def _get_active_connector(self) -> Any:
        """获取活跃连接器。"""
        if self._registry is None:
            return None
        return self._registry.get_active_connector()
