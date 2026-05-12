"""编排中心类型定义

定义编排中心使用的所有数据类型和枚举。
"""

import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any

from src.core.states import ExecutionStatus


class AgentLevel(IntEnum):
    """Agent 层级

    L1: 主 Agent，负责整体任务规划
    L2: SubAgent，负责子任务执行
    L3: 执行 Agent，不能再创建子任务
    """
    L1 = 1
    L2 = 2
    L3 = 3


class TaskPriority(IntEnum):
    """任务优先级

    数值越大优先级越高
    """
    LOW = 1
    NORMAL = 5
    HIGH = 8
    URGENT = 10


class TargetType(str, Enum):
    """任务目标类型"""
    AGENT = "agent"
    WORKFLOW = "workflow"
    TOOL = "tool"


@dataclass
class ResourceQuota:
    """资源配额配置

    Attributes:
        max_l1_agents: L1 Agent 最大并发数
        max_l2_agents: L2 Agent 最大并发数
        max_l3_agents: L3 Agent 最大并发数
        max_total_agents: 总 Agent 最大并发数
        max_cpu_percent: CPU 使用上限百分比
        max_memory_percent: 内存使用上限百分比
        priority_weights: 优先级权重配置
    """
    max_l1_agents: int = 2
    max_l2_agents: int = 10
    max_l3_agents: int = 50
    max_total_agents: int = 60
    max_cpu_percent: float = 80.0
    max_memory_percent: float = 80.0
    priority_weights: dict[TaskPriority, float] = field(
        default_factory=lambda: {
            TaskPriority.LOW: 0.1,
            TaskPriority.NORMAL: 1.0,
            TaskPriority.HIGH: 2.0,
            TaskPriority.URGENT: 5.0,
        }
    )


@dataclass
class TaskRequest:
    """任务请求

    Attributes:
        task_id: 任务唯一标识
        agent_level: Agent 层级
        priority: 任务优先级
        target_type: 目标类型（agent/workflow/tool）
        parent_task_id: 父任务 ID（用于任务链）
        session_id: 会话 ID
        description: 任务描述
        prompt: 执行提示
        config: 任务配置
        created_at: 创建时间戳
        scheduled_at: 调度时间戳
        started_at: 开始执行时间戳
        completed_at: 完成时间戳
        status: 任务状态
        result: 执行结果
        error: 错误信息
        estimated_duration: 预计执行时长（秒）
        actual_duration: 实际执行时长（秒）
    """
    task_id: str
    agent_level: AgentLevel
    priority: TaskPriority
    target_type: TargetType = TargetType.AGENT
    description: str = ""
    prompt: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    parent_task_id: str | None = None
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    scheduled_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    result: Any | None = None
    error: str | None = None
    estimated_duration: float = 60.0
    actual_duration: float | None = None


@dataclass
class TaskResult:
    """任务结果

    Attributes:
        task_id: 任务 ID
        status: 任务状态
        output: 输出内容
        error: 错误信息
        duration_ms: 执行时长（毫秒）
    """
    task_id: str
    status: ExecutionStatus
    output: str | None = None
    error: str | None = None
    duration_ms: int | None = None


@dataclass
class ResourceAllocation:
    """资源分配记录

    Attributes:
        task_id: 任务 ID
        agent_level: Agent 层级
        allocated_at: 分配时间戳
        expected_release_at: 预计释放时间戳
        agent_instance: Agent 实例（可选）
    """
    task_id: str
    agent_level: AgentLevel
    allocated_at: float
    expected_release_at: float
    agent_instance: Any | None = None
