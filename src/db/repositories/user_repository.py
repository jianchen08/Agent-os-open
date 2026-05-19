"""
用户仓储（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

from typing import Any

from src.db.models.user import User
from src.db.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """用户仓储"""

    def __init__(self, session: Any = None):
        super().__init__(session=session, model_class=User)

    async def get_by_username(self, username: str) -> User | None:
        """按用户名查询用户。"""
        for user in self._store.values():
            if getattr(user, "username", None) == username:
                return user
        return None
