"""
Agent 配置模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base


class AgentConfig(Base):
    """Agent 配置"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.config_id = kwargs.get("config_id", "")
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description")
        self.agent_type = kwargs.get("agent_type", "atomic")
        self.model_name = kwargs.get("model_name", "")
        self.model_params = kwargs.get("model_params", {})
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tool_ids = kwargs.get("tool_ids", [])
        self.hard_constraints = kwargs.get("hard_constraints", [])
        self.soft_constraints = kwargs.get("soft_constraints", [])
        self.static_vars = kwargs.get("static_vars", {})
        self.dynamic_vars = kwargs.get("dynamic_vars", {})
        self.context_variables = kwargs.get("context_variables", {})
        self.input_schema = kwargs.get("input_schema", {})
        self.output_schema = kwargs.get("output_schema", {})
        self.version = kwargs.get("version", "1.0.0")
        self.is_active = kwargs.get("is_active", True)
        self.max_iterations = kwargs.get("max_iterations", 10)
        self.timeout_seconds = kwargs.get("timeout_seconds", 300)
        self.tags = kwargs.get("tags", [])
        self.agent_metadata = kwargs.get("agent_metadata", {})
        self.status = kwargs.get("status", "active")
        self.level = kwargs.get("level", 1)
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
