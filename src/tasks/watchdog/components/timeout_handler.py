"""
超时处理器组件

负责检测和处理任务超时、卡住等健康状态问题。
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.core.states import ExecutionStatus
from src.db.models import Task
from src.db.session_manager import managed_session

logger = logging.getLogger(__name__)


@dataclass
class TaskActivityStatus:
    """任务活动状态"""

    is_active: bool
    activity_type: str  # heartbeat/database_update/execution_log/none
    last_activity_time: datetime | None
    age_seconds: float | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubtaskActivityStatus:
    """子任务活动状态"""

    has_active_subtask: bool
    active_subtask_id: str | None = None
    last_activity: datetime | None = None
    active_subtask_status: str | None = None


class TimeoutHandler:
    """
    超时处理器

    核心职责：
    1. 检查任务健康状态（超时、卡住）
    2. 处理任务超时情况
    3. 处理任务卡住检测
    4. 多层检查机制确保准确性

    Attributes:
        task_timeout: 任务超时时间（秒）
        stuck_threshold: 卡住阈值（秒）
        notification_callback: 通知回调函数
        heartbeat_threshold: 心跳活跃阈值（秒）
    """

    # 默认配置
    DEFAULT_TASK_TIMEOUT = 3600  # 任务超时时间（秒），默认 1 小时
    DEFAULT_STUCK_THRESHOLD = 600  # 卡住阈值（秒），超过此时间无输出则认为卡住
    DEFAULT_HEARTBEAT_THRESHOLD = 300  # 心跳活跃阈值（秒），心跳在此时间内视为活跃

    def __init__(
        self,
        task_timeout: int | None = None,
        stuck_threshold: int | None = None,
        notification_callback: Callable | None = None,
        heartbeat_threshold: int | None = None,
    ):
        """
        初始化超时处理器

        Args:
            task_timeout: 任务超时时间（秒），为 None 时从配置读取
            stuck_threshold: 卡住阈值（秒），为 None 时从配置读取
            notification_callback: 通知回调函数
            heartbeat_threshold: 心跳活跃阈值（秒），为 None 时使用默认值
        """
        # 从配置文件读取阈值参数
        self.task_timeout = task_timeout if task_timeout is not None else settings.task_timeout
        self.stuck_threshold = stuck_threshold if stuck_threshold is not None else self.DEFAULT_STUCK_THRESHOLD
        self.heartbeat_threshold = heartbeat_threshold if heartbeat_threshold is not None else self.DEFAULT_HEARTBEAT_THRESHOLD
        self.notification_callback = notification_callback

        # 心跳记录 {task_id: last_heartbeat_time}
        self._heartbeats: dict[str, float] = {}

    def set_notification_callback(self, callback: Callable | None) -> None:
        """
        设置通知回调

        Args:
            callback: 回调函数
        """
        self.notification_callback = callback

    def update_heartbeat(self, task_id: str) -> None:
        """
        更新任务心跳

        Args:
            task_id: 任务 ID
        """
        self._heartbeats[task_id] = time.time()
        logger.debug(f"任务 {task_id} 心跳已更新")

    def get_heartbeat_age(self, task_id: str) -> float | None:
        """
        获取任务心跳年龄

        Args:
            task_id: 任务 ID

        Returns:
            心跳年龄（秒），如果不存在返回 None
        """
        heartbeat = self._heartbeats.get(task_id)
        if heartbeat is None:
            return None
        return time.time() - heartbeat

    def clear_heartbeat(self, task_id: str) -> None:
        """
        清除任务心跳

        Args:
            task_id: 任务 ID
        """
        self._heartbeats.pop(task_id, None)

    async def check_task_activity(
        self,
        session: AsyncSession,
        task: Task,
    ) -> TaskActivityStatus:
        """
        检查任务活动状态

        检查顺序：
        1. 心跳信号（从 TimerManager 获取）
        2. updated_at 时间戳
        3. 无活动

        Args:
            session: 数据库会话
            task: 任务对象

        Returns:
            任务活动状态
        """
        now = datetime.now()

        # 第一层：检查心跳信号
        heartbeat_age = self.get_heartbeat_age(task.id)
        if heartbeat_age is not None and heartbeat_age < self.heartbeat_threshold:
            return TaskActivityStatus(
                is_active=True,
                activity_type="heartbeat",
                last_activity_time=datetime.fromtimestamp(time.time() - heartbeat_age),
                age_seconds=heartbeat_age,
                details={"heartbeat_age": heartbeat_age},
            )

        # 第二层：检查数据库 updated_at 时间戳
        updated_at = task.updated_at or task.created_at
        if updated_at:
            age_seconds = (now - updated_at).total_seconds()
            if age_seconds < self.stuck_threshold:
                return TaskActivityStatus(
                    is_active=True,
                    activity_type="database_update",
                    last_activity_time=updated_at,
                    age_seconds=age_seconds,
                    details={"updated_at": updated_at.isoformat()},
                )

        # 无活动
        age_seconds = (now - updated_at).total_seconds() if updated_at else None

        return TaskActivityStatus(
            is_active=False,
            activity_type="none",
            last_activity_time=updated_at,
            age_seconds=age_seconds,
            details={},
        )

    async def check_subtask_activity(
        self,
        session: AsyncSession,
        parent_task_id: str,
    ) -> SubtaskActivityStatus:
        """
        检查子任务活动状态

        Args:
            session: 数据库会话
            parent_task_id: 父任务 ID

        Returns:
            子任务活动状态
        """
        # 查询是否有 running 状态的子任务
        query = select(Task).where(
            and_(
                Task.parent_task_id == parent_task_id,
                Task.status == ExecutionStatus.RUNNING.value,
            )
        )
        result = await session.execute(query)
        active_subtask = result.scalar_one_or_none()

        if active_subtask:
            return SubtaskActivityStatus(
                has_active_subtask=True,
                active_subtask_id=active_subtask.id,
                last_activity=active_subtask.updated_at,
                active_subtask_status=active_subtask.status,
            )

        # 查询最近更新的子任务
        query = (
            select(Task)
            .where(Task.parent_task_id == parent_task_id)
            .order_by(Task.updated_at.desc())
            .limit(1)
        )
        result = await session.execute(query)
        recent_subtask = result.scalar_one_or_none()

        return SubtaskActivityStatus(
            has_active_subtask=False,
            active_subtask_id=recent_subtask.id if recent_subtask else None,
            last_activity=recent_subtask.updated_at if recent_subtask else None,
            active_subtask_status=recent_subtask.status if recent_subtask else None,
        )

    async def handle_confirmed_timeout(
        self,
        session: AsyncSession,
        task: Task,
    ) -> dict[str, Any]:
        """
        确认超时后处理

        Args:
            session: 数据库会话
            task: 任务对象

        Returns:
            处理结果
        """
        task_id = task.id
        task_metadata = task.task_metadata or {}
        max_retries = task_metadata.get("max_retries", task.max_retries or 6)
        retry_count = task_metadata.get("retry_count", 0)

        logger.warning(f"任务 {task_id} 确认超时，重试次数: {retry_count}/{max_retries}")

        if retry_count < max_retries:
            # 可以重试
            task_metadata["retry_count"] = retry_count + 1
            task_metadata["last_timeout_at"] = datetime.now().isoformat()

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
                "task_id": task_id,
                "action": "retry",
                "retry_count": retry_count + 1,
                "max_retries": max_retries,
            }
        # 超过最大重试次数，标记为失败
        task_metadata["failed_at"] = datetime.now().isoformat()
        task_metadata["failure_reason"] = "timeout_max_retries_exceeded"

        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                status="failed",
                task_metadata=task_metadata,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        logger.error(f"任务 {task_id} 超时失败，已达到最大重试次数")

        # 发送通知
        if self.notification_callback:
            try:
                await self.notification_callback(
                    task_id=task_id,
                    project_id=task.parent_task_id,
                    event="task_timeout_failed",
                    message="任务超时失败，已达到最大重试次数",
                    details={
                        "retry_count": retry_count,
                        "max_retries": max_retries,
                    },
                )
            except Exception as e:
                logger.error(f"发送通知失败: {e}")

        return {
            "task_id": task_id,
            "action": "failed",
            "reason": "max_retries_exceeded",
        }

    async def check_task_health_with_activity(
        self,
        session: AsyncSession,
        root_task: Task,
        current_task: Task,
    ) -> dict[str, Any]:
        """
        带活动检测的任务健康检查

        多层检查机制：
        1. 任务状态检查 - 已完成/失败/取消的任务跳过
        2. 活动状态检查 - 检查心跳和数据库更新
        3. 子任务状态检查 - 根任务检查是否有活跃子任务
        4. 确认超时 - 执行超时处理

        Args:
            session: 数据库会话
            root_task: 根任务对象
            current_task: 当前任务对象

        Returns:
            检查结果
        """
        task_id = current_task.id

        # 第一层：任务状态检查
        if current_task.status in ["completed", "failed", "cancelled"]:
            return {
                "action": "skip",
                "reason": f"task_already_{current_task.status}",
                "task_id": task_id,
            }

        # 第二层：活动状态检查
        activity = await self.check_task_activity(session, current_task)
        if activity.is_active:
            logger.debug(
                f"任务 {task_id} 检测到活动: {activity.activity_type}, "
                f"年龄: {activity.age_seconds:.1f}s"
            )
            return {
                "action": "reset_timer",
                "reason": "activity_detected",
                "activity_type": activity.activity_type,
                "task_id": task_id,
            }

        # 第三层：子任务状态检查（仅根任务）
        if current_task.parent_task_id is None:
            subtask = await self.check_subtask_activity(session, current_task.id)
            if subtask.has_active_subtask:
                logger.debug(
                    f"根任务 {task_id} 有活跃子任务: {subtask.active_subtask_id}"
                )
                return {
                    "action": "reset_timer",
                    "reason": "subtask_active",
                    "active_subtask_id": subtask.active_subtask_id,
                    "task_id": task_id,
                }

        # 第四层：确认超时
        return await self.handle_confirmed_timeout(session, current_task)

    async def check_task_health(
        self,
        session,
        root_task: Task,
        current_task: Task,
    ) -> dict[str, Any]:
        """
        检查任务健康状态（超时、卡住）

        使用多层检查机制确保准确性：
        1. 任务状态检查
        2. 活动状态检查
        3. 子任务状态检查
        4. 确认超时

        Args:
            session: 数据库会话
            root_task: 根任务对象
            current_task: 当前任务对象

        Returns:
            检查结果
        """
        # 使用新的多层检查机制
        result = await self.check_task_health_with_activity(
            session, root_task, current_task
        )

        # 如果确认超时，同时检查是否卡住
        if result.get("action") in ["retry", "failed"]:
            task_id = current_task.id
            updated_at = current_task.updated_at or current_task.created_at

            if updated_at:
                idle_seconds = (datetime.now() - updated_at).total_seconds()

                # 检查是否卡住
                if idle_seconds > self.stuck_threshold:
                    await self.handle_stuck_detection(
                        root_task.id, task_id, idle_seconds
                    )

        return result

    async def handle_timeout(
        self,
        task_id: str,
        idle_seconds: float,
    ) -> dict[str, Any]:
        """
        处理任务执行超时

        注意：此方法会创建新的数据库会话，建议使用 check_task_health_with_activity

        Args:
            task_id: 任务 ID
            idle_seconds: 空闲秒数

        Returns:
            处理结果
        """
        logger.warning(f"任务 {task_id} 超时: {idle_seconds:.0f}秒")

        async with managed_session() as session:
            # 获取任务配置
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在"}

            # 使用统一的超时处理逻辑
            return await self.handle_confirmed_timeout(session, task)

    async def handle_stuck_detection(
        self,
        project_id: str,
        task_id: str,
        idle_seconds: float,
    ) -> dict[str, Any]:
        """
        检测任务是否卡住（长时间无输出）

        Args:
            project_id: 项目 ID
            task_id: 任务 ID
            idle_seconds: 空闲秒数

        Returns:
            处理结果
        """
        logger.warning(f"任务 {task_id} 可能卡住: {idle_seconds:.0f}秒无输出")

        # 发送通知
        if self.notification_callback:
            try:
                await self.notification_callback(
                    task_id=task_id,
                    project_id=project_id,
                    event="task_stuck",
                    message="任务长时间无输出，可能卡住",
                    details={
                        "idle_seconds": idle_seconds,
                        "threshold": self.stuck_threshold,
                    },
                )
            except Exception as e:
                logger.error(f"发送通知失败: {e}")

        return {
            "task_id": task_id,
            "project_id": project_id,
            "event": "stuck_detected",
            "idle_seconds": idle_seconds,
        }

    async def check_and_handle_timeout(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        """
        检查并处理任务超时

        使用多层检查机制确保准确性

        Args:
            task_id: 任务 ID

        Returns:
            处理结果
        """
        async with managed_session() as session:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在"}

            # 使用多层检查机制
            return await self.check_task_health_with_activity(session, task, task)
