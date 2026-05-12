"""
触发器系统模型
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base


class Trigger(Base):
    """触发器表

    存储所有类型的触发器配置和状态。
    """

    __tablename__ = "triggers"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # 触发器类型: time | event | condition
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # 是否启用
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # 配置（JSON 格式，根据类型不同结构不同）
    # time: { schedule: { type: "cron|interval|date", ... } }
    # event: { event: { type: "...", filter: "..." } }
    # condition: { condition: { expression: "...", watch_events: [...] } }
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # 元数据
    trigger_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSON, default=dict
    )

    # 时间信息
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 执行统计
    execution_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)

    # 关系
    actions: Mapped[list["TriggerAction"]] = relationship(
        "TriggerAction", back_populates="trigger", cascade="all, delete-orphan"
    )
    execution_logs: Mapped[list["TriggerExecutionLog"]] = relationship(
        "TriggerExecutionLog", back_populates="trigger", cascade="all, delete-orphan"
    )


class TriggerAction(Base):
    """触发器动作表

    定义触发器被触发时要执行的动作。
    """

    __tablename__ = "trigger_actions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    trigger_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("triggers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 动作类型: notification | api_call | task_retry | task_complete | custom
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # 动作配置（JSON 格式）
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # 执行顺序
    order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    trigger: Mapped["Trigger"] = relationship("Trigger", back_populates="actions")


class TriggerExecutionLog(Base):
    """触发器执行日志表

    记录每次触发器的执行情况。
    """

    __tablename__ = "trigger_execution_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    trigger_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("triggers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 执行状态: success | failed | pending
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # 执行结果（JSON 格式）
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # 错误信息
    error_message: Mapped[str | None] = mapped_column(Text)

    # 触发上下文（事件数据等）
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # 时间信息
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 执行时长（毫秒）
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # 关系
    trigger: Mapped["Trigger"] = relationship(
        "Trigger", back_populates="execution_logs"
    )
