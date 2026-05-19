"""
执行单元和经验模型（非 ORM 存根 - 排名系统）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any

from src.db.models.base import Base

VECTOR_DIMENSION = 1536


class ExecutionUnit(Base):
    """执行单元"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description")
        self.unit_type = kwargs.get("unit_type", "")
        self.unit_id = kwargs.get("unit_id", "")
        self.total_executions = kwargs.get("total_executions", 0)
        self.successful_executions = kwargs.get("successful_executions", 0)
        self.average_score = kwargs.get("average_score")
        self.last_used_at = kwargs.get("last_used_at")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")


class ExecutionExperience(Base):
    """执行经验"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.unit_id = kwargs.get("unit_id", "")
        self.session_id = kwargs.get("session_id")
        self.episode_id = kwargs.get("episode_id")
        self.intent_text = kwargs.get("intent_text")
        self.intent_vector = kwargs.get("intent_vector")
        self.input_params = kwargs.get("input_params")
        self.output_data = kwargs.get("output_data")
        self.status = kwargs.get("status", "")
        self.score = kwargs.get("score")
        self.duration_ms = kwargs.get("duration_ms")
        self.error_type = kwargs.get("error_type")
        self.error_message = kwargs.get("error_message")
        self.context_data = kwargs.get("context_data")
        self.created_at = kwargs.get("created_at", datetime.now())


class AgentCallRecord(Base):
    """Agent 调用记录"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.execution_id = kwargs.get("execution_id", "")
        self.caller_level = kwargs.get("caller_level", "")
        self.target_agent_id = kwargs.get("target_agent_id", "")
        self.target_agent_name = kwargs.get("target_agent_name", "")
        self.operation_type = kwargs.get("operation_type", "")
        self.instruction = kwargs.get("instruction", "")
        self.instruction_summary = kwargs.get("instruction_summary", "")
        self.context = kwargs.get("context")
        self.timeout = kwargs.get("timeout", 300)
        self.retry_count = kwargs.get("retry_count", 1)
        self.priority = kwargs.get("priority", "normal")
        self.status = kwargs.get("status", "pending")
        self.success = kwargs.get("success")
        self.result = kwargs.get("result")
        self.result_summary = kwargs.get("result_summary")
        self.error = kwargs.get("error")
        self.start_time = kwargs.get("start_time", datetime.now())
        self.end_time = kwargs.get("end_time")
        self.duration = kwargs.get("duration")
        self.created_at = kwargs.get("created_at", datetime.now())
