"""
任务和评估指标模型
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base


class Task(Base):
    """任务表

    存储通过 task_submit 工具提交的任务，包含：
    - 任务目标 (goal)
    - 执行状态和进度
    - 重试计数
    - 关联的执行记录
    - 引用的评估指标

    设计要点：
    - 支持 Task 嵌套（通过 parent_task_id）
    - 引用 EvaluationMetric 表（通过 evaluation_metric_ids）
    - 关联 ExecutionRecord（通过 execution_record_id）
    - 符合 docs/design/database.md 设计规范
    """

    __tablename__ = "tasks"

    # ==================== 核心标识 ====================
    # Task ID（从ExecutionRecord ID转换：exec- → task-，最大42字符）
    id: Mapped[str] = mapped_column(
        String(42),
        primary_key=True,
        # 不使用default，由task_submit工具创建时手动指定
    )

    # ==================== 层级关系 ====================
    parent_task_id: Mapped[str | None] = mapped_column(
        String(42),  # 支持嵌套ID
        ForeignKey("tasks.id"),
        nullable=True,
        index=True,
        comment="父任务ID（支持嵌套）",
    )

    execution_record_id: Mapped[str | None] = mapped_column(
        String(70),  # 支持嵌套ID
        ForeignKey("execution_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="关联的执行记录 ID",
    )

    # ==================== 关联 ====================
    user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id"),
        nullable=True,
        index=True,
        comment="所属用户",
    )

    session_id: Mapped[str | None] = mapped_column(
        String(36),  # 保持36，会话ID使用UUID
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="来源会话",
    )

    # ==================== 定义 ====================
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="任务标题")

    goal: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="任务目标（包含 title, description, document, context）",
    )

    # ==================== 执行配置 ====================
    target_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="目标执行者类型: agent | workflow"
    )

    target_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="目标执行者ID"
    )

    target_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="目标执行者名称"
    )

    priority: Mapped[int] = mapped_column(Integer, default=5, comment="优先级")

    # ==================== 依赖关系管理 ====================
    dependencies: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="依赖的任务 ID 列表（当前任务会在所有依赖任务完成后才开始执行）",
    )

    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="截止日期"
    )

    retry_count: Mapped[int] = mapped_column(Integer, default=0, comment="重试次数")

    max_retries: Mapped[int] = mapped_column(Integer, default=3, comment="最大重试次数")

    # ==================== 评估指标引用（JSON 数组） ====================
    evaluation_metric_ids: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="引用的评估指标 ID 列表",
    )

    # ==================== 状态 ====================
    # 状态: 使用 ExecutionStatus 枚举值
    # pending | scheduled | running | evaluating | suspended | blocked | completed | failed | cancelled | timeout
    status: Mapped[str] = mapped_column(
        String(50), default="pending", comment="任务状态（使用 ExecutionStatus 枚举值）"
    )

    # ==================== 时间 ====================
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="开始时间"
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="完成时间"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )

    # ==================== 元数据 ====================
    # 注意：使用 task_metadata 作为 Python 属性名，数据库列名为 metadata
    # 因为 metadata 是 SQLAlchemy 的保留字（Base.metadata）
    task_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSON,
        nullable=True,
        comment="元数据（错误信息、扩展信息等）",
    )

    # 标签（用于分类和检索）
    tags: Mapped[list[str] | None] = mapped_column(
        JSON,
        default=list,
        comment="标签列表（如'紧急'、'重要'、'bug修复'等）",
    )

    # ==================== 关系 ====================
    parent: Mapped[Optional["Task"]] = relationship(
        "Task", remote_side=[id], backref="subtasks"
    )

    # ==================== 兼容属性访问器 ====================
    # 以下属性从 task_metadata 或 goal 中读取，保持与旧代码兼容

    @property
    def description(self) -> str | None:
        """获取任务描述（从 goal 中读取）"""
        if self.goal:
            return self.goal.get("description") or self.goal.get("document")
        return None

    @description.setter
    def description(self, value: str | None):
        """设置任务描述（存储到 goal 中）"""
        if self.goal is None:
            self.goal = {}
        if value is not None:
            self.goal["description"] = value

    @property
    def acceptance_criteria(self) -> list[dict[str, Any]]:
        """获取验收标准列表（从 task_metadata 中读取）"""
        return (self.task_metadata or {}).get("acceptance_criteria", [])

    @acceptance_criteria.setter
    def acceptance_criteria(self, value: list[dict[str, Any]]):
        """设置验收标准列表（存储到 task_metadata 中）"""
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["acceptance_criteria"] = value

    @property
    def total_criteria(self) -> int:
        """获取总验收标准数（从 task_metadata 中读取）"""
        return (self.task_metadata or {}).get("total_criteria", 0)

    @total_criteria.setter
    def total_criteria(self, value: int):
        """设置总验收标准数（存储到 task_metadata 中）"""
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["total_criteria"] = value

    @property
    def passed_criteria(self) -> int:
        """获取已通过的验收标准数（从 task_metadata 中读取）"""
        return (self.task_metadata or {}).get("passed_criteria", 0)

    @passed_criteria.setter
    def passed_criteria(self, value: int):
        """设置已通过的验收标准数（存储到 task_metadata 中）"""
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["passed_criteria"] = value

    @property
    def failed_criteria(self) -> int:
        """获取失败的验收标准数（从 task_metadata 中读取）"""
        return (self.task_metadata or {}).get("failed_criteria", 0)

    @failed_criteria.setter
    def failed_criteria(self, value: int):
        """设置失败的验收标准数（存储到 task_metadata 中）"""
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["failed_criteria"] = value

    @property
    def progress_percent(self) -> float:
        """获取进度百分比（从 task_metadata 中读取）"""
        return (self.task_metadata or {}).get("progress_percent", 0.0)

    @progress_percent.setter
    def progress_percent(self, value: float):
        """设置进度百分比（存储到 task_metadata 中）"""
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["progress_percent"] = value

    @property
    def best_passed_count(self) -> int:
        """获取最佳通过数（从 task_metadata 中读取）"""
        return (self.task_metadata or {}).get("best_passed_count", 0)

    @best_passed_count.setter
    def best_passed_count(self, value: int):
        """设置最佳通过数（存储到 task_metadata 中）"""
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["best_passed_count"] = value

    @property
    def last_passed_count(self) -> int:
        """获取上次通过数（从 task_metadata 中读取）"""
        return (self.task_metadata or {}).get("last_passed_count", 0)

    @last_passed_count.setter
    def last_passed_count(self, value: int):
        """设置上次通过数（存储到 task_metadata 中）"""
        if self.task_metadata is None:
            self.task_metadata = {}
        self.task_metadata["last_passed_count"] = value


class EvaluationMetric(Base):
    """评估指标表（可复用）

    存储评估指标的定义，多个任务可以引用同一个指标。
    指标定义包含：
    - 指标名称和描述
    - 使用边界说明（when_to_use, when_not_to_use, examples, caveats）
    - 评估器类型和ID（指向 Tool/Agent/Workflow）
    - 默认配置和输入参数 Schema

    符合 docs/design/database.md 设计规范
    """

    __tablename__ = "evaluation_metrics"

    # ==================== 核心标识 ====================
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 指标标识（唯一，用于引用）
    name: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True, comment="指标名称（唯一）"
    )

    # ==================== 指标定义 ====================
    # 指标描述
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="指标描述")

    # 指标分类: file | schema | test | code | api | performance | semantic | human
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, comment="指标分类"
    )

    # ==================== 评估器配置 ====================
    # 评估器类型: tool | workflow | human
    evaluator_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="评估器类型"
    )

    # 评估器标识（tool_id 或 workflow_id 或 human_approval_id）
    evaluator_id: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="评估器标识"
    )

    # 默认配置（评估器参数）
    default_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, comment="默认配置"
    )

    # 输入参数 Schema（定义评估时需要的参数）
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, comment="输入参数 Schema"
    )

    # 默认通过阈值（0-100）
    # 为 null 时使用评估工具返回的 success 字段进行判定
    # 设置后系统会根据评估工具返回的 score 与此阈值比较来判定 passed
    default_pass_threshold: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="默认通过阈值（0-100），为 null 时使用评估工具的 success 字段",
    )

    # ==================== 依赖关系 ====================
    # 包含的低级指标（此评估已包含的检查）
    includes: Mapped[list[str] | None] = mapped_column(
        JSON, default=list, comment="包含的低级指标名称列表"
    )

    # 前置依赖（必须先通过的指标）
    requires: Mapped[list[str] | None] = mapped_column(
        JSON, default=list, comment="前置依赖指标名称列表"
    )

    # 指标层级（用于排序和优化）
    level: Mapped[int] = mapped_column(
        Integer, default=1, comment="指标层级（1=基础，2=格式，3=内容，4=语义）"
    )

    # ==================== 使用边界 ====================
    # 适用场景列表
    when_to_use: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="适用场景列表"
    )

    # 不适用场景列表
    when_not_to_use: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="不适用场景列表"
    )

    # 使用示例列表
    examples: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="使用示例列表"
    )

    # 注意事项列表
    caveats: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="注意事项列表"
    )

    # ==================== 使用边界属性 ====================
    # 是否红线指标（必须通过）
    is_red_line: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否红线指标"
    )

    # 默认权重
    default_weight: Mapped[float] = mapped_column(
        Float, default=1.0, comment="默认权重"
    )

    # ==================== 元数据 ====================
    # 来源: builtin | generated | custom
    source: Mapped[str] = mapped_column(String(50), default="builtin", comment="来源")

    # 状态: active | inactive | deprecated
    status: Mapped[str] = mapped_column(
        String(50), default="active", index=True, comment="状态"
    )

    # 标签
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=list, comment="标签")

    # ==================== 统计 ====================
    usage_count: Mapped[int] = mapped_column(Integer, default=0, comment="使用次数")

    success_count: Mapped[int] = mapped_column(Integer, default=0, comment="成功次数")

    avg_execution_time: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="平均执行时间（秒）"
    )

    # ==================== 时间 ====================
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=func.now(),
        comment="更新时间",
    )
