"""
任务人工审批服务

负责处理任务的人工审批流程。
使用统一的人类交互抽象层。

核心职责：
1. 创建审批请求
2. 处理审批决策
3. 更新任务状态
4. 发送审批通知

审批选项：
- manual_pass_criteria: 人工通过评估指标（让失败的指标通过，继续评估流程）
- adjust_criteria: 调整标准后重试（重置重试次数，继续执行）
- cancel_task: 取消任务

注意：任务只能通过评估流程自动完成，不能人工强制完成。
人工审批只能让评估指标通过，最终是否完成任务仍由评估系统决定。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from human_interaction.models import Priority
from human_interaction.service import get_human_interaction_service
from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.states import ExecutionStatus
from src.db.models import Task
from src.db.session_manager import managed_session

logger = logging.getLogger(__name__)


class TaskApprovalService:
    """
    任务人工审批服务

    使用统一的人类交互抽象层处理任务审批
    """

    ACTION_MANUAL_PASS_CRITERIA = "manual_pass_criteria"
    ACTION_ADJUST_CRITERIA = "adjust_criteria"
    ACTION_CANCEL_TASK = "cancel_task"

    VALID_ACTIONS = [
        ACTION_MANUAL_PASS_CRITERIA,
        ACTION_ADJUST_CRITERIA,
        ACTION_CANCEL_TASK,
    ]

    def __init__(self):
        self._interaction_service = get_human_interaction_service()

    async def create_approval_request(
        self,
        task_id: str,
        reason: str,
        failed_criteria: list[dict[str, Any]] | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """创建人工审批请求并阻塞等待用户决策，然后自动执行对应动作。

        BUG-FIX-fix_20260529_approval_stuck:
        问题根因: 原实现只创建 choice 请求后立即返回，不等待用户响应。
                 用户选择审批选项后，响应写入 HumanInteractionService 但
                 无人消费（没有 wait_for_choice 调用），process_decision()
                 从未被触发，任务永远停留在 BLOCKED 状态。
        修复方案: 创建请求后调用 wait_for_choice() 阻塞等待用户响应，
                 收到响应后自动调用 process_decision() 执行对应动作。

        Args:
            task_id: 任务 ID
            reason: 审批原因
            failed_criteria: 失败的指标列表
            thread_id: 线程 ID（可选）

        Returns:
            审批处理结果（来自 process_decision）
        """
        async with managed_session() as session:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

            if task.status != ExecutionStatus.BLOCKED.value:
                logger.error(
                    "[TaskApprovalService] 任务状态不是 blocked | "
                    "task_id=%s | status=%s", task_id, task.status
                )

            approval_options = [
                {
                    "id": self.ACTION_MANUAL_PASS_CRITERIA,
                    "label": "人工通过评估指标",
                    "description": "让失败的评估指标通过，继续评估流程",
                    "is_default": True,
                },
                {
                    "id": self.ACTION_ADJUST_CRITERIA,
                    "label": "调整标准后重试",
                    "description": "重置重试次数，继续执行任务",
                },
                {
                    "id": self.ACTION_CANCEL_TASK,
                    "label": "取消任务",
                    "description": "终止任务执行",
                    "is_destructive": True,
                },
            ]

            request_id = await self._interaction_service.create_choice_request(
                session_id=task_id,
                thread_id=thread_id or task_id,
                tab_id="task_approval",
                title=f"任务审批: {task.title}",
                description=f"任务评估失败，需要人工决策\n\n原因: {reason}",
                options=approval_options,
                timeout_seconds=600,
                priority=Priority.HIGH,
                agent_id=task_id,
                file_paths=None,
            )

            logger.info(
                "[TaskApprovalService] 审批请求已创建，等待用户决策 | "
                "task_id=%s | request_id=%s", task_id, request_id
            )

        from human_interaction.service import InteractionCancelledError, InteractionTimeoutError  # noqa: PLC0415

        try:
            response = await self._interaction_service.wait_for_choice(
                request_id, timeout=600,
            )
        except InteractionTimeoutError:
            logger.warning(
                "[TaskApprovalService] 审批超时 | task_id=%s | request_id=%s",
                task_id, request_id,
            )
            return {
                "task_id": task_id,
                "status": "timeout",
                "message": "审批超时，用户未在规定时间内响应",
            }
        except InteractionCancelledError as e:
            logger.info(
                "[TaskApprovalService] 审批取消 | task_id=%s | request_id=%s | reason=%s",
                task_id, request_id, e.reason,
            )
            return {
                "task_id": task_id,
                "status": "cancelled",
                "message": f"审批已取消: {e.reason or '用户取消'}",
            }

        selected_option = response.get("selected_option")
        feedback = response.get("feedback")

        logger.info(
            "[TaskApprovalService] 收到审批决策 | task_id=%s | action=%s | feedback=%s",
            task_id, selected_option, (feedback or "")[:100],
        )

        return await self.process_decision(
            task_id=task_id,
            action=selected_option,
            reason=feedback,
        )

    async def process_decision(
        self,
        task_id: str,
        action: str,
        reason: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """
        处理审批决策

        Args:
            task_id: 任务 ID
            action: 审批动作
            reason: 审批原因/备注
            user_id: 操作用户 ID

        Returns:
            处理结果
        """
        if action not in self.VALID_ACTIONS:
            return {
                "error": f"无效的审批动作: {action}",
                "error_code": "INVALID_ACTION",
                "valid_actions": self.VALID_ACTIONS,
            }

        async with managed_session() as session:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

            if task.status != ExecutionStatus.BLOCKED.value:
                return {
                    "error": f"任务状态不是 blocked: {task.status}",
                    "error_code": "INVALID_STATUS",
                }

            now = datetime.now(UTC)

            if action == self.ACTION_MANUAL_PASS_CRITERIA:
                result = await self._handle_manual_pass_criteria(
                    session, task, reason, user_id, now
                )
            elif action == self.ACTION_ADJUST_CRITERIA:
                result = await self._handle_adjust_criteria(
                    session, task, reason, user_id, now
                )
            elif action == self.ACTION_CANCEL_TASK:
                result = await self._handle_cancel_task(
                    session, task, reason, user_id, now
                )
            else:
                result = {"error": f"未知的审批动作: {action}"}

            if "error" not in result:
                event_bus = get_event_bus()
                await event_bus.publish(
                    ExecutionEvent(
                        event_type=EventType.CUSTOM,
                        session_id=None,
                        data={
                            "custom_event_type": "task.approval_completed",
                            "task_id": task_id,
                            "action": action,
                            "reason": reason,
                            "user_id": user_id,
                            "result": result,
                        },
                    )
                )

            return result

    async def _handle_manual_pass_criteria(
        self,
        session: AsyncSession,
        task: Task,
        reason: str | None,
        user_id: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        """
        处理人工通过评估指标

        将失败的评估指标标记为人工通过，然后触发评估流程继续。
        注意：这不直接完成任务，任务是否完成由评估系统决定。

        Args:
            session: 数据库会话
            task: 任务对象
            reason: 通过原因
            user_id: 操作用户 ID
            now: 当前时间

        Returns:
            处理结果
        """
        acceptance_criteria = task.acceptance_criteria or []
        passed_criteria_ids = []

        # 将所有失败的指标标记为人工通过
        for i, ac in enumerate(acceptance_criteria):
            if ac.get("status") != "passed":
                ac["status"] = "passed"
                ac["passed_at"] = now.isoformat()
                ac["evaluation_result"] = {
                    "passed": True,
                    "score": 100,
                    "feedback": f"人工通过: {reason or '无原因'} | 操作人: {user_id or '未知'}",
                    "details": {
                        "manual_pass": True,
                        "passed_by": user_id,
                        "passed_reason": reason,
                    },
                }
                passed_criteria_ids.append(ac.get("metric_id", "unknown"))
                acceptance_criteria[i] = ac

        # 更新任务状态为 evaluating，让评估系统继续处理
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                status=ExecutionStatus.EVALUATING.value,
                acceptance_criteria=acceptance_criteria,
                updated_at=now,
            )
        )
        await session.commit()

        logger.info(
            "[TaskApprovalService] 评估指标已人工通过 | "
            "task_id=%s | user_id=%s | passed_criteria=%s",
            task.id, user_id, passed_criteria_ids
        )

        # 触发评估流程（延迟导入避免与 evaluation_service 循环依赖）
        from src.tasks.services.evaluation_service import EvaluationService  # noqa: PLC0415

        evaluation_service = EvaluationService(session=session)
        eval_results = [
            {
                "metric_id": ac.get("metric_id"),
                "passed": True,
                "score": 100,
                "feedback": f"人工通过: {reason or '无原因'}",
            }
            for ac in acceptance_criteria
            if ac.get("metric_id") in passed_criteria_ids
        ]

        apply_result = await evaluation_service.apply_evaluation_results(
            task_id=task.id,
            evaluation_results=eval_results,
            session=session,
        )

        return {
            "task_id": task.id,
            "action": self.ACTION_MANUAL_PASS_CRITERIA,
            "passed_criteria": passed_criteria_ids,
            "evaluation_result": apply_result,
            "reason": reason,
            "processed_at": now.isoformat(),
        }

    async def _handle_adjust_criteria(
        self,
        session: AsyncSession,
        task: Task,
        reason: str | None,
        user_id: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                status=ExecutionStatus.RUNNING.value,
                retry_count=0,
                updated_at=now,
            )
        )
        await session.commit()

        logger.info(
            "[TaskApprovalService] 任务调整标准后重试 | "
            "task_id=%s | user_id=%s", task.id, user_id
        )

        return {
            "task_id": task.id,
            "action": self.ACTION_ADJUST_CRITERIA,
            "new_status": ExecutionStatus.RUNNING.value,
            "retry_count_reset": True,
            "reason": reason,
            "processed_at": now.isoformat(),
        }

    async def _handle_cancel_task(
        self,
        session: AsyncSession,
        task: Task,
        reason: str | None,
        user_id: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                status=ExecutionStatus.CANCELLED.value,
                updated_at=now,
            )
        )
        await session.commit()

        logger.info(
            "[TaskApprovalService] 任务已取消 | "
            "task_id=%s | user_id=%s", task.id, user_id
        )

        return {
            "task_id": task.id,
            "action": self.ACTION_CANCEL_TASK,
            "new_status": ExecutionStatus.CANCELLED.value,
            "reason": reason,
            "processed_at": now.isoformat(),
        }

    async def get_blocked_tasks(
        self,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with managed_session() as session:
            query = (
                select(Task)
                .where(Task.status == ExecutionStatus.BLOCKED.value)
                .order_by(Task.updated_at.desc())
                .limit(limit)
            )

            if user_id:
                query = query.where(Task.user_id == user_id)

            result = await session.execute(query)
            tasks = result.scalars().all()

            return [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "status": task.status,
                    "retry_count": task.retry_count,
                    "continuation_count": task.continuation_count,
                    "blocked_at": task.updated_at.isoformat() if task.updated_at else None,
                }
                for task in tasks
            ]

    async def get_approval_status(self, task_id: str) -> dict[str, Any]:
        async with managed_session() as session:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

            return {
                "task_id": task_id,
                "task_status": task.status,
                "is_blocked": task.status == ExecutionStatus.BLOCKED.value,
                "retry_count": task.retry_count,
                "continuation_count": task.continuation_count,
            }
