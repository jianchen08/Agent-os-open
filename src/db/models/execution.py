"""
执行记录模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
from datetime import datetime
from typing import Any

from src.db.models.base import Base


class ExecutionRecord(Base):
    """执行记录模型"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id")
        self.session_id = kwargs.get("session_id", "")
        self.parent_record_id = kwargs.get("parent_record_id")
        self.message_data = kwargs.get("message_data", {})
        self.created_at = kwargs.get("created_at", datetime.now())
        # 关系
        self.session = kwargs.get("session")
        self.parent_record = kwargs.get("parent_record")
        self.children = kwargs.get("children", [])

    # 属性访问器 - 从 message_data 中提取常用字段

    @property
    def status(self) -> str | None:
        return self.message_data.get("status")

    @status.setter
    def status(self, value: str | None):
        self.message_data["status"] = value

    @property
    def input_data(self) -> dict[str, Any] | None:
        return self.message_data.get("input")

    @input_data.setter
    def input_data(self, value: dict[str, Any] | None):
        self.message_data["input"] = value

    @property
    def output_data(self) -> dict[str, Any] | None:
        return self.message_data.get("output")

    @output_data.setter
    def output_data(self, value: dict[str, Any] | None):
        self.message_data["output"] = value

    @property
    def type(self) -> str | None:
        return self.message_data.get("type")

    @type.setter
    def type(self, value: str | None):
        self.message_data["type"] = value

    @property
    def content(self) -> str | None:
        return self.message_data.get("content")

    @content.setter
    def content(self, value: str | None):
        self.message_data["content"] = value

    @property
    def thinking(self) -> str | None:
        return self.message_data.get("thinking")

    @thinking.setter
    def thinking(self, value: str | None):
        self.message_data["thinking"] = value

    @property
    def duration_ms(self) -> int | None:
        return self.message_data.get("duration_ms")

    @duration_ms.setter
    def duration_ms(self, value: int | None):
        self.message_data["duration_ms"] = value

    @property
    def name(self) -> str | None:
        return self.message_data.get("name")

    @name.setter
    def name(self, value: str | None):
        self.message_data["name"] = value
