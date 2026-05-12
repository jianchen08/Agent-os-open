"""
基础仓储类

提供通用的数据库操作方法
"""

from typing import Any, Generic, TypeVar, Union
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")
EntityId = Union[str, UUID]


class BaseRepository(Generic[T]):
    """基础仓储类"""

    def __init__(self, session: AsyncSession, model_class: type[T]):
        self.session = session
        self.model_class = model_class

    async def create(self, **kwargs) -> T:
        """创建实体"""
        entity = self.model_class(**kwargs)
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def get(self, id: EntityId) -> T | None:
        """根据ID获取实体"""
        return await self.session.get(self.model_class, id)

    async def get_by(self, **kwargs) -> T | None:
        """根据条件获取单个实体"""
        query = select(self.model_class)
        for key, value in kwargs.items():
            query = query.where(getattr(self.model_class, key) == value)

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_all(
        self, limit: int = 100, offset: int = 0, order_by: str | None = None
    ) -> list[T]:
        """获取所有实体（优化版）"""
        query = select(self.model_class)

        # 添加排序以利用索引
        if order_by and hasattr(self.model_class, order_by):
            query = query.order_by(getattr(self.model_class, order_by))
        elif hasattr(self.model_class, "created_at"):
            query = query.order_by(self.model_class.created_at.desc())
        elif hasattr(self.model_class, "id"):
            query = query.order_by(self.model_class.id)

        query = query.limit(limit).offset(offset)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def find_by(
        self,
        limit: int = 100,
        offset: int = 0,
        order_by: str | None = None,
        **kwargs,
    ) -> list[T]:
        """根据条件查找实体（优化版）"""
        query = select(self.model_class)

        # 构建WHERE条件
        for key, value in kwargs.items():
            if hasattr(self.model_class, key):
                query = query.where(getattr(self.model_class, key) == value)

        # 添加排序以利用索引
        if order_by and hasattr(self.model_class, order_by):
            query = query.order_by(getattr(self.model_class, order_by))
        elif hasattr(self.model_class, "created_at"):
            query = query.order_by(self.model_class.created_at.desc())
        elif hasattr(self.model_class, "id"):
            query = query.order_by(self.model_class.id)

        query = query.limit(limit).offset(offset)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def update(self, id: EntityId, data: dict[str, Any]) -> bool:
        """更新实体"""
        query = update(self.model_class).where(self.model_class.id == id).values(**data)

        result = await self.session.execute(query)
        return result.rowcount > 0

    async def delete(self, id: EntityId) -> bool:
        """删除实体"""
        query = delete(self.model_class).where(self.model_class.id == id)

        result = await self.session.execute(query)
        return result.rowcount > 0

    async def count(self, **kwargs) -> int:
        """统计实体数量"""
        query = select(func.count(self.model_class.id))
        for key, value in kwargs.items():
            query = query.where(getattr(self.model_class, key) == value)

        result = await self.session.execute(query)
        return result.scalar() or 0
