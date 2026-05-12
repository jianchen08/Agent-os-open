"""
记忆系统模型
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base

# 向量维度常量
VECTOR_DIMENSION = 1536  # OpenAI ada-002 嵌入维度


class EpisodesMemory(Base):
    """情景记忆"""

    __tablename__ = "episodes_memory"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    intent_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent_vector: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSION), nullable=True)
    plan_dag: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    execution_summary: Mapped[str | None] = mapped_column(Text)
    evaluation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    final_score: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SemanticMemory(Base):
    """语义记忆"""

    __tablename__ = "semantic_memory"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(36))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSION), nullable=True)
    memory_metadata: Mapped[dict[str, Any] | None] = mapped_column("extra_data", JSON)
    # 标签（用于分类和检索）
    tags: Mapped[list[str] | None] = mapped_column(
        JSON,
        default=list,
        comment="标签列表（如'技术'、'业务'、'个人偏好'等）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class KnowledgeBase(Base):
    """外部知识库"""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(50), default="document")
    source_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="processing")
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    # 标签（用于分类和检索）
    tags: Mapped[list[str] | None] = mapped_column(
        JSON,
        default=list,
        comment="标签列表（如'API文档'、'教程'、'规范'等）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class Tag(Base):
    """Tag 注册表

    存储所有 Tag 及其向量表示，用于 Tag 网络检索
    """

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    vector: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSION), nullable=True)
    tag_type: Mapped[str] = mapped_column(String(50), default="auto")
    frequency: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    memory_tags: Mapped[list["MemoryTag"]] = relationship(back_populates="tag")


class MemoryTag(Base):
    """记忆-Tag 关联表"""

    __tablename__ = "memory_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(50), nullable=False)
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    tag: Mapped["Tag"] = relationship(back_populates="memory_tags")


class TagCooccurrence(Base):
    """Tag 共现关系表

    存储 Tag 之间的共现关系，用于构建 Tag 网络和支持语义检索。
    当两个 Tag 同时出现在同一记忆（episode/semantic）中时，记录共现关系。
    """

    __tablename__ = "tag_cooccurrences"

    tag1_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    tag2_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    cooccurrence_count: Mapped[int] = mapped_column(
        Integer, default=1, comment="共现次数"
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), server_default=func.now()
    )


class MemoryChunk(Base):
    """记忆分块表

    存储分层压缩后的记忆块，支持按执行者隔离。
    每个 Agent 有独立的压缩上下文，避免不同 Agent 的记忆混淆。
    """

    __tablename__ = "memory_chunks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )

    # 执行者信息（与 ExecutionRecord 对齐，用于上下文隔离）
    executor_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        index=True,
        comment="执行者类型: agent | tool | workflow",
    )
    executor_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True, comment="执行者 ID"
    )
    executor_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="执行者名称"
    )

    # 分层信息
    layer: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSION), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    graduated: Mapped[bool] = mapped_column(Boolean, default=False)
    episode_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("episodes_memory.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    chunk_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
