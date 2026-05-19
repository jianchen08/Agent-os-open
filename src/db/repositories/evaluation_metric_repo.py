"""
评估指标仓储（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

from typing import Any

from src.db.models.task import EvaluationMetric
from src.db.repositories.base import BaseRepository


class EvaluationMetricRepository(BaseRepository[EvaluationMetric]):
    """评估指标仓储"""

    def __init__(self, session: Any = None):
        super().__init__(session=session, model_class=EvaluationMetric)

    async def get_by_name(self, name: str) -> EvaluationMetric | None:
        """按名称查询指标。"""
        for metric in self._store.values():
            if getattr(metric, "name", None) == name:
                return metric
        return None
