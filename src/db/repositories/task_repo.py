"""
任务仓储

提供 Task 的 CRUD 操作和查询功能
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Task
from src.db.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class TaskRepository(BaseRepository[Task]):
    """任务仓储"""

    def __init__(self, session: AsyncSession):
        super().__init__(session, Task)

    async def get(self, task_id: str) -> Task | None:
        """
        获取任务（确保属性已加载，避免懒加载问题）

        Args:
            task_id: 任务 ID

        Returns:
            任务对象，不存在则返回 None
        """
        result = await self.session.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task:
            _ = task.status
        return task

    async def create_task(self, task_data: dict[str, Any]) -> Task:
        """
        创建任务（符合设计文档规范）

        幂等性保证：如果任务 ID 已存在，直接返回已有任务而不是抛出异常。
        这可以防止 LLM 重复调用 task_submit 工具时导致的 UNIQUE constraint 错误。

        Args:
            task_data: 任务数据字典

        Returns:
            创建的任务对象（或已存在的任务）
        """
        task_id = task_data.get("id", str(uuid.uuid4()))

        existing_task = await self.get(task_id)
        if existing_task:
            logger.info(
                f"[TaskRepository] 任务已存在，返回已有任务（幂等性保护） | task_id={task_id}"
            )
            return existing_task

        # 构建任务对象（只使用设计文档中定义的字段）
        # 注意：acceptance_criteria、total_criteria、passed_criteria、failed_criteria、progress_percent
        # 字段已从 Task 模型中移除，使用 evaluation_metric_ids 替代
        evaluation_metric_ids = task_data.get("evaluation_metric_ids", [])

        # 将进度统计存储在 task_metadata 中
        task_metadata = task_data.get("task_metadata", {}) or {}
        task_metadata["total_criteria"] = task_metadata.get("total_criteria", len(evaluation_metric_ids))
        task_metadata["passed_criteria"] = task_metadata.get("passed_criteria", 0)
        task_metadata["failed_criteria"] = task_metadata.get("failed_criteria", 0)
        task_metadata["progress_percent"] = task_metadata.get("progress_percent", 0.0)

        task = Task(
            id=task_id,
            # 层级关系
            parent_task_id=task_data.get("parent_task_id"),
            execution_record_id=task_data.get("execution_record_id"),
            # 关联
            user_id=task_data.get("user_id"),
            session_id=task_data.get("session_id"),
            # 定义
            title=task_data.get("title", ""),
            description=task_data.get("description"),
            goal=task_data.get("goal"),
            # 执行配置
            target_type=task_data.get("target_type"),
            target_id=task_data.get("target_id"),
            target_name=task_data.get("target_name"),
            priority=task_data.get("priority", 5),
            due_date=task_data.get("due_date"),
            retry_count=task_data.get("retry_count", 0),
            max_retries=task_data.get("max_retries", 3),
            # 评估指标引用
            evaluation_metric_ids=evaluation_metric_ids,
            # 状态
            status=task_data.get("status", "pending"),
            # 时间
            started_at=task_data.get("started_at"),
            completed_at=task_data.get("completed_at"),
            # 元数据（包含错误信息、进度统计等）
            task_metadata=task_metadata,
            # 标签（用于分类和检索）
            tags=task_data.get("tags", []),
        )

        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)

        return task

    async def get_task_with_metrics(self, task_id: str) -> dict[str, Any] | None:
        """
        获取任务及其关联的指标（符合设计文档规范）

        注意：当前实现中没有独立的 task_metrics 关联表，
        任务通过 evaluation_metric_ids JSON 数组字段引用评估指标。

        Args:
            task_id: 任务 ID

        Returns:
            包含任务信息的字典，如果没有找到则返回 None
        """
        query = select(Task).where(Task.id == task_id)

        result = await self.session.execute(query)
        task = result.scalar_one_or_none()

        if task is None:
            return None

        # 构建返回数据（只包含设计文档中定义的字段）
        return {
            "task": {
                # 核心标识
                "id": task.id,
                # 层级关系
                "parent_task_id": task.parent_task_id,
                "execution_record_id": task.execution_record_id,
                # 关联
                "user_id": task.user_id,
                "session_id": task.session_id,
                # 定义
                "title": task.title,
                "description": task.description,
                "goal": task.goal,
                # 执行配置
                "target_type": task.target_type,
                "target_id": task.target_id,
                "target_name": task.target_name,
                "priority": task.priority,
                "due_date": task.due_date.isoformat() if task.due_date else None,
                "retry_count": task.retry_count,
                "max_retries": task.max_retries,
                # 评估指标引用
                "evaluation_metric_ids": task.evaluation_metric_ids or [],
                # 状态
                "status": task.status,
                # 时间
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "completed_at": (
                    task.completed_at.isoformat() if task.completed_at else None
                ),
                "created_at": task.created_at.isoformat() if task.created_at else None,
                # 元数据
                "metadata": task.task_metadata,
                # 标签
                "tags": task.tags or [],
            },
            # 注意：metrics 需要通过 evaluation_metric_ids 单独查询
            "metrics": [],
        }

    async def get_root_tasks(
        self,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """
        获取根任务列表（即项目，parent_task_id 为 None 的任务）

        Args:
            user_id: 用户 ID（可选）
            session_id: 会话 ID（可选）
            status: 任务状态（可选）
            limit: 返回数量限制

        Returns:
            根任务列表
        """
        query = select(Task).where(Task.parent_task_id.is_(None))

        if user_id:
            query = query.where(Task.user_id == str(user_id))
        if session_id:
            query = query.where(Task.session_id == session_id)
        if status:
            query = query.where(Task.status == status)

        query = query.order_by(Task.created_at.desc()).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_project_tasks(self, project_id: str) -> list[Task]:
        """
        获取项目的所有任务（已废弃，使用 get_root_tasks 和 get_subtasks 替代）

        Args:
            project_id: 项目 ID（已废弃，保留用于向后兼容）

        Returns:
            空列表（此方法已废弃）
        """
        # 此方法已废弃，项目现在通过 parent_task_id is None 查询
        return []

    async def get_tasks_by_status(
        self, status: str, session_id: str | None = None, limit: int = 100
    ) -> list[Task]:
        """
        按状态查询任务

        Args:
            status: 任务状态
            session_id: 会话 ID（可选）
            limit: 返回数量限制

        Returns:
            任务列表
        """
        query = select(Task).where(Task.status == status)

        if session_id:
            query = query.where(Task.session_id == session_id)

        query = query.order_by(Task.created_at.desc()).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_tasks_by_user(
        self, user_id: str, status: str | None = None, limit: int = 100
    ) -> list[Task]:
        """
        获取用户的任务列表

        Args:
            user_id: 用户 ID
            status: 任务状态（可选）
            limit: 返回数量限制

        Returns:
            任务列表
        """
        query = select(Task).where(Task.user_id == str(user_id))

        if status:
            query = query.where(Task.status == status)

        query = query.order_by(Task.created_at.desc()).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        """
        获取子任务列表

        Args:
            parent_task_id: 父任务 ID

        Returns:
            子任务列表
        """
        query = (
            select(Task)
            .where(Task.parent_task_id == parent_task_id)
            .order_by(Task.created_at.asc())
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_task_status(
        self, task_id: str, status: str, error_message: str | None = None
    ) -> bool:
        """
        更新任务状态（符合设计文档规范）

        Args:
            task_id: 任务 ID
            status: 新状态
            error_message: 错误信息（可选，将存储在 metadata 中）

        Returns:
            是否更新成功
        """
        values: dict[str, Any] = {"status": status}

        # 根据 status 设置时间字段
        if status == "running" and not error_message:
            values["started_at"] = datetime.now(UTC)
        elif status in ("completed", "failed"):
            values["completed_at"] = datetime.now(UTC)

        # 如果有错误信息，更新 task_metadata
        if error_message:
            # 先获取当前 task
            task = await self.get(task_id)
            if task:
                task_metadata = task.task_metadata or {}
                task_metadata["error_message"] = error_message
                values["task_metadata"] = task_metadata

        query = update(Task).where(Task.id == task_id).values(**values)

        result = await self.session.execute(query)
        await self.session.flush()

        # SQLAlchemy 2.0 返回 CursorResult，需要检查 rowcount
        return bool(getattr(result, "rowcount", 0) > 0)

    async def increment_retry_count(self, task_id: str) -> int:
        """
        增加重试计数（符合设计文档规范）

        Args:
            task_id: 任务 ID

        Returns:
            更新后的重试次数
        """
        # 先获取当前计数
        task = await self.get(task_id)
        if not task:
            return 0

        new_count = task.retry_count + 1

        query = update(Task).where(Task.id == task_id).values(retry_count=new_count)

        await self.session.execute(query)
        await self.session.flush()
        return new_count

    async def get_tasks_by_execution_record(
        self, execution_record_id: str
    ) -> list[Task]:
        """
        获取关联到指定执行记录的任务

        Args:
            execution_record_id: 执行记录 ID

        Returns:
            任务列表
        """
        query = (
            select(Task)
            .where(Task.execution_record_id == execution_record_id)
            .order_by(Task.created_at.desc())
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_by_status(
        self, session_id: str | None = None, user_id: str | None = None
    ) -> dict[str, int]:
        """
        按状态统计任务数量（符合设计文档规范）

        Args:
            session_id: 会话 ID（可选）
            user_id: 用户 ID（可选）

        Returns:
            状态统计字典
        """
        query = select(Task.status, func.count(Task.id))

        conditions = []
        if session_id:
            conditions.append(Task.session_id == session_id)
        if user_id:
            conditions.append(Task.user_id == str(user_id))

        if conditions:
            query = query.where(and_(*conditions))

        query = query.group_by(Task.status)

        result = await self.session.execute(query)
        return {row[0]: row[1] for row in result}

    async def get_pending_tasks(
        self, limit: int = 50, priority_min: int | None = None
    ) -> list[Task]:
        """
        获取待处理任务（按优先级排序）

        Args:
            limit: 返回数量限制
            priority_min: 最低优先级（可选）

        Returns:
            任务列表
        """
        query = select(Task).where(Task.status == "pending")

        if priority_min is not None:
            query = query.where(Task.priority >= priority_min)

        query = query.order_by(Task.priority.desc(), Task.created_at.asc()).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_overdue_tasks(self, current_time: datetime) -> list[Task]:
        """
        获取过期任务

        Args:
            current_time: 当前时间

        Returns:
            过期任务列表
        """
        query = (
            select(Task)
            .where(
                and_(
                    Task.due_date < current_time,
                    Task.status.notin_(["completed", "failed", "cancelled"]),
                )
            )
            .order_by(Task.due_date.asc())
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())
