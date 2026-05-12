"""
通知仓储类

提供通知数据的数据库操作接口
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Notification
from src.db.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    """通知仓储"""

    def __init__(self, session: AsyncSession):
        super().__init__(session, Notification)

    async def create_notification(
        self,
        user_id: str,
        title: str,
        message: str,
        notification_type: str = "info",
        priority: str = "normal",
    ) -> Notification:
        """
        创建新通知

        Args:
            user_id: 用户ID
            title: 通知标题
            message: 通知内容
            notification_type: 通知类型 (info, warning, error, success)
            priority: 优先级 (low, normal, high, urgent)

        Returns:
            创建的通知对象
        """
        notification = Notification(
            user_id=user_id,
            title=title,
            message=message,
            notification_type=notification_type,
            priority=priority,
        )

        self.session.add(notification)
        await self.session.flush()
        await self.session.refresh(notification)

        return notification

    async def get_user_notifications(
        self, user_id: str, limit: int = 10, offset: int = 0, unread_only: bool = False
    ) -> list[Notification]:
        """
        获取用户通知列表

        Args:
            user_id: 用户ID
            limit: 返回数量限制
            offset: 偏移量
            unread_only: 是否只返回未读通知

        Returns:
            通知列表
        """
        query = select(Notification).where(Notification.user_id == user_id)

        if unread_only:
            query = query.where(not Notification.read)

        query = (
            query.order_by(desc(Notification.created_at)).limit(limit).offset(offset)
        )

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_unread_notifications(
        self, user_id: str, limit: int = 10
    ) -> list[Notification]:
        """
        获取用户未读且未推送的通知

        Args:
            user_id: 用户ID
            limit: 返回数量限制

        Returns:
            未读通知列表
        """
        query = (
            select(Notification)
            .where(
                and_(
                    Notification.user_id == user_id,
                    not Notification.read,
                    not Notification.pushed,
                )
            )
            .order_by(desc(Notification.created_at), desc(Notification.id))
            .limit(limit)
        )

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_unpushed_notifications(
        self, user_id: str, limit: int = 10
    ) -> list[Notification]:
        """
        获取用户未推送通知

        Args:
            user_id: 用户ID
            limit: 返回数量限制

        Returns:
            未推送通知列表
        """
        query = (
            select(Notification)
            .where(and_(Notification.user_id == user_id, not Notification.pushed))
            .order_by(desc(Notification.created_at))
            .limit(limit)
        )

        result = await self.session.execute(query)
        return result.scalars().all()

    async def mark_as_read(self, notification_id: str) -> bool:
        """
        标记通知为已读

        Args:
            notification_id: 通知ID

        Returns:
            是否成功
        """
        result = await self.session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(read=True, updated_at=datetime.now())
        )

        return result.rowcount > 0

    async def mark_as_pushed(self, notification_id: str) -> bool:
        """
        标记通知为已推送

        Args:
            notification_id: 通知ID

        Returns:
            是否成功
        """
        result = await self.session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(pushed=True, updated_at=datetime.now())
        )

        return result.rowcount > 0

    async def mark_multiple_as_read(self, notification_ids: list[str]) -> int:
        """
        批量标记通知为已读

        Args:
            notification_ids: 通知ID列表

        Returns:
            成功标记的数量
        """
        result = await self.session.execute(
            update(Notification)
            .where(Notification.id.in_(notification_ids))
            .values(read=True, updated_at=datetime.now())
        )

        return result.rowcount

    async def mark_multiple_as_pushed(self, notification_ids: list[str]) -> int:
        """
        批量标记通知为已推送

        Args:
            notification_ids: 通知ID列表

        Returns:
            成功标记的数量
        """
        result = await self.session.execute(
            update(Notification)
            .where(Notification.id.in_(notification_ids))
            .values(pushed=True, updated_at=datetime.now())
        )

        return result.rowcount

    async def count_unread_notifications(self, user_id: str) -> int:
        """
        统计用户未读通知数量

        Args:
            user_id: 用户ID

        Returns:
            未读通知数量
        """
        result = await self.session.execute(
            select(func.count(Notification.id)).where(
                and_(Notification.user_id == user_id, not Notification.read)
            )
        )

        return result.scalar() or 0

    async def delete_old_notifications(self, user_id: str, days_old: int = 30) -> int:
        """
        删除用户的旧通知

        Args:
            user_id: 用户ID
            days_old: 保留天数

        Returns:
            删除的通知数量
        """

        cutoff_date = datetime.now() - timedelta(days=days_old)

        result = await self.session.execute(
            select(Notification.id).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.created_at < cutoff_date,
                    Notification.read,  # 只删除已读通知
                )
            )
        )

        notification_ids = [row[0] for row in result.fetchall()]

        if notification_ids:
            await self.session.execute(
                Notification.__table__.delete().where(
                    Notification.id.in_(notification_ids)
                )
            )

        return len(notification_ids)
