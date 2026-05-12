"""
用户仓储

提供用户数据的访问接口
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import UserCreate, UserInDB
from src.db.models_sqlite import SQLiteUser
from src.db.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    """用户仓储类 - 统一实现"""

    def __init__(self, session: AsyncSession):
        super().__init__(session, SQLiteUser)
        self.model_class = SQLiteUser

    async def get_by_id(self, user_id: uuid.UUID) -> UserInDB | None:
        """根据ID获取用户"""
        result = await self.session.execute(
            select(self.model_class).where(self.model_class.id == str(user_id))
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None

        return UserInDB(
            id=user_id,
            username=user.username,
            email="",  # SQLite 版本暂不支持加密邮箱
            password_hash=user.password_hash,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=None,
        )

    async def get_by_username(self, username: str) -> UserInDB | None:
        """根据用户名获取用户"""
        result = await self.session.execute(
            select(self.model_class).where(self.model_class.username == username)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None

        return UserInDB(
            id=(
                uuid.UUID(user.id)
                if len(user.id.replace("-", "")) == 32
                else uuid.uuid4()
            ),
            username=user.username,
            email="",
            password_hash=user.password_hash,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=None,
        )

    async def create(self, user_create: UserCreate, password_hash: str) -> UserInDB:
        """创建用户"""
        user = SQLiteUser(
            id=str(uuid.uuid4()),
            username=user_create.username,
            password_hash=password_hash,
            role=user_create.role or "user",
            is_active=True,
        )

        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)

        return UserInDB(
            id=uuid.UUID(user.id),
            username=user.username,
            email=user_create.email or "",
            password_hash=user.password_hash,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=None,
        )

    async def update_last_login(self, user_id: uuid.UUID) -> None:
        """更新最后登录时间"""
        result = await self.session.execute(
            select(self.model_class).where(self.model_class.id == str(user_id))
        )
        user = result.scalar_one_or_none()
        if user is not None:
            # SQLite版本暂时不存储last_login字段，这里只是为了满足协议
            await self.session.flush()

    async def update_role(self, user_id: uuid.UUID, new_role: str) -> UserInDB | None:
        """更新用户角色"""
        result = await self.session.execute(
            select(self.model_class).where(self.model_class.id == str(user_id))
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None

        user.role = new_role
        await self.session.flush()
        await self.session.refresh(user)

        return UserInDB(
            id=uuid.UUID(user.id),
            username=user.username,
            email="",
            password_hash=user.password_hash,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=None,
        )
