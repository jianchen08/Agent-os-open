"""任务评估工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskEvaluateTool：任务评估工具类
"""

import logging
from typing import Any

from tasks.types import TaskStatus

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

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_EVAL_TIMEOUT = 1200.0

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


class TaskEvaluateTool(BuiltinTool):
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
            description=(
                "任务评估工具：用于评估任务的验收指标是否满足，评估通过则自动完成任务。"
                "\n\n两种评估模式（均可完成任务）："
                "\n1. evaluate_single（单指标评估）：逐个评估指标，每次只评估一个 metric_id。"
                "当所有指标都通过后，任务自动完成。适合需要分步验证或针对性修复的场景。"
                "\n2. auto_complete（完全评估）：一次性评估所有指标。"
                "已通过的指标会自动跳过，只评估未通过的。适合首次评估或最终验证。"
                "\n\n【重要】调用前提：你必须已完成任务要求的全部工作步骤和产出物。"
                "如果你还有未完成的步骤、未输出的产出物、或未处理的待办事项，禁止调用此工具——先完成它们。"
                "无验收标准的任务会自动通过；指标重试超过上限后任务会被标记为失败。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["evaluate_single", "auto_complete"],
                        "description": (
                            "评估模式："
                            "evaluate_single-评估单个指标(需提供metric_id)，"
                            "所有指标逐一通过后任务自动完成；"
                            "auto_complete-评估所有未通过的指标(已通过的自动跳过)，默认"
                        ),
                        "default": "auto_complete",
                    },
                    "metric_id": {
                        "type": "string",
                        "description": "评估指标ID，仅在evaluate_single模式时必填",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "任务完成摘要（推荐填写）。内容应包含："
                            "1) 完成了什么工作（简要说明实现思路和做了哪些改动）；"
                            "2) 产出了什么（文件、配置、数据等产物）；"
                            "3) 产物的存放路径（相对路径，如 src/auth/login.py、config/rules.yaml）。"
                            "示例：'实现了用户登录功能，新增 JWT 认证模块。产出：src/auth/login.py、src/auth/jwt_handler.py、tests/test_login.py。'"
                            "评估器将依据此摘要了解任务成果并验证产物。"
                        ),
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

        BUG-FIX-fix_20260418_task_inject: 系统级错误直接标记任务失败
        问题根因: INJECTION_ERROR/SERVICE_UNAVAILABLE 时 LLM 无意义重试
        修复方案: 系统级错误直接 fail_task + 返回 task_failed 标记
        """
        action = inputs.get("action", "auto_complete")
        task_id = inputs.get("task_id")

        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="TaskService 不可用",
                error_code="SERVICE_UNAVAILABLE",
                metadata={"task_failed": True},
            )

        if not task_id:
            task_id = self._infer_task_id(task_service)
            if task_id:
                logger.warning(
                    "[TaskEvaluate] task_id 为推断值: %s，注入链可能断裂",
                    task_id,
                )

        if not task_id:
            return create_failure_result(
                error="系统错误：task_id 未注入，请联系管理员",
                error_code="INJECTION_ERROR",
                metadata={"task_failed": True},
            )

        task = task_service.get_task(task_id)
        if task is None:
            return create_failure_result(
                error="任务不存在", error_code="TASK_NOT_FOUND"
            )

        if action == "evaluate_single":
            return await self._evaluate_single(inputs, task_service, task)
        elif action == "auto_complete":
            return await self._auto_complete(inputs, task_service, task)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}", error_code="INVALID_ACTION"
            )

    async def _evaluate_single(
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """评估单个指标（增量模式）。

        逐个评估指标，每次只评估指定的 metric_id。
        评估后汇总历史记录：如果所有声明的指标都已通过，自动完成任务；
        否则返回当前结果，Agent 可继续评估其他指标或改进后重试。

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
            return await self._auto_complete(inputs, task_service, task)

        try:
            import asyncio
            loop = asyncio.get_running_loop()
            executor = self._create_executor(task_service)
            timeout = self._get_eval_timeout(task)
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: executor.run_evaluation(
                        task_id=task_id,
                        metric_ids=[metric_id],
                    ),
                ),
                timeout=timeout,
            )
        except litellm.RateLimitError as exc:
            logger.warning(
                "[TaskEvaluate] 评估期间 API 限速 | task_id=%s | metric_id=%s: %s",
                task_id, metric_id, exc,
            )
            return create_failure_result(
                error=f"评估期间 API 限速: {exc}",
                error_code="RATE_LIMITED",
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[TaskEvaluate] 单指标评估超时 | task_id=%s | metric_id=%s | timeout=%ss",
                task_id, metric_id, timeout,
            )
            return create_failure_result(
                error=f"评估超时（{timeout}s）: 指标 {metric_id} 执行时间过长",
                error_code="EVAL_TIMEOUT",
            )
        except Exception as e:
            logger.exception("[TaskEvaluate] 单指标评估失败: %s", e)
            return create_failure_result(
                error=f"评估失败: {e}", error_code="EVAL_FAILED"
            )

        # 注册评估子管道 + 追加历史记录
        self._register_eval_pipelines(task_service, task, result)
        self._append_eval_history(task, result)
        self._save_task(task_service, task)

        # 当前指标未通过 → 返回结果，Agent 继续改进
        if not result.overall_passed:
            return create_success_result(
                data=self._build_result_data(result),
                metadata={
                    "action": "evaluate_single",
                    "result": "retry",
                    "message": f"指标 {metric_id} 未通过，请根据反馈继续改进",
                },
            )

        # 当前指标通过，检查所有声明指标是否都已通过
        if self._all_metrics_passed(task, metric_ids):
            logger.info(
                "[TaskEvaluate] 所有指标已通过，完成任务 | task_id=%s",
                task_id,
            )
            return self._complete_task(task_service, task, result)

        # 还有指标未评估，返回进度
        evaluated, remaining = self._get_eval_progress(task, metric_ids)
        return create_success_result(
            data=self._build_result_data(result),
            metadata={
                "action": "evaluate_single",
                "result": "partial_pass",
                "message": (
                    f"指标 {metric_id} 已通过。"
                    f"进度：{evaluated}/{len(metric_ids)}，"
                    f"剩余：{', '.join(remaining)}"
                ),
            },
        )

    async def _auto_complete(
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """自动完成评估（评估任务提交时声明的所有指标）。

        从 task.metadata 中提取 evaluation_metric_ids 和 acceptance_criteria，
        只评估任务提交时声明的指标，不自动注入无关指标。
        已通过的指标会自动跳过，只评估未通过的指标。

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

        # 跳过已通过的指标，只评估未通过的
        already_passed, remaining_ids = self._get_eval_progress(
            task, metric_ids,
        )
        if not remaining_ids:
            logger.info(
                "[TaskEvaluate] 所有指标已通过，直接完成任务 | "
                "task_id=%s | passed=%d/%d",
                task.id, already_passed, len(metric_ids),
            )
            return self._complete_task(
                task_service, task,
                type("EvalResult", (), {
                    "task_id": task.id,
                    "overall_passed": True,
                    "summary": (
                        f"所有 {len(metric_ids)} 个指标均已通过"
                        f"（来自历史评估记录）"
                    ),
                    "results": [],
                })(),
            )

        logger.info(
            "[TaskEvaluate] 自动评估 | task_id=%s | total=%d | "
            "already_passed=%d | to_eval=%s",
            task.id, len(metric_ids), already_passed, remaining_ids,
        )

        try:
            import asyncio
            loop = asyncio.get_running_loop()
            executor = self._create_executor(task_service)
            timeout = self._get_eval_timeout(task)
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: executor.run_evaluation(
                        task_id=task.id,
                        metric_ids=remaining_ids,
                        input_params=input_params,
                        skip_state_update=True,
                    ),
                ),
                timeout=timeout,
            )
            return self._handle_evaluation_result(inputs, task_service, task, result)
        except asyncio.TimeoutError:
            logger.warning(
                "[TaskEvaluate] 自动评估超时 | task_id=%s | "
                "metrics=%s | timeout=%ss",
                task.id, remaining_ids, timeout,
            )
            return create_failure_result(
                error=f"评估超时（{timeout}s）: 指标 {metric_ids} 执行时间过长",
                error_code="EVAL_TIMEOUT",
            )
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

        _UNRECOVERABLE_PATTERNS = ("command not found", "no such file or directory", "module not found", "is not recognized")

        for r in eval_result.results:
            if not r.passed:
                has_failure = True
                mid = r.metric_id
                output_str = str(r.evaluator_output or "").lower()
                message_str = (r.message or "").lower()
                is_unrecoverable = any(p in output_str or p in message_str for p in _UNRECOVERABLE_PATTERNS)
                if is_unrecoverable:
                    retry_counts[mid] = max_retries
                    exhausted = True
                    continue
                current = retry_counts.get(mid, 0) + 1
                retry_counts[mid] = current
                if current >= max_retries:
                    exhausted = True

        if task.metadata is None:
            task.metadata = {}
        task.metadata["eval_retry_count"] = retry_counts

        # 注册评估子管道到根任务子目录
        self._register_eval_pipelines(task_service, task, eval_result)

        # 追加本次评估记录到历史（保留所有评估尝试）
        self._append_eval_history(task, eval_result)

        self._save_task(task_service, task)

        if not has_failure:
            return self._complete_task(task_service, task, eval_result)
        elif exhausted:
            return self._fail_task(task_service, task, eval_result, max_retries)
        else:
            min_remaining = max_retries - min(retry_counts.values())
            failed_details = []
            for r in eval_result.results:
                if not r.passed:
                    detail = f"- [{r.metric_id}] 未通过"
                    if r.message:
                        detail += f": {r.message}"
                    if r.score is not None:
                        detail += f" (得分: {r.score})"
                    failed_details.append(detail)
            feedback = "评估未通过，请根据以下反馈继续改进：\n" + "\n".join(failed_details)
            feedback += f"\n\n剩余重试次数：{min_remaining}"
            return create_success_result(
                data=self._build_result_data(eval_result),
                metadata={
                    "action": inputs.get("action", "auto_complete"),
                    "result": "retry",
                    "retry_remaining": min_remaining,
                    "message": feedback,
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
        if task.status == TaskStatus.COMPLETED:
            logger.info("[TaskEvaluate] 任务 %s 已完成，跳过状态回写", task.id)
        elif task.status == TaskStatus.FAILED:
            # 评估通过但任务已被标记失败（如 idle 超时），恢复为完成
            logger.warning(
                "[TaskEvaluate] 任务 %s 已失败但评估通过，尝试恢复为完成", task.id,
            )
            try:
                eval_data = self._build_result_data(eval_result)
                task_service.recover_to_completed(task.id, result=eval_data)
            except Exception as e:
                logger.error("[TaskEvaluate] 恢复失败状态为完成失败: %s", e)
        else:
            try:
                eval_data = self._build_result_data(eval_result)
                task_service.complete_evaluation(task.id, passed=True, result=eval_data)
            except Exception as e:
                logger.error("[TaskEvaluate] complete_evaluation(passed=True) 失败: %s", e)
                return create_failure_result(
                    error=f"complete_evaluation(passed=True) 失败: {e}",
                    metadata={"eval_data": str(self._build_result_data(eval_result))[:200]},
                )

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
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                eval_data = self._build_result_data(eval_result)
                task_service.complete_evaluation(task.id, passed=False, result=eval_data)
            else:
                logger.info("[TaskEvaluate] 任务 %s 已是终态(%s)，跳过状态回写", task.id, task.status.value)
        except Exception as e:
            logger.error("[TaskEvaluate] complete_evaluation(passed=False) 失败: %s", e)
            return create_failure_result(
                error=f"complete_evaluation(passed=False) 失败: {e}",
                metadata={"eval_data": str(self._build_result_data(eval_result))[:200]},
            )

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
            task_service.save_task(task)
        except Exception as e:
            logger.warning("[TaskEvaluate] 保存任务元数据失败: %s", e)

    @staticmethod
    def _register_eval_pipelines(
        task_service: Any, task: Any, eval_result: Any,
    ) -> None:
        """将 Agent 型评估产生的子管道注册到根任务子目录。

        作为 _pre_register_eval_pipeline 的兜底：如果 engine.py
        的早期注册因 ServiceProvider 不可用等原因被跳过，
        此处会在评估完成后再次尝试。
        """
        try:
            root_id = task_service.get_root_task_id(task.id)
            if not root_id:
                logger.debug(
                    "[TaskEvaluate] 无 root_id，跳过评估管道注册 | "
                    "task=%s", task.id,
                )
                return
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            exec_storage = provider.get("execution_record_storage")
            if not exec_storage:
                logger.warning(
                    "[TaskEvaluate] execution_record_storage 不可用，"
                    "跳过评估管道注册"
                )
                return
            registered = 0
            skipped = 0
            for r in eval_result.results:
                pid = getattr(r, "pipeline_run_id", None)
                if pid:
                    exec_storage.register_pipeline(pid, root_id)
                    registered += 1
                elif hasattr(r, "pipeline_run_id"):
                    skipped += 1
            if registered or skipped:
                logger.info(
                    "[TaskEvaluate] 评估管道注册 | task=%s | root=%s | "
                    "registered=%d | skipped=%d",
                    task.id, root_id, registered, skipped,
                )
        except Exception as exc:
            logger.warning(
                "[TaskEvaluate] 注册评估管道分组失败 | task=%s | error=%s",
                task.id, exc,
            )

    @staticmethod
    def _append_eval_history(task: Any, eval_result: Any) -> None:
        """将本次评估结果追加到 task.metadata 的 evaluation_history。

        每次评估（无论通过/失败/重试）都会被记录，包含时间戳、
        评估指标详情（含评估器输入/输出和 Agent 管道 ID）。

        Args:
            task: TaskModel 实例
            eval_result: EvaluationResult 实例
        """
        from datetime import datetime

        metrics = []
        for r in eval_result.results:
            m: dict[str, Any] = {
                "metric_id": r.metric_id,
                "passed": r.passed,
                "score": r.score,
                "message": r.message,
                "error": r.error,
            }
            if hasattr(r, "evaluator_input") and r.evaluator_input:
                m["evaluator_input"] = r.evaluator_input
            if hasattr(r, "evaluator_output") and r.evaluator_output:
                m["evaluator_output"] = r.evaluator_output
            if hasattr(r, "pipeline_run_id") and r.pipeline_run_id:
                m["pipeline_run_id"] = r.pipeline_run_id
            metrics.append(m)

        history = task.metadata.get("evaluation_history", [])
        if not isinstance(history, list):
            history = []
        history.append({
            "timestamp": datetime.now().isoformat(),
            "passed": eval_result.overall_passed,
            "summary": getattr(eval_result, "summary", ""),
            "metrics": metrics,
        })
        task.metadata["evaluation_history"] = history

    def _get_task_service(self) -> Any:
        """获取共享的 TaskService 实例。

        通过 ServiceProvider 统一获取，支持显式注册、sys 全局变量和懒加载创建。

        Returns:
            TaskService 实例，获取失败返回 None
        """
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        return provider.get_or_create("task_service", lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService())

    def _create_executor(self, task_service: Any) -> Any:
        """创建 EvaluationExecutor 实例。

        从全局变量获取 pipeline_factory 和 agent_registry，
        传递给 EvaluationExecutor 以支持 Agent 型评估器。

        Args:
            task_service: TaskService 实例，用于状态回写

        Returns:
            EvaluationExecutor 实例
        """
        import asyncio
        from evaluation.executor import EvaluationExecutor

        pipeline_factory = self._get_pipeline_factory()
        agent_registry = self._get_agent_registry()
        tool_registry = self._get_tool_registry()

        main_loop = None
        try:
            main_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        return EvaluationExecutor(
            task_service=task_service,
            pipeline_factory=pipeline_factory,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            main_loop=main_loop,
        )

    @staticmethod
    def _get_pipeline_factory() -> Any:
        """获取管道工厂（创建 PipelineEngine 的可调用对象）。

        通过 ServiceProvider 统一获取，保留从 _agent_os_services 构建的兜底逻辑。
        """
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        factory = provider.get("pipeline_factory")
        if factory is not None:
            return factory

        # 兜底：从 _agent_os_services 构建 pipeline factory
        services = provider.get("services")
        if services is None:
            return None

        try:
            from pipeline.engine import PipelineEngine

            input_routes = services.get("input_route_table")
            output_routes = services.get("output_route_table")
            plugin_registry = services.get("plugin_registry")

            if input_routes and output_routes and plugin_registry:
                def _factory():
                    return PipelineEngine(
                        input_route_table=input_routes,
                        output_route_table=output_routes,
                        plugin_registry=plugin_registry,
                        services=services,
                    )
                return _factory
        except Exception:
            pass

        return None

    @staticmethod
    def _get_agent_registry() -> Any:
        """获取 AgentRegistry 实例。

        通过 ServiceProvider 统一获取。
        """
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        return provider.get("agent_registry")

    @staticmethod
    def _get_tool_registry() -> Any:
        """获取 ToolRegistry 实例。

        通过 ServiceProvider 统一获取，保留从全局注册表模块获取的兜底逻辑。
        """
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        registry = provider.get("tool_registry")
        if registry is not None:
            return registry
        try:
            from tools.global_registry import get_global_tool_registry_sync
            return get_global_tool_registry_sync()
        except Exception:
            return None

    @staticmethod
    def _all_metrics_passed(task: Any, metric_ids: list[str]) -> bool:
        """检查所有声明指标是否都在历史记录中通过了。

        从 task.metadata.evaluation_history 中收集每个指标最近一次评估结果，
        判断是否所有指标都已通过。

        Args:
            task: TaskModel 实例
            metric_ids: 所有声明的指标 ID 列表

        Returns:
            所有指标是否都已通过
        """
        metadata = task.metadata if task.metadata else {}
        history = metadata.get("evaluation_history", [])
        if not isinstance(history, list):
            return False

        # 收集每个指标最近一次评估结果
        latest: dict[str, bool] = {}
        for entry in history:
            metrics = entry.get("metrics", [])
            for m in metrics:
                mid = m.get("metric_id")
                if mid:
                    latest[mid] = m.get("passed", False)

        return all(latest.get(mid, False) for mid in metric_ids)

    @staticmethod
    def _get_eval_progress(
        task: Any, metric_ids: list[str],
    ) -> tuple[int, list[str]]:
        """获取评估进度：已通过数量和剩余未通过的指标 ID。

        Args:
            task: TaskModel 实例
            metric_ids: 所有声明的指标 ID 列表

        Returns:
            (已通过数量, 未通过的指标 ID 列表)
        """
        metadata = task.metadata if task.metadata else {}
        history = metadata.get("evaluation_history", [])

        latest: dict[str, bool] = {}
        if isinstance(history, list):
            for entry in history:
                metrics = entry.get("metrics", [])
                for m in metrics:
                    mid = m.get("metric_id")
                    if mid:
                        latest[mid] = m.get("passed", False)

        passed_count = sum(1 for mid in metric_ids if latest.get(mid, False))
        remaining = [mid for mid in metric_ids if not latest.get(mid, False)]
        return passed_count, remaining

    @staticmethod
    def _get_eval_timeout(task: Any) -> float:
        """根据任务元数据获取评估超时时间（秒）。

        优先使用 task.metadata.eval_timeout（允许单个任务自定义），
        默认 _DEFAULT_EVAL_TIMEOUT（300秒）。
        """
        metadata = task.metadata if task.metadata else {}
        custom_timeout = metadata.get("eval_timeout")
        if custom_timeout is not None:
            try:
                return float(custom_timeout)
            except (TypeError, ValueError):
                pass
        return _DEFAULT_EVAL_TIMEOUT

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

        对于 input_params 为空的指标，自动从任务描述中构建 criteria。
        对于工具型评估指标（如 file_check），自动注入 workspace 参数，
        确保评估工具在正确的工作目录下解析文件路径。

        BUG-FIX-fix_20260419_eval_workspace:
        问题根因: file_check 评估器调用 file_read 时未传递 workspace，
                 导致 file_read 在项目根目录而非任务工作空间中查找文件，
                 文件路径解析错误使评估永远失败。
        修复方案: 从 task.metadata 解析 workspace 绝对路径，注入到工具型评估指标的参数中。
        影响范围: 所有使用工具型评估指标（file_check、bash_check 等）的任务评估

        Args:
            task: TaskModel 实例

        Returns:
            key=metric_id, value=input_params 的字典
        """
        params: dict[str, dict[str, Any]] = {}
        ac = {}
        if task.metadata and "acceptance_criteria" in task.metadata:
            ac = task.metadata["acceptance_criteria"]
            if isinstance(ac, dict):
                _non_param_keys = {"expected_output", "pass_threshold", "description"}
                for metric_id, config in ac.items():
                    if isinstance(config, dict):
                        if "input_params" in config:
                            params[metric_id] = config["input_params"]
                        else:
                            # LLM may put params at top level; filter known non-param keys
                            params[metric_id] = {
                                k: v for k, v in config.items()
                                if k not in _non_param_keys
                            }

        task_desc = ""
        if hasattr(task, "description") and task.description:
            task_desc = task.description
        elif hasattr(task, "title") and task.title:
            task_desc = task.title

        all_metric_ids = set()
        if task.metadata and "evaluation_metric_ids" in task.metadata:
            all_metric_ids = set(task.metadata["evaluation_metric_ids"])
        if isinstance(ac, dict):
            all_metric_ids.update(ac.keys())

        workspace_abs = self._resolve_task_workspace_abs(task)

        for metric_id in all_metric_ids:
            p = params.get(metric_id, {})
            if not p.get("criteria") and task_desc:
                p.setdefault("criteria", task_desc)
            if workspace_abs and "workspace" not in p:
                p["workspace"] = workspace_abs
            # BUG-FIX: Substitute template variables {{workspace}}, {{task_id}}, {tool_id}
            for key, val in list(p.items()):
                if isinstance(val, str):
                    if workspace_abs:
                        val = val.replace("{{workspace}}", workspace_abs)
                    val = val.replace("{{task_id}}", task.id)
                    p[key] = val

        # Resolve {tool_id} template from workspace files
        _tool_id_val = self._resolve_tool_id_from_workspace(task, workspace_abs)
        if _tool_id_val:
            for metric_id in all_metric_ids:
                p = params.get(metric_id, {})
                for key, val in list(p.items()):
                    if isinstance(val, str) and "{tool_id}" in val:
                        p[key] = val.replace("{tool_id}", _tool_id_val)
                params[metric_id] = p
            params[metric_id] = p

        return params

    @staticmethod
    def _resolve_task_workspace_abs(task: Any) -> str | None:
        """解析任务的绝对工作空间路径。

        BUG-FIX-fix_20260425_eval_ws_meta:
        问题根因: 容器子任务的实际工作空间是 worktree（如 .ai_workspaces/容器__wt_任务ID前8位），
                 但 resolve_workspace 链路计算出的是 拼接路径（如 .ai_workspaces/容器/任务ID），
                 两者不一致导致 file_check 永远找不到 agent 写入的文件。
        修复方案: 优先使用 task.metadata.ws_meta.path（TaskWorker 执行时保存的实际工作空间路径），
                 仅在 ws_meta 不可用时才走 resolve_workspace 计算逻辑。
        影响范围: 所有使用工具型评估指标（file_check 等）的任务评估

        Args:
            task: TaskModel 实例

        Returns:
            绝对工作空间路径字符串，无法解析时返回 None
        """
        from pathlib import Path

        metadata = task.metadata if task.metadata else {}

        # 优先使用 ws_meta.path — 这是 TaskWorker 执行时保存的实际工作空间路径
        ws_meta = metadata.get("ws_meta")
        if ws_meta and isinstance(ws_meta, dict):
            ws_path = ws_meta.get("path")
            if ws_path:
                p = Path(ws_path)
                if not p.is_absolute():
                    p = Path.cwd() / p
                if p.exists():
                    return str(p)

        # fallback: 原有的 resolve_workspace 链路
        from isolation.workspace import get_workspace_config_root, resolve_workspace

        task_workspace = metadata.get("workspace")
        root = get_workspace_config_root()

        task_service = None
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            services = provider.get("services")
            if services:
                task_service = services.get("task_service")
        except Exception:
            pass

        if not task_service:
            if task_workspace:
                return str(Path.cwd() / task_workspace)
            return str(Path.cwd() / root / task.id)

        ancestor_chain: list[tuple[str, str | None]] = []
        current_id = task.id
        visited: set[str] = set()

        while current_id and current_id not in visited:
            t = task_service.get_task(current_id)
            if t is None:
                break
            visited.add(current_id)
            stored_ws = (t.metadata or {}).get("workspace") if t.metadata else None
            ancestor_chain.append((current_id, stored_ws))
            current_id = t.parent_task_id if hasattr(t, "parent_task_id") else None

        if not ancestor_chain:
            return str(Path.cwd() / root / task.id)

        resolved: str | None = None
        for tid, tws in reversed(ancestor_chain):
            if resolved is None:
                resolved = resolve_workspace(tid, tws, config_root=root)
            elif tid == task.id:
                resolved = resolve_workspace(tid, task_workspace, parent_resolved_workspace=resolved)
            else:
                resolved = resolve_workspace(tid, tws, parent_resolved_workspace=resolved)

        if resolved:
            return str(Path.cwd() / resolved)
        return None

    @staticmethod
    def _resolve_tool_id_from_workspace(task: Any, workspace_abs: str | None) -> str | None:
        """从工作空间文件中推断 tool_id，用于替换 {tool_id} 模板变量。

        在 src/tools/builtin/ 目录下查找 .py 文件（排除 test_ 前缀和 __init__.py），
        返回第一个匹配的文件名（不含 .py 后缀）作为 tool_id。
        """
        if not workspace_abs:
            return None
        from pathlib import Path
        tools_dir = Path(workspace_abs) / "src" / "tools" / "builtin"
        if not tools_dir.exists():
            return None
        for py_file in tools_dir.glob("*.py"):
            name = py_file.stem
            if name.startswith("test_") or name.startswith("__"):
                continue
            return name
        return None

    def _build_result_data(self, result: Any) -> dict[str, Any]:
        """将评估结果构建为工具返回数据。

        包含评估器输入/输出、Agent 评估的结构化反馈（issues/suggestions/
        report_path）和管道 ID，便于 LLM 直接定位问题并修复。

        Args:
            result: EvaluationResult 实例

        Returns:
            可序列化的结果字典
        """
        metrics = []
        for r in result.results:
            if r.passed:
                metrics.append({
                    "metric_id": r.metric_id,
                    "passed": True,
                })
            else:
                m: dict[str, Any] = {
                    "metric_id": r.metric_id,
                    "passed": False,
                    "score": r.score,
                    "message": r.message,
                    "error": r.error,
                }
                # Agent 评估的结构化反馈
                if r.evaluator_output:
                    eo = r.evaluator_output
                    if eo.get("issues"):
                        m["issues"] = eo["issues"]
                    if eo.get("suggestions"):
                        m["suggestions"] = eo["suggestions"]
                    if eo.get("report_path"):
                        m["report_path"] = eo["report_path"]
                # 期望条件失败的详细信息
                if r.details and isinstance(r.details, dict):
                    failed = r.details.get("failed_conditions")
                    if failed:
                        m["failed_conditions"] = failed
                if r.evaluator_input:
                    m["evaluator_input"] = r.evaluator_input
                if r.evaluator_output:
                    m["evaluator_output"] = r.evaluator_output
                if r.pipeline_run_id:
                    m["pipeline_run_id"] = r.pipeline_run_id
                metrics.append(m)
        return {
            "task_id": result.task_id,
            "overall_passed": result.overall_passed,
            "summary": result.summary,
            "metrics": metrics,
        }

    @staticmethod
    def _infer_task_id(task_service: Any) -> str | None:
        """从 TaskService 推断当前活跃的 task_id。

        当 task_id 未通过注入获取时，尝试从 TaskService 中
        查找当前处于 RUNNING 或 EVALUATING 状态的任务作为 fallback。

        BUG-FIX-fix_20260418_task_inject: 扩展推断范围
        问题根因: 原仅查 RUNNING 状态，任务可能已转为 EVALUATING
        修复方案: 覆盖 RUNNING + EVALUATING 两种状态

        Args:
            task_service: TaskService 实例

        Returns:
            task_id 字符串，未找到返回 None
        """
        try:
            for status in [TaskStatus.RUNNING, TaskStatus.EVALUATING]:
                tasks = task_service.list_by_status(status)
                if tasks:
                    if len(tasks) > 1:
                        logger.warning(
                            "[TaskEvaluate] 有 %d 个 %s 任务，使用最新的",
                            len(tasks), status.value,
                        )
                    latest = max(
                        tasks,
                        key=lambda t: t.created_at
                        if hasattr(t, "created_at")
                        else "",
                    )
                    tid = latest.id if hasattr(latest, "id") else latest.get("id")
                    logger.info(
                        "[TaskEvaluate] 推断 task_id=%s (从 %s 任务列表)",
                        tid, status.value,
                    )
                    return tid
        except Exception as exc:
            logger.warning("[TaskEvaluate] 推断 task_id 失败: %s", exc)
        return None
