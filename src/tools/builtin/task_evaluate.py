"""任务评估工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskEvaluateTool：任务评估工具类
"""

import logging
from typing import Any

from tasks.types import TaskStatus

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

_DEFAULT_MAX_RETRIES = 3

_VALID_EVALUATE_STATUSES = {TaskStatus.RUNNING, TaskStatus.EVALUATING}
_VALID_AUTO_COMPLETE_STATUSES = {TaskStatus.RUNNING, TaskStatus.EVALUATING}


def _simple_evaluate(task: Any, notes: str = "") -> tuple[bool, str]:
    """简化评估逻辑：根据任务状态判断是否通过。

    Args:
        task: TaskModel 实例
        notes: 评估备注

    Returns:
        (passed, detail) 元组
    """
    from tasks.types import TaskStatus

    ac = (task.metadata or {}).get("acceptance_criteria", {})

    if not ac:
        detail = "无验收标准，默认通过"
        if notes:
            detail += f"；备注：{notes}"
        return True, detail

    if task.result is None and not task.result:
        detail = "无执行结果，评估不通过"
        if notes:
            detail += f"；备注：{notes}"
        return False, detail

    detail = f"共 {len(ac)} 项验收标准，均有执行结果"
    if notes:
        detail += f"；备注：{notes}"
    return True, detail


def task_evaluate_func(inputs: dict[str, Any]) -> dict[str, Any]:
    """同步任务评估函数（供测试和简单场景使用）。

    Args:
        inputs: 包含 action 和 task_id 的字典

    Returns:
        评估结果字典
    """
    from tasks.types import TaskStatus

    action = inputs.get("action")
    task_id = inputs.get("task_id")

    if not action:
        return {"success": False, "error_code": "MISSING_ACTION", "error": "缺少 action 参数"}

    if not task_id:
        return {"success": False, "error_code": "MISSING_TASK_ID", "error": "缺少 task_id 参数"}

    if action not in ("evaluate_single", "auto_complete"):
        return {"success": False, "error_code": "INVALID_ACTION", "error": f"不支持的操作: {action}"}

    try:
        from tasks.service import TaskService

        task_service = TaskService()
    except Exception:
        return {"success": False, "error_code": "SERVICE_UNAVAILABLE", "error": "TaskService 不可用"}

    task = task_service.get_task(task_id)
    if task is None:
        return {"success": False, "error_code": "TASK_NOT_FOUND", "error": "任务不存在"}

    if action == "evaluate_single":
        valid_statuses = _VALID_EVALUATE_STATUSES
    else:
        valid_statuses = _VALID_AUTO_COMPLETE_STATUSES

    if task.status == TaskStatus.RUNNING:
        try:
            task_service.move_to_evaluating(task_id)
        except Exception:
            pass

    if task.status not in valid_statuses and task.status != TaskStatus.RUNNING:
        return {"success": False, "error_code": "INVALID_STATUS", "error": f"不支持的状态: {task.status}"}

    try:
        if inputs.get("result") is not None:
            task.result = inputs["result"]

        task_service.complete_evaluation(task_id, passed=True)
        return {"success": True, "status": "completed"}
    except Exception as e:
        return {"success": False, "error_code": "EVAL_FAILED", "error": str(e)}


class TaskEvaluateTool:
    """任务评估工具。

    负责：
    1. 解析输入参数
    2. 调用 EvaluationExecutor 执行评估
    3. 根据评估结果处理三种情况：
       - 全部通过 → 更新状态 COMPLETED + 通知提交者
       - 失败但次数未耗尽 → 返回评估结果，Agent 继续工作
       - 失败且次数耗尽 → 更新状态 FAILED + 通知提交者
    """

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="task_evaluate",
            description="任务评估工具：用于评估任务的评估指标是否满足。支持两种模式：evaluate_single(评估单个指标，增量模式，需提供metric_id)、auto_complete(自动评估所有指标，推荐)。完成任务执行后使用，验证是否满足验收标准。注意：任务未完成时先执行任务；无验收标准的任务无法评估；指标重试超过上限后任务会被标记为失败。",
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

        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="TaskService 不可用",
                error_code="SERVICE_UNAVAILABLE",
            )

        if not task_id:
            task_id = self._infer_task_id(task_service)

        if not task_id:
            return create_failure_result(
                error="系统错误：task_id 未注入，请联系管理员",
                error_code="INJECTION_ERROR",
            )

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
            return self._handle_evaluation_result(inputs, task_service, task, result)
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
        """自动完成评估（评估任务提交时声明的所有指标）。

        从 task.metadata 中提取 evaluation_metric_ids 和 acceptance_criteria，
        只评估任务提交时声明的指标，不自动注入无关指标。

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
        """
        metric_ids = self._get_metric_ids(task)
        input_params = self._get_input_params(task)

        if not metric_ids:
            logger.warning(
                "[TaskEvaluate] 任务 %s 未声明任何评估指标，跳过评估 | "
                "直接标记完成",
                task.id,
            )
            return self._complete_task(
                task_service, task,
                type("EvalResult", (), {
                    "task_id": task.id,
                    "overall_passed": True,
                    "summary": "未声明评估指标，自动通过",
                    "results": [],
                })(),
            )

        logger.info(
            "[TaskEvaluate] 自动评估 | task_id=%s | metrics=%s",
            task.id, metric_ids,
        )

        try:
            executor = self._create_executor(task_service)
            result = executor.run_evaluation(
                task_id=task.id,
                metric_ids=metric_ids,
                input_params=input_params,
            )
            return self._handle_evaluation_result(inputs, task_service, task, result)
        except Exception as e:
            logger.exception("[TaskEvaluate] 自动完成评估失败: %s", e)
            return create_failure_result(
                error=f"评估失败: {e}", error_code="EVAL_FAILED"
            )

    def _handle_evaluation_result(
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
        eval_result: Any,
    ) -> ToolExecutionResult:
        """根据评估结果处理三种情况。

        - 全部通过 → COMPLETED + 通知
        - 失败但次数未耗尽 → 返回结果，Agent 继续
        - 失败且次数耗尽 → FAILED + 通知

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例

        Returns:
            工具执行结果
        """
        max_retries = _DEFAULT_MAX_RETRIES
        if task.metadata and isinstance(task.metadata, dict):
            max_retries = task.metadata.get("max_eval_retries", _DEFAULT_MAX_RETRIES)

        retry_counts: dict[str, int] = {}
        if task.metadata and isinstance(task.metadata, dict):
            retry_counts = task.metadata.get("eval_retry_count", {})
            if not isinstance(retry_counts, dict):
                retry_counts = {}

        has_failure = False
        exhausted = False

        for r in eval_result.results:
            if not r.passed:
                has_failure = True
                mid = r.metric_id
                current = retry_counts.get(mid, 0) + 1
                retry_counts[mid] = current
                if current >= max_retries:
                    exhausted = True

        if task.metadata is None:
            task.metadata = {}
        task.metadata["eval_retry_count"] = retry_counts
        self._save_task(task_service, task)

        if not has_failure:
            return self._complete_task(task_service, task, eval_result)
        elif exhausted:
            return self._fail_task(task_service, task, eval_result, max_retries)
        else:
            min_remaining = max_retries - min(retry_counts.values())
            return create_success_result(
                data=self._build_result_data(eval_result),
                metadata={
                    "action": inputs.get("action", "auto_complete"),
                    "result": "retry",
                    "retry_remaining": min_remaining,
                    "message": f"评估未通过，请继续改进。剩余重试次数：{min_remaining}",
                },
            )

    def _complete_task(
        self, task_service: Any, task: Any, eval_result: Any
    ) -> ToolExecutionResult:
        """评估通过，完成任务。

        TaskService.on_state_change 回调会自动发送终态通知。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例

        Returns:
            工具执行结果
        """
        try:
            task_service.complete_evaluation(task.id, passed=True)
        except Exception as e:
            logger.error("[TaskEvaluate] complete_evaluation(passed=True) 失败: %s", e)

        return create_success_result(
            data=self._build_result_data(eval_result),
            metadata={
                "action": "auto_complete",
                "result": "completed",
                "message": "评估通过，任务已完成",
            },
        )

    def _fail_task(
        self,
        task_service: Any,
        task: Any,
        eval_result: Any,
        max_retries: int,
    ) -> ToolExecutionResult:
        """评估失败且次数耗尽，标记任务失败。

        TaskService.on_state_change 回调会自动发送终态通知。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例
            max_retries: 最大重试次数

        Returns:
            工具执行结果
        """
        try:
            task_service.complete_evaluation(task.id, passed=False)
        except Exception as e:
            logger.error("[TaskEvaluate] complete_evaluation(passed=False) 失败: %s", e)

        failed_metrics = [
            r.metric_id for r in eval_result.results if not r.passed
        ]
        return create_success_result(
            data=self._build_result_data(eval_result),
            metadata={
                "action": "auto_complete",
                "result": "failed",
                "message": (
                    f"评估未通过且重试次数耗尽({max_retries}次)，"
                    f"任务失败。未通过指标：{', '.join(failed_metrics)}"
                ),
            },
        )

    def _save_task(self, task_service: Any, task: Any) -> None:
        """保存任务元数据更新。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
        """
        try:
            task_service._storage.save(task)
        except Exception as e:
            logger.warning("[TaskEvaluate] 保存任务元数据失败: %s", e)

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
        if task.metadata and "evaluation_metric_ids" in task.metadata:
            return task.metadata["evaluation_metric_ids"]
        if task.metadata and "acceptance_criteria" in task.metadata:
            ac = task.metadata["acceptance_criteria"]
            if isinstance(ac, dict):
                return list(ac.keys())
        return []

    def _get_input_params(self, task: Any) -> dict[str, dict[str, Any]]:
        """从任务模型的 acceptance_criteria 中提取各指标的输入参数。

        Args:
            task: TaskModel 实例

        Returns:
            key=metric_id, value=input_params 的字典
        """
        params: dict[str, dict[str, Any]] = {}
        if task.metadata and "acceptance_criteria" in task.metadata:
            ac = task.metadata["acceptance_criteria"]
            if isinstance(ac, dict):
                for metric_id, config in ac.items():
                    if isinstance(config, dict) and "input_params" in config:
                        params[metric_id] = config["input_params"]
        return params

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

    @staticmethod
    def _infer_task_id(task_service: Any) -> str | None:
        """从 TaskService 推断当前活跃的 task_id。

        当 task_id 未通过注入获取时，尝试从 TaskService 中
        查找当前处于 running 状态的任务作为 fallback。

        Args:
            task_service: TaskService 实例

        Returns:
            task_id 字符串，未找到返回 None
        """
        try:
            from tasks.types import TaskStatus

            running_tasks = task_service.list_by_status(TaskStatus.RUNNING)
            if running_tasks:
                if len(running_tasks) > 1:
                    logger.warning(
                        "[TaskEvaluate] 有 %d 个 running 任务，使用最新的",
                        len(running_tasks),
                    )
                latest = max(
                    running_tasks,
                    key=lambda t: t.created_at
                    if hasattr(t, "created_at")
                    else "",
                )
                tid = latest.id if hasattr(latest, "id") else latest.get("id")
                logger.info(
                    "[TaskEvaluate] 推断 task_id=%s (从 running 任务列表)",
                    tid,
                )
                return tid
        except Exception as exc:
            logger.warning("[TaskEvaluate] 推断 task_id 失败: %s", exc)
        return None
