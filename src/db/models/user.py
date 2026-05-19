"""
用户和会话模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base


class User(Base):
    """用户"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.username = kwargs.get("username", "")
        self.email_encrypted = kwargs.get("email_encrypted")
        self.password_hash = kwargs.get("password_hash", "")
        self.role = kwargs.get("role", "user")
        self.preferences = kwargs.get("preferences")
        self.is_active = kwargs.get("is_active", True)
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
        self.sessions = kwargs.get("sessions", [])


class Session(Base):
    """会话"""

    def __init__(self, **kwargs):
        self.user_id = kwargs.get("user_id", "")
        self.session_seq = kwargs.get("session_seq", 0)
        self.id = kwargs.get("id", "")
        self.agent_id = kwargs.get("agent_id")
        self.title = kwargs.get("title")
        self.status = kwargs.get("status", "active")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
        self.user = kwargs.get("user")
        self.execution_records = kwargs.get("execution_records", [])
