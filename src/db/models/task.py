"""
任务和评估指标模型（非 ORM 存根）

纯 Python 实现，替代 SQLAlchemy ORM 模型，保持字段兼容。
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from src.db.models.base import Base


class Task(Base):
    """任务模型

    字段与原 SQLAlchemy 模型完全一致，支持 property 访问器。
    """

    def __init__(self, **kwargs):
        # 核心标识
        self.id = kwargs.get("id")
        # 层级关系
        self.parent_task_id = kwargs.get("parent_task_id")
        self.execution_record_id = kwargs.get("execution_record_id")
        # 关联
        self.user_id = kwargs.get("user_id")
        self.session_id = kwargs.get("session_id")
        # 定义
        self.title = kwargs.get("title", "")
        self.goal = kwargs.get("goal")
        # 执行配置
        self.target_type = kwargs.get("target_type")
        self.target_id = kwargs.get("target_id")
        self.target_name = kwargs.get("target_name")
        self.priority = kwargs.get("priority", 5)
        self.dependencies = kwargs.get("dependencies")
        self.due_date = kwargs.get("due_date")
        self.retry_count = kwargs.get("retry_count", 0)
        self.max_retries = kwargs.get("max_retries", 3)
        # 评估指标引用
        self.evaluation_metric_ids = kwargs.get("evaluation_metric_ids", [])
        # 状态
        self.status = kwargs.get("status", "pending")
        # 时间
        self.started_at = kwargs.get("started_at")
        self.completed_at = kwargs.get("completed_at")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at", datetime.now())
        # 元数据
        self.task_metadata = kwargs.get("task_metadata") or {}
        self.tags = kwargs.get("tags", [])
        # 关系（兼容）
        self.parent = kwargs.get("parent")
        self.subtasks = kwargs.get("subtasks", [])

    # ==================== 兼容属性访问器 ====================

    @property
    def description(self) -> str | None:
        if self.goal:
            return self.goal.get("description") or self.goal.get("document")
        return None

    @description.setter
    def description(self, value: str | None):
        if self.goal is None:
            self.goal = {}
        if value is not None:
            self.goal["description"] = value

    @property
    def acceptance_criteria(self) -> list[dict[str, Any]]:
        return (self.task_metadata or {}).get("acceptance_criteria", [])

    @acceptance_criteria.setter
    def acceptance_criteria(self, value: list[dict[str, Any]]):
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["acceptance_criteria"] = value

    @property
    def total_criteria(self) -> int:
        return (self.task_metadata or {}).get("total_criteria", 0)

    @total_criteria.setter
    def total_criteria(self, value: int):
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["total_criteria"] = value

    @property
    def passed_criteria(self) -> int:
        return (self.task_metadata or {}).get("passed_criteria", 0)

    @passed_criteria.setter
    def passed_criteria(self, value: int):
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["passed_criteria"] = value

    @property
    def failed_criteria(self) -> int:
        return (self.task_metadata or {}).get("failed_criteria", 0)

    @failed_criteria.setter
    def failed_criteria(self, value: int):
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["failed_criteria"] = value

    @property
    def progress_percent(self) -> float:
        return (self.task_metadata or {}).get("progress_percent", 0.0)

    @progress_percent.setter
    def progress_percent(self, value: float):
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["progress_percent"] = value

    @property
    def best_passed_count(self) -> int:
        return (self.task_metadata or {}).get("best_passed_count", 0)

    @best_passed_count.setter
    def best_passed_count(self, value: int):
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["best_passed_count"] = value

    @property
    def last_passed_count(self) -> int:
        return (self.task_metadata or {}).get("last_passed_count", 0)

    @last_passed_count.setter
    def last_passed_count(self, value: int):
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["last_passed_count"] = value


class EvaluationMetric(Base):
    """评估指标模型"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description", "")
        self.category = kwargs.get("category", "")
        self.evaluator_type = kwargs.get("evaluator_type", "")
        self.evaluator_id = kwargs.get("evaluator_id", "")
        self.default_config = kwargs.get("default_config", {})
        self.input_schema = kwargs.get("input_schema", {})
        self.default_pass_threshold = kwargs.get("default_pass_threshold")
        self.includes = kwargs.get("includes", [])
        self.requires = kwargs.get("requires", [])
        self.level = kwargs.get("level", 1)
        self.when_to_use = kwargs.get("when_to_use")
        self.when_not_to_use = kwargs.get("when_not_to_use")
        self.examples = kwargs.get("examples")
        self.caveats = kwargs.get("caveats")
        self.is_red_line = kwargs.get("is_red_line", False)
        self.default_weight = kwargs.get("default_weight", 1.0)
        self.source = kwargs.get("source", "builtin")
        self.status = kwargs.get("status", "active")
        self.tags = kwargs.get("tags", [])
        self.usage_count = kwargs.get("usage_count", 0)
        self.success_count = kwargs.get("success_count", 0)
        self.avg_execution_time = kwargs.get("avg_execution_time")
        self.created_at = kwargs.get("created_at", datetime.now())
        self.updated_at = kwargs.get("updated_at")
