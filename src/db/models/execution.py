"""
执行记录模型
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base

if TYPE_CHECKING:
    from src.db.models.user import Session


class ExecutionRecord(Base):
    """统一执行记录表（极简设计）

    存储所有执行细节，支持嵌套结构：
    - Agent 思考过程
    - 工具调用
    - 子 Agent 执行
    - 工作流节点执行

    设计理念：
    - 只保留 5 个核心字段
    - 所有其他信息存储在 message_data JSON 字段中
    - 通过 parent_record_id 支持任意深度嵌套

    message_data 结构示例：
    {
        "type": "human | ai | tool | workflow_node | evaluation",
        "content": "文本内容",  # 可选，用于存储主要文本内容
        "thinking": "思考内容",  # 可选，仅 ai 类型
        "tool_calls": [...],  # 可选，仅 ai 类型
        "tool_call_id": "call_abc123",  # 可选，仅 tool 类型
        "name": "file_read",  # 可选，工具/节点名称
        "status": "pending | running | completed | failed | cancelled",  # 可选
        "duration_ms": 150,  # 可选，执行时长（毫秒）
        "input": {...},  # 可选，输入数据
        "output": {...},  # 可选，输出数据
        "error": "错误信息",  # 可选
        "node_id": "node-validate",  # 可选，仅 workflow_node 类型
        "node_type": "agent",  # 可选，仅 workflow_node 类型
        "agent_name": "DataValidator",  # 可选，仅 workflow_node 类型
        "metric_id": "metric-file-exists",  # 可选，仅 evaluation 类型
        "evaluator_name": "file_exists_checker"  # 可选，仅 evaluation 类型
    }
    """

    __tablename__ = "execution_records"

    # 编码 ID（使用嵌套ID，最大70字符：支持更深层次的嵌套结构）
    id: Mapped[str] = mapped_column(
        String(70),
        primary_key=True,
        # 不使用default，创建时手动指定嵌套ID
    )

    # 所属会话 ID
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 父记录 ID（支持嵌套）
    parent_record_id: Mapped[str | None] = mapped_column(
        String(70),  # 支持嵌套ID
        ForeignKey("execution_records.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # 完整消息数据（JSON 格式，存储所有执行细节）
    message_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="完整的消息数据，包含所有执行细节",
    )

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # 关系
    session: Mapped[Session] = relationship(
        back_populates="execution_records",
        foreign_keys=[session_id],
    )
    parent_record: Mapped[ExecutionRecord | None] = relationship(
        "ExecutionRecord",
        remote_side=[id],
        backref="children",
    )

    # ============================================
    # 属性访问器 - 从 message_data 中提取常用字段
    # ============================================

    @property
    def status(self) -> str | None:
        """获取执行状态"""
        return self.message_data.get("status")

    @status.setter
    def status(self, value: str | None):
        """设置执行状态"""
        self.message_data["status"] = value

    @property
    def input_data(self) -> dict[str, Any] | None:
        """获取输入数据"""
        return self.message_data.get("input")

    @input_data.setter
    def input_data(self, value: dict[str, Any] | None):
        """设置输入数据"""
        self.message_data["input"] = value

    @property
    def output_data(self) -> dict[str, Any] | None:
        """获取输出数据"""
        return self.message_data.get("output")

    @output_data.setter
    def output_data(self, value: dict[str, Any] | None):
        """设置输出数据"""
        self.message_data["output"] = value

    @property
    def type(self) -> str | None:
        """获取记录类型：human | ai | tool | workflow_node | evaluation"""
        return self.message_data.get("type")

    @type.setter
    def type(self, value: str | None):
        """设置记录类型"""
        self.message_data["type"] = value

    @property
    def content(self) -> str | None:
        """获取内容"""
        return self.message_data.get("content")

    @content.setter
    def content(self, value: str | None):
        """设置内容"""
        self.message_data["content"] = value

    @property
    def thinking(self) -> str | None:
        """获取思考内容（仅 ai 类型）"""
        return self.message_data.get("thinking")

    @thinking.setter
    def thinking(self, value: str | None):
        """设置思考内容"""
        self.message_data["thinking"] = value

    @property
    def duration_ms(self) -> int | None:
        """获取执行时长（毫秒）"""
        return self.message_data.get("duration_ms")

    @duration_ms.setter
    def duration_ms(self, value: int | None):
        """设置执行时长"""
        self.message_data["duration_ms"] = value

    @property
    def name(self) -> str | None:
        """获取工具/节点名称"""
        return self.message_data.get("name")

    @name.setter
    def name(self, value: str | None):
        """设置工具/节点名称"""
        self.message_data["name"] = value
