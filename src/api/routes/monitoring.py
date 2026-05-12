"""
监控路由

提供系统监控相关的 API 端点，包括：
- 系统性能指标
- 任务统计与进度
- 资源使用监控
- Agent 可靠性指标
- 执行图状态
"""

from datetime import datetime
from typing import Any

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.auth import get_current_user
from src.api.schemas.monitoring import (
    AgentReliability,
    AgentReliabilityListResponse,
    AgentReliabilityRankingResponse,
    AgentReliabilityResponse,
    AlertLevel,
    CostEstimate,
    ExecutionGraphResponse,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionNodeType,
    QuotaStatus,
    ResourceUsageResponse,
    SessionResourceResponse,
    SessionResourceStats,
    TokenUsageStats,
    UsageAlert,
    UsageAlertListResponse,
)
from src.db.connection import get_async_session
from src.db.models import AgentConfig, ExecutionRecord, Session, Task

# ============================================================================
# 辅助函数
# ============================================================================


async def _get_queued_tasks_count(db: AsyncSession) -> int:
    """获取队列中的任务数量"""
    from src.services.monitoring_service import MonitoringService

    monitoring_service = MonitoringService(db)
    stats = await monitoring_service.get_current_task_queue_stats()
    return stats.get("pending_tasks", 0)


router = APIRouter()


# ============================================================================
# 内部数据模型 (兼容旧接口)
# ============================================================================


class SystemMetrics(BaseModel):
    """系统性能指标"""

    cpu_usage: float = Field(..., description="CPU 使用率 (%)")
    memory_usage: float = Field(..., description="内存使用率 (%)")
    memory_total: int = Field(..., description="总内存 (bytes)")
    memory_available: int = Field(..., description="可用内存 (bytes)")
    disk_usage: float = Field(..., description="磁盘使用率 (%)")
    uptime: int = Field(..., description="系统运行时间 (秒)")


class SystemMetricsResponse(BaseModel):
    """系统指标响应"""

    metrics: SystemMetrics


class TaskStatistics(BaseModel):
    """任务统计"""

    total: int = Field(..., description="总任务数")
    running: int = Field(..., description="运行中")
    completed: int = Field(..., description="已完成")
    failed: int = Field(..., description="失败")
    pending: int = Field(..., description="等待中")


class TaskStatisticsResponse(BaseModel):
    """任务统计响应"""

    statistics: TaskStatistics


class TaskInfo(BaseModel):
    """任务信息"""

    id: str = Field(..., description="任务 ID")
    name: str = Field(..., description="任务名称")
    status: str = Field(..., description="状态")
    created_at: str = Field(..., description="创建时间")
    updated_at: str | None = Field(None, description="更新时间")


class TaskListResponse(BaseModel):
    """任务列表响应"""

    items: list[TaskInfo] = Field(..., description="任务列表")
    total: int = Field(..., description="总数")


class SystemEvent(BaseModel):
    """系统事件"""

    id: str = Field(..., description="事件 ID")
    event_type: str = Field(..., description="事件类型")
    message: str = Field(..., description="事件消息")
    level: str = Field(..., description="级别: info/warning/error")
    timestamp: str = Field(..., description="时间戳")


class EventListResponse(BaseModel):
    """事件列表响应"""

    items: list[SystemEvent] = Field(..., description="事件列表")
    total: int = Field(..., description="总数")


# ============================================================================
# 常量配置
# ============================================================================

# Token 成本估算 ($/1K tokens)
TOKEN_COST_PER_1K = 0.002

# 默认配额
DEFAULT_DAILY_TOKEN_LIMIT = 100000
DEFAULT_MONTHLY_TOKEN_LIMIT = 3000000
DEFAULT_MAX_CONCURRENT = 5


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "/system/metrics",
    response_model=SystemMetricsResponse,
    summary="获取系统性能指标",
    description="获取 CPU、内存、磁盘等系统性能指标",
)
async def get_system_metrics(
    current_user=Depends(get_current_user),
) -> SystemMetricsResponse:
    """获取系统性能指标"""
    # 获取 CPU 使用率
    cpu_usage = psutil.cpu_percent(interval=0.1)

    # 获取内存信息
    memory = psutil.virtual_memory()

    # 获取磁盘信息
    disk = psutil.disk_usage("/")

    # 获取系统启动时间
    boot_time = psutil.boot_time()
    uptime = int(datetime.now().timestamp() - boot_time)

    metrics = SystemMetrics(
        cpu_usage=cpu_usage,
        memory_usage=memory.percent,
        memory_total=memory.total,
        memory_available=memory.available,
        disk_usage=disk.percent,
        uptime=uptime,
    )

    return SystemMetricsResponse(metrics=metrics)


@router.get(
    "/tasks/statistics",
    response_model=TaskStatisticsResponse,
    summary="获取任务统计",
    description="获取通过任务工具提交的任务统计数据",
)
async def get_task_statistics(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> TaskStatisticsResponse:
    """获取任务统计（仅统计通过任务工具提交的任务）"""
    user_id = str(current_user.id)

    # 统计 Task 表中的任务
    total_query = select(func.count(Task.id)).where(Task.user_id == user_id)
    total_result = await db.execute(total_query)
    total = total_result.scalar() or 0

    # 按任务状态统计
    # Task 状态: pending | running | completed | failed
    status_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}

    for status_name in status_counts.keys():
        count_query = select(func.count(Task.id)).where(
            Task.user_id == user_id, Task.status == status_name
        )
        result = await db.execute(count_query)
        status_counts[status_name] = result.scalar() or 0

    statistics = TaskStatistics(
        total=total,
        running=status_counts["running"],
        completed=status_counts["completed"],
        failed=status_counts["failed"],
        pending=status_counts["pending"],
    )

    return TaskStatisticsResponse(statistics=statistics)


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="获取任务列表",
    description="获取通过任务工具提交的任务列表",
)
async def get_task_list(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: str | None = Query(None, description="状态过滤"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> TaskListResponse:
    """获取任务列表（仅显示通过任务工具提交的任务）"""
    user_id = str(current_user.id)

    # 构建查询 - 从 Task 表获取数据
    query = select(Task).where(Task.user_id == user_id)

    # 状态过滤
    status_mapping = {
        "active": "running",
        "running": "running",
    }
    if status:
        mapped_status = status_mapping.get(status, status)
        query = query.where(Task.status == mapped_status)

    # 统计总数
    count_query = select(func.count(Task.id)).where(Task.user_id == user_id)
    if status:
        mapped_status = status_mapping.get(status, status)
        count_query = count_query.where(Task.status == mapped_status)
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # 分页查询，按创建时间降序
    query = query.order_by(Task.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    tasks = result.scalars().all()

    items = [
        TaskInfo(
            id=str(t.id),
            name=t.title or "未命名任务",
            status=t.status or "pending",
            created_at=t.created_at.isoformat(),
            updated_at=t.updated_at.isoformat() if t.updated_at else None,
        )
        for t in tasks
    ]

    return TaskListResponse(items=items, total=total)


# ============================================================================
# 任务详情 API（支持前端展示 AC 状态）
# ============================================================================


class AcceptanceCriteriaInfo(BaseModel):
    """验收标准信息"""

    id: str = Field(..., description="标准 ID")
    description: str = Field(..., description="描述")
    type: str = Field(
        default="semantic", description="类型: programmatic | semantic | manual"
    )
    is_red_line: bool = Field(default=False, description="是否红线指标")
    weight: float = Field(default=1.0, description="权重")
    status: str = Field(
        default="pending", description="状态: pending | passed | failed"
    )
    evaluated_at: str | None = Field(None, description="评估时间")
    retry_count: int = Field(default=0, description="重试次数")


class TaskDetailResponse(BaseModel):
    """任务详情响应"""

    id: str = Field(..., description="任务 ID")
    title: str = Field(..., description="任务标题")
    description: str | None = Field(None, description="任务描述")
    status: str = Field(..., description="状态")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    acceptance_criteria: list[AcceptanceCriteriaInfo] = Field(
        default_factory=list, description="验收标准列表"
    )
    target_type: str | None = Field(None, description="目标类型")
    target_id: str | None = Field(None, description="目标 ID")
    target_name: str | None = Field(None, description="目标名称")
    progress: dict[str, Any] = Field(default_factory=dict, description="进度信息")
    retry_info: dict[str, Any] = Field(default_factory=dict, description="重试信息")
    created_at: str = Field(..., description="创建时间")
    started_at: str | None = Field(None, description="开始时间")
    completed_at: str | None = Field(None, description="完成时间")
    error_message: str | None = Field(None, description="错误信息")


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailResponse,
    summary="获取任务详情",
    description="获取任务详情，包含验收标准状态",
)
async def get_task_detail(
    task_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> TaskDetailResponse:
    """获取任务详情"""
    user_id = str(current_user.id)

    # 查询任务
    query = select(Task).where(Task.id == task_id, Task.user_id == user_id)
    result = await db.execute(query)
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 转换验收标准
    ac_list = []
    for ac in task.acceptance_criteria or []:
        ac_list.append(
            AcceptanceCriteriaInfo(
                id=ac.get("id", ""),
                description=ac.get("description", ""),
                type=ac.get("type", "semantic"),
                is_red_line=ac.get("is_red_line", False),
                weight=ac.get("weight", 1.0),
                status=ac.get("status", "pending"),
                evaluated_at=ac.get("evaluated_at"),
                retry_count=ac.get("retry_count", 0),
            )
        )

    return TaskDetailResponse(
        id=str(task.id),
        title=task.title or "未命名任务",
        description=task.description,
        status=task.status or "pending",
        goal=task.goal,
        acceptance_criteria=ac_list,
        target_type=task.target_type,
        target_id=task.target_id,
        target_name=task.target_name,
        progress={
            "total": task.total_criteria or 0,
            "passed": task.passed_criteria or 0,
            "failed": task.failed_criteria or 0,
            "percent": task.progress_percent or 0.0,
        },
        retry_info={
            "retry_count": task.retry_count or 0,
            "max_retries": task.max_retries or 3,
        },
        created_at=task.created_at.isoformat(),
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        error_message=task.error_message,
    )


@router.get(
    "/events",
    response_model=EventListResponse,
    summary="获取系统事件",
    description="获取最近的系统事件列表",
)
async def get_event_list(
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    event_type: str | None = Query(None, description="事件类型过滤"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> EventListResponse:
    """获取系统事件列表"""
    user_id = str(current_user.id)

    # 从执行记录表获取最近的记录作为事件
    query = (
        select(ExecutionRecord)
        .join(Session, ExecutionRecord.session_id == Session.id)
        .where(Session.user_id == user_id)
        .order_by(ExecutionRecord.created_at.desc())
        .limit(limit)
    )

    result = await db.execute(query)
    records = result.scalars().all()

    # 转换为事件格式
    items = []
    for record in records:
        # 根据记录类型确定事件类型
        if record.record_type == "error":
            evt_type = "error"
            level = "error"
        elif record.record_type in ["agent_think", "subagent_start"]:
            evt_type = "agent"
            level = "info"
        elif record.record_type == "tool_call":
            evt_type = "tool"
            level = "info"
        else:
            evt_type = "system"
            level = "info"

        # 如果指定了事件类型过滤
        if event_type and evt_type != event_type:
            continue

        items.append(
            SystemEvent(
                id=str(record.id),
                event_type=evt_type,
                message=(record.content or record.executor_name or "")[:100],
                level=level,
                timestamp=record.created_at.isoformat(),
            )
        )

    return EventListResponse(items=items, total=len(items))


# ============================================================================
# 资源监控 API
# ============================================================================


@router.get(
    "/resources",
    response_model=ResourceUsageResponse,
    summary="获取资源使用情况",
    description="获取 Token 消耗、配额状态、成本估算等资源使用信息",
)
async def get_resource_usage(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> ResourceUsageResponse:
    """获取资源使用情况"""
    user_id = str(current_user.id)
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # 统计今日执行记录数 (作为请求次数的近似)
    today_query = (
        select(func.count(ExecutionRecord.id))
        .join(Session, ExecutionRecord.session_id == Session.id)
        .where(
            and_(
                Session.user_id == user_id,
                ExecutionRecord.created_at >= today_start,
                func.json_extract(ExecutionRecord.message_data, "$.record_type").in_(
                    ["agent_think", "tool_call"]
                ),
            )
        )
    )
    today_result = await db.execute(today_query)
    today_requests = today_result.scalar() or 0

    # 统计本月执行记录数
    month_query = (
        select(func.count(ExecutionRecord.id))
        .join(Session, ExecutionRecord.session_id == Session.id)
        .where(
            and_(
                Session.user_id == user_id,
                ExecutionRecord.created_at >= month_start,
                func.json_extract(ExecutionRecord.message_data, "$.record_type").in_(
                    ["agent_think", "tool_call"]
                ),
            )
        )
    )
    month_result = await db.execute(month_query)
    month_requests = month_result.scalar() or 0

    # 统计总执行记录数
    total_query = (
        select(func.count(ExecutionRecord.id))
        .join(Session, ExecutionRecord.session_id == Session.id)
        .where(
            and_(
                Session.user_id == user_id,
                func.json_extract(ExecutionRecord.message_data, "$.record_type").in_(
                    ["agent_think", "tool_call"]
                ),
            )
        )
    )
    total_result = await db.execute(total_query)
    total_requests = total_result.scalar() or 0

    # 估算 Token 消耗 (假设每条记录平均 500 tokens)
    avg_tokens_per_record = 500
    today_tokens = today_requests * avg_tokens_per_record
    month_tokens = month_requests * avg_tokens_per_record
    total_tokens = total_requests * avg_tokens_per_record

    # 计算配额使用率
    daily_usage_percent = (today_tokens / DEFAULT_DAILY_TOKEN_LIMIT) * 100
    monthly_usage_percent = (month_tokens / DEFAULT_MONTHLY_TOKEN_LIMIT) * 100

    # 确定告警级别
    max_usage = max(daily_usage_percent, monthly_usage_percent)
    if max_usage >= 100:
        alert_level = AlertLevel.EXHAUSTED
    elif max_usage >= 90:
        alert_level = AlertLevel.CRITICAL
    elif max_usage >= 80:
        alert_level = AlertLevel.WARNING
    else:
        alert_level = AlertLevel.INFO

    # 计算成本
    today_cost = (today_tokens / 1000) * TOKEN_COST_PER_1K
    month_cost = (month_tokens / 1000) * TOKEN_COST_PER_1K

    # 预估月度成本 (基于当前日均)
    days_in_month = 30
    days_elapsed = now.day
    if days_elapsed > 0:
        estimated_monthly = (month_cost / days_elapsed) * days_in_month
    else:
        estimated_monthly = 0.0

    # 统计活跃会话数 (作为活跃 Agent 的近似)
    # 活跃会话 = 有未完成的 execution_record 的会话
    active_query = (
        select(func.count(func.distinct(ExecutionRecord.session_id)))
        .join(Session, ExecutionRecord.session_id == Session.id)
        .where(
            and_(
                Session.user_id == user_id,
                func.json_extract(ExecutionRecord.message_data, "$.status").in_(
                    ["pending", "running"]
                ),
            )
        )
    )
    active_result = await db.execute(active_query)
    active_agents = active_result.scalar() or 0

    return ResourceUsageResponse(
        token_usage=TokenUsageStats(
            today_tokens=today_tokens,
            today_requests=today_requests,
            month_tokens=month_tokens,
            month_requests=month_requests,
            total_tokens=total_tokens,
            total_requests=total_requests,
        ),
        quota_status=QuotaStatus(
            daily_limit=DEFAULT_DAILY_TOKEN_LIMIT,
            monthly_limit=DEFAULT_MONTHLY_TOKEN_LIMIT,
            daily_usage_percent=min(daily_usage_percent, 100),
            monthly_usage_percent=min(monthly_usage_percent, 100),
            alert_level=alert_level,
        ),
        cost_estimate=CostEstimate(
            today_cost=round(today_cost, 4),
            month_cost=round(month_cost, 4),
            estimated_monthly=round(estimated_monthly, 4),
        ),
        active_agents=active_agents,
        max_concurrent=DEFAULT_MAX_CONCURRENT,
        queued_tasks=await _get_queued_tasks_count(db),  # 从数据库获取队列任务数
        updated_at=now,
    )


# ============================================================================
# 会话资源监控 API
# ============================================================================


@router.get(
    "/sessions/{session_id}/resources",
    response_model=SessionResourceResponse,
    summary="获取会话资源统计",
    description="获取指定会话的 Token 消耗、成本、活跃 Agent 等信息",
)
async def get_session_resources(
    session_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> SessionResourceResponse:
    """获取会话资源统计"""
    user_id = str(current_user.id)

    # 验证会话存在且属于当前用户
    session_query = select(Session).where(
        and_(Session.id == session_id, Session.user_id == user_id)
    )
    session_result = await db.execute(session_query)
    session = session_result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    # 统计会话执行记录数
    record_query = select(func.count(ExecutionRecord.id)).where(
        and_(
            ExecutionRecord.session_id == session_id,
            func.json_extract(ExecutionRecord.message_data, "$.record_type").in_(
                ["agent_think", "tool_call"]
            ),
        )
    )
    record_result = await db.execute(record_query)
    record_count = record_result.scalar() or 0

    # 估算 Token 消耗
    avg_tokens_per_record = 500
    token_usage = record_count * avg_tokens_per_record
    estimated_cost = (token_usage / 1000) * TOKEN_COST_PER_1K

    # 计算已用时间
    elapsed_seconds = 0
    if session.created_at:
        elapsed_seconds = int((datetime.now() - session.created_at).total_seconds())

    # 计算配额使用率
    quota_usage_percent = (token_usage / DEFAULT_DAILY_TOKEN_LIMIT) * 100

    # 计算活跃 Agent 数（检查是否有未完成的 execution_record）
    active_record_query = select(func.count(ExecutionRecord.id)).where(
        and_(
            ExecutionRecord.session_id == session_id,
            func.json_extract(ExecutionRecord.message_data, "$.status").in_(
                ["pending", "running"]
            ),
        )
    )
    active_record_result = await db.execute(active_record_query)
    active_agents = 1 if (active_record_result.scalar() or 0) > 0 else 0

    return SessionResourceResponse(
        stats=SessionResourceStats(
            session_id=session_id,
            token_usage=token_usage,
            estimated_cost=round(estimated_cost, 4),
            elapsed_time_seconds=elapsed_seconds,
            active_agents=active_agents,
            max_concurrent=DEFAULT_MAX_CONCURRENT,
            queued_tasks=0,
            quota_limit=DEFAULT_DAILY_TOKEN_LIMIT,
            quota_usage_percent=min(quota_usage_percent, 100),
        ),
        updated_at=datetime.now(),
    )


# ============================================================================
# Agent 可靠性 API
# ============================================================================


@router.get(
    "/agents/reliability",
    response_model=AgentReliabilityListResponse,
    summary="获取 Agent 可靠性列表",
    description="获取所有 Agent 的可靠性指标",
)
async def get_agent_reliability_list(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AgentReliabilityListResponse:
    """获取 Agent 可靠性列表"""
    # 查询所有活跃 Agent 配置
    query = select(AgentConfig).where(AgentConfig.is_active)
    count_query = select(func.count(AgentConfig.id)).where(
        AgentConfig.is_active
    )

    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    query = query.order_by(AgentConfig.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    agents = result.scalars().all()

    items = []
    for agent in agents:
        # 从 Agent 配置中获取可靠性指标 (如果有)
        model_params = agent.model_params or {}
        reliability_data = model_params.get("reliability", {})

        items.append(
            AgentReliability(
                agent_id=str(agent.id),
                agent_name=agent.name,
                reliability_score=reliability_data.get("score", 0.0),
                total_executions=reliability_data.get("total_executions", 0),
                success_count=reliability_data.get("success_count", 0),
                failure_count=reliability_data.get("failure_count", 0),
                success_rate=reliability_data.get("success_rate", 0.0),
                avg_retries=reliability_data.get("avg_retries", 0.0),
                avg_execution_time_ms=reliability_data.get(
                    "avg_execution_time_ms", 0.0
                ),
                last_execution_at=None,
            )
        )

    return AgentReliabilityListResponse(items=items, total=total)


@router.get(
    "/agents/reliability/ranking",
    response_model=AgentReliabilityRankingResponse,
    summary="获取 Agent 可靠性排行",
    description="获取 Agent 可靠性排行榜，按可靠性评分降序排列",
)
async def get_agent_reliability_ranking(
    limit: int = Query(10, ge=1, le=50, description="返回数量"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AgentReliabilityRankingResponse:
    """获取 Agent 可靠性排行"""
    # 查询所有活跃 Agent 配置
    query = select(AgentConfig).where(AgentConfig.is_active).limit(limit * 2)

    result = await db.execute(query)
    agents = result.scalars().all()

    # 构建可靠性列表并排序
    items = []
    for agent in agents:
        model_params = agent.model_params or {}
        reliability_data = model_params.get("reliability", {})

        items.append(
            AgentReliability(
                agent_id=str(agent.id),
                agent_name=agent.name,
                reliability_score=reliability_data.get("score", 0.0),
                total_executions=reliability_data.get("total_executions", 0),
                success_count=reliability_data.get("success_count", 0),
                failure_count=reliability_data.get("failure_count", 0),
                success_rate=reliability_data.get("success_rate", 0.0),
                avg_retries=reliability_data.get("avg_retries", 0.0),
                avg_execution_time_ms=reliability_data.get(
                    "avg_execution_time_ms", 0.0
                ),
                last_execution_at=None,
            )
        )

    # 按可靠性评分降序排序
    items.sort(key=lambda x: x.reliability_score, reverse=True)

    return AgentReliabilityRankingResponse(
        ranking=items[:limit],
        updated_at=datetime.now(),
    )


@router.get(
    "/agents/{agent_id}/reliability",
    response_model=AgentReliabilityResponse,
    summary="获取单个 Agent 可靠性",
    description="获取指定 Agent 的可靠性指标",
)
async def get_agent_reliability(
    agent_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AgentReliabilityResponse:
    """获取单个 Agent 可靠性"""
    query = select(AgentConfig).where(AgentConfig.id == agent_id)
    result = await db.execute(query)
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent 不存在"
        )

    model_params = agent.model_params or {}
    reliability_data = model_params.get("reliability", {})

    return AgentReliabilityResponse(
        agent=AgentReliability(
            agent_id=str(agent.id),
            agent_name=agent.name,
            reliability_score=reliability_data.get("score", 0.0),
            total_executions=reliability_data.get("total_executions", 0),
            success_count=reliability_data.get("success_count", 0),
            failure_count=reliability_data.get("failure_count", 0),
            success_rate=reliability_data.get("success_rate", 0.0),
            avg_retries=reliability_data.get("avg_retries", 0.0),
            avg_execution_time_ms=reliability_data.get("avg_execution_time_ms", 0.0),
            last_execution_at=None,
        )
    )


# ============================================================================
# 执行图 API
# ============================================================================


@router.get(
    "/sessions/{session_id}/execution-graph",
    response_model=ExecutionGraphResponse,
    summary="获取执行图",
    description="获取指定会话的执行图，包含节点状态、AC、Token 消耗等",
)
async def get_execution_graph(
    session_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> ExecutionGraphResponse:
    """获取执行图"""
    user_id = str(current_user.id)

    # 验证会话存在且属于当前用户
    session_query = select(Session).where(
        and_(Session.id == session_id, Session.user_id == user_id)
    )
    session_result = await db.execute(session_query)
    session = session_result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    # 从 execution_records 构建执行图
    # 查询所有执行记录
    records_query = (
        select(ExecutionRecord)
        .where(ExecutionRecord.session_id == session_id)
        .order_by(
            func.json_extract(ExecutionRecord.message_data, "$.order.sequence").asc(),
            ExecutionRecord.created_at.asc(),
        )
    )
    records_result = await db.execute(records_query)
    records = records_result.scalars().all()

    # 转换为节点和边
    nodes = []
    edges = []
    total_token_usage = 0
    total_execution_time_ms = 0
    current_node_id = None

    for record in records:
        # 从 message_data 中提取字段
        message_data = record.message_data or {}
        node_status = message_data.get("status", "pending")
        record_type = message_data.get("record_type", "tool_call")
        executor = message_data.get("executor", {})
        executor_name = executor.get("name", "Unknown")
        order = message_data.get("order", {})
        sequence = order.get("sequence", 0)
        timing = message_data.get("timing", {})
        duration_ms = timing.get("duration_ms", 0)
        started_at = timing.get("started_at")
        completed_at = timing.get("completed_at")

        # 映射状态
        status_map = {
            "pending": ExecutionNodeStatus.PENDING,
            "running": ExecutionNodeStatus.RUNNING,
            "completed": ExecutionNodeStatus.COMPLETED,
            "failed": ExecutionNodeStatus.FAILED,
        }
        node_status = status_map.get(node_status, ExecutionNodeStatus.PENDING)

        if node_status == ExecutionNodeStatus.RUNNING:
            current_node_id = str(record.id)

        # 映射节点类型
        type_map = {
            "tool_call": ExecutionNodeType.TOOL,
            "agent_think": ExecutionNodeType.AGENT,
            "workflow_node": ExecutionNodeType.WORKFLOW,
        }
        node_type = type_map.get(record_type, ExecutionNodeType.TOOL)

        execution_time_ms = duration_ms or 0
        total_execution_time_ms += execution_time_ms

        nodes.append(
            ExecutionNode(
                id=str(record.id),
                node_type=node_type,
                name=executor_name or f"节点 {sequence}",
                status=node_status,
                acceptance_criteria=None,  # AC 信息在 Task 表中
                ac_met=None,
                token_usage=0,  # Token 统计需要单独实现
                execution_time_ms=execution_time_ms,
                dependencies=(
                    [str(record.parent_record_id)] if record.parent_record_id else []
                ),
                start_time=started_at,
                end_time=completed_at,
                error_message=message_data.get("error"),
            )
        )

        # 构建边（父子关系）
        if record.parent_record_id:
            edges.append(
                {
                    "source": str(record.parent_record_id),
                    "target": str(record.id),
                }
            )

    return ExecutionGraphResponse(
        session_id=session_id,
        nodes=nodes,
        edges=edges,
        current_node_id=current_node_id,
        total_token_usage=total_token_usage,
        total_execution_time_ms=total_execution_time_ms,
    )


# ============================================================================
# 用量告警 API
# ============================================================================


@router.get(
    "/alerts",
    response_model=UsageAlertListResponse,
    summary="获取用量告警列表",
    description="获取用户的用量告警列表",
)
async def get_usage_alerts(
    alert_type: str | None = Query(None, description="告警类型"),
    level: str | None = Query(None, description="告警级别"),
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    acknowledged: bool | None = Query(None, description="是否已确认"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> UsageAlertListResponse:
    """获取用量告警列表"""
    # 从监控服务获取告警数据
    from src.services.monitoring_service import MonitoringService

    monitoring_service = MonitoringService(db)
    alerts = await monitoring_service.get_alerts(
        user_id=current_user.id if current_user else None,
        alert_type=alert_type,
        level=level,
        acknowledged=acknowledged,
        limit=limit,
        offset=offset,
    )

    # 转换为响应格式
    items = []
    for alert in alerts:
        usage_alert = UsageAlert(
            id=alert.id,
            level=AlertLevel(alert.level),
            usage_percent=alert.usage_percent or 0.0,
            threshold=alert.threshold or 80.0,
            message=alert.message,
            acknowledged=alert.acknowledged,
            created_at=alert.created_at,
        )
        items.append(usage_alert)

    # 统计未确认告警数量
    unacknowledged_count = await monitoring_service.count_unacknowledged_alerts(
        user_id=current_user.id if current_user else None
    )

    # 过滤
    if acknowledged is not None:
        items = [item for item in items if item.acknowledged == acknowledged]

    return UsageAlertListResponse(
        items=items[:limit],
        total=len(items),
        unacknowledged_count=unacknowledged_count,
    )


def _build_alert_message(level: AlertLevel, quota_status: QuotaStatus) -> str:
    """构建告警消息"""
    if level == AlertLevel.EXHAUSTED:
        return (
            f"⛔ API 配额已耗尽! "
            f"每日使用率: {quota_status.daily_usage_percent:.1f}%, "
            f"每月使用率: {quota_status.monthly_usage_percent:.1f}%"
        )
    elif level == AlertLevel.CRITICAL:
        return (
            f"🚨 API 配额即将耗尽! "
            f"每日使用率: {quota_status.daily_usage_percent:.1f}%, "
            f"每月使用率: {quota_status.monthly_usage_percent:.1f}%"
        )
    elif level == AlertLevel.WARNING:
        return (
            f"[警告] API 配额使用警告 "
            f"每日使用率: {quota_status.daily_usage_percent:.1f}%, "
            f"每月使用率: {quota_status.monthly_usage_percent:.1f}%"
        )
    else:
        return "[正常] 用量正常"
