"""
评估服务

负责任务评估的执行和结果应用，处理评估结果更新任务状态。

核心功能：
1. 评估任务执行（工具/工作流/人工）
2. 应用评估结果
3. 更新任务状态（通过 TaskStateService）
4. 发布评估事件
5. 进步重置机制（有进步时重置重试次数）
6. 子任务完成通知（通过 ExecutionRecord 注入到父任务上下文）
7. 条件判断（使用 ExpectConditionEvaluator）

人工审批：
- 重试耗尽时调用 TaskApprovalService 触发人工审批
- 人工审批由 approval_service.py 处理

核心原则：
- 任务状态变更为 completed 只能通过此服务的 complete_task_after_evaluation()
- 所有状态转换通过 TaskStateService 进行
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import get_settings
from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.states import ExecutionStatus
from src.db.models import ExecutionRecord, Task
from src.db.repositories.execution_record_repo import ExecutionRecordRepository
from src.evaluation.expect_evaluator import get_expect_evaluator
from src.evaluation.metric_loader import get_metric_loader
from src.tasks.services.progress_calculator import get_progress_calculator
from src.tasks.services.state_service import TaskStateService
from src.utils.message_id_helper import generate_execution_record_id

if TYPE_CHECKING:
    from src.tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


class EvaluationService:
    """
    评估服务

    负责任务评估的执行和结果应用，处理评估结果更新任务状态。

    核心职责：
    1. 评估任务执行
    2. 应用评估结果
    3. 更新任务状态
    4. 发布评估事件
    5. 触发人工审批（重试耗尽时）
    6. 进步重置机制（有进步时重置重试次数）

    进步重置机制：
    - 如果当前通过指标数 > 历史最佳，重试次数重置为 0
    - 如果当前通过指标数 <= 上次通过数，重试次数 +1
    - 重试次数达到 max_retries 时，进入 blocked 状态

    Example:
        >>> service = EvaluationService(session)
        >>> result = await service.apply_evaluation_results(
        ...     task_id="task-001",
        ...     evaluation_results=[{"metric_id": "m1", "passed": True, ...}],
        ... )
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        state_service: TaskStateService | None = None,
        tool_executor: "ToolExecutor | None" = None,
    ):
        """
        初始化评估服务

        Args:
            session: 数据库会话（可选）
            state_service: 任务状态服务（可选）
            tool_executor: 工具执行器（可选，用于执行评估工具）
        """
        self.session = session
        self.state_service = state_service
        self.tool_executor = tool_executor
        settings = get_settings()
        self.ac_max_retries = settings.ac_max_retries
        self.task_max_retries = settings.task_max_retries
        self.progress_calculator = get_progress_calculator()

    async def execute_and_apply(
        self,
        task_id: str,
        summary: str = "",
        tool_record_id: str | None = None,
    ) -> dict[str, Any]:
        """
        执行评估并应用结果（一站式入口）

        这是 task_evaluate 工具的主要调用入口。

        流程：
        1. 获取任务和评估指标
        2. 执行所有评估（评估器根据预设配置自主验证）
        3. 更新 acceptance_criteria
        4. 应用进步重置机制
        5. 决定任务状态
        6. 通过 TaskStateService 更新状态

        Args:
            task_id: 任务 ID
            summary: 任务完成说明（可选，用于语义类评估器）
            tool_record_id: 工具执行记录 ID（用于嵌套）

        Returns:
            评估结果
        """
        if not self.session:
            return {"error": "未提供数据库会话", "error_code": "NO_SESSION"}

        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        metric_ids = task.evaluation_metric_ids or []
        if not metric_ids:
            return await self._handle_no_metrics(task_id, summary)

        # 使用 MetricLoader 加载评估指标
        metric_loader = get_metric_loader()
        metrics = await metric_loader.get_metrics_by_ids(metric_ids)

        if not metrics:
            return {"error": "未找到有效的评估指标", "error_code": "NO_METRICS_FOUND"}

        acceptance_criteria = task.acceptance_criteria or []
        if not acceptance_criteria:
            acceptance_criteria = self._init_acceptance_criteria(metrics)

        now = datetime.now(UTC)
        results = []
        blocked_metrics = []

        for metric in metrics:
            ac = self._find_or_create_ac(acceptance_criteria, metric)

            if ac.get("retry_count", 0) >= self.ac_max_retries:
                blocked_metrics.append(
                    {
                        "metric_id": metric.get("id"),
                        "metric_name": metric.get("name", ""),
                        "retry_count": ac.get("retry_count", 0),
                    }
                )
                continue

            evidence = {
                "description": summary,
                "output": "",
            }

            evaluation_record_id = None
            if tool_record_id:
                evaluation_record_id = await self._create_evaluation_record(
                    task_id=task_id,
                    metric_id=metric.get("id"),
                    parent_record_id=tool_record_id,
                )

            result = await self._execute_metric(
                task=task,
                metric=metric,
                ac=ac,
                evidence=evidence,
                evaluation_record_id=evaluation_record_id,
            )
            results.append(result)

            if evaluation_record_id:
                await self._update_evaluation_record(
                    record_id=evaluation_record_id,
                    result=result,
                )

            ac["evaluated_at"] = now.isoformat()
            ac["evaluation_result"] = {
                "passed": result.get("passed", False),
                "score": result.get("score", 0),
                "feedback": result.get("feedback", ""),
                "details": result.get("details", {}),
            }

            if result.get("passed", False):
                ac["status"] = "passed"
            else:
                ac["status"] = "failed"
                ac["retry_count"] = ac.get("retry_count", 0) + 1

        return await self._finalize_evaluation(
            task=task,
            acceptance_criteria=acceptance_criteria,
            results=results,
            blocked_metrics=blocked_metrics,
            summary=summary,
        )

    async def _execute_metric(
        self,
        task: Task,
        metric: dict[str, Any],
        ac: dict[str, Any],
        evidence: dict[str, Any],
        evaluation_record_id: str | None = None,
    ) -> dict[str, Any]:
        """
        执行单个指标评估

        Args:
            task: 任务对象
            metric: 评估指标配置字典
            ac: 验收标准项
            evidence: 评估证据
            evaluation_record_id: 评估记录 ID（用于嵌套评估器执行记录）

        Returns:
            评估结果
        """
        if not self.tool_executor:
            return {
                "metric_id": metric.get("id"),
                "metric_name": metric.get("name", ""),
                "passed": False,
                "score": 0.0,
                "feedback": "未配置工具执行器",
            }

        metric_config = {
            **(metric.get("default_config") or {}),
            **ac.get("input_params", {}),
        }
        pass_threshold = ac.get("pass_threshold") or metric.get(
            "default_pass_threshold"
        )

        try:
            from src.tools.executor import ExecutionContext

            context = ExecutionContext(
                session_id=task.id,
                user_id=None,
                metadata={
                    "metric_id": metric.get("id"),
                    "task_id": task.id,
                    "evaluation_record_id": evaluation_record_id,
                },
            )

            result = await self.tool_executor.execute(
                tool_name=metric.get("evaluator_id", ""),
                inputs={
                    **metric_config,
                    "evidence": evidence,
                    "task_id": task.id,
                },
                context=context,
            )

            output_data = result.data if result.success and result.data else {}

            # 使用 ExpectConditionEvaluator 进行条件判断
            expect = metric.get("expect", {})
            evaluator = get_expect_evaluator()
            eval_result = evaluator.evaluate(output_data, expect)

            # 使用评估结果
            passed = eval_result["passed"]
            score = eval_result["score"]
            feedback = eval_result["message"] or ("执行成功" if passed else "执行失败")

            return {
                "metric_id": metric.get("id"),
                "metric_name": metric.get("name", ""),
                "passed": passed,
                "score": score,
                "feedback": feedback,
                "threshold_used": pass_threshold,
                "details": {
                    "success": result.success,
                    "result": result.data if result.success else None,
                    "error": result.error,
                    "evaluation_details": eval_result.get("details", {}),
                },
            }
        except Exception as e:
            logger.exception(f"评估指标执行失败 ({metric.get('evaluator_id')}): {e}")
            return {
                "metric_id": metric.get("id"),
                "metric_name": metric.get("name", ""),
                "passed": False,
                "score": 0.0,
                "feedback": f"评估执行失败: {str(e)}",
            }

    async def _handle_no_metrics(
        self,
        task_id: str,
        summary: str,
    ) -> dict[str, Any]:
        """处理无评估指标的情况"""
        if not self.state_service:
            return {
                "error": "未注入 TaskStateService",
                "error_code": "NO_STATE_SERVICE",
            }

        complete_result = await self.complete_task_after_evaluation(
            task_id=task_id,
            session=self.session,
        )

        return {
            "task_id": task_id,
            "task_status": complete_result.get("task_status", "completed"),
            "message": "🎉 任务完成！（无评估指标）",
            "summary": summary,
        }

    def _init_acceptance_criteria(
        self,
        metrics: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """初始化验收标准"""
        return [
            {
                "metric_id": metric.get("id"),
                "input_params": {},
                "status": "pending",
                "retry_count": 0,
                "evaluated_at": None,
                "evaluation_result": None,
            }
            for metric in metrics
        ]

    def _find_or_create_ac(
        self,
        acceptance_criteria: list[dict[str, Any]],
        metric: dict[str, Any],
    ) -> dict[str, Any]:
        """查找或创建验收标准项"""
        for ac in acceptance_criteria:
            if ac.get("metric_id") == metric.get("id"):
                return ac

        ac = {
            "metric_id": metric.get("id"),
            "input_params": {},
            "status": "pending",
            "retry_count": 0,
            "evaluated_at": None,
            "evaluation_result": None,
        }
        acceptance_criteria.append(ac)
        return ac

    async def _finalize_evaluation(
        self,
        task: Task,
        acceptance_criteria: list[dict[str, Any]],
        results: list[dict[str, Any]],
        blocked_metrics: list[dict[str, Any]],
        summary: str,
    ) -> dict[str, Any]:
        """完成评估处理"""
        task.acceptance_criteria = acceptance_criteria

        passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")
        failed = sum(1 for ac in acceptance_criteria if ac.get("status") == "failed")
        total = len(acceptance_criteria)

        task.passed_criteria = passed
        task.failed_criteria = failed
        task.progress_percent = (passed / total * 100) if total > 0 else 0

        any_blocked = any(
            ac.get("retry_count", 0) >= self.ac_max_retries
            for ac in acceptance_criteria
            if ac.get("status") != "passed"
        )

        if passed == total:
            return await self._handle_all_passed(
                task=task,
                results=results,
                passed=passed,
                total=total,
                summary=summary,
            )
        elif any_blocked:
            return await self._handle_blocked(
                task=task,
                blocked_metrics=blocked_metrics,
                passed=passed,
                total=total,
                results=results,
            )
        else:
            return await self._handle_partial_passed(
                task=task,
                acceptance_criteria=acceptance_criteria,
                passed=passed,
                failed=failed,
                total=total,
                results=results,
            )

    async def _handle_all_passed(
        self,
        task: Task,
        results: list[dict[str, Any]],
        passed: int,
        total: int,
        summary: str,
    ) -> dict[str, Any]:
        """处理全部通过的情况"""
        if not self.state_service:
            return {
                "error": "未注入 TaskStateService",
                "error_code": "NO_STATE_SERVICE",
            }

        complete_result = await self.complete_task_after_evaluation(
            task_id=task.id,
            evaluation_results=results,
            session=self.session,
        )

        return {
            "task_id": task.id,
            "task_status": complete_result.get("task_status", "completed"),
            "message": f"🎉 任务完成！所有评估指标通过 ({passed}/{total})",
            "evaluation_summary": {
                "total": total,
                "passed": passed,
                "failed": 0,
                "results": results,
            },
            "summary": summary,
        }

    async def _handle_blocked(
        self,
        task: Task,
        blocked_metrics: list[dict[str, Any]],
        passed: int,
        total: int,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """处理阻塞的情况"""
        if not self.state_service:
            return {
                "error": "未注入 TaskStateService",
                "error_code": "NO_STATE_SERVICE",
            }

        await self.state_service.set_blocked(
            task_id=task.id,
            reason=f"{len(blocked_metrics)} 个指标达到最大重试次数",
        )

        return {
            "task_id": task.id,
            "task_status": ExecutionStatus.BLOCKED.value,
            "message": f"🚫 任务阻塞：{len(blocked_metrics)} 个指标达到最大重试次数 ({passed}/{total})",
            "blocked_metrics": blocked_metrics,
            "evaluation_summary": {
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "results": results,
            },
        }

    async def _handle_partial_passed(
        self,
        task: Task,
        acceptance_criteria: list[dict[str, Any]],
        passed: int,
        failed: int,
        total: int,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """处理部分通过的情况（使用进步重置机制）"""
        if not self.state_service:
            return {
                "error": "未注入 TaskStateService",
                "error_code": "NO_STATE_SERVICE",
            }

        retry_info = self._calculate_progress_and_retry(
            acceptance_criteria=acceptance_criteria,
            task=task,
        )

        new_status = self._determine_task_status(
            acceptance_criteria=acceptance_criteria,
            task=task,
            retry_info=retry_info,
        )

        progress = self.progress_calculator.calculate(acceptance_criteria)

        await self.state_service.apply_evaluation_result(
            task_id=task.id,
            evaluation_result={
                "acceptance_criteria": acceptance_criteria,
                "progress": progress.to_dict(),
                "retry_info": retry_info,
                "new_status": new_status,
            },
        )

        await self._publish_evaluation_events(
            task_id=task.id,
            new_status=new_status,
            progress=progress.to_dict(),
        )

        failed_metrics = [
            {
                "metric_id": ac.get("metric_id"),
                "retry_count": ac.get("retry_count", 0),
                "feedback": ac.get("evaluation_result", {}).get("feedback", ""),
            }
            for ac in acceptance_criteria
            if ac.get("status") == "failed"
        ]

        if new_status == ExecutionStatus.BLOCKED.value:
            return {
                "task_id": task.id,
                "task_status": ExecutionStatus.BLOCKED.value,
                "message": f"🚫 任务阻塞：达到最大重试次数 ({passed}/{total})",
                "retry_count": retry_info["retry_count"],
                "failed_metrics": failed_metrics,
                "evaluation_summary": {
                    "total": total,
                    "passed": passed,
                    "failed": failed,
                    "results": results,
                },
            }

        return {
            "task_id": task.id,
            "task_status": ExecutionStatus.RUNNING.value,
            "message": f"🔄 任务继续：{failed} 个评估指标失败 ({passed}/{total})",
            "retry_count": retry_info["retry_count"],
            "has_progress": retry_info["has_progress"],
            "failed_metrics": failed_metrics,
            "evaluation_summary": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "results": results,
            },
            "next_step": "修复失败的指标后重新评估",
        }

    async def _create_evaluation_record(
        self,
        task_id: str,
        metric_id: str,
        parent_record_id: str,
    ) -> str:
        """
        创建评估记录（不包含结果）

        评估记录在评估器执行前创建，用于建立嵌套结构。
        评估结果在评估器执行完成后通过 _update_evaluation_record() 更新。

        Args:
            task_id: 任务 ID
            metric_id: 评估指标 ID
            parent_record_id: 父记录 ID（工具调用记录 ID）

        Returns:
            创建的评估记录 ID
        """
        from src.db.repositories.execution_record_repo import ExecutionRecordRepository
        from src.utils.id_encoder import parse_nested_id
        from src.utils.sequence_manager import get_next_sequence

        sequence = await get_next_sequence(parent_record_id)

        try:
            parent_parsed = parse_nested_id(parent_record_id)
            depth = parent_parsed.get("depth", 0) + 1
        except Exception:
            depth = 1

        message_data = {
            "type": "evaluation",
            "record_type": "evaluation",
            "task_id": task_id,
            "metric_id": metric_id,
            "status": "running",
            "order": {
                "sequence": sequence,
                "depth": depth,
            },
        }

        repo = ExecutionRecordRepository(self.session)
        record_id = await repo.save_execution_record(
            session_id=task_id,
            message_data=message_data,
            parent_record_id=parent_record_id,
        )

        logger.info(
            f"[EvaluationService] 创建评估记录 | record_id={record_id} | metric_id={metric_id}"
        )

        return record_id

    async def _update_evaluation_record(
        self,
        record_id: str,
        result: dict[str, Any],
    ) -> None:
        """
        更新评估记录的结果

        在评估器执行完成后调用，更新评估记录的输出和状态。

        Args:
            record_id: 评估记录 ID
            result: 评估结果，包含 passed、score、feedback 等字段
        """
        try:
            db_record = await self.session.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == record_id)
            )
            record = db_record.scalar_one_or_none()

            if not record:
                logger.warning(
                    f"[EvaluationService] 未找到评估记录 | record_id={record_id}"
                )
                return

            # 更新消息数据
            message_data = record.message_data or {}
            message_data["output"] = {
                "passed": result.get("passed", False),
                "score": result.get("score", 0.0),
                "feedback": result.get("feedback", ""),
                "details": result.get("details", {}),
            }
            message_data["status"] = (
                "completed" if result.get("passed", False) else "failed"
            )

            record.message_data = message_data
            await self.session.commit()

            logger.info(
                f"[EvaluationService] 更新评估记录 | record_id={record_id} | "
                f"passed={result.get('passed', False)} | score={result.get('score', 0.0)}"
            )

        except Exception as e:
            logger.error(
                f"[EvaluationService] 更新评估记录失败 | record_id={record_id} | error={e}"
            )

    async def apply_evaluation_results(
        self,
        task_id: str,
        evaluation_results: list[dict[str, Any]],
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """
        应用评估结果（不执行评估）

        用于外部评估完成后，只应用结果更新任务状态。
        与 execute_and_apply() 的区别：此方法不执行评估，只应用已有结果。

        Args:
            task_id: 任务 ID
            evaluation_results: 评估结果列表，每个元素包含：
                - metric_id: 指标 ID
                - passed: 是否通过
                - score: 评分
                - feedback: 反馈信息
                - details: 详细信息（可选）
            session: 数据库会话（可选，为空时使用实例session）

        Returns:
            应用结果，包含：
            - task_id: 任务 ID
            - task_status: 任务状态
            - progress: 进度信息
            - all_passed: 是否全部通过
            - has_progress: 是否有进步
            - retry_count: 当前重试次数
        """
        db_session = session or self.session
        if not db_session:
            return {"error": "未提供数据库会话", "error_code": "NO_SESSION"}

        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        acceptance_criteria = task.acceptance_criteria or []
        now = datetime.now(UTC)

        for eval_result in evaluation_results:
            metric_id = eval_result.get("metric_id")
            if not metric_id:
                continue

            for i, ac in enumerate(acceptance_criteria):
                if ac.get("metric_id") == metric_id:
                    if ac.get("retry_count", 0) >= self.ac_max_retries:
                        logger.warning(
                            f"[EvaluationService] AC {metric_id} 已达最大重试次数，跳过"
                        )
                        continue

                    ac["evaluated_at"] = now.isoformat()
                    ac["evaluation_result"] = {
                        "passed": eval_result.get("passed", False),
                        "score": eval_result.get("score", 0),
                        "feedback": eval_result.get("feedback", ""),
                        "details": eval_result.get("details", {}),
                    }

                    if eval_result.get("passed", False):
                        ac["status"] = "passed"
                        ac["passed_at"] = now.isoformat()
                    else:
                        ac["status"] = "failed"
                        ac["retry_count"] = ac.get("retry_count", 0) + 1

                    acceptance_criteria[i] = ac
                    break

        progress = self.progress_calculator.calculate(acceptance_criteria)

        retry_info = self._calculate_progress_and_retry(
            acceptance_criteria=acceptance_criteria,
            task=task,
        )

        new_status = self._determine_task_status(
            acceptance_criteria=acceptance_criteria,
            task=task,
            retry_info=retry_info,
        )

        # 通过 TaskStateService 应用评估结果
        if not self.state_service:
            return {
                "error": "未注入 TaskStateService",
                "error_code": "NO_STATE_SERVICE",
            }

        apply_result = await self.state_service.apply_evaluation_result(
            task_id=task_id,
            evaluation_result={
                "acceptance_criteria": acceptance_criteria,
                "progress": progress.to_dict(),
                "retry_info": retry_info,
                "new_status": new_status,
            },
        )

        # 发布事件
        await self._publish_evaluation_events(
            task_id=task_id,
            new_status=new_status,
            progress=progress.to_dict(),
        )

        # 如果状态为 blocked，触发人工审批
        if new_status == ExecutionStatus.BLOCKED.value:
            await self._trigger_human_approval(
                task_id=task_id,
                task=task,
                acceptance_criteria=acceptance_criteria,
            )

        # 如果任务完成，通知父任务
        if new_status == ExecutionStatus.COMPLETED.value:
            await self._notify_parent_task_via_record(
                child_task_id=task_id,
                child_task=task,
                evaluation_results=evaluation_results,
                session=db_session,
            )

        return apply_result

    async def complete_task_after_evaluation(
        self,
        task_id: str,
        evaluation_results: list[dict[str, Any]] | None = None,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """
        评估完成后将任务状态变更为完成

        这是任务状态变更为 completed 的唯一入口。
        所有需要将任务标记为完成的地方都必须调用此方法。

        流程：
        1. 验证任务存在
        2. 如果有评估指标，必须全部通过才能完成
        3. 通过 TaskStateService 更新状态
        4. 发布任务完成事件
        5. 通知父任务

        Args:
            task_id: 任务 ID
            evaluation_results: 评估结果列表（可选，用于记录）
            session: 数据库会话（可选）

        Returns:
            完成结果，包含：
            - task_id: 任务 ID
            - task_status: 任务状态
            - message: 结果消息
            - error: 错误信息（如果失败）

        Raises:
            ValueError: 任务不存在或评估未全部通过
        """
        db_session = session or self.session
        if not db_session:
            return {"error": "未提供数据库会话", "error_code": "NO_SESSION"}

        result = await db_session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        acceptance_criteria = task.acceptance_criteria or []
        total = len(acceptance_criteria)
        passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")

        if total > 0 and passed < total:
            failed_count = total - passed
            logger.warning(
                f"[EvaluationService] 任务 {task_id} 评估未全部通过，无法完成 | "
                f"passed={passed}/{total} | failed={failed_count}"
            )
            return {
                "error": f"评估未全部通过，还有 {failed_count} 个指标未通过",
                "error_code": "EVALUATION_NOT_PASSED",
                "task_id": task_id,
                "passed": passed,
                "total": total,
            }

        if not self.state_service:
            return {
                "error": "未注入 TaskStateService",
                "error_code": "NO_STATE_SERVICE",
            }

        progress = self.progress_calculator.calculate(acceptance_criteria)

        apply_result = await self.state_service.apply_evaluation_result(
            task_id=task_id,
            evaluation_result={
                "acceptance_criteria": acceptance_criteria,
                "progress": progress.to_dict(),
                "retry_info": {
                    "current_passed_count": passed,
                    "best_passed_count": task.best_passed_count or passed,
                    "last_passed_count": passed,
                    "retry_count": task.retry_count or 0,
                    "has_progress": False,
                    "max_retries_exceeded": False,
                },
                "new_status": ExecutionStatus.COMPLETED.value,
            },
        )

        await self._publish_evaluation_events(
            task_id=task_id,
            new_status=ExecutionStatus.COMPLETED.value,
            progress=progress.to_dict(),
        )

        if evaluation_results:
            await self._notify_parent_task_via_record(
                child_task_id=task_id,
                child_task=task,
                evaluation_results=evaluation_results,
                session=db_session,
            )

        logger.info(
            f"[EvaluationService] 任务已完成 | task_id={task_id} | "
            f"passed={passed}/{total}"
        )

        return {
            "task_id": task_id,
            "task_status": ExecutionStatus.COMPLETED.value,
            "message": f"任务完成！评估指标全部通过 ({passed}/{total})",
            "progress": progress.to_dict(),
            **apply_result,
        }

    async def _get_task(self, task_id: str) -> Task | None:
        """
        获取任务

        Args:
            task_id: 任务 ID

        Returns:
            任务对象
        """
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()

    def _calculate_progress_and_retry(
        self,
        acceptance_criteria: list[dict[str, Any]],
        task: Task,
    ) -> dict[str, Any]:
        """
        计算进度和重试次数（进步重置机制）

        进步重置机制：
        - 如果当前通过指标数 > 历史最佳，重试次数重置为 0
        - 如果当前通过指标数 <= 上次通过数，重试次数 +1
        - 重试次数达到 max_retries 时，标记需要人工审批

        Args:
            acceptance_criteria: 验收标准列表
            task: 任务对象

        Returns:
            重试信息，包含：
            - current_passed_count: 当前通过指标数
            - best_passed_count: 历史最佳通过指标数
            - last_passed_count: 上次评估通过指标数
            - retry_count: 当前重试次数
            - has_progress: 是否有进步
            - max_retries_exceeded: 是否超过最大重试次数
        """
        current_passed = sum(
            1 for ac in acceptance_criteria if ac.get("status") == "passed"
        )

        best_passed = getattr(task, "best_passed_count", 0) or 0
        last_passed = getattr(task, "last_passed_count", 0) or 0
        current_retry = getattr(task, "retry_count", 0) or 0
        max_retries = (
            getattr(task, "max_retries", self.task_max_retries) or self.task_max_retries
        )

        has_progress = False
        new_retry_count = current_retry

        if current_passed > best_passed:
            # 有进步：重置重试次数
            has_progress = True
            new_retry_count = 0
            best_passed = current_passed
            logger.info(
                f"[EvaluationService] 任务 {task.id} 有进步 | "
                f"通过指标: {last_passed} → {current_passed} | "
                f"重试次数重置为 0"
            )
        elif current_passed <= last_passed:
            # 无进步或退步：累加重试次数
            new_retry_count = current_retry + 1
            logger.info(
                f"[EvaluationService] 任务 {task.id} 无进步 | "
                f"通过指标: {current_passed} | "
                f"重试次数: {current_retry} → {new_retry_count}"
            )

        max_retries_exceeded = new_retry_count >= max_retries

        return {
            "current_passed_count": current_passed,
            "best_passed_count": best_passed,
            "last_passed_count": current_passed,  # 更新为当前值
            "retry_count": new_retry_count,
            "has_progress": has_progress,
            "max_retries_exceeded": max_retries_exceeded,
        }

    def _determine_task_status(
        self,
        acceptance_criteria: list[dict[str, Any]],
        task: Task,
        retry_info: dict[str, Any] | None = None,
    ) -> str:
        """
        判断任务状态

        Args:
            acceptance_criteria: 验收标准列表
            task: 任务对象
            retry_info: 重试信息（进步重置机制）

        Returns:
            任务状态
        """
        total = len(acceptance_criteria)
        passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")

        # 全部通过，任务完成
        if passed == total:
            return ExecutionStatus.COMPLETED.value

        # 检查是否有 AC 达到最大重试次数
        any_ac_max_retries = any(
            ac.get("retry_count", 0) >= self.ac_max_retries
            for ac in acceptance_criteria
            if ac.get("status") != "passed"
        )

        if any_ac_max_retries:
            logger.warning(
                f"[EvaluationService] 任务 {task.id} 被阻塞：至少一个 AC 达到最大重试次数"
            )
            return ExecutionStatus.BLOCKED.value

        # 检查任务级别的重试次数（进步重置机制）
        if retry_info and retry_info.get("max_retries_exceeded", False):
            logger.warning(
                f"[EvaluationService] 任务 {task.id} 被阻塞：达到最大重试次数 "
                f"({retry_info['retry_count']}/{task.max_retries or self.task_max_retries})"
            )
            return ExecutionStatus.BLOCKED.value

        # 回到执行阶段继续重试
        return ExecutionStatus.RUNNING.value

    async def _trigger_human_approval(
        self,
        task_id: str,
        task: Task,
        acceptance_criteria: list[dict[str, Any]],
    ) -> None:
        """
        触发人工审批

        当任务评估失败且达到最大重试次数时，调用 TaskApprovalService 请求人工审批。

        Args:
            task_id: 任务 ID
            task: 任务对象
            acceptance_criteria: 验收标准列表
        """
        try:
            from src.tasks.services.approval_service import TaskApprovalService

            logger.info(f"[EvaluationService] 触发人工审批 | task_id={task_id}")

            failed_acs = [
                {
                    "metric_id": ac.get("metric_id"),
                    "retry_count": ac.get("retry_count", 0),
                    "last_feedback": ac.get("evaluation_result", {}).get(
                        "feedback", ""
                    ),
                }
                for ac in acceptance_criteria
                if ac.get("status") != "passed"
            ]

            approval_service = TaskApprovalService()
            await approval_service.create_approval_request(
                task_id=task_id,
                reason=f"评估失败，已重试 {self.ac_max_retries} 次",
                failed_criteria=failed_acs,
            )

            logger.info(f"[EvaluationService] 人工审批请求已创建 | task_id={task_id}")

        except Exception as e:
            logger.error(
                f"[EvaluationService] 触发人工审批失败 | task_id={task_id} | error={e}"
            )

    async def _publish_evaluation_events(
        self,
        task_id: str,
        new_status: str,
        progress: dict[str, Any],
    ) -> None:
        """
        发布评估相关事件

        Args:
            task_id: 任务 ID
            new_status: 新状态
            progress: 进度信息
        """
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.STATE_CHANGE,
                session_id=task_id,
                data={
                    "task_id": task_id,
                    "new_status": new_status,
                    "progress": progress,
                    "source": "evaluation_service",
                },
            )
        )

        if new_status == ExecutionStatus.COMPLETED.value:
            await event_bus.publish(
                ExecutionEvent(
                    event_type=EventType.CUSTOM,
                    session_id=task_id,
                    data={
                        "custom_event_type": "task_completed",
                        "task_id": task_id,
                        "status": "completed",
                    },
                )
            )

        logger.info(
            f"[EvaluationService] 评估事件已发布 | "
            f"task_id={task_id} | status={new_status}"
        )

    async def _notify_parent_task_via_record(
        self,
        child_task_id: str,
        child_task: Task,
        evaluation_results: list[dict[str, Any]],
        session: AsyncSession,
    ) -> None:
        """
        通知父任务：子任务已完成（通过 ExecutionRecord）

        将通知消息插入到父任务执行记录的同一层级，父任务下次构建上下文时会自动加载。
        使用现有的 "human" 消息类型，确保上下文构建器能正确解析。

        Args:
            child_task_id: 子任务 ID
            child_task: 子任务对象
            evaluation_results: 评估结果列表
            session: 数据库会话
        """
        try:
            if not child_task.parent_task_id:
                return

            result = await session.execute(
                select(Task).where(Task.id == child_task.parent_task_id)
            )
            parent_task = result.scalar_one_or_none()
            if not parent_task:
                return

            parent_execution_record_id = parent_task.execution_record_id
            if not parent_execution_record_id:
                return

            result = await session.execute(
                select(ExecutionRecord).where(
                    ExecutionRecord.id == parent_execution_record_id
                )
            )
            parent_record = result.scalar_one_or_none()
            if not parent_record:
                return

            insert_parent_id = parent_record.parent_record_id

            new_record_id = await generate_execution_record_id(
                db=session,
                session_id=parent_record.session_id,
                parent_record_id=insert_parent_id,
            )

            passed_count = sum(1 for r in evaluation_results if r.get("passed"))
            total_count = len(evaluation_results)

            content = (
                f"[子任务完成通知] {child_task.title or child_task_id}\n"
                f"状态: 已完成 | 评估: {passed_count}/{total_count} 通过"
            )

            from src.utils.id_encoder import decode_base36

            sequence = decode_base36(new_record_id.split("-")[-1])
            depth = len(new_record_id.split("-")) - 2

            message_data = {
                "type": "human",
                "role": "user",
                "content": content,
                "order": {
                    "sequence": sequence,
                    "depth": depth,
                },
            }

            execution_record_repo = ExecutionRecordRepository(session)
            await execution_record_repo.save_execution_record(
                session_id=parent_record.session_id,
                message_data=message_data,
                parent_record_id=insert_parent_id,
                record_id=new_record_id,
            )

            logger.info(
                f"[EvaluationService] 已通知父任务 | "
                f"child_task_id={child_task_id} | "
                f"parent_task_id={parent_task.id} | "
                f"new_record_id={new_record_id}"
            )

        except Exception as e:
            logger.error(
                f"[EvaluationService] 通知父任务失败 | "
                f"child_task_id={child_task_id} | error={e}",
                exc_info=True,
            )
