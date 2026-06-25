"""
任务恢复服务

负责任务的恢复、取消和重试操作。

核心功能：
1. 恢复阻塞/失败的任务
2. 取消任务（支持级联）
3. 重试失败的任务
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.states import ExecutionStatus
from src.db.models import Task
from src.tasks.state_machine import get_task_state_machine

logger = logging.getLogger(__name__)


class TaskRecoveryService:
    """
    任务恢复服务

    负责任务的恢复、取消和重试操作。

    核心职责：
    1. 恢复阻塞/失败的任务
    2. 取消任务（支持级联）
    3. 重试失败的任务

    Example:
        >>> service = TaskRecoveryService(session)
        >>> result = await service.resume_task("task-001")
        >>> result = await service.cancel_task("task-002", reason="用户取消")
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务恢复服务

        Args:
            session: 数据库会话
        """
        self.session = session
        self.state_machine = get_task_state_machine()

    async def resume_task(
        self,
        task_id: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """
        恢复任务执行

        将阻塞或失败的任务恢复为执行中状态。

        Args:
            task_id: 任务 ID
            message: 恢复消息

        Returns:
            恢复结果，包含：
            - task_id: 任务 ID
            - status: 任务状态
            - pending_criteria: 待完成的验收标准
            - message: 恢复消息
            - resumed_at: 恢复时间
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        # 检查任务状态是否可恢复
        if task.status == ExecutionStatus.COMPLETED.value:
            return {"error": "任务已完成", "error_code": "TASK_COMPLETED"}

        if task.status == ExecutionStatus.CANCELLED.value:
            return {"error": "任务已取消", "error_code": "TASK_CANCELLED"}

        now = datetime.now(UTC)

        # 找出未完成的 AC
        pending_criteria = [
            ac
            for ac in (task.acceptance_criteria or [])
            if ac.get("status") != "passed"
        ]

        # 重置失败 AC 的重试计数（允许重新尝试）
        for ac in task.acceptance_criteria or []:
            if ac.get("status") == "failed":
                ac["retry_count"] = 0
                ac["status"] = "pending"

        # 更新任务状态
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=ExecutionStatus.RUNNING.value,
                acceptance_criteria=task.acceptance_criteria,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(
            f"[TaskRecoveryService] 任务已恢复 | "
            f"task_id={task_id} | pending_criteria={len(pending_criteria)}"
        )

        # 发布恢复事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.CUSTOM,
                session_id=None,
                data={
                    "custom_event_type": "task.resumed",
                    "task_id": task_id,
                    "old_status": task.status,
                    "new_status": ExecutionStatus.RUNNING.value,
                    "message": message,
                },
            )
        )

        return {
            "task_id": task_id,
            "status": ExecutionStatus.RUNNING.value,
            "pending_criteria": [
                {"id": ac.get("metric_id"), "description": ac.get("description")}
                for ac in pending_criteria
            ],
            "message": message or "任务已恢复，请继续执行",
            "resumed_at": now.isoformat(),
        }

    async def cancel_task(
        self,
        task_id: str,
        reason: str | None = None,
        cascade: bool = True,
    ) -> dict[str, Any]:
        """
        取消任务

        Args:
            task_id: 任务 ID
            reason: 取消原因
            cascade: 是否级联取消子任务

        Returns:
            取消结果，包含：
            - success: 是否成功
            - task_id: 任务 ID
            - cancelled_at: 取消时间
            - reason: 取消原因
            - cascaded_count: 级联取消的子任务数量
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        # 检查是否可取消
        if task.status in [ExecutionStatus.COMPLETED.value, ExecutionStatus.CANCELLED.value]:
            return {
                "error": f"任务已{task.status}，无法取消",
                "error_code": "TASK_ALREADY_FINISHED",
            }

        now = datetime.now(UTC)

        # 更新任务状态
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=ExecutionStatus.CANCELLED.value,
                updated_at=now,
                task_metadata={
                    **(task.task_metadata or {}),
                    "cancelled_at": now.isoformat(),
                    "cancel_reason": reason or "用户取消",
                },
            )
        )
        await self.session.flush()

        logger.info(
            f"[TaskRecoveryService] 任务已取消 | task_id={task_id} | reason={reason}"
        )

        # 发布取消事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.TASK_CANCELLED,
                session_id=None,
                data={
                    "task_id": task_id,
                    "reason": reason,
                    "cascade": cascade,
                    "parent_task_id": task.parent_task_id,
                },
            )
        )

        # 级联取消子任务
        cascaded_count = 0
        if cascade:
            subtasks = await self._get_subtasks(task_id)
            for subtask in subtasks:
                if subtask.status not in [
                    ExecutionStatus.COMPLETED.value,
                    ExecutionStatus.CANCELLED.value,
                ]:
                    result = await self.cancel_task(
                        subtask.id, f"父任务取消: {reason}", cascade=True
                    )
                    if result.get("success"):
                        cascaded_count += 1

        return {
            "success": True,
            "task_id": task_id,
            "cancelled_at": now.isoformat(),
            "reason": reason or "用户取消",
            "cascaded_count": cascaded_count,
        }

    async def retry_task(
        self,
        task_id: str,
        reset_retry_count: bool = True,
    ) -> dict[str, Any]:
        """
        重试失败的任务

        将失败的任务重置为待执行状态。

        Args:
            task_id: 任务 ID
            reset_retry_count: 是否重置重试计数

        Returns:
            重试结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        # 只有失败的任务可以重试
        if task.status != ExecutionStatus.FAILED.value:
            return {
                "error": f"只有失败的任务可以重试，当前状态: {task.status}",
                "error_code": "INVALID_STATUS_FOR_RETRY",
            }

        now = datetime.now(UTC)

        # 重置 AC 状态
        acceptance_criteria = task.acceptance_criteria or []
        for ac in acceptance_criteria:
            if ac.get("status") != "passed":
                ac["status"] = "pending"
                if reset_retry_count:
                    ac["retry_count"] = 0

        # 更新任务状态
        update_data = {
            "status": ExecutionStatus.PENDING.value,
            "acceptance_criteria": acceptance_criteria,
            "updated_at": now,
        }

        if reset_retry_count:
            update_data["retry_count"] = 0

        await self.session.execute(
            update(Task).where(Task.id == task_id).values(**update_data)
        )
        await self.session.flush()

        logger.info(
            f"[TaskRecoveryService] 任务已重置为待执行 | task_id={task_id}"
        )

        # 发布重试事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.CUSTOM,
                session_id=None,
                data={
                    "custom_event_type": "task.retry",
                    "task_id": task_id,
                    "reset_retry_count": reset_retry_count,
                },
            )
        )

        return {
            "success": True,
            "task_id": task_id,
            "status": ExecutionStatus.PENDING.value,
            "reset_retry_count": reset_retry_count,
            "retried_at": now.isoformat(),
        }

    async def unblock_task(
        self,
        task_id: str,
        action: str = "continue",
    ) -> dict[str, Any]:
        """
        解除任务阻塞状态

        Args:
            task_id: 任务 ID
            action: 操作类型
                - "continue": 继续执行（重置 AC 重试计数）
                - "skip": 跳过失败的 AC
                - "abort": 放弃任务

        Returns:
            解除阻塞结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        if task.status != ExecutionStatus.BLOCKED.value:
            return {
                "error": f"任务未阻塞，当前状态: {task.status}",
                "error_code": "TASK_NOT_BLOCKED",
            }

        now = datetime.now(UTC)

        if action == "abort":
            # 放弃任务
            await self.session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=ExecutionStatus.CANCELLED.value,
                    updated_at=now,
                    task_metadata={
                        **(task.task_metadata or {}),
                        "unblocked_at": now.isoformat(),
                        "unblock_action": "abort",
                    },
                )
            )
            await self.session.flush()

            return {
                "success": True,
                "task_id": task_id,
                "status": ExecutionStatus.CANCELLED.value,
                "action": action,
                "message": "任务已放弃",
            }

        # 处理 AC
        acceptance_criteria = task.acceptance_criteria or []

        if action == "continue":
            # 重置失败 AC 的重试计数
            for ac in acceptance_criteria:
                if ac.get("status") == "failed":
                    ac["retry_count"] = 0
                    ac["status"] = "pending"

        elif action == "skip":
            # 跳过失败的 AC（标记为通过）
            for ac in acceptance_criteria:
                if ac.get("status") == "failed":
                    ac["status"] = "skipped"
                    ac["skipped_at"] = now.isoformat()

        # 计算新状态
        passed = sum(1 for ac in acceptance_criteria if ac.get("status") in ["passed", "skipped"])
        total = len(acceptance_criteria)

        if passed == total:
            # 全部通过，不直接设置状态，等待 task_evaluate 工具完成
            # 只更新 acceptance_criteria，任务状态由 task_evaluate 工具处理
            new_status = task.status  # 保持当前状态
            message = "验收标准已全部通过，请使用 task_evaluate 工具完成任务"
        else:
            new_status = ExecutionStatus.RUNNING.value
            message = "任务已恢复执行"

        # 更新任务
        update_data = {
            "acceptance_criteria": acceptance_criteria,
            "updated_at": now,
            "task_metadata": {
                **(task.task_metadata or {}),
                "unblocked_at": now.isoformat(),
                "unblock_action": action,
            },
        }

        # 只有非完成状态才更新 status
        if new_status != ExecutionStatus.COMPLETED.value:
            update_data["status"] = new_status

        await self.session.execute(
            update(Task).where(Task.id == task_id).values(**update_data)
        )
        await self.session.flush()

        logger.info(
            f"[TaskRecoveryService] 任务阻塞已解除 | "
            f"task_id={task_id} | action={action} | new_status={new_status}"
        )

        # 发布解除阻塞事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.CUSTOM,
                session_id=None,
                data={
                    "custom_event_type": "task.unblocked",
                    "task_id": task_id,
                    "action": action,
                    "new_status": new_status,
                },
            )
        )

        return {
            "success": True,
            "task_id": task_id,
            "status": new_status,
            "action": action,
            "message": message,
        }

    # ========================================================================
    # 内部方法
    # ========================================================================

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

    async def _get_subtasks(self, parent_task_id: str) -> list[Task]:
        """
        获取子任务列表

        Args:
            parent_task_id: 父任务 ID

        Returns:
            子任务列表
        """
        result = await self.session.execute(
            select(Task).where(Task.parent_task_id == parent_task_id)
        )
        return list(result.scalars().all())
