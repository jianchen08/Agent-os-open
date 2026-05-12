"""
监控和用量统计模型
"""

import uuid
from datetime import datetime
from typing import Any

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


class MonitoringAlert(Base):
    """监控告警表"""

    __tablename__ = "monitoring_alerts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    alert_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # usage, performance, error
    level: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )  # info, warning, error, critical
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    usage_percent: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(36))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alert_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class TaskQueueStats(Base):
    """任务队列统计表"""

    __tablename__ = "task_queue_stats"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    pending_tasks: Mapped[int] = mapped_column(Integer, default=0)
    running_tasks: Mapped[int] = mapped_column(Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    avg_wait_time: Mapped[float | None] = mapped_column(Float)  # 平均等待时间（秒）
    avg_execution_time: Mapped[float | None] = mapped_column(
        Float
    )  # 平均执行时间（秒）
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)  # 队列深度
    active_workers: Mapped[int] = mapped_column(Integer, default=0)  # 活跃工作者数量


class UsageRecord(Base):
    """用量记录表"""

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UsageStatistics(Base):
    """用量统计表"""

    __tablename__ = "usage_statistics"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), index=True
    )
    period_type: Mapped[str] = mapped_column(String(20), nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    model_stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    quota_limit: Mapped[int | None] = mapped_column(Integer)
    quota_used_percent: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )
