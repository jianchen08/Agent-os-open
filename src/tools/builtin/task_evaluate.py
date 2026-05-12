"""
任务评估工具

Agent 调用接口，所有评估业务逻辑由 EvaluationService 处理。

核心原则：
- 此文件只是接口层，不包含业务逻辑
- 任务状态变更为 completed 只能通过 EvaluationService.complete_task_after_evaluation()
- 所有状态转换通过 TaskStateService 进行
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.results import ToolExecutionResult
from src.db.models import Task
from src.tasks.services.evaluation_service import EvaluationService
from src.tasks.services.state_service import TaskStateService
from src.tools.executor import ExecutionContext, ToolExecutor
from src.tools.global_registry import get_global_tool_registry_sync
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class TaskEvaluateTool:
    """
    任务评估工具（接口层）

    只负责：
    1. 解析输入参数
    2. 调用 EvaluationService 执行评估
    3. 格式化输出结果

    不包含任何业务逻辑。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务评估工具

        Args:
            session: 数据库会话
        """
        self.session = session
        registry = get_global_tool_registry_sync()
        self.tool_executor = ToolExecutor(registry, db_session=session)

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="task_evaluate",
            description="任务评估工具：用于评估任务的评估指标是否满足。支持两种模式：evaluate_single(评估单个指标，增量模式，需提供metric_id)、auto_complete(自动评估所有指标，推荐)。完成任务执行后使用，验证是否满足验收标准。注意：任务未完成时先执行任务；无验收标准的任务无法评估；指标重试5次后任务会被标记为blocked。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["evaluate_single", "auto_complete"],
                        "description": "评估类型：evaluate_single-评估单个指标(增量模式，需要提供metric_id)，auto_complete-自动评估所有指标(推荐，一次性完成评估)，默认为auto_complete",
                        "default": "auto_complete",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "任务ID，从任务上下文中获取",
                    },
                    "metric_id": {
                        "type": "string",
                        "description": "评估指标ID，仅在evaluate_single模式时必填",
                    },
                    "summary": {
                        "type": "string",
                        "description": "任务完成说明（可选），用于语义类评估器",
                    },
                },
                "required": ["task_id", "action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            requires_approval=False,
            dangerous_operations=[],
            tags=["task", "evaluate", "metric", "completion"],
            injected_params=["session_id", "user_id", "tool_record_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""
        action = inputs.get("action", "auto_complete")
        task_id = inputs.get("task_id")
        tool_record_id = inputs.get("tool_record_id")

        if not task_id:
            return create_failure_result(
                error="任务 ID 不能为空", error_code="MISSING_TASK_ID"
            )

        if action == "evaluate_single":
            return await self._evaluate_single(inputs, tool_record_id)
        elif action == "auto_complete":
            return await self._auto_complete(inputs, tool_record_id)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}", error_code="INVALID_ACTION"
            )

    async def _evaluate_single(
        self, inputs: dict[str, Any], tool_record_id: str | None = None
    ) -> ToolExecutionResult:
        """
        评估单个指标（增量模式）

        当前实现：调用 auto_complete 逻辑
        TODO: 后续可以优化为只评估单个指标
        """
        return await self._auto_complete(inputs, tool_record_id)

    async def _auto_complete(
        self, inputs: dict[str, Any], tool_record_id: str | None = None
    ) -> ToolExecutionResult:
        """
        自动完成评估（评估所有指标）

        调用 EvaluationService.execute_and_apply() 执行评估。
        评估器根据预设配置自主验证，不需要传入 artifacts。
        """
        task_id = inputs.get("task_id")
        summary = inputs.get("summary", "")

        try:
            task = await self.session.get(Task, task_id)
            if not task:
                return create_failure_result(
                    error="任务不存在", error_code="TASK_NOT_FOUND"
                )

            state_service = TaskStateService(session=self.session)
            eval_service = EvaluationService(
                session=self.session,
                state_service=state_service,
                tool_executor=self.tool_executor,
            )

            result = await eval_service.execute_and_apply(
                task_id=task_id,
                summary=summary,
                tool_record_id=tool_record_id,
            )

            if result.get("error"):
                return create_failure_result(
                    error=result.get("error"),
                    error_code=result.get("error_code", "EVAL_FAILED"),
                )

            return create_success_result(
                data=result,
                metadata={
                    "action": "auto_complete",
                    "result": result.get("task_status"),
                },
            )

        except Exception as e:
            logger.exception(f"自动完成评估失败: {e}")
            return create_failure_result(
                error=f"评估失败: {str(e)}", error_code="EVAL_FAILED"
            )
