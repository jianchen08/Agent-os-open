"""
通知仓储（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

from typing import Any

from src.db.models.notification import Notification
from src.db.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    """通知仓储"""

    def __init__(self, session: Any = None):
        super().__init__(session=session, model_class=Notification)

    async def get_unread(self, user_id: str) -> list[Notification]:
        """获取用户未读通知。"""
        return [
            n
            for n in self._store.values()
            if getattr(n, "user_id", None) == user_id and not getattr(n, "read", False)
        ]
