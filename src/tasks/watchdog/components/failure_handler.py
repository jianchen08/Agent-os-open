"""
失败处理器组件

负责处理任务失败、重试逻辑等失败相关功能。

FEATURE-EXCEPTION-HANDLING: 异常处理流程优化
设计决策:
  - 差异化异常处理：根据异常类型采取不同策略
  - 依赖失败：标记为 blocked 状态，停止重试
  - 资源不足：等待资源释放后重试
  - 配置错误：直接标记为 blocked，触发人工干预
  - 超时但有进展：基于已有进度重试
"""

import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select, update

from src.core.states import ExecutionStatus
from src.db.models import Task

logger = logging.getLogger(__name__)


class FailureReason(str, Enum):
    """
    失败原因枚举

    FEATURE-EXCEPTION-HANDLING: 异常类型定义
    """

    UNKNOWN = "unknown"
    RESOURCE_INSUFFICIENT = "resource_insufficient"
    DEPENDENCY_FAILED = "dependency_failed"
    CONFIG_ERROR = "config_error"
    TIMEOUT_WITH_PROGRESS = "timeout_with_progress"
    TIMEOUT_NO_PROGRESS = "timeout_no_progress"
    EXECUTION_ERROR = "execution_error"
    PARTIAL_SUCCESS = "partial_success"


class FailureHandler:
    """
    失败处理器

    核心职责：
    1. 处理失败任务
    2. 判断是否需要重试
    3. 管理重试计数
    4. 差异化异常处理

    Attributes:
        project_controller: 项目控制器组件（用于暂停项目）
    """

    def __init__(self, project_controller: Any | None = None):
        """
        初始化失败处理器

        Args:
            project_controller: 项目控制器组件
        """
        self.project_controller = project_controller

    def set_project_controller(self, project_controller: Any) -> None:
        """
        设置项目控制器

        Args:
            project_controller: 项目控制器组件
        """
        self.project_controller = project_controller

    async def handle_failed_task(
        self,
        session,
        root_task: Task,
        failed_task: Task,
        failure_reason: FailureReason = FailureReason.UNKNOWN,
        progress_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        处理失败任务（支持差异化异常处理）

        FEATURE-EXCEPTION-HANDLING: 差异化处理逻辑
        处理策略:
          - DEPENDENCY_FAILED: 标记为 blocked，停止重试
          - CONFIG_ERROR: 标记为 blocked，触发人工干预
          - RESOURCE_INSUFFICIENT: 等待后重试
          - TIMEOUT_WITH_PROGRESS: 基于进度重试
          - TIMEOUT_NO_PROGRESS: 正常重试
          - PARTIAL_SUCCESS: 记录进度，继续执行
          - 其他: 正常重试

        Args:
            session: 数据库会话
            root_task: 根任务对象
            failed_task: 失败的任务对象
            failure_reason: 失败原因
            progress_info: 进度信息（用于部分成功场景）

        Returns:
            处理结果
        """
        task_id = failed_task.id
        task_metadata = failed_task.task_metadata or {}
        max_retries = task_metadata.get("max_retries", 6)
        retry_count = task_metadata.get("retry_count", 0)

        logger.info(
            f"[FailureHandler] 处理失败任务 | "
            f"task_id={task_id} | reason={failure_reason.value} | "
            f"retry_count={retry_count}/{max_retries}"
        )

        if failure_reason == FailureReason.DEPENDENCY_FAILED:
            return await self._handle_dependency_failed(
                session, root_task, failed_task, task_metadata
            )

        if failure_reason == FailureReason.CONFIG_ERROR:
            return await self._handle_config_error(
                session, root_task, failed_task, task_metadata
            )

        if failure_reason == FailureReason.RESOURCE_INSUFFICIENT:
            return await self._handle_resource_insufficient(
                session, root_task, failed_task, task_metadata, max_retries, retry_count
            )

        if failure_reason == FailureReason.TIMEOUT_WITH_PROGRESS:
            return await self._handle_timeout_with_progress(
                session,
                root_task,
                failed_task,
                task_metadata,
                max_retries,
                retry_count,
                progress_info,
            )

        if failure_reason == FailureReason.PARTIAL_SUCCESS:
            return await self._handle_partial_success(
                session, failed_task, task_metadata, progress_info
            )

        return await self._handle_normal_retry(
            session, root_task, failed_task, task_metadata, max_retries, retry_count
        )

    async def _handle_dependency_failed(
        self,
        session,
        root_task: Task,
        failed_task: Task,
        task_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """
        处理依赖任务失败

        FEATURE-EXCEPTION-HANDLING: 依赖失败处理
        策略: 标记为 blocked 状态，停止重试，等待依赖任务恢复

        Args:
            session: 数据库会话
            root_task: 根任务对象
            failed_task: 失败的任务对象
            task_metadata: 任务元数据

        Returns:
            处理结果
        """
        task_id = failed_task.id
        task_metadata["blocked_reason"] = "dependency_failed"
        task_metadata["blocked_at"] = datetime.now(UTC).isoformat()

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="blocked",
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        logger.warning(
            f"[FailureHandler] 任务 {task_id} 因依赖失败被阻塞 | "
            f"等待依赖任务恢复"
        )

        return {
            "project_id": root_task.id,
            "task_id": task_id,
            "action": "blocked",
            "reason": "dependency_failed",
            "message": "依赖任务失败，等待依赖恢复",
        }

    async def _handle_config_error(
        self,
        session,
        root_task: Task,
        failed_task: Task,
        task_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """
        处理配置错误

        FEATURE-EXCEPTION-HANDLING: 配置错误处理
        策略: 标记为 blocked 状态，触发人工干预

        Args:
            session: 数据库会话
            root_task: 根任务对象
            failed_task: 失败的任务对象
            task_metadata: 任务元数据

        Returns:
            处理结果
        """
        task_id = failed_task.id
        task_metadata["blocked_reason"] = "config_error"
        task_metadata["blocked_at"] = datetime.now(UTC).isoformat()
        task_metadata["requires_manual_intervention"] = True

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="blocked",
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        logger.error(
            f"[FailureHandler] 任务 {task_id} 因配置错误被阻塞 | "
            f"需要人工干预"
        )

        return {
            "project_id": root_task.id,
            "task_id": task_id,
            "action": "blocked",
            "reason": "config_error",
            "message": "配置错误，需要人工干预",
            "requires_manual_intervention": True,
        }

    async def _handle_resource_insufficient(
        self,
        session,
        root_task: Task,
        failed_task: Task,
        task_metadata: dict[str, Any],
        max_retries: int,
        retry_count: int,
    ) -> dict[str, Any]:
        """
        处理资源不足

        FEATURE-EXCEPTION-HANDLING: 资源不足处理
        策略: 等待资源释放后重试，不增加重试计数

        Args:
            session: 数据库会话
            root_task: 根任务对象
            failed_task: 失败的任务对象
            task_metadata: 任务元数据
            max_retries: 最大重试次数
            retry_count: 当前重试次数

        Returns:
            处理结果
        """
        task_id = failed_task.id
        resource_wait_count = task_metadata.get("resource_wait_count", 0) + 1
        task_metadata["resource_wait_count"] = resource_wait_count
        task_metadata["last_resource_wait"] = datetime.now(UTC).isoformat()

        max_resource_waits = task_metadata.get("max_resource_waits", 5)
        if resource_wait_count >= max_resource_waits:
            task_metadata["retry_count"] = retry_count + 1
            resource_wait_count = 0
            task_metadata["resource_wait_count"] = resource_wait_count

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="pending",
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        logger.info(
            f"[FailureHandler] 任务 {task_id} 等待资源释放 | "
            f"resource_wait_count={resource_wait_count}"
        )

        return {
            "project_id": root_task.id,
            "task_id": task_id,
            "action": "waiting_resource",
            "resource_wait_count": resource_wait_count,
            "message": "等待资源释放后重试",
        }

    async def _handle_timeout_with_progress(
        self,
        session,
        root_task: Task,
        failed_task: Task,
        task_metadata: dict[str, Any],
        max_retries: int,
        retry_count: int,
        progress_info: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """
        处理超时但有进展

        FEATURE-EXCEPTION-HANDLING: 超时有进展处理
        策略: 保存进度，基于已有进度重试，不增加重试计数

        Args:
            session: 数据库会话
            root_task: 根任务对象
            failed_task: 失败的任务对象
            task_metadata: 任务元数据
            max_retries: 最大重试次数
            retry_count: 当前重试次数
            progress_info: 进度信息

        Returns:
            处理结果
        """
        task_id = failed_task.id

        if progress_info:
            task_metadata["saved_progress"] = progress_info
            task_metadata["progress_saved_at"] = datetime.now(UTC).isoformat()

        timeout_retry_count = task_metadata.get("timeout_retry_count", 0) + 1
        task_metadata["timeout_retry_count"] = timeout_retry_count

        max_timeout_retries = task_metadata.get("max_timeout_retries", 3)
        if timeout_retry_count >= max_timeout_retries:
            task_metadata["retry_count"] = retry_count + 1
            task_metadata["timeout_retry_count"] = 0

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="pending",
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        logger.info(
            f"[FailureHandler] 任务 {task_id} 超时但有进展，基于进度重试 | "
            f"timeout_retry_count={timeout_retry_count}"
        )

        return {
            "project_id": root_task.id,
            "task_id": task_id,
            "action": "retry_with_progress",
            "timeout_retry_count": timeout_retry_count,
            "has_saved_progress": bool(progress_info),
            "message": "超时但有进展，基于已有进度重试",
        }

    async def _handle_partial_success(
        self,
        session,
        failed_task: Task,
        task_metadata: dict[str, Any],
        progress_info: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """
        处理部分成功

        FEATURE-EXCEPTION-HANDLING: 部分成功处理
        策略: 记录进度，继续执行剩余部分

        Args:
            session: 数据库会话
            failed_task: 失败的任务对象
            task_metadata: 任务元数据
            progress_info: 进度信息

        Returns:
            处理结果
        """
        task_id = failed_task.id

        if progress_info:
            task_metadata["partial_progress"] = progress_info
            task_metadata["partial_progress_at"] = datetime.now(UTC).isoformat()

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status=ExecutionStatus.RUNNING.value,
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        logger.info(
            f"[FailureHandler] 任务 {task_id} 部分成功，继续执行 | "
            f"progress={progress_info}"
        )

        return {
            "task_id": task_id,
            "action": "continue_with_progress",
            "has_partial_progress": bool(progress_info),
            "message": "部分成功，继续执行剩余部分",
        }

    async def _handle_normal_retry(
        self,
        session,
        root_task: Task,
        failed_task: Task,
        task_metadata: dict[str, Any],
        max_retries: int,
        retry_count: int,
    ) -> dict[str, Any]:
        """
        处理普通重试

        Args:
            session: 数据库会话
            root_task: 根任务对象
            failed_task: 失败的任务对象
            task_metadata: 任务元数据
            max_retries: 最大重试次数
            retry_count: 当前重试次数

        Returns:
            处理结果
        """
        task_id = failed_task.id

        if retry_count < max_retries:
            task_metadata["retry_count"] = retry_count + 1

            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status="pending",
                    task_metadata=task_metadata,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()

            logger.info(f"任务 {task_id} 将重试 ({retry_count + 1}/{max_retries})")

            return {
                "project_id": root_task.id,
                "task_id": task_id,
                "action": "retry",
                "retry_count": retry_count + 1,
            }
        if self.project_controller:
            await self.project_controller.pause_project(
                root_task.id,
                f"任务 {task_id} 失败，达到最大重试次数",
            )

        return {
            "project_id": root_task.id,
            "task_id": task_id,
            "action": "paused",
            "reason": "max_retries_exceeded",
        }

    async def should_retry(self, task: Task) -> bool:
        """
        判断任务是否应该重试

        Args:
            task: 任务对象

        Returns:
            是否应该重试
        """
        task_metadata = task.task_metadata or {}
        max_retries = task_metadata.get("max_retries", 6)
        retry_count = task_metadata.get("retry_count", 0)

        return retry_count < max_retries

    async def get_retry_info(self, task: Task) -> dict[str, Any]:
        """
        获取任务重试信息

        Args:
            task: 任务对象

        Returns:
            重试信息
        """
        task_metadata = task.task_metadata or {}
        max_retries = task_metadata.get("max_retries", 6)
        retry_count = task_metadata.get("retry_count", 0)

        return {
            "task_id": task.id,
            "retry_count": retry_count,
            "max_retries": max_retries,
            "can_retry": retry_count < max_retries,
            "remaining_retries": max(0, max_retries - retry_count),
        }

    async def increment_retry_count(
        self,
        session,
        task_id: str,
    ) -> dict[str, Any]:
        """
        增加任务重试计数

        Args:
            session: 数据库会话
            task_id: 任务 ID

        Returns:
            更新结果
        """
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if not task:
            return {"error": "任务不存在"}

        task_metadata = task.task_metadata or {}
        retry_count = task_metadata.get("retry_count", 0)
        task_metadata["retry_count"] = retry_count + 1

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        return {
            "task_id": task_id,
            "retry_count": retry_count + 1,
        }

    async def reset_retry_count(
        self,
        session,
        task_id: str,
    ) -> dict[str, Any]:
        """
        重置任务重试计数

        Args:
            session: 数据库会话
            task_id: 任务 ID

        Returns:
            更新结果
        """
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if not task:
            return {"error": "任务不存在"}

        task_metadata = task.task_metadata or {}
        task_metadata["retry_count"] = 0

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        return {
            "task_id": task_id,
            "retry_count": 0,
        }
