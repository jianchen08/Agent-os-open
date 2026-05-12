"""
任务查询服务

提供任务查询相关的操作，包括：
- 任务列表查询
- 任务详情查询
- 评估状态查询
- 任务数据转换
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Task
from src.db.repositories.task_repo import TaskRepository
from src.evaluation.metric_loader import get_metric_loader
from src.services.task_service import task_to_dict
from src.tasks.services.progress_calculator import get_progress_calculator

logger = logging.getLogger(__name__)


class TaskQueryService:
    """
    任务查询服务

    负责所有任务查询相关的操作，包括：
    - 任务列表查询
    - 任务详情查询
    - 评估状态查询
    - 任务数据转换和格式化
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务查询服务

        Args:
            session: 数据库会话（由调用者管理生命周期）
        """
        self.session = session
        self.task_repo = TaskRepository(session)
        self.metric_loader = get_metric_loader()
        self.progress_calculator = get_progress_calculator()

    async def list_tasks(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 100,
        include_subtasks: bool = True,
        root_only: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取任务列表

        Args:
            user_id: 用户 ID
            skip: 跳过数量
            limit: 限制数量
            include_subtasks: 是否包含子任务
            root_only: 是否只返回根任务
            filters: 过滤条件

        Returns:
            任务字典列表
        """
        session_id = filters.get("session_id") if filters else None
        status = filters.get("status") if filters else None

        # 如果提供了 session_id，使用 get_root_tasks 方法
        if session_id:
            tasks = await self.task_repo.get_root_tasks(
                user_id=user_id,
                session_id=session_id,
                status=status,
                limit=limit,
            )
        else:
            tasks = await self.task_repo.get_tasks_by_user(
                user_id=user_id,
                status=status,
                limit=limit,
            )

        # 过滤根任务
        if root_only:
            tasks = [t for t in tasks if t.parent_task_id is None]

        # 构建任务字典
        task_dict = {task.id: task for task in tasks}

        # 如果需要包含子任务，递归构建树结构
        if include_subtasks:
            return [
                await self._task_to_dict_with_subtasks(task, task_dict)
                for task in tasks
            ]

        return [await self._task_to_dict(task) for task in tasks]

    async def get_task(
        self,
        task_id: str,
        user_id: str,
        include_metrics: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        获取任务详情

        Args:
            task_id: 任务 ID
            user_id: 用户 ID
            include_metrics: 是否包含评估指标详情
            session_id: 会话 ID（可选）

        Returns:
            任务详情字典，不存在或无权访问返回 None
        """
        task = await self.task_repo.get(task_id)

        # 验证用户权限
        if not task or task.user_id != str(user_id):
            return None

        # 验证会话权限
        if session_id is not None and task.session_id != session_id:
            return None

        # 使用仓储方法获取任务及指标
        task_data = await self.task_repo.get_task_with_metrics(task_id)

        if not task_data:
            return None

        # 转换为返回格式
        result = task_data["task"].copy()

        # 添加评估指标详情
        if include_metrics and task_data["task"].get("evaluation_metric_ids"):
            result["evaluation_metrics"] = await self._get_metrics_details(
                task_data["task"]["evaluation_metric_ids"]
            )

        result["task_metrics"] = []

        # 获取子任务
        subtasks = await self.task_repo.get_subtasks(task_id)
        result["subtasks"] = [await self._task_to_dict(st) for st in subtasks]

        return result

    async def get_evaluation_status(self, task_id: str, user_id: str) -> dict[str, Any]:
        """
        查询任务评估状态

        Args:
            task_id: 任务 ID
            user_id: 用户 ID

        Returns:
            评估状态字典
        """
        task = await self.get_task(task_id, user_id)
        if not task:
            from src.core.exceptions import NotFoundException

            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        # 从数据库获取任务对象以获取 acceptance_criteria
        task_obj = await self.task_repo.get(task_id)
        if task_obj and task_obj.acceptance_criteria:
            # 使用进度计算器计算进度
            progress = self.progress_calculator.calculate(task_obj.acceptance_criteria)
            total = progress.total_criteria
            passed = progress.passed_criteria
            failed = progress.failed_criteria
            pending = progress.pending_criteria
            progress_percent = progress.progress_percent
        else:
            # 无 acceptance_criteria 时从 task_metadata 获取
            task_metadata = task.get("metadata", {}) or {}
            total = task_metadata.get("total_criteria", 0)
            passed = task_metadata.get("passed_criteria", 0)
            failed = task_metadata.get("failed_criteria", 0)
            pending = total - passed - failed
            progress_percent = (passed / total * 100) if total > 0 else 0.0

        skipped = 0

        # 构建指标状态列表
        metrics = []
        for metric in task.get("evaluation_metrics", []):
            metrics.append(
                {
                    "metric_id": metric["id"],
                    "status": "pending",
                    "score": None,
                    "feedback": None,
                    "evaluated_at": None,
                }
            )

        return {
            "task_id": task_id,
            "total_metrics": total,
            "pending_metrics": pending,
            "passed_metrics": passed,
            "failed_metrics": failed,
            "skipped_metrics": skipped,
            "progress_percent": round(progress_percent, 2),
            "metrics": metrics,
        }

    async def _task_to_dict(self, task: Task) -> dict[str, Any]:
        """
        将任务对象转换为字典

        Args:
            task: 任务对象

        Returns:
            任务字典
        """
        metrics_details = await self._get_metrics_details(task.evaluation_metric_ids)
        return task_to_dict(task, metrics_details)

    async def _task_to_dict_with_subtasks(
        self, task: Task, task_dict: dict[str, Task]
    ) -> dict[str, Any]:
        """
        将任务对象转换为字典，包含子任务

        Args:
            task: 任务对象
            task_dict: 任务字典（用于查找子任务）

        Returns:
            包含子任务的任务字典
        """
        result = await self._task_to_dict(task)

        # 查找子任务
        subtasks = []
        for t in task_dict.values():
            if t.parent_task_id == task.id:
                subtasks.append(await self._task_to_dict_with_subtasks(t, task_dict))

        result["subtasks"] = subtasks
        return result

    async def _get_metrics_details(
        self, metric_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        """
        获取评估指标详情

        Args:
            metric_ids: 指标 ID 列表

        Returns:
            指标详情列表
        """
        if not metric_ids:
            return []

        metrics = await self.metric_loader.get_metrics_by_ids(metric_ids)
        return [
            {
                "id": m.get("id"),
                "name": m.get("name", ""),
                "description": m.get("description", ""),
                "category": m.get("category", ""),
                "evaluator_type": m.get("evaluator_type", "tool"),
                "evaluator_id": m.get("evaluator_id", ""),
                "is_red_line": m.get("is_red_line", False),
                "default_weight": m.get("default_weight", 1.0),
            }
            for m in metrics
        ]
