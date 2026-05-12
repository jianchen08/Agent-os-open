"""
用户和会话模型
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base

if TYPE_CHECKING:
    from src.db.models.execution import ExecutionRecord


class User(Base):
    """用户表"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email_encrypted: Mapped[bytes | None] = mapped_column(nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="user")
    preferences: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    # 关系
    sessions: Mapped[list[Session]] = relationship(back_populates="user")


class Session(Base):
    """会话表（极简设计）

    只保留基础信息，其他数据由 execution_records 和 episodes_memory 承载。

    删除的冗余字段（迁移到 execution_records.message_data）：
    - workflow_state: 工作流状态
    - intent: 意图
    - plan: 计划
    - graph: 图数据
    - artifacts: 产物
    - evaluation: 评估
    - error: 错误
    - error_trace: 错误跟踪
    - session_metadata: 会话元数据
    - context_data: 上下文数据
    """

    __tablename__ = "sessions"

    # 复合主键：user_id + session_seq
    # 这样每个用户的会话序列号独立，从1开始递增
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    session_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, primary_key=True, autoincrement=False
    )
    # 全局唯一ID（用于外部引用，格式：thread-{user_id_short}-{session_seq}）
    # 修改后：包含用户ID前缀，确保全局唯一
    id: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    # 绑定的主 Agent ID
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # 会话标题
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 会话状态: active | archived | deleted
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    # 关系
    user: Mapped[User] = relationship(back_populates="sessions")
    execution_records: Mapped[list[ExecutionRecord]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
