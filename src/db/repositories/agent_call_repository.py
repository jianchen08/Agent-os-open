"""
Agent 调用记录仓储（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

from typing import Any

from src.db.models.experience import AgentCallRecord
from src.db.repositories.base import BaseRepository


class AgentCallRepository(BaseRepository[AgentCallRecord]):
    """Agent 调用记录仓储"""

    def __init__(self, session: Any = None):
        super().__init__(session=session, model_class=AgentCallRecord)
