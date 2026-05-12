"""
数据库任务存储实现

适配现有的 Task 模型和 TaskRepository，提供统一的存储接口。
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.task import Task
from src.db.repositories.task_repo import TaskRepository
from src.tasks.storage.base import ITaskStorage, StorageError, TaskModel

logger = logging.getLogger(__name__)


class DatabaseTaskStorage(ITaskStorage):
    """
    数据库任务存储

    适配现有的 Task 模型和 TaskRepository，实现统一的存储接口。
    支持所有数据库操作，包括事务支持。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化数据库存储

        Args:
            session: SQLAlchemy 异步会话
        """
        self.session = session
        self._repo = TaskRepository(session)

    def _model_to_entity(self, task: Task) -> TaskModel:
        """
        将数据库实体转换为 TaskModel

        Args:
            task: 数据库任务实体

        Returns:
            任务数据模型
        """
        return TaskModel(
            id=task.id,
            parent_task_id=task.parent_task_id,
            execution_record_id=task.execution_record_id,
            user_id=task.user_id,
            session_id=task.session_id,
            title=task.title,
            description=task.description,
            goal=task.goal,
            target_type=task.target_type,
            target_id=task.target_id,
            target_name=task.target_name,
            priority=task.priority,
            dependencies=task.dependencies,
            due_date=task.due_date,
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            evaluation_metric_ids=task.evaluation_metric_ids,
            status=task.status,
            started_at=task.started_at,
            completed_at=task.completed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            task_metadata=task.task_metadata,
            tags=task.tags,
        )

    def _model_to_dict(self, task: TaskModel) -> dict[str, Any]:
        """
        将 TaskModel 转换为字典（用于创建/更新数据库实体）

        Args:
            task: 任务数据模型

        Returns:
            任务数据字典
        """
        return {
            "id": task.id,
            "parent_task_id": task.parent_task_id,
            "execution_record_id": task.execution_record_id,
            "user_id": task.user_id,
            "session_id": task.session_id,
            "title": task.title,
            "description": task.description,
            "goal": task.goal,
            "target_type": task.target_type,
            "target_id": task.target_id,
            "target_name": task.target_name,
            "priority": task.priority,
            "dependencies": task.dependencies,
            "due_date": task.due_date,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "evaluation_metric_ids": task.evaluation_metric_ids,
            "status": task.status,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "task_metadata": task.task_metadata,
            "tags": task.tags,
        }

    async def save(self, task: TaskModel) -> TaskModel:
        """
        保存任务到数据库

        如果任务已存在则更新，否则创建新任务。

        Args:
            task: 任务数据模型

        Returns:
            保存后的任务数据模型

        Raises:
            StorageError: 存储操作失败时抛出
        """
        try:
            # 检查任务是否存在
            existing = await self._repo.get(task.id)

            if existing:
                # 更新现有任务
                task_dict = self._model_to_dict(task)
                await self._repo.update(task.id, task_dict)
                updated = await self._repo.get(task.id)
                logger.debug("任务已更新: %s", task.id)
                return self._model_to_entity(updated)
            else:
                # 创建新任务
                task_dict = self._model_to_dict(task)
                created = await self._repo.create_task(task_dict)
                logger.debug("任务已创建: %s", task.id)
                return self._model_to_entity(created)

        except Exception as e:
            logger.error("保存任务失败: %s, 错误: %s", task.id, str(e))
            raise StorageError(f"保存任务失败: {task.id}", e)

    async def load(self, task_id: str) -> TaskModel | None:
        """
        从数据库加载任务

        Args:
            task_id: 任务ID

        Returns:
            任务数据模型，如果不存在则返回 None
        """
        try:
            task = await self._repo.get(task_id)
            if task is None:
                return None
            return self._model_to_entity(task)
        except Exception as e:
            logger.error("加载任务失败: %s, 错误: %s", task_id, str(e))
            raise StorageError(f"加载任务失败: {task_id}", e)

    async def load_by_id(self, task_id: str) -> TaskModel | None:
        """
        根据ID加载任务（load 的别名）

        Args:
            task_id: 任务ID

        Returns:
            任务数据模型，如果不存在则返回 None
        """
        return await self.load(task_id)

    async def list_by_status(
        self,
        status: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskModel]:
        """
        按状态列出任务

        Args:
            status: 任务状态
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            任务列表
        """
        try:
            # TaskRepository 的 get_tasks_by_status 不支持 offset，
            # 我们需要手动处理
            tasks = await self._repo.get_tasks_by_status(status, limit=limit + offset)
            result = [self._model_to_entity(t) for t in tasks]
            return result[offset : offset + limit]
        except Exception as e:
            logger.error("按状态列出任务失败: %s, 错误: %s", status, str(e))
            raise StorageError(f"按状态列出任务失败: {status}", e)

    async def update_status(
        self,
        task_id: str,
        status: str,
        error_message: str | None = None,
    ) -> bool:
        """
        更新任务状态

        Args:
            task_id: 任务ID
            status: 新状态
            error_message: 错误信息（可选）

        Returns:
            是否更新成功
        """
        try:
            return await self._repo.update_task_status(task_id, status, error_message)
        except Exception as e:
            logger.error("更新任务状态失败: %s, 错误: %s", task_id, str(e))
            raise StorageError(f"更新任务状态失败: {task_id}", e)

    async def delete(self, task_id: str) -> bool:
        """
        从数据库删除任务

        Args:
            task_id: 任务ID

        Returns:
            是否删除成功
        """
        try:
            return await self._repo.delete(task_id)
        except Exception as e:
            logger.error("删除任务失败: %s, 错误: %s", task_id, str(e))
            raise StorageError(f"删除任务失败: {task_id}", e)

    async def list_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskModel]:
        """
        列出所有任务

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            任务列表
        """
        try:
            tasks = await self._repo.get_all(limit=limit, offset=offset, order_by="created_at")
            return [self._model_to_entity(t) for t in tasks]
        except Exception as e:
            logger.error("列出所有任务失败: %s", str(e))
            raise StorageError("列出所有任务失败", e)

    async def get_subtasks(self, parent_task_id: str) -> list[TaskModel]:
        """
        获取子任务列表

        Args:
            parent_task_id: 父任务ID

        Returns:
            子任务列表
        """
        try:
            tasks = await self._repo.get_subtasks(parent_task_id)
            return [self._model_to_entity(t) for t in tasks]
        except Exception as e:
            logger.error("获取子任务失败: %s, 错误: %s", parent_task_id, str(e))
            raise StorageError(f"获取子任务失败: {parent_task_id}", e)

    async def get_root_tasks(
        self,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TaskModel]:
        """
        获取根任务列表（parent_task_id 为 None 的任务）

        Args:
            user_id: 用户ID（可选）
            session_id: 会话ID（可选）
            status: 任务状态（可选）
            limit: 返回数量限制

        Returns:
            根任务列表
        """
        try:
            tasks = await self._repo.get_root_tasks(
                user_id=user_id,
                session_id=session_id,
                status=status,
                limit=limit,
            )
            return [self._model_to_entity(t) for t in tasks]
        except Exception as e:
            logger.error("获取根任务失败: %s", str(e))
            raise StorageError("获取根任务失败", e)

    async def count_by_status(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, int]:
        """
        按状态统计任务数量

        Args:
            session_id: 会话ID（可选）
            user_id: 用户ID（可选）

        Returns:
            状态统计字典
        """
        try:
            return await self._repo.count_by_status(session_id=session_id, user_id=user_id)
        except Exception as e:
            logger.error("统计任务数量失败: %s", str(e))
            raise StorageError("统计任务数量失败", e)

    async def get_pending_tasks(
        self,
        limit: int = 50,
        priority_min: int | None = None,
    ) -> list[TaskModel]:
        """
        获取待处理任务（按优先级排序）

        Args:
            limit: 返回数量限制
            priority_min: 最低优先级（可选）

        Returns:
            任务列表
        """
        try:
            tasks = await self._repo.get_pending_tasks(limit=limit, priority_min=priority_min)
            return [self._model_to_entity(t) for t in tasks]
        except Exception as e:
            logger.error("获取待处理任务失败: %s", str(e))
            raise StorageError("获取待处理任务失败", e)
