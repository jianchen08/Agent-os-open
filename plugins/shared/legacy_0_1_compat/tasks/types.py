"""任务系统类型定义。

包含任务状态枚举、任务模型、验收标准数据类和工厂函数，
供任务状态机、存储、服务等模块共同使用。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any

from agents.types import AgentLevel


class TaskPriority(IntEnum):
    """任务优先级枚举，数值越小优先级越高。"""

    CRITICAL = 1
    HIGH = 3
    NORMAL = 5
    LOW = 7
    BACKGROUND = 9


class TaskStatus(Enum):
    """任务状态枚举。

    7 种状态及其语义：
    - pending: 已创建，等待执行
    - running: 正在执行
    - evaluating: 评估中，任务正在评估执行结果
    - stopped: 已停止（数据完好，可 continue 恢复；合并旧 suspended/cancelled）
    - completed: 成功完成
    - failed: 执行失败（可 continue 重试）
    - timeout: 执行超时（可 continue 重试）

    EVALUATING 状态供 task_evaluate 工具、child_task_guard 插件、task_recovery 等
    多处引用（LLM 调用评估期间任务处于此状态），是所有使用 task_evaluate 工具的任务
    评估流程所必需的。
    """

    PENDING = "pending"
    RUNNING = "running"
    EVALUATING = "evaluating"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class AC:
    """验收标准（Acceptance Criteria）。

    定义任务的通过条件，评估引擎据此判断任务是否完成。

    Attributes:
        metric_id: 评估指标 ID，引用评估系统中的指标定义
        input_params: 评估输入参数
        expected_output: 期望输出
        pass_threshold: 通过阈值（0.0 ~ 1.0）
    """

    metric_id: str
    input_params: dict[str, Any] = field(default_factory=dict)
    expected_output: Any = None
    pass_threshold: float = 1.0


@dataclass
class TaskModel:
    """任务模型。

    任务系统的核心数据结构，包含任务的完整生命周期信息。

    Attributes:
        id: 任务唯一标识
        title: 任务标题
        description: 任务描述
        parent_task_id: 父任务 ID（用于子任务层级）
        parent_pipeline_id: 创建该任务的父管道 ID，用于子任务完成时直接通知父管道
        dependencies: 依赖的任务 ID 列表
        agent_name: 执行者角色名（如 "灵汐"），由 Agent 配置注入
        agent_level: Agent 层级
        target_type: 执行者类型（agent / workflow），预留
        pipeline_run_id: 管道实例 ID，由 PipelineEngine.run() 生成并回填
        execution_record_id: 创建本任务的 task_submit 工具调用记录 ID
        status: 当前状态
        priority: 优先级
        created_at: 创建时间
        updated_at: 更新时间
        started_at: 开始执行时间
        completed_at: 完成时间
        result: 任务结果
        error: 错误信息
        reject_count: 拒绝次数
        metadata: 扩展元数据
    """

    # ── 核心标识 ──
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    description: str = ""

    # ── 层级关系（任务管理） ──
    parent_task_id: str | None = None
    parent_pipeline_id: str | None = field(
        default=None, metadata={"description": "创建该任务的父管道 ID，用于子任务完成时直接通知父管道"}
    )
    dependencies: list[str] | None = None

    # ── 执行者（身份 vs 实例） ──
    agent_name: str = ""
    agent_level: AgentLevel = AgentLevel.L1_MAIN
    target_type: str | None = None
    pipeline_run_id: str | None = None
    execution_record_id: str | None = None

    # ── 状态 ──
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.NORMAL

    # ── 时间 ──
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: str | None = None
    completed_at: str | None = None

    # ── 结果 ──
    result: Any = None
    error: str | None = None
    reject_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def create_task(
    title: str,
    description: str = "",
    priority: TaskPriority | int = TaskPriority.NORMAL,
    agent_level: AgentLevel | str = AgentLevel.L1_MAIN,
    parent_task_id: str | None = None,
    parent_pipeline_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    agent_name: str = "",
    dependencies: list[str] | None = None,
    execution_record_id: str | None = None,
    target_type: str | None = None,
) -> TaskModel:
    """创建任务的工厂函数。

    Args:
        title: 任务标题
        description: 任务描述
        priority: 优先级，默认 NORMAL
        agent_level: Agent 层级，默认 L1_MAIN
        parent_task_id: 父任务 ID，默认 None（顶级任务）
        metadata: 扩展元数据
        agent_name: 执行者角色名，默认空字符串
        dependencies: 依赖的任务 ID 列表，默认 None
        execution_record_id: 创建本任务的 task_submit 工具调用记录 ID，默认 None
        target_type: 执行者类型（agent / workflow），默认 None

    Returns:
        初始化后的 TaskModel 实例
    """
    # 确保枚举类型正确
    if isinstance(priority, int) and not isinstance(priority, TaskPriority):
        priority = TaskPriority(priority)
    if isinstance(agent_level, str) and not isinstance(agent_level, AgentLevel):
        agent_level = AgentLevel(agent_level)

    return TaskModel(
        title=title,
        description=description,
        priority=priority,
        agent_level=agent_level,
        parent_task_id=parent_task_id,
        parent_pipeline_id=parent_pipeline_id,
        metadata=metadata or {},
        agent_name=agent_name,
        dependencies=dependencies,
        execution_record_id=execution_record_id,
        target_type=target_type,
    )
