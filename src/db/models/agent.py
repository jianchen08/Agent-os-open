"""
Agent 配置模型
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base


class AgentConfig(Base):
    """Agent 配置表

    Agent 只有一种类型：原子智能体（atomic），负责单一职责的任务执行。
    复杂任务通过工作流编排多个 Agent 和工具来完成。
    """

    __tablename__ = "agent_configs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    config_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    agent_type: Mapped[str] = mapped_column(String(50), default="atomic")

    # Agent 配置字段
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_params: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    tool_ids: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    hard_constraints: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    soft_constraints: Mapped[list[str] | None] = mapped_column(JSON, default=list)

    # 四层提示词结构配置
    # 第1层：系统静态层（可缓存）- static_vars
    static_vars: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, doc="静态变量配置，包含 enabled 和 sources"
    )
    # 第4层：尾部动态层（每轮变化）- dynamic_vars
    dynamic_vars: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, doc="动态变量配置，包含 enabled, vars 和 rules"
    )

    # 上下文变量 - 可加载到 prompt 中的知识/记忆/数据（静态，替换到 prompt 中）
    context_variables: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    # 输入输出 Schema
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)

    version: Mapped[str] = mapped_column(String(50), default="1.0.0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_iterations: Mapped[int] = mapped_column(Integer, default=10)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)

    # 兼容后端 API 字段
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    agent_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="active")

    # Agent 层级 (1=L1, 2=L2, 3=L3)
    level: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )
