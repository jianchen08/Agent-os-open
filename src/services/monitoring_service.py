"""
监控服务模块

提供系统监控、告警管理和任务队列统计功能
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import MonitoringAlert, Task, TaskQueueStats
from src.services.base import BaseService

logger = logging.getLogger(__name__)


class MonitoringService(BaseService):
    """监控服务"""

    def __init__(self, session: AsyncSession | None = None):
        super().__init__(session)

    # ============================================================================
    # 告警管理
    # ============================================================================

    async def create_alert(
        self,
        alert_type: str,
        level: str,
        title: str,
        message: str,
        user_id: str | None = None,
        usage_percent: float | None = None,
        threshold: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MonitoringAlert:
        """
        创建监控告警

        Args:
            alert_type: 告警类型 (usage, performance, error)
            level: 告警级别 (info, warning, error, critical)
            title: 告警标题
            message: 告警消息
            user_id: 用户ID（可选）
            usage_percent: 使用百分比（可选）
            threshold: 阈值（可选）
            metadata: 元数据（可选）

        Returns:
            创建的告警对象
        """
        session = await self._get_session()

        alert = MonitoringAlert(
            user_id=user_id,
            alert_type=alert_type,
            level=level,
            title=title,
            message=message,
            usage_percent=usage_percent,
            threshold=threshold,
            metadata=metadata or {},
        )

        session.add(alert)
        await session.flush()
        await session.refresh(alert)

        logger.info(f"[MonitoringService] 创建告警: {level} - {title}")

        return alert

    async def get_alerts(
        self,
        user_id: str | None = None,
        alert_type: str | None = None,
        level: str | None = None,
        acknowledged: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MonitoringAlert]:
        """
        获取告警列表

        Args:
            user_id: 用户ID过滤
            alert_type: 告警类型过滤
            level: 告警级别过滤
            acknowledged: 确认状态过滤
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            告警列表
        """
        session = await self._get_session()

        query = select(MonitoringAlert)

        conditions = []
        if user_id:
            conditions.append(MonitoringAlert.user_id == user_id)
        if alert_type:
            conditions.append(MonitoringAlert.alert_type == alert_type)
        if level:
            conditions.append(MonitoringAlert.level == level)
        if acknowledged is not None:
            conditions.append(MonitoringAlert.acknowledged == acknowledged)

        if conditions:
            query = query.where(and_(*conditions))

        query = (
            query.order_by(desc(MonitoringAlert.created_at)).limit(limit).offset(offset)
        )

        result = await session.execute(query)
        return result.scalars().all()

    async def acknowledge_alert(self, alert_id: str, acknowledged_by: str) -> bool:
        """
        确认告警

        Args:
            alert_id: 告警ID
            acknowledged_by: 确认人ID

        Returns:
            是否成功
        """
        session = await self._get_session()

        alert = await session.get(MonitoringAlert, alert_id)
        if not alert:
            return False

        alert.acknowledged = True
        alert.acknowledged_at = datetime.now()
        alert.acknowledged_by = acknowledged_by
        alert.updated_at = datetime.now()

        await session.flush()

        logger.info(f"[MonitoringService] 确认告警: {alert_id}")

        return True

    async def resolve_alert(self, alert_id: str) -> bool:
        """
        解决告警

        Args:
            alert_id: 告警ID

        Returns:
            是否成功
        """
        session = await self._get_session()

        alert = await session.get(MonitoringAlert, alert_id)
        if not alert:
            return False

        alert.resolved = True
        alert.resolved_at = datetime.now()
        alert.updated_at = datetime.now()

        await session.flush()

        logger.info(f"[MonitoringService] 解决告警: {alert_id}")

        return True

    async def count_unacknowledged_alerts(self, user_id: str | None = None) -> int:
        """
        统计未确认告警数量

        Args:
            user_id: 用户ID过滤

        Returns:
            未确认告警数量
        """
        session = await self._get_session()

        query = select(func.count(MonitoringAlert.id)).where(
            not MonitoringAlert.acknowledged
        )

        if user_id:
            query = query.where(MonitoringAlert.user_id == user_id)

        result = await session.execute(query)
        return result.scalar() or 0

    # ============================================================================
    # 任务队列统计
    # ============================================================================

    async def record_task_queue_stats(
        self,
        total_tasks: int,
        pending_tasks: int,
        running_tasks: int,
        completed_tasks: int,
        failed_tasks: int,
        avg_wait_time: float | None = None,
        avg_execution_time: float | None = None,
        queue_depth: int = 0,
        active_workers: int = 0,
    ) -> TaskQueueStats:
        """
        记录任务队列统计数据

        Args:
            total_tasks: 总任务数
            pending_tasks: 待处理任务数
            running_tasks: 运行中任务数
            completed_tasks: 已完成任务数
            failed_tasks: 失败任务数
            avg_wait_time: 平均等待时间
            avg_execution_time: 平均执行时间
            queue_depth: 队列深度
            active_workers: 活跃工作者数量

        Returns:
            统计记录对象
        """
        session = await self._get_session()

        stats = TaskQueueStats(
            total_tasks=total_tasks,
            pending_tasks=pending_tasks,
            running_tasks=running_tasks,
            completed_tasks=completed_tasks,
            failed_tasks=failed_tasks,
            avg_wait_time=avg_wait_time,
            avg_execution_time=avg_execution_time,
            queue_depth=queue_depth,
            active_workers=active_workers,
        )

        session.add(stats)
        await session.flush()
        await session.refresh(stats)

        return stats

    async def get_current_task_queue_stats(self) -> dict[str, Any]:
        """
        获取当前任务队列统计

        Returns:
            任务队列统计数据
        """
        session = await self._get_session()

        # 统计各状态任务数量
        total_query = select(func.count(Task.id))
        pending_query = select(func.count(Task.id)).where(Task.status == "pending")
        running_query = select(func.count(Task.id)).where(Task.status == "running")
        completed_query = select(func.count(Task.id)).where(Task.status == "completed")
        failed_query = select(func.count(Task.id)).where(Task.status == "failed")

        total_tasks = (await session.execute(total_query)).scalar() or 0
        pending_tasks = (await session.execute(pending_query)).scalar() or 0
        running_tasks = (await session.execute(running_query)).scalar() or 0
        completed_tasks = (await session.execute(completed_query)).scalar() or 0
        failed_tasks = (await session.execute(failed_query)).scalar() or 0

        # 计算平均等待时间和执行时间
        avg_wait_time = await self._calculate_avg_wait_time()
        avg_execution_time = await self._calculate_avg_execution_time()

        # 统计活跃会话数（作为活跃工作者的代理）
        # 活跃会话 = 有未完成的 execution_record 的会话
        from src.db.models import ExecutionRecord

        active_sessions_query = select(
            func.count(func.distinct(ExecutionRecord.session_id))
        ).where(
            func.json_extract(ExecutionRecord.message_data, "$.status").in_(
                ["pending", "running"]
            )
        )
        active_workers = (await session.execute(active_sessions_query)).scalar() or 0

        stats = {
            "total_tasks": total_tasks,
            "pending_tasks": pending_tasks,
            "running_tasks": running_tasks,
            "completed_tasks": completed_tasks,
            "failed_tasks": failed_tasks,
            "avg_wait_time": avg_wait_time,
            "avg_execution_time": avg_execution_time,
            "queue_depth": pending_tasks,  # 队列深度等于待处理任务数
            "active_workers": active_workers,
            "timestamp": datetime.now(),
        }

        # 记录统计数据
        await self.record_task_queue_stats(
            **{k: v for k, v in stats.items() if k != "timestamp"}
        )

        return stats

    async def _calculate_avg_wait_time(self) -> float | None:
        """计算平均等待时间"""
        session = await self._get_session()

        # 查询最近完成的任务的等待时间
        query = select(
            func.avg(func.extract("epoch", Task.started_at - Task.created_at))
        ).where(
            and_(
                Task.started_at.isnot(None),
                Task.created_at >= datetime.now() - timedelta(hours=24),  # 最近24小时
            )
        )

        result = await session.execute(query)
        return result.scalar()

    async def _calculate_avg_execution_time(self) -> float | None:
        """计算平均执行时间"""
        session = await self._get_session()

        # 查询最近完成的任务的执行时间
        query = select(
            func.avg(func.extract("epoch", Task.completed_at - Task.started_at))
        ).where(
            and_(
                Task.completed_at.isnot(None),
                Task.started_at.isnot(None),
                Task.completed_at >= datetime.now() - timedelta(hours=24),  # 最近24小时
            )
        )

        result = await session.execute(query)
        return result.scalar()

    async def get_task_queue_history(
        self, hours: int = 24, limit: int = 100
    ) -> list[TaskQueueStats]:
        """
        获取任务队列历史统计

        Args:
            hours: 查询小时数
            limit: 返回数量限制

        Returns:
            历史统计列表
        """
        session = await self._get_session()

        cutoff_time = datetime.now() - timedelta(hours=hours)

        query = (
            select(TaskQueueStats)
            .where(TaskQueueStats.timestamp >= cutoff_time)
            .order_by(desc(TaskQueueStats.timestamp))
            .limit(limit)
        )

        result = await session.execute(query)
        return result.scalars().all()
