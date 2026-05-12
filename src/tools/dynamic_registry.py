"""
动态工具注册表

处理需要依赖注入（如数据库会话）的工具注册
"""

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class DynamicToolRegistry:
    """
    动态工具注册表

    负责注册需要依赖注入的工具（如需要数据库会话的 TaskTool、TaskSubmitTool）
    """

    def __init__(self):
        """初始化动态工具注册表"""
        self._registered_tools: dict[str, Any] = {}

    async def register_all_dynamic_tools(
        self,
        registry: Any,
        session: AsyncSession,
        evaluator_callback: Callable | None = None,
    ) -> dict[str, str]:
        """
        注册所有动态工具（需要依赖注入的工具）

        Args:
            registry: ToolRegistry 实例
            session: 数据库会话
            evaluator_callback: 评估器回调函数

        Returns:
            注册的工具名称映射 {tool_name: tool_id}
        """
        registered = {}

        # 1. 注册 TaskSubmitTool (需要 session)
        try:
            from src.tools.builtin.task_submit import TaskSubmitTool

            tool_instance = TaskSubmitTool(session=session)
            tool_def = tool_instance.get_tool_definition()
            tool_id = registry.register_with_handler(
                tool=tool_def, handler=tool_instance.execute
            )
            registered["task_submit"] = tool_id
            logger.debug(f"[动态注册] task_submit 已注册，ID: {tool_id}")
        except Exception as e:
            logger.warning(f"[动态注册] task_submit 注册失败: {e}")

        # 2. 注册 TaskTool (只需要 session)
        try:
            from src.tools.builtin.task import TaskTool

            tool_instance = TaskTool(session=session)
            tool_def = tool_instance.get_tool_definition()
            tool_id = registry.register_with_handler(
                tool=tool_def, handler=tool_instance.execute
            )
            registered["task_manage"] = tool_id
            logger.debug(f"[动态注册] task_manage 已注册，ID: {tool_id}")
        except Exception as e:
            logger.warning(f"[动态注册] task_manage 注册失败: {e}")

        # 3. 注册 TaskEvaluateTool (只需要 session)
        try:
            from src.tools.builtin.task_evaluate import TaskEvaluateTool

            tool_instance = TaskEvaluateTool(session=session)
            tool_def = tool_instance.get_tool_definition()
            tool_id = registry.register_with_handler(
                tool=tool_def, handler=tool_instance.execute
            )
            registered["task_evaluate"] = tool_id
            logger.debug(f"[动态注册] task_evaluate 已注册，ID: {tool_id}")
        except Exception as e:
            logger.warning(f"[动态注册] task_evaluate 注册失败: {e}")

        # 4. 注册 MemoryTool (需要 session)
        try:
            from src.tools.builtin.memory import MemoryTool

            tool_instance = MemoryTool(session=session)
            tool_def = tool_instance.get_tool_definition()
            tool_id = registry.register_with_handler(
                tool=tool_def, handler=tool_instance.execute
            )
            registered["memory_retrieve"] = tool_id
            logger.debug(f"[动态注册] memory_retrieve 已注册，ID: {tool_id}")
        except Exception as e:
            logger.warning(f"[动态注册] memory_retrieve 注册失败: {e}")

        # 5. 其他需要依赖注入的工具可以在此添加

        self._registered_tools.update(registered)
        return registered

    def get_registered_tools(self) -> dict[str, str]:
        """
        获取已注册的工具列表

        Returns:
            工具名称映射
        """
        return self._registered_tools.copy()

    def is_registered(self, tool_name: str) -> bool:
        """
        检查工具是否已注册

        Args:
            tool_name: 工具名称

        Returns:
            是否已注册
        """
        return tool_name in self._registered_tools
