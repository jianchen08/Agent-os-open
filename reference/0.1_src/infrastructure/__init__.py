"""管道基础设施模块。

提供资源管理、错误策略、统计收集、数据库连接管理、
消息队列和执行记录存储能力，
作为所有上层系统（任务系统、记忆系统、工具系统）的底层支撑。
"""

from infrastructure.db import close_engine, get_async_session, get_engine, init_db
from infrastructure.error_policy import apply_error_policy
from infrastructure.execution_record_storage import (
    ExecutionRecordData,
    ExecutionRecordStorage,
)
from infrastructure.resource import ResourceManager, ResourceQuota
from infrastructure.stats import StatsCollector
from pipeline.types import ErrorPolicy

__all__ = [
    "close_engine",
    "ErrorPolicy",
    "ExecutionRecordData",
    "ExecutionRecordStorage",
    "get_async_session",
    "get_engine",
    "init_db",
    "ResourceManager",
    "ResourceQuota",
    "StatsCollector",
    "apply_error_policy",
    "create_message_id",
]
