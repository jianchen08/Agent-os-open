"""
回滚工具

暴露接口：
- manager(self) -> RollbackManager：manager功能
- get_tool_definition() -> Tool：get_tool_definition功能
- RollbackTool：RollbackTool类
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from rollback.manager import RollbackManager, get_rollback_manager
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class RollbackTool:
    """
    回滚工具

    提供：
    - 创建检查点
    - 回滚任务操作
    - 查询检查点和操作日志
    """

    def __init__(self, session: AsyncSession | None = None):
        """初始化回滚工具"""
        self.session = session
        self._manager: RollbackManager | None = None

    @property
    def manager(self) -> RollbackManager:
        """获取回滚管理器"""
        if self._manager is None:
            self._manager = get_rollback_manager(self.session)
        return self._manager

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="rollback_task",
            description="任务回滚工具：创建检查点、回滚操作、查询操作历史。"
            "适用场景：将任务回滚到之前状态、查看任务操作历史、查看所有检查点、任务出错时撤销操作、关键步骤前创建检查点。"
            "不适用场景：任务尚未开始时、需要删除任务本身时（使用任务管理工具）、仅需查看任务状态时。"
            "注意事项：回滚操作不可逆，执行前请确认；回滚后任务状态会恢复到检查点时的状态；"
            "部分操作可能无法完全回滚（如已发送的通知）；需要确保有足够的权限执行回滚操作。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create_checkpoint",
                            "rollback",
                            "list_checkpoints",
                            "list_operations",
                        ],
                        "description": "操作类型：create_checkpoint(创建检查点), rollback(回滚到检查点), "
                        "list_checkpoints(列出所有检查点), list_operations(列出操作历史)",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "任务ID，用于标识要操作的任务",
                    },
                    "checkpoint_id": {
                        "type": "string",
                        "description": "检查点ID（当action=rollback时可选，指定要回滚到的检查点）",
                    },
                    "checkpoint_name": {
                        "type": "string",
                        "description": "检查点名称（当action=create_checkpoint时可选，用于标识检查点）",
                    },
                    "steps": {
                        "type": "integer",
                        "description": "回滚步数（当action=rollback时可选，指定回滚多少步操作，不指定则回滚到指定检查点）",
                    },
                },
                "required": ["action", "task_id"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.ALL,
            requires_approval=False,
            dangerous_operations=[],
            tags=["rollback", "checkpoint", "undo"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行工具"""
        action = inputs.get("action")
        task_id = inputs.get("task_id")

        if not task_id:
            return create_failure_result(
                error="任务 ID 不能为空",
                error_code="MISSING_TASK_ID",
            )

        if action == "create_checkpoint":
            return await self._create_checkpoint(inputs)
        elif action == "rollback":
            return await self._rollback(inputs)
        elif action == "list_checkpoints":
            return await self._list_checkpoints(inputs)
        elif action == "list_operations":
            return await self._list_operations(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    async def _create_checkpoint(self, inputs: dict[str, Any]) -> ToolResult:
        """创建检查点"""
        try:
            task_id = inputs.get("task_id")
            name = inputs.get("checkpoint_name")
            description = inputs.get("description")

            checkpoint_id = await self.manager.create_checkpoint(
                task_id=task_id,
                name=name,
                description=description,
            )

            return create_success_result(
                data={
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "name": name,
                },
                metadata={"action": "create_checkpoint"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"创建检查点失败: {str(e)}",
                error_code="CREATE_CHECKPOINT_FAILED",
            )

    async def _rollback(self, inputs: dict[str, Any]) -> ToolResult:
        """回滚操作"""
        try:
            task_id = inputs.get("task_id")
            checkpoint_id = inputs.get("checkpoint_id")
            steps = inputs.get("steps")

            result = await self.manager.rollback(
                task_id=task_id,
                to_checkpoint=checkpoint_id,
                steps=steps,
            )

            return create_success_result(
                data=result.to_dict(),
                metadata={"action": "rollback"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"回滚失败: {str(e)}",
                error_code="ROLLBACK_FAILED",
            )

    async def _list_checkpoints(self, inputs: dict[str, Any]) -> ToolResult:
        """列出检查点"""
        try:
            task_id = inputs.get("task_id")

            checkpoints = await self.manager.list_checkpoints(task_id)

            return create_success_result(
                data={
                    "task_id": task_id,
                    "checkpoints": [cp.to_dict() for cp in checkpoints],
                    "count": len(checkpoints),
                },
                metadata={"action": "list_checkpoints"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"列出检查点失败: {str(e)}",
                error_code="LIST_CHECKPOINTS_FAILED",
            )

    async def _list_operations(self, inputs: dict[str, Any]) -> ToolResult:
        """列出操作日志"""
        try:
            task_id = inputs.get("task_id")
            checkpoint_id = inputs.get("checkpoint_id")

            operations = await self.manager.list_operations(
                task_id=task_id,
                checkpoint_id=checkpoint_id,
            )

            return create_success_result(
                data={
                    "task_id": task_id,
                    "operations": [op.to_dict() for op in operations],
                    "count": len(operations),
                },
                metadata={"action": "list_operations"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"列出操作日志失败: {str(e)}",
                error_code="LIST_OPERATIONS_FAILED",
            )
