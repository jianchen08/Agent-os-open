"""
触发器系统模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base


class Trigger(Base):
    """触发器"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description")
        self.trigger_type = kwargs.get("trigger_type", "")
        self.enabled = kwargs.get("enabled", True)
        self.config = kwargs.get("config", {})
        self.trigger_metadata = kwargs.get("trigger_metadata", {})
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
        self.last_triggered_at = kwargs.get("last_triggered_at")
        self.execution_count = kwargs.get("execution_count", 0)
        self.success_count = kwargs.get("success_count", 0)
        self.failure_count = kwargs.get("failure_count", 0)
        self.actions = kwargs.get("actions", [])
        self.execution_logs = kwargs.get("execution_logs", [])


class TriggerAction(Base):
    """触发器动作"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.trigger_id = kwargs.get("trigger_id", "")
        self.action_type = kwargs.get("action_type", "")
        self.config = kwargs.get("config", {})
        self.order = kwargs.get("order", 0)
        self.created_at = kwargs.get("created_at", datetime.now())
        self.trigger = kwargs.get("trigger")


class TriggerExecutionLog(Base):
    """触发器执行日志"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.trigger_id = kwargs.get("trigger_id", "")
        self.status = kwargs.get("status", "")
        self.result = kwargs.get("result")
        self.error_message = kwargs.get("error_message")
        self.context = kwargs.get("context")
        self.triggered_at = kwargs.get("triggered_at", datetime.now())
        self.completed_at = kwargs.get("completed_at")
        self.duration_ms = kwargs.get("duration_ms")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.trigger = kwargs.get("trigger")
