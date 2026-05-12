"""
数据库模型包

基于 SQLAlchemy 2.0 的异步模型
根据 docs/reports/03_TechSpec.md 设计
"""

# 基类
# Agent 配置
from src.db.models.agent import AgentConfig
from src.db.models.base import Base

# 执行记录
from src.db.models.execution import ExecutionRecord

# 执行单元和经验（排名系统）
from src.db.models.experience import (
    AgentCallRecord,
    ExecutionExperience,
    ExecutionUnit,
)

# 记忆系统
from src.db.models.memory import (
    EpisodesMemory,
    KnowledgeBase,
    MemoryChunk,
    MemoryTag,
    SemanticMemory,
    Tag,
    TagCooccurrence,
)

# 监控和用量统计
from src.db.models.monitoring import (
    MonitoringAlert,
    TaskQueueStats,
    UsageRecord,
    UsageStatistics,
)

# 通知系统
from src.db.models.notification import Notification

# 回滚机制
from src.db.models.rollback import RollbackCheckpoint, RollbackOperationLog

# 任务和评估指标
from src.db.models.task import EvaluationMetric, Task

# 工具库
from src.db.models.tool import ToolLibrary

# 用户和会话
from src.db.models.user import Session, User

# 工作流
from src.db.models.workflow import Workflow, WorkflowComposition

# 兼容性别名（逐步废弃）
# 保留 Agent 别名以兼容旧代码，指向 AgentConfig
Agent = AgentConfig

__all__ = [
    # 基类
    "Base",
    # 用户和会话
    "User",
    "Session",
    # 执行记录
    "ExecutionRecord",
    # Agent 配置
    "AgentConfig",
    "Agent",  # 兼容别名
    # 任务
    "Task",
    "EvaluationMetric",
    # 工作流
    "Workflow",
    "WorkflowComposition",
    # 工具库
    "ToolLibrary",
    # 记忆系统
    "EpisodesMemory",
    "SemanticMemory",
    "KnowledgeBase",
    "Tag",
    "MemoryTag",
    "MemoryChunk",
    "TagCooccurrence",
    # 监控和用量统计
    "MonitoringAlert",
    "TaskQueueStats",
    "UsageRecord",
    "UsageStatistics",
    # 通知系统
    "Notification",
    # 执行单元和经验
    "ExecutionUnit",
    "ExecutionExperience",
    "AgentCallRecord",
    # 回滚机制
    "RollbackCheckpoint",
    "RollbackOperationLog",
]
