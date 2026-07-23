"""
通用回滚机制模块

提供操作日志记录、检查点管理和回滚执行功能
"""

from src.rollback.decorators import OperationRecorder, reversible_operation
from src.rollback.integration import TaskRollbackIntegration, get_rollback_integration
from src.rollback.manager import RollbackManager, get_rollback_manager
from src.rollback.models import (
    Checkpoint,
    OperationLog,
    OperationStatus,
    OperationType,
    RollbackResult,
)
from src.rollback.reversers import (
    APIReverser,
    BaseReverser,
    FileReverser,
    GitReverser,
    ReverserRegistry,
    get_reverser_registry,
)

__all__ = [
    # 管理器
    "RollbackManager",
    "get_rollback_manager",
    # 集成
    "TaskRollbackIntegration",
    "get_rollback_integration",
    # 模型
    "Checkpoint",
    "OperationLog",
    "OperationType",
    "OperationStatus",
    "RollbackResult",
    # 逆操作器
    "BaseReverser",
    "FileReverser",
    "GitReverser",
    "APIReverser",
    "ReverserRegistry",
    "get_reverser_registry",
    # 装饰器和工具
    "reversible_operation",
    "OperationRecorder",
]
