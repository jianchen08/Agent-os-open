"""
任务状态服务

负责任务状态管理和进度计算，提供统一的状态查询和转换接口。
使用 YAML 存储（与 SimpleStateMachine 一致），不依赖 SQLAlchemy。

核心功能：
1. 任务查询（单个/列表）
2. 状态转换（通过状态机）
3. 进度计算（统一方法）
4. 状态概览统计
5. 事件发布
"""

import contextlib
import logging
from typing import Any

from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.db.models import Task
from src.db.repositories.task_repo import TaskRepository
from src.tasks.services.progress_calculator import get_progress_calculator
from src.tasks.state_machine import TaskStateMachine, get_task_state_machine

logger = logging.getLogger(__name__)


class TaskStateService:
    """
    任务状态服务

    负责任务状态管理和进度计算，提供统一的状态查询和转换接口。

    核心职责：
    1. 任务查询（单个/列表）
    2. 状态转换（通过状态机）
    3. 进度计算（统一方法）
    4. 状态概览统计
    5. 事件发布

    Example:
        >>> service = TaskStateService()
        >>> task = await service.get_task("task-001")
        >>> result = await service.transition_status("task-001", "running")
    """

    def __init__(
        self,
        session: Any = None,
        state_machine: TaskStateMachine | None = None,
    ):
        """
        初始化任务状态服务

        Args:
            session: 数据库会话（兼容参数，不再使用）
            state_machine: 状态机实例（可选，默认使用全局实例）
        """
        self.session = session
        self.state_machine = state_machine or get_task_state_machine()
        self.task_repo = TaskRepository(session)
        self.progress_calculator = get_progress_calculator()

    # ========================================================================
    # 查询方法
    # ========================================================================

    async def get_task(self, task_id: str) -> Task | None:
        """
        获取单个任务

        Args:
            task_id: 任务 ID

        Returns:
            任务对象，不存在则返回 None
        """
        return await self.task_repo.get(task_id)

    async def get_task_with_details(self, task_id: str) -> dict[str, Any] | None:
        """
        获取任务详情（包含计算后的进度信息）

        Args:
            task_id: 任务 ID

        Returns:
            任务详情字典，不存在则返回 None
        """
        task = await self.get_task(task_id)
        if not task:
            return None

        # 使用进度计算器计算进度
        progress = self.progress_calculator.calculate(task.acceptance_criteria or [])

        return {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "parent_task_id": task.parent_task_id,
            "goal": task.goal,
            "evaluation_metric_ids": task.evaluation_metric_ids or [],
            **progress.to_dict(),
            "target_type": task.target_type,
            "target_id": task.target_id,
            "target_name": task.target_name,
            "priority": task.priority,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "metadata": task.task_metadata or {},
            "tags": task.tags or [],
        }

    async def list_tasks(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """
        列出任务列表

        Args:
            filters: 过滤条件，支持：
                - status: 任务状态
                - parent_task_id: 父任务 ID
                - user_id: 用户 ID
                - session_id: 会话 ID
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            任务列表
        """
        filters = filters or {}
        all_tasks = await self.task_repo.list_all(limit=10000)

        # 应用过滤条件
        result = all_tasks
        if "status" in filters:
            result = [t for t in result if t.status == filters["status"]]
        if "parent_task_id" in filters:
            result = [t for t in result if t.parent_task_id == filters["parent_task_id"]]
        if "user_id" in filters:
            result = [t for t in result if getattr(t, "user_id", None) == filters["user_id"]]
        if "session_id" in filters:
            result = [t for t in result if getattr(t, "session_id", None) == filters["session_id"]]

        # 排序和分页
        result = sorted(result, key=lambda t: getattr(t, "created_at", ""), reverse=True)
        return result[offset : offset + limit]

    async def get_status_overview(
        self,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        获取任务状态概览

        Args:
            filters: 过滤条件

        Returns:
            状态概览数据，包含：
            - total_tasks: 总任务数
            - status_counts: 各状态数量
            - evaluation_summary: 评估进度汇总
            - recent_tasks: 最近任务列表
        """
        filters = filters or {}
        all_tasks = await self.task_repo.list_all(limit=10000)

        # 应用过滤条件
        if "project_id" in filters:
            all_tasks = [
                t for t in all_tasks
                if (t.task_metadata or {}).get("project_id") == filters["project_id"]
            ]
        if "task_scope" in filters and filters["task_scope"] != "all":
            all_tasks = [
                t for t in all_tasks
                if (t.task_metadata or {}).get("task_scope") == filters["task_scope"]
            ]

        # 统计信息
        status_counts: dict[str, int] = {}
        total_criteria = 0
        passed_criteria = 0
        failed_criteria = 0

        for task in all_tasks:
            status = task.status
            status_counts[status] = status_counts.get(status, 0) + 1

            # 累计评估进度
            metadata = task.task_metadata or {}
            total_criteria += metadata.get("total_criteria", 0)
            passed_criteria += metadata.get("passed_criteria", 0)
            failed_criteria += metadata.get("failed_criteria", 0)

        # 计算总体进度
        progress_percent = (
            (passed_criteria / total_criteria * 100) if total_criteria > 0 else 0
        )

        # 最近任务
        recent_tasks = [
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "task_scope": (
                    task.task_metadata.get("task_scope") if task.task_metadata else None
                ),
                "total_criteria": (task.task_metadata or {}).get("total_criteria", 0),
                "passed_criteria": (task.task_metadata or {}).get("passed_criteria", 0),
                "failed_criteria": (task.task_metadata or {}).get("failed_criteria", 0),
                "progress_percent": round(
                    (task.task_metadata or {}).get("progress_percent", 0.0), 2
                ),
                "created_at": task.created_at.isoformat(),
                "updated_at": (
                    task.updated_at.isoformat() if task.updated_at else None
                ),
            }
            for task in all_tasks[:10]
        ]

        return {
            "total_tasks": len(all_tasks),
            "status_counts": status_counts,
            "evaluation_summary": {
                "total_criteria": total_criteria,
                "passed_criteria": passed_criteria,
                "failed_criteria": failed_criteria,
                "progress_percent": round(progress_percent, 2),
            },
            "recent_tasks": recent_tasks,
        }

    # ========================================================================
    # 状态转换
    # ========================================================================

    async def transition_status(
        self,
        task_id: str,
        to_status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """
        执行状态转换

        Args:
            task_id: 任务 ID
            to_status: 目标状态
            reason: 转换原因

        Returns:
            转换结果，包含：
            - task_id: 任务 ID
            - old_status: 原状态
            - new_status: 新状态
            - success: 是否成功
            - error: 错误信息（如果失败）
        """
        task = await self.get_task(task_id)
        if not task:
            return {
                "task_id": task_id,
                "success": False,
                "error": "任务不存在",
                "error_code": "TASK_NOT_FOUND",
            }

        old_status = task.status

        try:
            # 使用状态机执行转换
            self.state_machine.transition(to_status)
            task.status = to_status
            await self.task_repo.update(task_id, status=to_status)

            # 发布状态变更事件
            await self.publish_status_change(
                task_id=task_id,
                old_status=old_status,
                new_status=to_status,
            )

            return {
                "task_id": task_id,
                "old_status": old_status,
                "new_status": to_status,
                "success": True,
            }

        except ValueError as e:
            return {
                "task_id": task_id,
                "old_status": old_status,
                "new_status": to_status,
                "success": False,
                "error": str(e),
                "error_code": "INVALID_TRANSITION",
            }

    # ========================================================================
    # 事件发布
    # ========================================================================

    async def publish_status_change(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        """
        发布状态变更事件

        Args:
            task_id: 任务 ID
            old_status: 原状态
            new_status: 新状态
            extra_data: 额外数据
        """
        event_data = {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "source": "task_state_service",
            **(extra_data or {}),
        }

        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.STATE_CHANGE,
                session_id=task_id,
                data=event_data,
            )
        )

        logger.info(
            f"[TaskStateService] 状态变更事件已发布 | "
            f"task_id={task_id} | {old_status} -> {new_status}"
        )

    # ========================================================================
    # 辅助方法
    # ========================================================================

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        """
        获取子任务列表

        Args:
            parent_task_id: 父任务 ID

        Returns:
            子任务列表
        """
        return await self.task_repo.get_by_parent(parent_task_id)

    async def count_by_status(self, status: str) -> int:
        """
        统计指定状态的任务数量

        Args:
            status: 任务状态

        Returns:
            任务数量
        """
        tasks = await self.task_repo.get_by_status(status)
        return len(tasks)

    # ========================================================================
    # 评估结果应用
    # ========================================================================

    async def apply_evaluation_result(
        self,
        task_id: str,
        evaluation_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        应用评估结果并更新状态

        封装：
        1. 进度计算
        2. 状态判断
        3. 状态转换（通过状态机）
        4. 数据持久化

        Args:
            task_id: 任务 ID
            evaluation_result: 评估结果

        Returns:
            应用结果
        """
        task = await self.get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        acceptance_criteria = evaluation_result.get("acceptance_criteria", [])
        progress = evaluation_result.get("progress", {})
        retry_info = evaluation_result.get("retry_info", {})
        new_status = evaluation_result.get("new_status")

        # 更新任务的验收标准和进度信息
        task.acceptance_criteria = acceptance_criteria
        task.total_criteria = progress.get("total_criteria", 0)
        task.passed_criteria = progress.get("passed_criteria", 0)
        task.failed_criteria = progress.get("failed_criteria", 0)
        task.progress_percent = progress.get("progress_percent", 0.0)
        task.best_passed_count = retry_info.get("best_passed_count", 0)
        task.last_passed_count = retry_info.get("last_passed_count", 0)
        task.retry_count = retry_info.get("retry_count", 0)

        # 通过状态机执行状态转换
        old_status = task.status
        if new_status and old_status != new_status:
            with contextlib.suppress(ValueError):
                self.state_machine.transition(new_status)

        # 持久化更新
        await self.task_repo.update(
            task_id,
            task_metadata=task.task_metadata,
            status=new_status or old_status,
        )

        # 发布状态变更事件
        await self.publish_status_change(
            task_id=task_id,
            old_status=old_status,
            new_status=new_status,
            extra_data={"progress": progress},
        )

        return {
            "task_id": task_id,
            "task_status": new_status,
            "progress": progress,
            "all_passed": progress.get("passed_criteria") == progress.get("total_criteria"),
            "has_progress": retry_info.get("has_progress", False),
            "retry_count": retry_info.get("retry_count", 0),
        }

    async def update_progress(
        self,
        task_id: str,
        acceptance_criteria: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        更新任务进度

        Args:
            task_id: 任务 ID
            acceptance_criteria: 验收标准列表

        Returns:
            进度信息
        """
        task = await self.get_task(task_id)
        if not task:
            return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

        progress = self.progress_calculator.calculate(acceptance_criteria)

        task.acceptance_criteria = acceptance_criteria
        task.total_criteria = progress.total_criteria
        task.passed_criteria = progress.passed_criteria
        task.failed_criteria = progress.failed_criteria
        task.progress_percent = progress.progress_percent

        await self.task_repo.update(
            task_id,
            task_metadata=task.task_metadata,
        )

        return progress.to_dict()
