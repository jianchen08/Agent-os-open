"""
监控和用量统计模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base


class MonitoringAlert(Base):
    """监控告警"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id")
        self.alert_type = kwargs.get("alert_type", "")
        self.level = kwargs.get("level", "")
        self.title = kwargs.get("title", "")
        self.message = kwargs.get("message", "")
        self.usage_percent = kwargs.get("usage_percent")
        self.threshold = kwargs.get("threshold")
        self.acknowledged = kwargs.get("acknowledged", False)
        self.acknowledged_at = kwargs.get("acknowledged_at")
        self.acknowledged_by = kwargs.get("acknowledged_by")
        self.resolved = kwargs.get("resolved", False)
        self.resolved_at = kwargs.get("resolved_at")
        self.alert_metadata = kwargs.get("alert_metadata")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")


class TaskQueueStats(Base):
    """任务队列统计"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.timestamp = kwargs.get("timestamp", datetime.now())
        self.total_tasks = kwargs.get("total_tasks", 0)
        self.pending_tasks = kwargs.get("pending_tasks", 0)
        self.running_tasks = kwargs.get("running_tasks", 0)
        self.completed_tasks = kwargs.get("completed_tasks", 0)
        self.failed_tasks = kwargs.get("failed_tasks", 0)
        self.avg_wait_time = kwargs.get("avg_wait_time")
        self.avg_execution_time = kwargs.get("avg_execution_time")
        self.queue_depth = kwargs.get("queue_depth", 0)
        self.active_workers = kwargs.get("active_workers", 0)


class UsageRecord(Base):
    """用量记录"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id")
        self.session_id = kwargs.get("session_id")
        self.prompt_tokens = kwargs.get("prompt_tokens", 0)
        self.completion_tokens = kwargs.get("completion_tokens", 0)
        self.total_tokens = kwargs.get("total_tokens", 0)
        self.model = kwargs.get("model", "")
        self.request_id = kwargs.get("request_id")
        self.created_at = kwargs.get("created_at", datetime.now())


class UsageStatistics(Base):
    """用量统计"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id")
        self.period_type = kwargs.get("period_type", "")
        self.period_start = kwargs.get("period_start")
        self.total_tokens = kwargs.get("total_tokens", 0)
        self.total_requests = kwargs.get("total_requests", 0)
        self.model_stats = kwargs.get("model_stats")
        self.quota_limit = kwargs.get("quota_limit")
        self.quota_used_percent = kwargs.get("quota_used_percent", 0.0)
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
