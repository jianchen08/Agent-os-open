"""
任务触发器组件

负责触发任务执行、管理任务状态转换等触发相关功能。
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, update

from src.core.states import ExecutionStatus
from src.db.models import Task
from src.db.session_manager import managed_session

logger = logging.getLogger(__name__)


class TaskTrigger:
    """
    任务触发器

    核心职责：
    1. 触发项目的下一个任务执行
    2. 管理任务状态转换
    3. 处理任务启动回调

    Attributes:
        task_manager_callback: 任务管理器回调函数
        heartbeat_callback: 心跳更新回调函数
    """

    def __init__(
        self,
        task_manager_callback: Callable | None = None,
        heartbeat_callback: Callable | None = None,
    ):
        """
        初始化任务触发器

        Args:
            task_manager_callback: 任务管理器回调（用于启动任务）
            heartbeat_callback: 心跳更新回调函数
        """
        self.task_manager_callback = task_manager_callback
        self.heartbeat_callback = heartbeat_callback

    def set_task_manager_callback(self, callback: Callable | None) -> None:
        """
        设置任务管理器回调

        Args:
            callback: 回调函数
        """
        self.task_manager_callback = callback

    def set_heartbeat_callback(self, callback: Callable | None) -> None:
        """
        设置心跳更新回调

        Args:
            callback: 回调函数
        """
        self.heartbeat_callback = callback

    async def trigger_next_task(self, project_id: str) -> dict[str, Any]:
        """
        触发项目的下一个任务执行

        Args:
            project_id: 项目 ID

        Returns:
            触发结果
        """
        async with managed_session() as session:
            # 获取下一个待执行任务
            next_task = await self._get_current_task(session, project_id)

            if not next_task:
                logger.info(f"项目 {project_id} 没有下一个待执行任务")
                return {
                    "project_id": project_id,
                    "task_triggered": False,
                    "reason": "no_next_task",
                }

            task_id = next_task.id

            # 更新任务状态为 running
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=ExecutionStatus.RUNNING.value,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()

            # 触发任务管理器回调
            if self.task_manager_callback:
                try:
                    result = await self.task_manager_callback(
                        task_id=task_id,
                        project_id=project_id,
                        task=next_task,
                    )

                    # 检查回调返回结果
                    if isinstance(result, dict):
                        status = result.get("status", "unknown")
                        if status == "success":
                            logger.info(f"已触发项目 {project_id} 的下一个任务 {task_id}")
                            # 更新心跳
                            if self.heartbeat_callback:
                                self.heartbeat_callback(project_id)
                            return {
                                "project_id": project_id,
                                "task_id": task_id,
                                "task_triggered": True,
                            }
                        elif status in ("deferred", "failed_but_queued"):
                            # 服务未就绪或启动失败但已加入队列，回滚任务状态
                            logger.info(
                                f"任务 {task_id} 已加入待处理队列，回滚任务状态为 pending"
                            )
                            await session.execute(
                                update(Task)
                                .where(Task.id == task_id)
                                .values(
                                    status="pending",
                                    updated_at=datetime.now(),
                                )
                            )
                            await session.commit()
                            return {
                                "project_id": project_id,
                                "task_id": task_id,
                                "task_triggered": False,
                                "status": status,
                                "message": result.get("message", "任务已加入待处理队列"),
                                "queued": True,
                            }
                        else:
                            # 其他状态，回滚任务状态
                            logger.warning(
                                f"任务 {task_id} 回调返回非成功状态: {status}"
                            )
                            await session.execute(
                                update(Task)
                                .where(Task.id == task_id)
                                .values(
                                    status="pending",
                                    updated_at=datetime.now(),
                                )
                            )
                            await session.commit()
                            return {
                                "project_id": project_id,
                                "task_id": task_id,
                                "task_triggered": False,
                                "status": status,
                                "callback_result": result,
                            }
                    else:
                        # 回调没有返回结果，假设成功
                        logger.info(f"已触发项目 {project_id} 的下一个任务 {task_id}")
                        # 更新心跳
                        if self.heartbeat_callback:
                            self.heartbeat_callback(project_id)
                        return {
                            "project_id": project_id,
                            "task_id": task_id,
                            "task_triggered": True,
                        }

                except Exception as e:
                    logger.error(f"任务管理器回调失败: {e}")
                    # 回滚任务状态
                    await session.execute(
                        update(Task)
                        .where(Task.id == task_id)
                        .values(
                            status="pending",
                            updated_at=datetime.now(),
                        )
                    )
                    await session.commit()
                    return {
                        "project_id": project_id,
                        "task_id": task_id,
                        "task_triggered": False,
                        "error": str(e),
                    }
            else:
                logger.warning("未设置 task_manager_callback，无法启动任务 %s", task_id)
                return {
                    "project_id": project_id,
                    "task_id": task_id,
                    "task_triggered": False,
                    "error": "no_callback",
                }

    async def trigger_task(self, task_id: str) -> dict[str, Any]:
        """
        触发指定任务执行

        Args:
            task_id: 任务 ID

        Returns:
            触发结果
        """
        async with managed_session() as session:
            # 获取任务
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {
                    "task_id": task_id,
                    "task_triggered": False,
                    "error": "task_not_found",
                }

            if task.status != "pending":
                return {
                    "task_id": task_id,
                    "task_triggered": False,
                    "error": f"invalid_status_{task.status}",
                }

            project_id = task.parent_task_id

            # 更新任务状态为 running
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=ExecutionStatus.RUNNING.value,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()

            # 触发任务管理器回调
            if self.task_manager_callback:
                try:
                    result = await self.task_manager_callback(
                        task_id=task_id,
                        project_id=project_id,
                        task=task,
                    )

                    if isinstance(result, dict) and result.get("status") == "success":
                        logger.info(f"已触发任务 {task_id}")
                        if project_id and self.heartbeat_callback:
                            self.heartbeat_callback(project_id)
                        return {
                            "task_id": task_id,
                            "project_id": project_id,
                            "task_triggered": True,
                        }
                    else:
                        # 回滚状态
                        await session.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(
                                status="pending",
                                updated_at=datetime.now(),
                            )
                        )
                        await session.commit()
                        return {
                            "task_id": task_id,
                            "task_triggered": False,
                            "callback_result": result,
                        }

                except Exception as e:
                    logger.error(f"任务管理器回调失败: {e}")
                    # 回滚状态
                    await session.execute(
                        update(Task)
                        .where(Task.id == task_id)
                        .values(
                            status="pending",
                            updated_at=datetime.now(),
                        )
                    )
                    await session.commit()
                    return {
                        "task_id": task_id,
                        "task_triggered": False,
                        "error": str(e),
                    }
            else:
                return {
                    "task_id": task_id,
                    "task_triggered": False,
                    "error": "no_callback",
                }

    async def _get_current_task(
        self,
        session,
        project_id: str,
    ) -> Task | None:
        """
        获取项目当前待执行的子任务

        Args:
            session: 数据库会话
            project_id: 项目 ID（根任务 ID）

        Returns:
            任务对象，如果没有则返回 None
        """
        # 查询项目中状态不是 completed 的最早任务
        query = (
            select(Task)
            .where(
                and_(
                    Task.parent_task_id == project_id,
                    Task.status.in_([
                        ExecutionStatus.PENDING.value,
                        ExecutionStatus.RUNNING.value,
                        ExecutionStatus.FAILED.value,
                        ExecutionStatus.BLOCKED.value,
                    ]),
                )
            )
            .order_by(Task.created_at)
            .limit(1)
        )

        result = await session.execute(query)
        return result.scalar_one_or_none()
