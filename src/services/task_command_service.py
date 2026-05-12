"""
任务命令服务

提供任务命令相关的操作，包括：
- 任务创建（委托给 TaskSubmissionService）
- 任务更新
- 任务删除
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.constants import TaskPriority
from src.db.models import Task
from src.db.repositories.task_repo import TaskRepository

logger = logging.getLogger(__name__)


class TaskCommandService:
    """
    任务命令服务

    负责所有任务修改相关的操作，包括：
    - 任务创建（委托给 TaskSubmissionService）
    - 任务更新
    - 任务删除

    设计说明：
    任务创建逻辑委托给 TaskSubmissionService，确保工具层和 API 层
    使用统一的创建流程，避免代码重复和行为不一致。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务命令服务

        Args:
            session: 数据库会话（由调用者管理生命周期）
        """
        self.session = session
        self.task_repo = TaskRepository(session)

    async def create_task(
        self,
        task_data: dict[str, Any],
        user_id: str,
        session_id: str | None = None,
    ) -> tuple[Task, dict[str, Any]]:
        """
        创建任务（委托给 TaskSubmissionService）

        统一使用 TaskSubmissionService 进行任务创建，确保：
        1. 依赖验证
        2. 评估指标解析
        3. ExecutionRecord 创建
        4. 事件发布

        Args:
            task_data: 任务数据字典
            user_id: 用户 ID
            session_id: 会话 ID（可选）

        Returns:
            元组 (创建的任务对象, 原始任务数据)

        Raises:
            Exception: 创建失败时抛出异常
        """
        from src.tasks.services.submission_service import TaskSubmissionService

        submission_service = TaskSubmissionService(self.session)

        goal = task_data.get("goal", {})
        if not goal.get("title"):
            goal["title"] = task_data.get("title", "")

        if isinstance(task_data.get("priority"), str):
            priority = TaskPriority.to_int(task_data.get("priority"))
        else:
            priority = task_data.get("priority", TaskPriority.MEDIUM)

        result = await submission_service.submit(
            goal=goal,
            evaluation_metric_ids=task_data.get("evaluation_metric_ids"),
            acceptance_criteria=task_data.get("acceptance_criteria"),
            target_type=task_data.get("target_type", "agent"),
            target_id=task_data.get("agent_id", ""),
            target_name=task_data.get("title", ""),
            user_id=str(user_id),
            parent_task_id=task_data.get("parent_task_id"),
            session_id=session_id,
            task_type=task_data.get("task_type", "execution"),
            priority=priority,
            max_retries=task_data.get("max_retries", 3),
            metadata={
                "execution_record_id": task_data.get("execution_record_id"),
                **task_data.get("task_metadata", {}),
            },
            dependencies=task_data.get("dependencies"),
        )

        if result.get("error"):
            raise ValueError(f"任务创建失败: {result.get('error')}")

        task = await self.task_repo.get(result["task_id"])
        if not task:
            raise ValueError(f"任务创建失败: 无法获取任务 {result['task_id']}")

        logger.info(
            f"任务创建成功（通过 TaskSubmissionService） | "
            f"task_id={task.id} | user_id={user_id}"
        )

        return task, task_data

    async def update_task(
        self,
        task_id: str,
        task_data: dict[str, Any],
        user_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        更新任务

        Args:
            task_id: 任务 ID
            task_data: 更新数据字典
            user_id: 用户 ID
            session_id: 会话 ID（可选）

        Returns:
            更新后的任务信息，不存在或无权访问返回 None
        """
        task = await self.task_repo.get(task_id)

        # 验证用户权限
        if not task or task.user_id != str(user_id):
            return None

        # 验证会话权限
        if session_id is not None and task.session_id != session_id:
            return None

        # 允许更新的字段
        allowed_fields = {
            "title",
            "description",
            "status",
            "goal",
            "priority",
            "task_metadata",
        }

        update_data = {k: v for k, v in task_data.items() if k in allowed_fields}

        if not update_data:
            return None

        # 处理 priority 转换
        if "priority" in update_data and isinstance(update_data["priority"], str):
            update_data["priority"] = TaskPriority.to_int(update_data["priority"])

        # 更新任务
        success = await self.task_repo.update(task_id, update_data)

        if not success:
            return None

        await self.session.commit()

        # 获取更新后的任务
        updated_task = await self.task_repo.get(task_id)
        return await self._task_to_dict(updated_task)

    async def delete_task(
        self, task_id: str, user_id: str, session_id: str | None = None
    ) -> bool:
        """
        删除任务

        Args:
            task_id: 任务 ID
            user_id: 用户 ID
            session_id: 会话 ID（可选）

        Returns:
            是否删除成功
        """
        task = await self.task_repo.get(task_id)

        # 验证用户权限
        if not task or task.user_id != str(user_id):
            return False

        # 验证会话权限
        if session_id is not None and task.session_id != session_id:
            return False

        return await self.task_repo.delete(task_id)

    async def _task_to_dict(self, task: Task) -> dict[str, Any]:
        """
        将任务对象转换为字典

        Args:
            task: 任务对象

        Returns:
            任务字典
        """
        priority_str = TaskPriority.to_str(task.priority)

        result = {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "agent_id": task.target_id,
            "priority": priority_str,
            "status": task.status,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "parent_task_id": task.parent_task_id,
            "session_id": task.session_id,
            "user_id": task.user_id,
            "goal": task.goal,
            "evaluation_metric_ids": task.evaluation_metric_ids,
            "execution_record_id": task.execution_record_id,
            "tags": task.tags or [],
            "subtasks": [],
        }

        # 从 task_metadata 中提取扩展信息
        if task.task_metadata:
            result["task_type"] = task.task_metadata.get("task_type")
            result["agent_level"] = task.task_metadata.get("agent_level")
            result["total_criteria"] = task.task_metadata.get("total_criteria", 0)
            result["passed_criteria"] = task.task_metadata.get("passed_criteria", 0)
            result["failed_criteria"] = task.task_metadata.get("failed_criteria", 0)
            result["progress_percent"] = task.task_metadata.get("progress_percent", 0.0)

        return result
