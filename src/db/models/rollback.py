"""
回滚机制模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base


class RollbackCheckpoint(Base):
    """回滚检查点"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.task_id = kwargs.get("task_id", "")
        self.name = kwargs.get("name")
        self.description = kwargs.get("description")
        self.checkpoint_metadata = kwargs.get("checkpoint_metadata", {})
        self.created_at = kwargs.get("created_at", datetime.now())


class RollbackOperationLog(Base):
    """回滚操作日志"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.task_id = kwargs.get("task_id", "")
        self.checkpoint_id = kwargs.get("checkpoint_id")
        self.tool_name = kwargs.get("tool_name", "")
        self.operation_type = kwargs.get("operation_type", "")
        self.target = kwargs.get("target")
        self.params = kwargs.get("params")
        self.before_state = kwargs.get("before_state")
        self.after_state = kwargs.get("after_state")
        self.reversible = kwargs.get("reversible", True)
        self.reverse_action = kwargs.get("reverse_action")
        self.sequence = kwargs.get("sequence", 0)
        self.status = kwargs.get("status", "executed")
        self.created_at = kwargs.get("created_at", datetime.now())
