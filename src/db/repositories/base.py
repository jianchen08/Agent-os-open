"""
基础仓储类（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

import logging
from typing import Any, Generic, TypeVar
from uuid import UUID

logger = logging.getLogger(__name__)

T = TypeVar("T")
EntityId = str | UUID

# 内存存储（降级用）
_store: dict[str, dict[str, Any]] = {}


class BaseRepository(Generic[T]):
    """基础仓储类（降级存根，使用内存字典存储）"""

    def __init__(self, session: Any = None, model_class: type[T] | None = None):
        self.session = session
        self.model_class = model_class
        self._store: dict[str, T] = {}

    async def create(self, **kwargs) -> T:
        """创建实体。"""
        if self.model_class is None:
            raise ValueError("model_class not set")
        entity = self.model_class(**kwargs)
        entity_id = getattr(entity, "id", str(id(entity)))
        self._store[str(entity_id)] = entity
        return entity

    async def get(self, id: EntityId) -> T | None:
        """根据 ID 获取实体。"""
        return self._store.get(str(id))

    async def get_by(self, **kwargs) -> T | None:
        """根据条件获取单个实体。"""
        for entity in self._store.values():
            match = all(getattr(entity, k, None) == v for k, v in kwargs.items())
            if match:
                return entity
        return None

    async def get_multi(self, **kwargs) -> list[T]:
        """根据条件获取多个实体。"""
        results = []
        for entity in self._store.values():
            match = all(getattr(entity, k, None) == v for k, v in kwargs.items())
            if match:
                results.append(entity)
        return results

    async def update(self, id: EntityId, **kwargs) -> T | None:
        """更新实体。"""
        entity = self._store.get(str(id))
        if entity is None:
            return None
        for key, value in kwargs.items():
            setattr(entity, key, value)
        return entity

    async def delete(self, id: EntityId) -> bool:
        """删除实体。"""
        sid = str(id)
        if sid in self._store:
            del self._store[sid]
            return True
        return False

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[T]:
        """列出所有实体。"""
        items = list(self._store.values())
        return items[offset : offset + limit]

    async def count(self, **kwargs) -> int:
        """统计实体数量。"""
        if not kwargs:
            return len(self._store)
        return sum(
            1
            for entity in self._store.values()
            if all(getattr(entity, k, None) == v for k, v in kwargs.items())
        )
