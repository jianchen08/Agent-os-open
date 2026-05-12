"""
工具库模型
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base

# 向量维度常量
VECTOR_DIMENSION = 1536  # OpenAI ada-002 嵌入维度


class ToolLibrary(Base):
    """
    工具库（程序性记忆）

    存储工具的完整定义，包括使用边界说明。
    与 src/tools/types.py 中的 Tool 类保持字段对齐。
    """

    __tablename__ = "tool_library"

    # 基础标识
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    description_vector: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSION), nullable=True)

    # 使用边界说明（新增，与 Tool 类对齐）
    when_to_use: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="适用场景列表"
    )
    when_not_to_use: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="不适用场景列表"
    )
    examples: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="使用示例列表"
    )
    caveats: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="注意事项列表"
    )

    # Schema 定义（重命名以与 Tool 类对齐）
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="输入参数 JSON Schema"
    )
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="输出 Schema"
    )

    # 兼容旧字段（逐步废弃）
    args_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    return_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # 元数据
    source_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="custom",
        comment="来源类型: builtin | mcp | custom",
    )
    category: Mapped[str | None] = mapped_column(String(255))
    level: Mapped[str] = mapped_column(
        String(50), default="user", comment="工具级别: system | user"
    )
    version: Mapped[str | None] = mapped_column(String(50), default="1.0.0")
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    checksum: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="定义校验和，用于检测变更"
    )

    # 状态与权限
    status: Mapped[str] = mapped_column(String(50), default="active")
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)

    # 统计信息
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 审计字段
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    # 兼容旧字段（可能被其他代码使用，逐步废弃）
    source_code: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dependencies: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSON)
