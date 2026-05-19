"""
工具库模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base

VECTOR_DIMENSION = 1536


class ToolLibrary(Base):
    """工具库"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description")
        self.description_vector = kwargs.get("description_vector")
        self.when_to_use = kwargs.get("when_to_use")
        self.when_not_to_use = kwargs.get("when_not_to_use")
        self.examples = kwargs.get("examples")
        self.caveats = kwargs.get("caveats")
        self.input_schema = kwargs.get("input_schema")
        self.output_schema = kwargs.get("output_schema")
        self.args_schema = kwargs.get("args_schema")
        self.return_schema = kwargs.get("return_schema")
        self.source_type = kwargs.get("source_type", "custom")
        self.category = kwargs.get("category")
        self.level = kwargs.get("level", "user")
        self.version = kwargs.get("version", "1.0.0")
        self.tags = kwargs.get("tags", [])
        self.checksum = kwargs.get("checksum")
        self.status = kwargs.get("status", "active")
        self.requires_approval = kwargs.get("requires_approval", False)
        self.success_count = kwargs.get("success_count", 0)
        self.failure_count = kwargs.get("failure_count", 0)
        self.last_used_at = kwargs.get("last_used_at")
        self.created_by = kwargs.get("created_by")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
        self.source_code = kwargs.get("source_code")
        self.config = kwargs.get("config")
        self.schema = kwargs.get("schema")
        self.dependencies = kwargs.get("dependencies", [])
        self.parameters = kwargs.get("parameters")
