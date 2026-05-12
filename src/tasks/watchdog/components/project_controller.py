"""
项目控制器组件

负责项目的暂停、恢复、完成等控制功能。
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from src.db.models import Task
from src.db.session_manager import managed_session

logger = logging.getLogger(__name__)


class ProjectController:
    """
    项目控制器

    核心职责：
    1. 暂停项目自动执行
    2. 恢复项目自动执行
    3. 完成项目
    4. 发送项目状态变更通知

    Attributes:
        notification_callback: 通知回调函数
    """

    def __init__(self, notification_callback: Callable | None = None):
        """
        初始化项目控制器

        Args:
            notification_callback: 通知回调函数
        """
        self.notification_callback = notification_callback

    def set_notification_callback(self, callback: Callable | None) -> None:
        """
        设置通知回调

        Args:
            callback: 回调函数
        """
        self.notification_callback = callback

    async def pause_project(
        self,
        project_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """
        暂停项目自动执行

        Args:
            project_id: 项目 ID
            reason: 暂停原因

        Returns:
            处理结果
        """
        async with managed_session() as session:
            # 获取根任务
            result = await session.execute(select(Task).where(Task.id == project_id))
            root_task = result.scalar_one_or_none()

            if not root_task:
                return {
                    "project_id": project_id,
                    "action": "error",
                    "error": "project_not_found",
                }

            task_metadata = root_task.task_metadata or {}
            task_metadata["auto_execute"] = False
            task_metadata["pause_reason"] = reason
            task_metadata["paused_at"] = datetime.now().isoformat()

            await session.execute(
                update(Task)
                .where(Task.id == project_id)
                .values(
                    status="paused",
                    task_metadata=task_metadata,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()

            logger.info(f"项目 {project_id} 已暂停自动执行: {reason}")

            # 发送通知
            if self.notification_callback:
                try:
                    await self.notification_callback(
                        project_id=project_id,
                        event="project_paused",
                        message="项目自动执行已暂停",
                        details={"reason": reason},
                    )
                except Exception as e:
                    logger.error(f"发送通知失败: {e}")

            return {
                "project_id": project_id,
                "action": "paused",
                "reason": reason,
            }

    async def resume_project(
        self,
        project_id: str,
    ) -> dict[str, Any]:
        """
        恢复项目自动执行

        Args:
            project_id: 项目 ID

        Returns:
            处理结果
        """
        async with managed_session() as session:
            # 获取根任务
            result = await session.execute(select(Task).where(Task.id == project_id))
            root_task = result.scalar_one_or_none()

            if not root_task:
                return {
                    "project_id": project_id,
                    "action": "error",
                    "error": "project_not_found",
                }

            task_metadata = root_task.task_metadata or {}
            task_metadata["auto_execute"] = True
            task_metadata["resumed_at"] = datetime.now().isoformat()

            # 清除暂停原因
            if "pause_reason" in task_metadata:
                del task_metadata["pause_reason"]

            await session.execute(
                update(Task)
                .where(Task.id == project_id)
                .values(
                    status="running",
                    task_metadata=task_metadata,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()

            logger.info(f"项目 {project_id} 已恢复自动执行")

            # 发送通知
            if self.notification_callback:
                try:
                    await self.notification_callback(
                        project_id=project_id,
                        event="project_resumed",
                        message="项目自动执行已恢复",
                        details={},
                    )
                except Exception as e:
                    logger.error(f"发送通知失败: {e}")

            return {
                "project_id": project_id,
                "action": "resumed",
            }

    async def complete_project(
        self,
        project_id: str,
    ) -> dict[str, Any]:
        """
        完成项目

        核心原则：执行器不处理状态转换
        - 只更新 task_metadata，不设置任务状态
        - 任务状态变更由 task_evaluate 工具触发
        - 项目完成需要通过 task_evaluate 工具完成

        Args:
            project_id: 项目 ID

        Returns:
            处理结果
        """
        async with managed_session() as session:
            # 获取根任务
            result = await session.execute(select(Task).where(Task.id == project_id))
            root_task = result.scalar_one_or_none()

            if not root_task:
                return {
                    "project_id": project_id,
                    "action": "error",
                    "error": "project_not_found",
                }

            task_metadata = root_task.task_metadata or {}
            task_metadata["auto_execute"] = False
            task_metadata["manual_complete_requested"] = True
            task_metadata["manual_complete_requested_at"] = datetime.now().isoformat()

            await session.execute(
                update(Task)
                .where(Task.id == project_id)
                .values(
                    task_metadata=task_metadata,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()

            logger.info(
                f"项目 {project_id} 已请求完成，等待 task_evaluate 工具完成"
            )

            return {
                "project_id": project_id,
                "action": "complete_requested",
                "message": "项目完成请求已提交，请使用 task_evaluate 工具完成项目",
            }

    async def get_project_status(
        self,
        project_id: str,
    ) -> dict[str, Any]:
        """
        获取项目状态

        Args:
            project_id: 项目 ID

        Returns:
            项目状态信息
        """
        async with managed_session() as session:
            result = await session.execute(select(Task).where(Task.id == project_id))
            root_task = result.scalar_one_or_none()

            if not root_task:
                return {
                    "project_id": project_id,
                    "error": "project_not_found",
                }

            task_metadata = root_task.task_metadata or {}

            return {
                "project_id": project_id,
                "status": root_task.status,
                "auto_execute": task_metadata.get("auto_execute", False),
                "pause_reason": task_metadata.get("pause_reason"),
                "paused_at": task_metadata.get("paused_at"),
                "resumed_at": task_metadata.get("resumed_at"),
                "completed_at": task_metadata.get("completed_at"),
            }

    async def cancel_project(
        self,
        project_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """
        取消项目

        Args:
            project_id: 项目 ID
            reason: 取消原因

        Returns:
            处理结果
        """
        async with managed_session() as session:
            # 获取根任务
            result = await session.execute(select(Task).where(Task.id == project_id))
            root_task = result.scalar_one_or_none()

            if not root_task:
                return {
                    "project_id": project_id,
                    "action": "error",
                    "error": "project_not_found",
                }

            task_metadata = root_task.task_metadata or {}
            task_metadata["auto_execute"] = False
            task_metadata["cancelled_at"] = datetime.now().isoformat()
            if reason:
                task_metadata["cancel_reason"] = reason

            await session.execute(
                update(Task)
                .where(Task.id == project_id)
                .values(
                    status="cancelled",
                    task_metadata=task_metadata,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()

            logger.info(f"项目 {project_id} 已取消")

            # 发送通知
            if self.notification_callback:
                try:
                    await self.notification_callback(
                        project_id=project_id,
                        event="project_cancelled",
                        message="项目已取消",
                        details={"reason": reason} if reason else {},
                    )
                except Exception as e:
                    logger.error(f"发送通知失败: {e}")

            return {
                "project_id": project_id,
                "action": "cancelled",
                "reason": reason,
            }
