"""
任务阶段控制器

提供任务三阶段模型的核心控制逻辑：
- 准备阶段 (prepare): 系统强制触发，收集上下文、生成计划
- 执行阶段 (execute): Agent 自主执行
- 评估阶段 (evaluate): 系统强制触发，验证 AC

核心原则：
- 准备和评估阶段由系统强制触发
- 执行阶段由 Agent 自主控制
- 评估失败可回到执行阶段重试
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundException
from src.db.models import Task

logger = logging.getLogger(__name__)


def get_event_service():
    """获取事件推送服务（延迟加载避免循环导入）"""
    try:
        from src.api.websocket.service import get_event_service

        return get_event_service()
    except Exception:
        return None


class TaskPhase(str, Enum):
    """任务阶段"""

    PREPARE = "prepare"  # 准备阶段
    EXECUTE = "execute"  # 执行阶段
    EVALUATE = "evaluate"  # 评估阶段


class PhaseStatus(str, Enum):
    """阶段状态"""

    PENDING = "pending"  # 待执行
    RUNNING = "running"  # 执行中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败


# 有效的阶段转换
VALID_TRANSITIONS = {
    (None, TaskPhase.PREPARE.value),  # 初始 -> 准备
    (TaskPhase.PREPARE.value, TaskPhase.EXECUTE.value),  # 准备 -> 执行
    (TaskPhase.EXECUTE.value, TaskPhase.EVALUATE.value),  # 执行 -> 评估
    (TaskPhase.EVALUATE.value, TaskPhase.EXECUTE.value),  # 评估 -> 执行（重试）
}


class TaskPhaseController:
    """
    任务阶段控制器

    核心职责：
    1. 管理任务的三阶段生命周期
    2. 强制触发准备和评估阶段
    3. 管理阶段产物
    4. 控制阶段转换
    """

    def __init__(self, session: AsyncSession):
        """
        初始化阶段控制器

        Args:
            session: 数据库会话
        """
        self.session = session

    # ========================================================================
    # 静态方法
    # ========================================================================

    @staticmethod
    def is_valid_transition(from_phase: str | None, to_phase: str) -> bool:
        """
        检查阶段转换是否有效

        Args:
            from_phase: 源阶段
            to_phase: 目标阶段

        Returns:
            是否有效
        """
        return (from_phase, to_phase) in VALID_TRANSITIONS

    # ========================================================================
    # 任务启动
    # ========================================================================

    async def start_task(self, task_id: str) -> dict[str, Any]:
        """
        启动任务，强制进入准备阶段

        Args:
            task_id: 任务 ID

        Returns:
            启动结果

        Raises:
            NotFoundException: 任务不存在
        """
        task = await self._get_task(task_id)
        if not task:
            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        now = datetime.now()

        # 初始化阶段状态
        phase_status = {
            TaskPhase.PREPARE.value: {
                "status": PhaseStatus.RUNNING.value,
                "start_time": now.isoformat(),
            },
            TaskPhase.EXECUTE.value: {
                "status": PhaseStatus.PENDING.value,
            },
            TaskPhase.EVALUATE.value: {
                "status": PhaseStatus.PENDING.value,
            },
        }

        # 更新任务
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="in_progress",
                current_phase=TaskPhase.PREPARE.value,
                phase_status=phase_status,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(f"任务 {task_id} 已启动，进入准备阶段")

        # 推送 WebSocket 事件
        event_service = get_event_service()
        if event_service and task.user_id:
            await event_service.send_task_phase_changed(
                user_id=task.user_id,
                taskId=task_id,
                phase=TaskPhase.PREPARE.value,
                status=PhaseStatus.RUNNING.value,
                timestamp=now,
            )

        return {
            "task_id": task_id,
            "current_phase": TaskPhase.PREPARE.value,
            "phase_status": PhaseStatus.RUNNING.value,
            "started_at": now.isoformat(),
        }

    # ========================================================================
    # 准备阶段
    # ========================================================================

    async def force_prepare_phase(self, task_id: str) -> dict[str, Any]:
        """
        强制触发准备阶段

        Args:
            task_id: 任务 ID

        Returns:
            触发结果

        Raises:
            NotFoundException: 任务不存在
        """
        task = await self._get_task(task_id)
        if not task:
            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        now = datetime.now()
        phase_status = task.phase_status or {}

        # 更新准备阶段状态
        phase_status[TaskPhase.PREPARE.value] = {
            "status": PhaseStatus.RUNNING.value,
            "start_time": now.isoformat(),
        }

        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                current_phase=TaskPhase.PREPARE.value,
                phase_status=phase_status,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(f"任务 {task_id} 强制进入准备阶段")

        return {
            "task_id": task_id,
            "phase": TaskPhase.PREPARE.value,
            "status": PhaseStatus.RUNNING.value,
            "triggered_at": now.isoformat(),
        }

    async def complete_prepare_phase(
        self,
        task_id: str,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        """
        完成准备阶段，进入执行阶段

        Args:
            task_id: 任务 ID
            output: 准备阶段产物（调研报告、执行计划等）

        Returns:
            完成结果

        Raises:
            NotFoundException: 任务不存在
            BusinessRuleException: 当前阶段不是准备阶段
        """
        task = await self._get_task(task_id)
        if not task:
            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        from src.core.exceptions import BusinessRuleException

        # 检查当前阶段
        if task.current_phase != TaskPhase.PREPARE.value:
            raise BusinessRuleException(
                message=f"当前阶段不是准备阶段: {task.current_phase}",
                rule="phase_transition",
                details={
                    "current_phase": task.current_phase,
                    "expected_phase": TaskPhase.PREPARE.value,
                },
                code="TASK_004",
            )

        now = datetime.now()
        phase_status = task.phase_status or {}

        # 完成准备阶段
        phase_status[TaskPhase.PREPARE.value] = {
            **phase_status.get(TaskPhase.PREPARE.value, {}),
            "status": PhaseStatus.COMPLETED.value,
            "end_time": now.isoformat(),
            "output": output,
        }

        # 开始执行阶段
        phase_status[TaskPhase.EXECUTE.value] = {
            "status": PhaseStatus.RUNNING.value,
            "start_time": now.isoformat(),
        }

        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                current_phase=TaskPhase.EXECUTE.value,
                phase_status=phase_status,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(f"任务 {task_id} 准备阶段完成，进入执行阶段")

        # 推送 WebSocket 事件
        event_service = get_event_service()
        if event_service and task.user_id:
            await event_service.send_task_phase_changed(
                user_id=task.user_id,
                taskId=task_id,
                phase=TaskPhase.EXECUTE.value,
                status=PhaseStatus.RUNNING.value,
                timestamp=now,
            )

        return {
            "task_id": task_id,
            "current_phase": TaskPhase.EXECUTE.value,
            "prepare_output": output,
            "completed_at": now.isoformat(),
        }

    # ========================================================================
    # 执行阶段
    # ========================================================================

    async def complete_execute_phase(self, task_id: str) -> dict[str, Any]:
        """
        完成执行阶段，强制进入评估阶段

        Args:
            task_id: 任务 ID

        Returns:
            完成结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        # 检查当前阶段
        if task.current_phase != TaskPhase.EXECUTE.value:
            return {
                "error": f"当前阶段不是执行阶段: {task.current_phase}",
                "error_code": "INVALID_PHASE",
            }

        now = datetime.now()
        phase_status = task.phase_status or {}

        # 完成执行阶段
        phase_status[TaskPhase.EXECUTE.value] = {
            **phase_status.get(TaskPhase.EXECUTE.value, {}),
            "status": PhaseStatus.COMPLETED.value,
            "end_time": now.isoformat(),
        }

        # 强制开始评估阶段
        phase_status[TaskPhase.EVALUATE.value] = {
            "status": PhaseStatus.RUNNING.value,
            "start_time": now.isoformat(),
        }

        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                current_phase=TaskPhase.EVALUATE.value,
                phase_status=phase_status,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(f"任务 {task_id} 执行阶段完成，强制进入评估阶段")

        # 推送 WebSocket 事件
        event_service = get_event_service()
        if event_service and task.user_id:
            await event_service.send_task_phase_changed(
                user_id=task.user_id,
                taskId=task_id,
                phase=TaskPhase.EVALUATE.value,
                status=PhaseStatus.RUNNING.value,
                timestamp=now,
            )

        return {
            "task_id": task_id,
            "current_phase": TaskPhase.EVALUATE.value,
            "phase_status": PhaseStatus.RUNNING.value,
            "completed_at": now.isoformat(),
        }

    # ========================================================================
    # 评估阶段
    # ========================================================================

    async def force_evaluate_phase(self, task_id: str) -> dict[str, Any]:
        """
        强制触发评估阶段

        Args:
            task_id: 任务 ID

        Returns:
            触发结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        now = datetime.now()
        phase_status = task.phase_status or {}

        # 更新评估阶段状态
        phase_status[TaskPhase.EVALUATE.value] = {
            "status": PhaseStatus.RUNNING.value,
            "start_time": now.isoformat(),
        }

        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                current_phase=TaskPhase.EVALUATE.value,
                phase_status=phase_status,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(f"任务 {task_id} 强制进入评估阶段")

        return {
            "task_id": task_id,
            "phase": TaskPhase.EVALUATE.value,
            "status": PhaseStatus.RUNNING.value,
            "triggered_at": now.isoformat(),
        }

    async def complete_evaluate_phase(
        self,
        task_id: str,
        eval_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        完成评估阶段

        Args:
            task_id: 任务 ID
            eval_result: 评估结果

        Returns:
            完成结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        # 检查当前阶段
        if task.current_phase != TaskPhase.EVALUATE.value:
            return {
                "error": f"当前阶段不是评估阶段: {task.current_phase}",
                "error_code": "INVALID_PHASE",
            }

        now = datetime.now()
        phase_status = task.phase_status or {}
        all_passed = eval_result.get("all_passed", False)

        # 完成评估阶段
        phase_status[TaskPhase.EVALUATE.value] = {
            **phase_status.get(TaskPhase.EVALUATE.value, {}),
            "status": PhaseStatus.COMPLETED.value,
            "end_time": now.isoformat(),
            "result": eval_result,
        }

        if all_passed:
            # 全部通过，任务完成
            new_status = "completed"
            new_phase = TaskPhase.EVALUATE.value

            logger.info(f"任务 {task_id} 评估通过，任务完成")
        else:
            # 部分失败，检查是否可以重试
            retry_count = getattr(task, "retry_count", 0) or 0
            max_retries = getattr(task, "max_retries", 3) or 3

            if retry_count >= max_retries:
                # 达到最大重试次数，任务阻塞
                new_status = "blocked"
                new_phase = TaskPhase.EVALUATE.value

                logger.warning(f"任务 {task_id} 评估失败，已达最大重试次数，任务阻塞")
            else:
                # 回到执行阶段重试
                new_status = "in_progress"
                new_phase = TaskPhase.EXECUTE.value

                # 重置执行阶段状态
                phase_status[TaskPhase.EXECUTE.value] = {
                    "status": PhaseStatus.RUNNING.value,
                    "start_time": now.isoformat(),
                    "retry_count": retry_count + 1,
                }

                logger.info(
                    f"任务 {task_id} 评估失败，回到执行阶段重试 ({retry_count + 1}/{max_retries})"
                )

        update_data = {
            "status": new_status,
            "current_phase": new_phase,
            "phase_status": phase_status,
            "updated_at": now,
        }

        if new_status == "completed":
            update_data["completed_at"] = now

        if not all_passed:
            update_data["retry_count"] = (getattr(task, "retry_count", 0) or 0) + 1

        await self.session.execute(
            update(Task).where(Task.id == task_id).values(**update_data)
        )
        await self.session.flush()

        # 注意：任务完成事件由 evaluation_service 统一发布，这里不再重复发送

        return {
            "task_id": task_id,
            "evaluation_passed": all_passed,
            "task_status": new_status,
            "current_phase": new_phase,
            "ac_results": eval_result.get("ac_results", []),
            "completed_at": now.isoformat(),
        }

    # ========================================================================
    # 状态查询
    # ========================================================================

    async def get_phase_status(self, task_id: str) -> dict[str, Any]:
        """
        获取任务阶段状态

        Args:
            task_id: 任务 ID

        Returns:
            阶段状态
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        return {
            "task_id": task_id,
            "current_phase": task.current_phase,
            "task_status": task.status,
            "phases": task.phase_status or {},
        }

    async def get_phase_output(
        self,
        task_id: str,
        phase: str,
    ) -> dict[str, Any]:
        """
        获取阶段产物

        Args:
            task_id: 任务 ID
            phase: 阶段名称

        Returns:
            阶段产物
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        phase_status = task.phase_status or {}
        phase_data = phase_status.get(phase, {})

        if phase_data.get("status") != PhaseStatus.COMPLETED.value:
            return {
                "error": f"阶段 {phase} 尚未完成",
                "error_code": "PHASE_NOT_COMPLETED",
            }

        return {
            "task_id": task_id,
            "phase": phase,
            "status": phase_data.get("status"),
            "output": phase_data.get("output", {}),
            "start_time": phase_data.get("start_time"),
            "end_time": phase_data.get("end_time"),
        }

    # ========================================================================
    # 重试机制
    # ========================================================================

    async def retry_execute_phase(self, task_id: str) -> dict[str, Any]:
        """
        评估失败后回到执行阶段重试

        Args:
            task_id: 任务 ID

        Returns:
            重试结果
        """
        task = await self._get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        retry_count = getattr(task, "retry_count", 0) or 0
        max_retries = getattr(task, "max_retries", 3) or 3

        if retry_count >= max_retries:
            # 达到最大重试次数，阻塞任务
            await self.session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status="blocked",
                    updated_at=datetime.now(),
                )
            )
            await self.session.flush()

            logger.warning(f"任务 {task_id} 达到最大重试次数，已阻塞")

            # 推送 WebSocket 事件
            event_service = get_event_service()
            if event_service and task.user_id:
                await event_service.send_task_failed(
                    user_id=task.user_id,
                    taskId=task_id,
                    error="评估失败，已达最大重试次数",
                    retryCount=retry_count,
                )

            return {
                "error": "达到最大重试次数",
                "error_code": "MAX_RETRIES_EXCEEDED",
                "task_status": "blocked",
                "retry_count": retry_count,
            }

        now = datetime.now()
        phase_status = task.phase_status or {}

        # 回到执行阶段
        phase_status[TaskPhase.EXECUTE.value] = {
            "status": PhaseStatus.RUNNING.value,
            "start_time": now.isoformat(),
            "retry_count": retry_count + 1,
        }

        # 标记评估阶段为失败
        if TaskPhase.EVALUATE.value in phase_status:
            phase_status[TaskPhase.EVALUATE.value]["status"] = PhaseStatus.FAILED.value
            phase_status[TaskPhase.EVALUATE.value]["end_time"] = now.isoformat()

        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="in_progress",
                current_phase=TaskPhase.EXECUTE.value,
                retry_count=retry_count + 1,
                phase_status=phase_status,
                updated_at=now,
            )
        )
        await self.session.flush()

        logger.info(
            f"任务 {task_id} 回到执行阶段重试 ({retry_count + 1}/{max_retries})"
        )

        # 推送 WebSocket 事件
        event_service = get_event_service()
        if event_service and task.user_id:
            await event_service.send_task_phase_changed(
                user_id=task.user_id,
                taskId=task_id,
                phase=TaskPhase.EXECUTE.value,
                status=PhaseStatus.RUNNING.value,
                timestamp=now,
            )

        return {
            "task_id": task_id,
            "current_phase": TaskPhase.EXECUTE.value,
            "retry_count": retry_count + 1,
            "max_retries": max_retries,
        }

    # ========================================================================
    # 内部方法
    # ========================================================================

    async def _get_task(self, task_id: str) -> Task | None:
        """获取任务"""
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()
