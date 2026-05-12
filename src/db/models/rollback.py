"""
回滚机制模型
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base


class RollbackCheckpoint(Base):
    """回滚检查点表

    存储任务执行过程中的检查点，用于回滚操作
    """

    __tablename__ = "rollback_checkpoints"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    checkpoint_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSON, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class RollbackOperationLog(Base):
    """回滚操作日志表

    记录所有可回滚的操作，包含操作前后状态
    """

    __tablename__ = "rollback_operation_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("rollback_checkpoints.id", ondelete="SET NULL"),
        index=True,
    )

    # 工具信息
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # create | update | delete | execute

    # 操作目标（文件路径/API地址等）
    target: Mapped[str | None] = mapped_column(Text)

    # 操作参数和状态
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # 操作前状态
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # 操作后状态

    # 逆操作信息
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    reverse_action: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # 逆操作定义

    # 排序和状态
    sequence: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[str] = mapped_column(
        String(50), default="executed", index=True
    )  # executed | rolled_back | failed

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
