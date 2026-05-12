"""
执行状态监控器

提供任务执行状态的实时监控功能。

注意：Project 相关功能已废弃，系统已迁移到独立的 Task 架构。
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundException
from src.db.models import Task

logger = logging.getLogger(__name__)


class ExecutionMonitor:
    """
    执行状态监控器

    提供任务执行状态的监控、统计和报告功能。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化执行监控器

        Args:
            session: 数据库会话
        """
        self.session = session

    async def get_task_execution_status(self, task_id: str) -> dict[str, Any]:
        """
        获取任务执行状态

        Args:
            task_id: 任务 ID

        Returns:
            任务执行状态信息

        Raises:
            NotFoundException: 任务不存在
        """
        # 获取任务信息
        task_result = await self.session.execute(select(Task).where(Task.id == task_id))
        task = task_result.scalar_one_or_none()

        if not task:
            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
            )

        # 计算执行时间
        start_time = task.created_at
        current_time = datetime.now()
        execution_duration = (
            (current_time - start_time).total_seconds() if start_time else 0
        )

        # 计算空闲时间
        last_update = task.updated_at or task.created_at
        idle_duration = (
            (current_time - last_update).total_seconds() if last_update else 0
        )

        # 获取验收标准状态（兼容旧字段，已废弃）
        acceptance_criteria = task.acceptance_criteria or []
        total_criteria = len(acceptance_criteria)
        passed_criteria = sum(
            1 for ac in acceptance_criteria if ac.get("status") == "passed"
        )
        failed_criteria = sum(
            1 for ac in acceptance_criteria if ac.get("status") == "failed"
        )
        pending_criteria = total_criteria - passed_criteria - failed_criteria

        # 计算任务进度
        task_progress = (
            (passed_criteria / total_criteria * 100) if total_criteria > 0 else 0
        )

        return {
            "task_id": task_id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "progress": {
                "total_criteria": total_criteria,
                "passed_criteria": passed_criteria,
                "failed_criteria": failed_criteria,
                "pending_criteria": pending_criteria,
                "progress_percent": round(task_progress, 2),
            },
            "timing": {
                "execution_duration": round(execution_duration, 2),
                "idle_duration": round(idle_duration, 2),
                "is_idle": idle_duration > 300,  # 5分钟空闲阈值
            },
            "retry_info": {
                "retry_count": task.retry_count or 0,
                "max_retries": task.max_retries or 3,
            },
            "metadata": task.task_metadata or {},
            "error_message": task.error_message,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }

    async def get_execution_statistics(
        self, user_id: str | None = None, time_range: int | None = None
    ) -> dict[str, Any]:
        """
        获取执行统计信息

        Args:
            user_id: 用户 ID（可选，用于过滤）
            time_range: 时间范围（秒，可选）

        Returns:
            执行统计信息
        """
        # 构建查询条件
        task_conditions = []

        if user_id:
            task_conditions.append(Task.user_id == user_id)

        if time_range:
            cutoff_time = datetime.now() - timedelta(seconds=time_range)
            task_conditions.append(Task.created_at >= cutoff_time)

        # 任务统计
        task_query = select(
            func.count(Task.id).label("total"),
            func.sum(func.case((Task.status == "completed", 1), else_=0)).label(
                "completed"
            ),
            func.sum(func.case((Task.status == "running", 1), else_=0)).label(
                "running"
            ),
            func.sum(func.case((Task.status == "pending", 1), else_=0)).label(
                "pending"
            ),
            func.sum(func.case((Task.status == "failed", 1), else_=0)).label("failed"),
            func.sum(func.case((Task.status == "blocked", 1), else_=0)).label(
                "blocked"
            ),
            func.avg(
                func.case(
                    (
                        Task.status == "completed",
                        func.extract("epoch", Task.updated_at - Task.created_at),
                    ),
                    else_=None,
                )
            ).label("avg_completion_time"),
        )

        if task_conditions:
            task_query = task_query.where(and_(*task_conditions))

        task_stats_result = await self.session.execute(task_query)
        task_stats = task_stats_result.first()

        return {
            "tasks": {
                "total": task_stats.total or 0,
                "completed": task_stats.completed or 0,
                "running": task_stats.running or 0,
                "pending": task_stats.pending or 0,
                "failed": task_stats.failed or 0,
                "blocked": task_stats.blocked or 0,
                "avg_completion_time": round(task_stats.avg_completion_time or 0, 2),
            },
            "generated_at": datetime.now().isoformat(),
            "time_range": time_range,
            "user_id": user_id,
        }

    async def get_active_executions(self) -> list[dict[str, Any]]:
        """
        获取当前活跃的执行任务

        Returns:
            活跃执行任务列表
        """
        # 查询进行中的任务
        query = (
            select(Task)
            .where(Task.status.in_(["running", "pending"]))
            .order_by(Task.updated_at.desc())
        )

        result = await self.session.execute(query)
        active_tasks = result.scalars().all()

        executions = []
        for task in active_tasks:
            # 计算空闲时间
            last_update = task.updated_at or task.created_at
            idle_duration = (
                (datetime.now() - last_update).total_seconds() if last_update else 0
            )

            executions.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "idle_duration": round(idle_duration, 2),
                    "is_idle": idle_duration > 300,
                    "retry_count": task.retry_count or 0,
                    "updated_at": (
                        task.updated_at.isoformat() if task.updated_at else None
                    ),
                }
            )

        return executions

    async def check_execution_health(self) -> dict[str, Any]:
        """
        检查执行健康状态

        Returns:
            执行健康状态报告
        """
        # 获取活跃执行
        active_executions = await self.get_active_executions()

        # 统计健康状态
        total_active = len(active_executions)
        idle_tasks = [e for e in active_executions if e["is_idle"]]
        stuck_tasks = [
            e for e in active_executions if e["idle_duration"] > 600
        ]  # 10分钟

        return {
            "overall_health": (
                "healthy"
                if len(stuck_tasks) == 0
                else "warning"
                if len(stuck_tasks) < 3
                else "critical"
            ),
            "active_executions": {
                "total": total_active,
                "idle": len(idle_tasks),
                "stuck": len(stuck_tasks),
            },
            "issues": [
                {
                    "type": "stuck_task",
                    "task_id": task["task_id"],
                    "idle_duration": task["idle_duration"],
                }
                for task in stuck_tasks
            ],
            "checked_at": datetime.now().isoformat(),
        }
