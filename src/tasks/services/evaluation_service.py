"""
评估服务

负责任务评估的结果应用和状态管理。

核心功能：
1. 应用评估结果（由外部评估器执行后调用）
2. 更新任务状态（通过 TaskStateService）
3. 发布评估事件
4. 进步重置机制（有进步时重置重试次数）
5. 子任务完成通知（通过 ExecutionRecord 注入到父任务上下文）
6. 触发人工审批（重试耗尽时）

评估执行由 TaskEvaluateTool → EvaluationExecutor → EvaluationEngine 完成，
本服务只负责将评估结果应用到任务状态。

核心原则：
- 任务状态变更为 completed 只能通过此服务的 complete_task_after_evaluation()
- 所有状态转换通过 TaskStateService 进行
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import get_settings
from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.states import ExecutionStatus
from src.db.models import ExecutionRecord, Task
from src.db.repositories.execution_record_repo import ExecutionRecordRepository
from src.tasks.services.progress_calculator import get_progress_calculator
from src.tasks.services.state_service import TaskStateService
from src.utils.message_id_helper import generate_execution_record_id

logger = logging.getLogger(__name__)


class EvaluationService:
    """
    评估服务

    负责任务评估的结果应用和状态管理。

    核心职责：
    1. 应用评估结果
    2. 更新任务状态
    3. 发布评估事件
    4. 触发人工审批（重试耗尽时）
    5. 进步重置机制（有进步时重置重试次数）

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
    ):
        """初始化评估服务

        Args:
            session: 数据库会话（可选）
            state_service: 任务状态服务（可选）
        """
        self.session = session
        self.state_service = state_service
        settings = get_settings()
        self.ac_max_retries = settings.ac_max_retries
        self.task_max_retries = settings.task_max_retries
        self.progress_calculator = get_progress_calculator()

    async def apply_evaluation_results(
        self,
        task_id: str,
        evaluation_results: list[dict[str, Any]],
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """应用评估结果（不执行评估）

        用于外部评估完成后，只应用结果更新任务状态。

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
            应用结果
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

        await self._publish_evaluation_events(
            task_id=task_id,
            new_status=new_status,
            progress=progress.to_dict(),
        )

        if new_status == ExecutionStatus.BLOCKED.value:
            await self._trigger_human_approval(
                task_id=task_id,
                task=task,
                acceptance_criteria=acceptance_criteria,
            )

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
        """评估完成后将任务状态变更为完成

        这是任务状态变更为 completed 的唯一入口。

        Args:
            task_id: 任务 ID
            evaluation_results: 评估结果列表（可选，用于记录）
            session: 数据库会话（可选）

        Returns:
            完成结果
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
        """获取任务"""
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()

    def _calculate_progress_and_retry(
        self,
        acceptance_criteria: list[dict[str, Any]],
        task: Task,
    ) -> dict[str, Any]:
        """计算进度和重试次数（进步重置机制）

        进步重置机制：
        - 如果当前通过指标数 > 历史最佳，重试次数重置为 0
        - 如果当前通过指标数 <= 上次通过数，重试次数 +1
        - 重试次数达到 max_retries 时，标记需要人工审批
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
            has_progress = True
            new_retry_count = 0
            best_passed = current_passed
            logger.info(
                f"[EvaluationService] 任务 {task.id} 有进步 | "
                f"通过指标: {last_passed} → {current_passed} | "
                f"重试次数重置为 0"
            )
        elif current_passed <= last_passed:
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
            "last_passed_count": current_passed,
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
        """判断任务状态"""
        total = len(acceptance_criteria)
        passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")

        if passed == total:
            return ExecutionStatus.COMPLETED.value

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

        if retry_info and retry_info.get("max_retries_exceeded", False):
            logger.warning(
                f"[EvaluationService] 任务 {task.id} 被阻塞：达到最大重试次数 "
                f"({retry_info['retry_count']}/{task.max_retries or self.task_max_retries})"
            )
            return ExecutionStatus.BLOCKED.value

        return ExecutionStatus.RUNNING.value

    async def _trigger_human_approval(
        self,
        task_id: str,
        task: Task,
        acceptance_criteria: list[dict[str, Any]],
    ) -> None:
        """触发人工审批（重试耗尽时调用 TaskApprovalService）"""
        try:
            from src.tasks.services.approval_service import TaskApprovalService  # noqa: PLC0415

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
        """发布评估相关事件"""
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
        """通知父任务：子任务已完成（通过 ExecutionRecord）"""
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

            from src.utils.id_encoder import decode_base36  # noqa: PLC0415

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
