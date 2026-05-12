"""
监控模块数据模型

定义监控 API 的请求和响应数据结构
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from src.core.states import ExecutionStatus

# ============================================================================
# 枚举类型
# ============================================================================


class AlertLevel(str, Enum):
    """告警级别"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


class ExecutionNodeType(str, Enum):
    """执行节点类型"""

    TOOL = "tool"
    AGENT = "agent"
    WORKFLOW = "workflow"


# ============================================================================
# 资源监控模型
# ============================================================================


class TokenUsageStats(BaseModel):
    """Token 使用统计"""

    today_tokens: int = Field(0, description="今日 Token 消耗")
    today_requests: int = Field(0, description="今日请求次数")
    month_tokens: int = Field(0, description="本月 Token 消耗")
    month_requests: int = Field(0, description="本月请求次数")
    total_tokens: int = Field(0, description="总 Token 消耗")
    total_requests: int = Field(0, description="总请求次数")


class QuotaStatus(BaseModel):
    """配额状态"""

    daily_limit: int | None = Field(None, description="每日 Token 限制")
    monthly_limit: int | None = Field(None, description="每月 Token 限制")
    daily_usage_percent: float = Field(0.0, description="每日使用率 (%)")
    monthly_usage_percent: float = Field(0.0, description="每月使用率 (%)")
    alert_level: AlertLevel = Field(AlertLevel.INFO, description="告警级别")


class CostEstimate(BaseModel):
    """成本估算"""

    today_cost: float = Field(0.0, description="今日成本 ($)")
    month_cost: float = Field(0.0, description="本月成本 ($)")
    estimated_monthly: float = Field(0.0, description="预估月度成本 ($)")


class ResourceUsageResponse(BaseModel):
    """资源使用响应"""

    token_usage: TokenUsageStats = Field(..., description="Token 使用统计")
    quota_status: QuotaStatus = Field(..., description="配额状态")
    cost_estimate: CostEstimate = Field(..., description="成本估算")
    active_agents: int = Field(0, description="活跃 Agent 数")
    max_concurrent: int = Field(5, description="最大并发数")
    queued_tasks: int = Field(0, description="排队任务数")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")


# ============================================================================
# 任务进度模型
# ============================================================================


class SubTaskProgress(BaseModel):
    """子任务进度"""

    id: str = Field(..., description="子任务 ID")
    title: str = Field(..., description="子任务标题")
    status: ExecutionStatus = Field(..., description="状态")
    progress_percent: float = Field(0.0, ge=0, le=100, description="进度百分比")
    start_time: datetime | None = Field(None, description="开始时间")
    end_time: datetime | None = Field(None, description="结束时间")
    error_message: str | None = Field(None, description="错误信息")


class TaskProgressResponse(BaseModel):
    """任务进度响应"""

    id: str = Field(..., description="任务 ID")
    session_id: str = Field(..., description="会话 ID")
    title: str = Field(..., description="任务标题")
    status: ExecutionStatus = Field(..., description="任务状态")
    progress_percent: float = Field(0.0, ge=0, le=100, description="整体进度")
    total_steps: int = Field(0, description="总步骤数")
    completed_steps: int = Field(0, description="已完成步骤数")
    subtasks: list[SubTaskProgress] = Field(
        default_factory=list, description="子任务列表"
    )
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")
    error_message: str | None = Field(None, description="错误信息")


class TaskProgressListResponse(BaseModel):
    """任务进度列表响应"""

    items: list[TaskProgressResponse] = Field(..., description="任务列表")
    total: int = Field(..., description="总数")


# ============================================================================
# Agent 可靠性模型
# ============================================================================


class AgentReliability(BaseModel):
    """Agent 可靠性指标"""

    agent_id: str = Field(..., description="Agent ID")
    agent_name: str = Field(..., description="Agent 名称")
    reliability_score: float = Field(
        0.0, ge=0, le=100, description="可靠性评分 (0-100)"
    )
    total_executions: int = Field(0, description="总执行次数")
    success_count: int = Field(0, description="成功次数")
    failure_count: int = Field(0, description="失败次数")
    success_rate: float = Field(0.0, ge=0, le=100, description="成功率 (%)")
    avg_retries: float = Field(0.0, description="平均重试次数")
    avg_execution_time_ms: float = Field(0.0, description="平均执行时间 (ms)")
    last_execution_at: datetime | None = Field(None, description="最后执行时间")


class AgentReliabilityResponse(BaseModel):
    """Agent 可靠性响应"""

    agent: AgentReliability = Field(..., description="Agent 可靠性指标")


class AgentReliabilityListResponse(BaseModel):
    """Agent 可靠性列表响应"""

    items: list[AgentReliability] = Field(..., description="Agent 列表")
    total: int = Field(..., description="总数")


class AgentReliabilityRankingResponse(BaseModel):
    """Agent 可靠性排行响应"""

    ranking: list[AgentReliability] = Field(..., description="排行榜")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")


# ============================================================================
# 执行图节点模型
# ============================================================================


class ExecutionNodeStatus(str, Enum):
    """执行节点状态"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_INPUT = "waiting_input"


class ExecutionNode(BaseModel):
    """执行节点"""

    id: str = Field(..., description="节点 ID")
    node_type: ExecutionNodeType = Field(..., description="节点类型")
    name: str = Field(..., description="节点名称")
    status: ExecutionNodeStatus = Field(..., description="节点状态")
    acceptance_criteria: str | None = Field(None, description="验收标准")
    ac_met: bool | None = Field(None, description="AC 是否满足")
    token_usage: int = Field(0, description="Token 消耗")
    execution_time_ms: int = Field(0, description="执行时间 (ms)")
    dependencies: list[str] = Field(default_factory=list, description="依赖节点 ID")
    start_time: datetime | None = Field(None, description="开始时间")
    end_time: datetime | None = Field(None, description="结束时间")
    error_message: str | None = Field(None, description="错误信息")


class ExecutionGraphResponse(BaseModel):
    """执行图响应"""

    session_id: str = Field(..., description="会话 ID")
    nodes: list[ExecutionNode] = Field(..., description="节点列表")
    edges: list[dict[str, str]] = Field(..., description="边列表 [{from, to}]")
    current_node_id: str | None = Field(None, description="当前执行节点 ID")
    total_token_usage: int = Field(0, description="总 Token 消耗")
    total_execution_time_ms: int = Field(0, description="总执行时间 (ms)")


# ============================================================================
# 用量告警模型
# ============================================================================


class UsageAlert(BaseModel):
    """用量告警"""

    id: str = Field(..., description="告警 ID")
    level: AlertLevel = Field(..., description="告警级别")
    usage_percent: float = Field(..., description="使用率 (%)")
    message: str = Field(..., description="告警消息")
    timestamp: datetime = Field(..., description="告警时间")
    acknowledged: bool = Field(False, description="是否已确认")


class UsageAlertListResponse(BaseModel):
    """用量告警列表响应"""

    items: list[UsageAlert] = Field(..., description="告警列表")
    total: int = Field(..., description="总数")
    unacknowledged_count: int = Field(0, description="未确认数量")


# ============================================================================
# 会话资源监控模型
# ============================================================================


class SessionResourceStats(BaseModel):
    """会话资源统计"""

    session_id: str = Field(..., description="会话 ID")
    token_usage: int = Field(0, description="Token 消耗")
    estimated_cost: float = Field(0.0, description="预计成本 ($)")
    elapsed_time_seconds: int = Field(0, description="已用时间 (秒)")
    active_agents: int = Field(0, description="活跃 Agent 数")
    max_concurrent: int = Field(5, description="最大并发数")
    queued_tasks: int = Field(0, description="排队任务数")
    quota_limit: int | None = Field(None, description="配额限制")
    quota_usage_percent: float = Field(0.0, description="配额使用率 (%)")


class SessionResourceResponse(BaseModel):
    """会话资源响应"""

    stats: SessionResourceStats = Field(..., description="资源统计")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")
