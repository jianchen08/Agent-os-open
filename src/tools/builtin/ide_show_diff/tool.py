"""
IDE 显示差异工具

通过连接器在 IDE 中显示文件差异，无连接器时降级为生成 unified diff 文本。

暴露接口：
- IDEShowDiffTool: IDE 显示差异工具
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


class IDEShowDiffTool(BuiltinTool):
    """IDE 显示差异工具。

    行为：
    1. 检查是否有活跃连接器
    2. 有连接器 → 通过连接器发送 show_diff 指令
    3. 无连接器 → 降级为生成 unified diff 文本输出
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
            name="ide_show_diff",
            description="在 IDE 中显示文件差异对比视图。无连接器时降级为生成 unified diff 文本输出。",
            when_to_use=["需要向用户展示文件修改前后的差异"],
            when_not_to_use=["仅需比较两个文件内容（使用 bash_execute 的 diff 命令）"],
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径",
                    },
                    "original_content": {
                        "type": "string",
                        "description": "原始文件内容",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "修改后的文件内容",
                    },
                    "title": {
                        "type": "string",
                        "description": "差异视图标题（可选）",
                    },
                },
                "required": ["file_path", "original_content", "new_content"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE,
            level=ToolLevel.USER,
            tags=["ide", "diff", "compare"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行显示差异操作。"""
        from connectors.degradation import DegradationManager  # noqa: PLC0415
        from connectors.types import ConnectorAction  # noqa: PLC0415

        file_path = inputs.get("file_path", "")
        original_content = inputs.get("original_content", "")
        new_content = inputs.get("new_content", "")
        title = inputs.get("title", "")

        if not file_path:
            return create_failure_result(error="file_path 参数不能为空")

        # 尝试通过连接器执行
        connector = self._get_active_connector()
        if connector is not None:
            action = ConnectorAction(
                action_type="show_diff",
                parameters={
                    "file_path": file_path,
                    "original_content": original_content,
                    "new_content": new_content,
                    "title": title,
                },
            )
            try:
                result = await connector.execute_action(action)
                if result.success:
                    return create_success_result(
                        data={
                            "message": f"已在 IDE 中显示差异: {file_path}",
                            "file_path": file_path,
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
        result = manager.execute_with_fallback(
            "show_diff",
            {
                "file_path": file_path,
                "original_content": original_content,
                "new_content": new_content,
                "title": title,
            },
        )
        if result.success:
            return create_success_result(data=result.data)
        return create_failure_result(error=result.error or "降级执行失败")

    def _get_active_connector(self) -> Any:
        """获取活跃连接器。"""
        if self._registry is None:
            return None
        return self._registry.get_active_connector()
