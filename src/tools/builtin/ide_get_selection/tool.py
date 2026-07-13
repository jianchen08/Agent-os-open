"""
IDE 获取选区工具

通过连接器获取 IDE 当前上下文（活动文件、选中文本、光标位置），
无连接器时返回提示信息。

暴露接口：
- IDEGetSelectionTool: IDE 获取选区工具
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


class IDEGetSelectionTool(BuiltinTool):
    """IDE 获取选区工具。

    行为：
    1. 检查是否有活跃连接器
    2. 有连接器 → 通过连接器获取上下文
    3. 无连接器 → 降级为返回"无连接器，请手动提供上下文"
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
            name="ide_get_selection",
            description="获取当前 IDE 中的上下文信息，包括活动文件、选中文本和光标位置。无连接器时返回提示信息。",
            when_to_use=["需要获取用户当前在 IDE 中的上下文"],
            when_not_to_use=["已经知道文件路径和内容"],
            input_schema={
                "type": "object",
                "properties": {},
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE,
            level=ToolLevel.USER,
            tags=["ide", "context", "selection"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行获取选区操作。"""
        from connectors.degradation import DegradationManager  # noqa: PLC0415

        # 尝试通过连接器获取上下文
        connector = self._get_active_connector()
        if connector is not None:
            try:
                context = await connector.get_context()
                data: dict[str, Any] = {
                    "active_file": context.active_file,
                    "selected_text": context.selected_text,
                    "cursor_position": (
                        {
                            "line": context.cursor_position.line,
                            "column": context.cursor_position.column,
                        }
                        if context.cursor_position
                        else None
                    ),
                    "open_files": context.open_files,
                    "connector": connector.connector_type,
                }
                return create_success_result(data=data)
            except Exception as e:
                logger.warning(f"连接器获取上下文失败，降级处理: {e}")

        # 降级处理
        manager = DegradationManager()
        result = manager.execute_with_fallback("get_selection", {})
        if result.success:
            return create_success_result(data=result.data)
        return create_failure_result(error=result.error or "降级执行失败")

    def _get_active_connector(self) -> Any:
        """获取活跃连接器。"""
        if self._registry is None:
            return None
        return self._registry.get_active_connector()
