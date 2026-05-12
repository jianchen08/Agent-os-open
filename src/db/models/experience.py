"""
执行单元和经验模型（排名系统）
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
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base

# 向量维度常量
VECTOR_DIMENSION = 1536  # OpenAI ada-002 嵌入维度


class ExecutionUnit(Base):
    """执行单元表

    用于排名系统，记录可执行的单元（Agent、工具、工作流等）
    """

    __tablename__ = "execution_units"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    unit_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # agent | tool | workflow
    unit_id: Mapped[str] = mapped_column(String(36), nullable=False)  # 对应的实际ID

    # 统计信息
    total_executions: Mapped[int] = mapped_column(Integer, default=0)
    successful_executions: Mapped[int] = mapped_column(Integer, default=0)
    average_score: Mapped[float | None] = mapped_column(Float)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class ExecutionExperience(Base):
    """执行经验表

    记录每次执行的详细经验，用于排名和学习
    """

    __tablename__ = "execution_experiences"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    unit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("execution_units.id"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True
    )
    episode_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("episodes_memory.id", ondelete="SET NULL"), index=True
    )

    # 意图信息
    intent_text: Mapped[str | None] = mapped_column(Text)
    intent_vector: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSION), nullable=True)

    # 输入输出
    input_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # 执行状态
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # success | failed | timeout
    score: Mapped[float | None] = mapped_column(Float)  # 执行评分
    duration_ms: Mapped[int | None] = mapped_column(Integer)  # 执行时长

    # 错误信息
    error_type: Mapped[str | None] = mapped_column(String(100), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)

    # 上下文信息
    context_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AgentCallRecord(Base):
    """Agent调用记录表

    持久化存储 AgentCallTool 的执行记录，支持：
    - 服务重启后记录可查询
    - 按调用者层级、状态过滤
    - 自动清理过期记录
    """

    __tablename__ = "agent_call_records"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 执行标识
    execution_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )

    # 调用者信息
    caller_level: Mapped[str] = mapped_column(
        String(10), nullable=False, index=True
    )  # L1, L2

    # 目标Agent信息
    target_agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target_agent_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # 操作信息
    operation_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    instruction_summary: Mapped[str] = mapped_column(String(255), nullable=False)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # 执行配置
    timeout: Mapped[int] = mapped_column(Integer, default=300)
    retry_count: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[str] = mapped_column(String(20), default="normal")

    # 执行状态: pending | running | completed | failed
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # 执行结果
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 时间信息
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)  # 秒

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
