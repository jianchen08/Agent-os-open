"""
数据库模块

提供数据库连接、模型定义和仓储接口
"""

from src.db.connection import (
    DatabaseManager,
    get_async_session,
    get_db_manager,
    get_session_context,
)
from src.db.models import (
    Agent,  # 兼容别名
    AgentConfig,
    Base,
    EpisodesMemory,
    ExecutionRecord,
    KnowledgeBase,
    MemoryChunk,
    MemoryTag,
    SemanticMemory,
    Session,
    Tag,
    Task,
    ToolLibrary,
    UsageRecord,
    UsageStatistics,
    User,
    Workflow,
)

__all__ = [
    # 连接管理
    "DatabaseManager",
    "get_db_manager",
    "get_async_session",
    "get_session_context",
    "Base",
    # 模型
    "User",
    "Session",
    "ExecutionRecord",
    "AgentConfig",
    "Agent",  # 兼容别名
    "Workflow",
    "ToolLibrary",
    "EpisodesMemory",
    "SemanticMemory",
    "KnowledgeBase",
    "Tag",
    "MemoryTag",
    "MemoryChunk",
    "Task",
    "UsageRecord",
    "UsageStatistics",
]
