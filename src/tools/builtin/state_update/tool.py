"""
工作流状态更新工具

暴露接口：
- create_state_update_tool() -> StateUpdateTool：create_state_update_tool功能
- get_tool_definition() -> Tool：get_tool_definition功能
- StateUpdateTool：StateUpdateTool类
"""

import logging
from typing import Any

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class StateUpdateTool(BuiltinTool):
    """
    工作流状态更新工具

    用于在工作流执行过程中更新共享状态变量
    """

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="state_update",
            description="在工作流执行过程中更新共享状态变量。"
            "适用场景：更新重试计数器、在工作流节点间传递累计结果、更新工作流级状态变量。"
            "不适用场景：仅需在节点内部使用临时变量时、需要更新数据库记录时（使用持久化工具）。"
            "注意事项：更新的变量会被添加到shared_variables中；支持increment(增量)和append(追加)操作；"
            "context参数用于获取当前状态值。"
            '示例：{"updates": {"retry_count": {"operation": "increment", "value": 1}}} 表示将retry_count增加1',
            input_schema={
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "object",
                        "description": "要更新的状态变量键值对。支持两种格式："
                        '1. 直接赋值：{"key": value}；'
                        '2. 操作模式：{"key": {"operation": "increment"/"append", "value": n}}。'
                        "increment操作用于数值累加，append操作用于列表追加",
                        "additionalProperties": True,
                    }
                },
                "required": ["updates"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.SYSTEM,
            tags=["workflow", "state", "system"],
        )

    async def execute(
        self,
        inputs: dict[str, Any],
        context: Any = None,
    ) -> ToolExecutionResult:
        """执行状态更新"""
        updates = inputs.get("updates", {})

        try:
            logger.info("[工作流状态更新] 更新变量: %s", list(updates.keys()))

            # 获取当前共享变量（用于增量操作）
            current_shared = {}
            if context and hasattr(context, "metadata"):
                # 尝试从上下文获取当前状态
                current_shared = context.metadata.get("shared_variables", {})

            result_updates = {}

            for key, value in updates.items():
                if isinstance(value, dict) and "operation" in value:
                    # 处理特殊操作
                    operation = value.get("operation")
                    operand = value.get("value", 0)

                    if operation == "increment":
                        # 增量操作
                        current_val = current_shared.get(key, 0)
                        if isinstance(current_val, int):
                            result_updates[key] = current_val + operand
                        else:
                            result_updates[key] = operand
                    elif operation == "append":
                        # 追加操作
                        current_val = current_shared.get(key, [])
                        if isinstance(current_val, list):
                            result_updates[key] = current_val + [operand]
                        else:
                            result_updates[key] = [operand]
                    else:
                        logger.warning("[工作流状态更新] 未知操作: %s", operation)
                        result_updates[key] = value
                else:
                    # 直接赋值
                    result_updates[key] = value

            # 返回更新的键值对，这些将被合并到 shared_variables 中
            result_data = {
                "success": True,
                "updated": list(result_updates.keys()),
                "updates": result_updates,
            }

            # 将更新项直接添加到结果中
            result_data.update(result_updates)

            logger.info("[工作流状态更新] 更新完成: %s", result_updates)

            return create_success_result(
                data=result_data,
                metadata={"action": "state_update"},
            )

        except Exception as e:
            logger.error("[工作流状态更新] 更新失败: %s", e)
            return create_failure_result(
                error=f"状态更新失败: {str(e)}",
                metadata={"action": "state_update"},
            )


def create_state_update_tool() -> StateUpdateTool:
    """创建状态更新工具实例"""
    return StateUpdateTool()


__all__ = ["StateUpdateTool", "create_state_update_tool"]
