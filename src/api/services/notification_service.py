"""
通知服务模块

提供系统通知的创建、查询和管理功能
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.constants import QueryLimits
from src.db.connection import get_async_session
from src.db.models import Notification
from src.db.repositories.notification_repository import NotificationRepository
from src.services.base import BaseService

logger = logging.getLogger(__name__)


class NotificationService(BaseService):
    """通知服务"""

    def __init__(self, session: AsyncSession | None = None):
        super().__init__(session)
        self._repository: NotificationRepository | None = None

    async def _get_repository(self) -> NotificationRepository:
        """获取通知仓储"""
        if self._repository is None:
            session = await self._get_session()
            self._repository = NotificationRepository(session)
        return self._repository

    async def create_notification(
        self,
        user_id: UUID,
        title: str,
        message: str,
        notification_type: str = "info",
        priority: str = "normal",
    ) -> dict:
        """
        创建新通知

        Args:
            user_id: 用户ID
            title: 通知标题
            message: 通知内容
            notification_type: 通知类型 (info, warning, error, success)
            priority: 优先级 (low, normal, high, urgent)

        Returns:
            通知数据字典
        """
        try:
            repository = await self._get_repository()
            notification = await repository.create_notification(
                user_id=str(user_id),
                title=title,
                message=message,
                notification_type=notification_type,
                priority=priority,
            )

            await self._commit_transaction()

            logger.info(
                f"[NotificationService] 创建通知: 用户={user_id}, "
                f"类型={notification_type}, 标题={title}"
            )

            return self._notification_to_dict(notification)

        except Exception as e:
            await self._rollback_transaction()
            logger.error(f"[NotificationService] 创建通知失败: {e}")
            raise

    async def get_unread_notifications(
        self, user_id: UUID, limit: int = 10
    ) -> list[dict]:
        """
        获取用户未读通知（未读且未推送）

        Args:
            user_id: 用户ID
            limit: 返回数量限制

        Returns:
            未读通知列表（pushed=False）
        """
        try:
            repository = await self._get_repository()
            notifications = await repository.get_unread_notifications(
                user_id=str(user_id), limit=limit
            )

            return [self._notification_to_dict(notif) for notif in notifications]

        except Exception as e:
            logger.error(f"[NotificationService] 获取未读通知失败: {e}")
            raise

    async def get_unpushed_notifications(
        self, user_id: UUID, limit: int = 10
    ) -> list[dict]:
        """
        获取用户未推送通知

        Args:
            user_id: 用户ID
            limit: 返回数量限制

        Returns:
            未推送通知列表
        """
        try:
            repository = await self._get_repository()
            notifications = await repository.get_unpushed_notifications(
                user_id=str(user_id), limit=limit
            )

            return [self._notification_to_dict(notif) for notif in notifications]

        except Exception as e:
            logger.error(f"[NotificationService] 获取未推送通知失败: {e}")
            raise

    async def get_user_notifications(
        self, user_id: UUID, limit: int = 20, offset: int = 0, unread_only: bool = False
    ) -> list[dict]:
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
        try:
            repository = await self._get_repository()
            notifications = await repository.get_user_notifications(
                user_id=str(user_id),
                limit=limit,
                offset=offset,
                unread_only=unread_only,
            )

            return [self._notification_to_dict(notif) for notif in notifications]

        except Exception as e:
            logger.error(f"[NotificationService] 获取用户通知失败: {e}")
            raise

    async def mark_as_read(self, notification_id: str, user_id: UUID) -> bool:
        """
        标记通知为已读

        Args:
            notification_id: 通知ID
            user_id: 用户ID（用于验证通知所有权）

        Returns:
            是否成功
        """
        try:
            repository = await self._get_repository()
            # TODO: 可以添加验证逻辑，确保通知属于该用户
            success = await repository.mark_as_read(notification_id)

            if success:
                await self._commit_transaction()
                logger.info(
                    f"[NotificationService] 标记通知已读: "
                    f"{notification_id}, 用户={user_id}"
                )

            return success

        except Exception as e:
            await self._rollback_transaction()
            logger.error(f"[NotificationService] 标记通知已读失败: {e}")
            raise

    async def mark_as_pushed(self, notification_id: str) -> bool:
        """
        标记通知为已推送

        Args:
            notification_id: 通知ID

        Returns:
            是否成功
        """
        try:
            repository = await self._get_repository()
            success = await repository.mark_as_pushed(notification_id)

            if success:
                await self._commit_transaction()
                logger.info(f"[NotificationService] 标记通知已推送: {notification_id}")

            return success

        except Exception as e:
            await self._rollback_transaction()
            logger.error(f"[NotificationService] 标记通知已推送失败: {e}")
            raise

    async def mark_multiple_as_read(self, notification_ids: list[str]) -> int:
        """
        批量标记通知为已读

        Args:
            notification_ids: 通知ID列表

        Returns:
            成功标记的数量
        """
        try:
            repository = await self._get_repository()
            count = await repository.mark_multiple_as_read(notification_ids)

            if count > 0:
                await self._commit_transaction()
                logger.info(f"[NotificationService] 批量标记通知已读: {count}条")

            return count

        except Exception as e:
            await self._rollback_transaction()
            logger.error(f"[NotificationService] 批量标记通知已读失败: {e}")
            raise

    async def count_unread_notifications(self, user_id: UUID) -> int:
        """
        统计用户未读通知数量

        Args:
            user_id: 用户ID

        Returns:
            未读通知数量
        """
        try:
            repository = await self._get_repository()
            count = await repository.count_unread_notifications(str(user_id))
            return count

        except Exception as e:
            logger.error(f"[NotificationService] 统计未读通知失败: {e}")
            raise

    async def get_notification_stats(self, user_id: UUID) -> dict:
        """
        获取用户通知统计信息

        Args:
            user_id: 用户ID

        Returns:
            统计信息字典，包含:
            - total: 总通知数
            - read: 已读数量
            - unread: 未读数量
            - by_type: 按类型分组的统计
        """
        try:
            repository = await self._get_repository()

            # 获取所有通知
            all_notifications = await repository.get_user_notifications(
                user_id=str(user_id), limit=QueryLimits.NOTIFICATION_QUERY_LIMIT
            )

            # 统计各类型数量
            total = len(all_notifications)
            read_count = sum(1 for n in all_notifications if n.read)
            unread_count = total - read_count

            # 按类型分组
            by_type = {}
            for notif in all_notifications:
                notif_type = notif.notification_type
                by_type[notif_type] = by_type.get(notif_type, 0) + 1

            return {
                "total": total,
                "read": read_count,
                "unread": unread_count,
                "by_type": by_type,
            }

        except Exception as e:
            logger.error(f"[NotificationService] 获取通知统计失败: {e}")
            raise

    async def cleanup_old_notifications(self, user_id: UUID, days: int = 30) -> int:
        """
        清理用户的旧通知

        Args:
            user_id: 用户ID
            days: 保留天数

        Returns:
            删除的通知数量
        """
        try:
            repository = await self._get_repository()
            count = await repository.delete_old_notifications(
                user_id=str(user_id), days_old=days
            )

            if count > 0:
                await self._commit_transaction()
                logger.info(f"[NotificationService] 清理旧通知: {count}条")

            return count

        except Exception as e:
            await self._rollback_transaction()
            logger.error(f"[NotificationService] 清理旧通知失败: {e}")
            raise

    def _notification_to_dict(self, notification: Notification) -> dict:
        """
        将通知对象转换为字典

        Args:
            notification: 通知对象

        Returns:
            通知数据字典
        """
        from src.utils.converters import to_dict

        # 使用通用转换器，但保持原有的字段映射
        to_dict(notification)

        # 确保字段名称映射正确
        return {
            "id": notification.id,
            "user_id": notification.user_id,
            "title": notification.title,
            "message": notification.message,
            "type": notification.notification_type,
            "priority": notification.priority,
            "read": notification.read,
            "pushed": notification.pushed,
            "created_at": notification.created_at,
            "updated_at": notification.updated_at,
        }


# 创建通知服务的工厂函数
async def create_notification_service() -> NotificationService:
    """
    创建通知服务实例

    Returns:
        配置好的通知服务实例
    """
    async for session in get_async_session():
        return NotificationService(session)


async def create_system_notification(
    user_id: UUID, title: str, message: str, notification_type: str = "info"
) -> dict:
    """
    创建系统通知的便捷函数

    Args:
        user_id: 用户ID
        title: 通知标题
        message: 通知内容
        notification_type: 通知类型
    """
    async for session in get_async_session():
        service = NotificationService(session)
        return await service.create_notification(
            user_id=user_id,
            title=title,
            message=message,
            notification_type=notification_type,
            priority="normal",
        )
