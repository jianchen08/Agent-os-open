"""
通知系统模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime

from src.db.models.base import Base


class Notification(Base):
    """通知"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = kwargs.get("user_id", "")
        self.title = kwargs.get("title", "")
        self.message = kwargs.get("message", "")
        self.notification_type = kwargs.get("notification_type", "info")
        self.priority = kwargs.get("priority", "normal")
        self.read = kwargs.get("read", False)
        self.pushed = kwargs.get("pushed", False)
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
        self.user = kwargs.get("user")
