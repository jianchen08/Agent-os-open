"""
工作流模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base

VECTOR_DIMENSION = 1536


class Workflow(Base):
    """工作流注册表"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description")
        self.description_vector = kwargs.get("description_vector")
        self.type = kwargs.get("type", "user_defined")
        self.source = kwargs.get("source", "native")
        self.source_id = kwargs.get("source_id")
        self.definition = kwargs.get("definition", {})
        self.inputs_schema = kwargs.get("inputs_schema")
        self.outputs_schema = kwargs.get("outputs_schema")
        self.status = kwargs.get("status", "active")
        self.tags = kwargs.get("tags", [])
        self.success_count = kwargs.get("success_count", 0)
        self.avg_score = kwargs.get("avg_score")
        self.last_used_at = kwargs.get("last_used_at")
        self.created_by = kwargs.get("created_by")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")


class WorkflowComposition(Base):
    """工作流组合"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.workflow_id = kwargs.get("workflow_id", "")
        self.component_type = kwargs.get("component_type", "")
        self.component_id = kwargs.get("component_id", "")
        self.component_name = kwargs.get("component_name", "")
        self.node_id = kwargs.get("node_id", "")
        self.position = kwargs.get("position")
        self.config = kwargs.get("config")
        self.created_at = kwargs.get("created_at", datetime.now())
