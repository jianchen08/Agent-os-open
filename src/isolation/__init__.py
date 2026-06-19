"""
任务隔离系统

提供多层次的任务执行环境隔离：
- CONTAINER: Docker 容器隔离
- HOST: 直接执行（宿主机）
"""

from isolation.decider import IsolationDecider, IsolationError
from isolation.manager import IsolationManager, get_isolation_manager
from isolation.permission_checker import PermissionChecker, check_write_permission
from isolation.permission_policy import (
    PermissionPolicyManager,
    PermissionPolicyType,
    PermissionScope,
    ReadPermission,
    WorkspacePermissionPolicy,
    WritePermission,
)
from isolation.policy import IsolationPolicyLoader, ToolIsolationPolicy
from isolation.providers.base import IsolationProvider
from isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    OperationType,
    TaskType,
)
from isolation.workspace import get_workspace_config_root, resolve_workspace, resolve_workspace_chain

__all__ = [
    # 类型
    "EnvironmentStatus",
    "ExecutionResult",
    "IsolationContext",
    "IsolationEnvironment",
    "IsolationLevel",
    "OperationType",
    "TaskType",
    # 核心
    "IsolationManager",
    "get_isolation_manager",
    "IsolationDecider",
    "IsolationError",
    "IsolationPolicyLoader",
    "ToolIsolationPolicy",
    "IsolationProvider",
    # 权限策略
    "PermissionScope",
    "PermissionPolicyType",
    "ReadPermission",
    "WritePermission",
    "WorkspacePermissionPolicy",
    "PermissionPolicyManager",
    # 权限检查
    "PermissionChecker",
    "check_write_permission",
    # 工作空间
    "resolve_workspace",
    "resolve_workspace_chain",
    "get_workspace_config_root",
]
