"""
执行记录仓储（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

from typing import Any

from src.db.models.execution import ExecutionRecord
from src.db.repositories.base import BaseRepository


class ExecutionRecordRepository(BaseRepository[ExecutionRecord]):
    """执行记录仓储"""

    def __init__(self, session: Any = None):
        super().__init__(session=session, model_class=ExecutionRecord)

    async def get_by_session(self, session_id: str) -> list[ExecutionRecord]:
        """按会话 ID 查询执行记录。"""
        return [
            r
            for r in self._store.values()
            if getattr(r, "session_id", None) == session_id
        ]

    async def get_children(self, parent_record_id: str) -> list[ExecutionRecord]:
        """查询子记录。"""
        return [
            r
            for r in self._store.values()
            if getattr(r, "parent_record_id", None) == parent_record_id
        ]
