"""
SQLite 数据库模型定义

简化的SQLite兼容模型，避免循环导入问题
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """模型基类"""


class SQLiteUser(Base):
    """简化的用户表（SQLite 兼容）"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(50), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.now
    )
