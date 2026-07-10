"""
隔离系统核心类型定义

暴露接口：
- to_dict(self) -> dict[str, Any]：to_dict功能
- IsolationLevel：IsolationLevel类
- TaskType：TaskType类
- OperationType：OperationType类
- IsolationContext：IsolationContext类
- IsolationEnvironment：IsolationEnvironment类
- ExecutionResult：ExecutionResult类
- EnvironmentStatus：EnvironmentStatus类
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IsolationLevel(str, Enum):
    """隔离级别

    定义两种隔离级别：
    - CONTAINER: 隔离的工作空间（不影响原项目），字符串值 "isolated"
    - HOST: 直接执行（在原空间工作，不做隔离），字符串值 "non_isolated"
    """

    CONTAINER = "isolated"
    HOST = "non_isolated"


class TaskType(str, Enum):
    """任务类型

    定义三种任务层级：
    - PROJECT: 长期任务（数周到数月）
    - MODULE: 短期任务（数天到1周）
    - ATOMIC: 原子任务（数小时到1天）
    """

    PROJECT = "project"
    MODULE = "module"
    ATOMIC = "atomic"


class OperationType(str, Enum):
    """操作类型

    定义可能需要隔离的操作类型
    """

    CODE_EXECUTION = "code_execution"
    UNTRUSTED_CODE = "untrusted_code"
    DESKTOP_CONTROL = "desktop_control"
    FILE_OPERATION = "file_operation"
    COMPLEX_FILE_OP = "complex_file_op"
    SYSTEM_CONFIG = "system_config"
    NETWORK_REQUEST = "network_request"


@dataclass
class IsolationContext:
    """隔离上下文

    包含创建隔离环境所需的所有信息
    """

    task_id: str
    task_type: TaskType
    operation_type: OperationType | None = None
    parent_env_id: str | None = None
    # 工作区沙盒配置
    workspace: str | None = None  # 工作目录路径（相对路径或绝对路径）
    parent_workspace: str | None = None  # 父任务的工作目录（子任务时使用）
    is_root_task: bool = True  # 是否为根任务
    isolation_level: IsolationLevel = IsolationLevel.CONTAINER  # 隔离级别（默认隔离）
    requires_approval: bool = False  # 是否需要人工审批（HOST 模式需要）
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IsolationEnvironment:
    """隔离环境

    表示一个已创建的隔离环境
    """

    env_id: str
    level: IsolationLevel
    provider_type: str
    status: str  # creating, ready, busy, stopping, stopped, error
    context: IsolationContext
    provider_info: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    last_used_at: str = ""
    expires_at: str | None = None


@dataclass
class ExecutionResult:
    """执行结果

    表示在隔离环境中执行操作的结果
    """

    success: bool
    output: Any
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
        }


class EnvironmentStatus(str, Enum):
    """环境状态"""

    CREATING = "creating"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
