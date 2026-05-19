"""
任务仓储（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

from typing import Any

from src.db.models.task import Task
from src.db.repositories.base import BaseRepository


class TaskRepository(BaseRepository[Task]):
    """任务仓储"""

    def __init__(self, session: Any = None):
        super().__init__(session=session, model_class=Task)

    async def get_by_status(self, status: str, limit: int = 100) -> list[Task]:
        """按状态查询任务。"""
        return [t for t in self._store.values() if getattr(t, "status", None) == status][:limit]

    async def get_by_parent(self, parent_task_id: str) -> list[Task]:
        """按父任务 ID 查询子任务。"""
        return [
            t for t in self._store.values() if getattr(t, "parent_task_id", None) == parent_task_id
        ]

    async def get_by_session(self, session_id: str) -> list[Task]:
        """按会话 ID 查询任务。"""
        return [
            t for t in self._store.values() if getattr(t, "session_id", None) == session_id
        ]
