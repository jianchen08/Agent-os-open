"""
回滚数据库模型（SQLAlchemy ORM）

与 models.py 的 Checkpoint / OperationLog 一一对齐（含 sequence 列），
供 RollbackManager 在 session=非 None 时走真实持久化（SQLite，agentos_kernel.db
风格的单进程内存库）。字段命名遵循既有契约：检查点元数据列名 checkpoint_metadata
（manager.py DB 分支已按此名读写）。

- RollbackCheckpoint.sequence: 创建检查点时该 task 的当前操作序号。
  回滚按单调序号精确定位（而非墙钟 created_at，Windows 下同 tick 会误判）。
- RollbackOperationLog.status: OperationStatus.value（"executed"/"rolled_back"/"failed"）。
- JSON 列存储 params/before_state/after_state/reverse_action/checkpoint_metadata。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


def create_db_engine(url: str = "sqlite:///agentos_kernel.db"):
    """创建 SQLite 引擎（RollbackManager DB 模式的统一入口）。

    - ``check_same_thread=False``：RollbackManager 经 ``asyncio.to_thread`` 在
      工作线程访问 DB，SQLite 默认线程亲和会报 ProgrammingError，必须放开。
    - 内存库（``sqlite://``）配 StaticPool 单连接保活。
    """
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if url == "sqlite://" or (url.startswith("sqlite:///") and ":memory:" in url):
        kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


class RollbackCheckpoint(Base):
    """检查点（对应 models.Checkpoint）"""

    __tablename__ = "rollback_checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RollbackOperationLog(Base):
    """操作日志（对应 models.OperationLog）"""

    __tablename__ = "rollback_operation_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)  # OperationType.value
    target: Mapped[str] = mapped_column(Text, nullable=False, default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reversible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reverse_action: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="executed")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
