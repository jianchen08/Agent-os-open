"""
任务评估工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskEvaluateTool：任务评估工具类
"""

import logging
from typing import Any

from core.results import ToolExecutionResult
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class TaskEvaluateTool:
    """任务评估工具（接口层）。

    只负责：
    1. 解析输入参数
    2. 调用 EvaluationExecutor 执行评估
    3. 格式化输出结果

    不包含任何业务逻辑。
    """

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
                    "metric_id": {
                        "type": "string",
                        "description": "评估指标ID，仅在evaluate_single模式时必填",
                    },
                    "summary": {
                        "type": "string",
                        "description": "任务完成说明（可选），用于语义类评估器",
                    },
                },
                "required": [],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            requires_approval=False,
            dangerous_operations=[],
            tags=["task", "evaluate", "metric", "completion"],
            injected_params=["session_id", "user_id", "tool_record_id", "task_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行任务评估。

        通过 injected_params 获取 task_id 等运行时参数，
        通过 TaskService 获取任务数据，通过 EvaluationExecutor 执行评估。
        """
        action = inputs.get("action", "auto_complete")
        task_id = inputs.get("task_id")

        if not task_id:
            return create_failure_result(
                error="系统错误：task_id 未注入，请联系管理员",
                error_code="INJECTION_ERROR",
            )

        # 获取 TaskService
        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="TaskService 不可用",
                error_code="SERVICE_UNAVAILABLE",
            )

        # 查询任务
        task = task_service.get_task(task_id)
        if task is None:
            return create_failure_result(
                error="任务不存在", error_code="TASK_NOT_FOUND"
            )

        if action == "evaluate_single":
            return self._evaluate_single(inputs, task_service, task)
        elif action == "auto_complete":
            return self._auto_complete(inputs, task_service, task)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}", error_code="INVALID_ACTION"
            )

    def _evaluate_single(
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """评估单个指标（增量模式）。

        如果任务只有一个指标，自动转为完全评估。

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
        """
        metric_id = inputs.get("metric_id")
        task_id = task.id

        if not metric_id:
            return create_failure_result(
                error="单指标评估模式需要提供 metric_id",
                error_code="METRIC_ID_REQUIRED",
            )

        # 单指标任务自动转为完全评估
        metric_ids = self._get_metric_ids(task)
        if len(metric_ids) == 1:
            logger.info(
                "[TaskEvaluate] 单指标任务自动转为完全评估 | "
                "task_id=%s | metric_count=%d",
                task_id, len(metric_ids),
            )
            return self._auto_complete(inputs, task_service, task)

        try:
            executor = self._create_executor(task_service)
            result = executor.run_evaluation(
                task_id=task_id,
                metric_ids=[metric_id],
            )

            return create_success_result(
                data=self._build_result_data(result),
                metadata={
                    "action": "evaluate_single",
                    "metric_id": metric_id,
                    "result": "passed" if result.overall_passed else "failed",
                },
            )

        except Exception as e:
            logger.exception("[TaskEvaluate] 单指标评估失败: %s", e)
            return create_failure_result(
                error=f"评估失败: {e}", error_code="EVAL_FAILED"
            )

    def _auto_complete(
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """自动完成评估（评估所有指标）。

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
        """
        task_id = task.id

        try:
            executor = self._create_executor(task_service)
            result = executor.run_evaluation(task_id=task_id)

            return create_success_result(
                data=self._build_result_data(result),
                metadata={
                    "action": "auto_complete",
                    "result": "passed" if result.overall_passed else "failed",
                },
            )

        except Exception as e:
            logger.exception("[TaskEvaluate] 自动完成评估失败: %s", e)
            return create_failure_result(
                error=f"评估失败: {e}", error_code="EVAL_FAILED"
            )

    def _get_task_service(self) -> Any:
        """获取共享的 TaskService 实例。

        获取优先级：
        1. sys._agent_os_task_service（CLI 设置的全局共享实例）
        2. 创建新实例（降级兜底）

        Returns:
            TaskService 实例，获取失败返回 None
        """
        try:
            import sys
            global_ts = getattr(sys, "_agent_os_task_service", None)
            if global_ts is not None:
                return global_ts
            from tasks.service import TaskService

            return TaskService()
        except Exception as e:
            logger.error("[TaskEvaluate] TaskService 创建失败: %s", e)
            return None

    def _create_executor(self, task_service: Any) -> Any:
        """创建 EvaluationExecutor 实例。

        Args:
            task_service: TaskService 实例，用于状态回写

        Returns:
            EvaluationExecutor 实例
        """
        from evaluation.executor import EvaluationExecutor

        return EvaluationExecutor(task_service=task_service)

    def _get_metric_ids(self, task: Any) -> list[str]:
        """从任务模型中提取评估指标 ID 列表。

        Args:
            task: TaskModel 实例

        Returns:
            指标 ID 列表
        """
        # 优先从 metadata 中获取
        if task.metadata and "evaluation_metric_ids" in task.metadata:
            return task.metadata["evaluation_metric_ids"]
        # 从验收标准中提取
        if task.metadata and "acceptance_criteria" in task.metadata:
            return list(task.metadata["acceptance_criteria"].keys())
        return []

    def _build_result_data(self, result: Any) -> dict[str, Any]:
        """将评估结果构建为工具返回数据。

        Args:
            result: EvaluationResult 实例

        Returns:
            可序列化的结果字典
        """
        return {
            "task_id": result.task_id,
            "overall_passed": result.overall_passed,
            "summary": result.summary,
            "metrics": [
                {
                    "metric_id": r.metric_id,
                    "passed": r.passed,
                    "score": r.score,
                    "message": r.message,
                    "error": r.error,
                }
                for r in result.results
            ],
        }
