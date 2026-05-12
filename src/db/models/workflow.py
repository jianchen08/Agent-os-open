"""
工作流模型
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base

# 向量维度常量
VECTOR_DIMENSION = 1536  # OpenAI ada-002 嵌入维度


class Workflow(Base):
    """工作流注册表"""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    description_vector: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSION), nullable=True)
    type: Mapped[str] = mapped_column(String(50), default="user_defined")
    source: Mapped[str] = mapped_column(String(50), default="native")
    source_id: Mapped[str | None] = mapped_column(String(255))
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    inputs_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    outputs_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(50), default="active")
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_score: Mapped[float | None] = mapped_column(Float)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class WorkflowComposition(Base):
    """工作流组合表

    记录工作流中包含的组件和它们的关系
    """

    __tablename__ = "workflow_compositions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id"), nullable=False, index=True
    )
    component_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # agent | tool | subworkflow
    component_id: Mapped[str] = mapped_column(String(36), nullable=False)
    component_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # 在工作流中的位置和配置
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # 位置信息
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # 节点配置

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
