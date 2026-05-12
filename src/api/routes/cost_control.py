"""
成本控制 API 路由

提供成本监控、预算管理、使用统计等 API 端点
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from src.api.routes.auth import get_current_user
from src.cost_control.budget_manager import get_budget_manager
from src.cost_control.config import get_cost_control_config

router = APIRouter()


# ============================================================================
# 响应模型
# ============================================================================


class BudgetStatusResponse(BaseModel):
    """预算状态响应"""

    scope: str = Field(..., description="范围: global, user, task, session")
    scope_id: str | None = Field(None, description="范围 ID")
    limit: int = Field(..., description="Token 限制")
    used: int = Field(..., description="已使用 Token")
    remaining: int = Field(..., description="剩余 Token")
    usage_percent: float = Field(..., description="使用率 (%)")
    alert_level: str = Field(..., description="告警级别")
    estimated_cost: float = Field(..., description="估算成本 ($)")


class GlobalUsageStats(BaseModel):
    """全局使用统计"""

    daily_tokens: int = Field(..., description="今日 Token 用量")
    monthly_tokens: int = Field(..., description="本月 Token 用量")
    daily_limit: int = Field(..., description="每日限制")
    monthly_limit: int = Field(..., description="每月限制")
    daily_usage_percent: float = Field(..., description="每日使用率 (%)")
    monthly_usage_percent: float = Field(..., description="每月使用率 (%)")
    estimated_daily_cost: float = Field(..., description="今日估算成本 ($)")
    estimated_monthly_cost: float = Field(..., description="本月估算成本 ($)")


class TaskUsageStats(BaseModel):
    """任务使用统计"""

    task_id: str = Field(..., description="任务 ID")
    tokens: int = Field(..., description="Token 用量")
    limit: int = Field(..., description="限制")
    usage_percent: float = Field(..., description="使用率 (%)")


class SessionUsageStats(BaseModel):
    """会话使用统计"""

    session_id: str = Field(..., description="会话 ID")
    tokens: int = Field(..., description="Token 用量")
    limit: int = Field(..., description="限制")
    usage_percent: float = Field(..., description="使用率 (%)")


class UsageRecord(BaseModel):
    """使用记录"""

    tokens: int = Field(..., description="Token 数")
    model: str = Field(..., description="模型名称")
    cost: float = Field(..., description="成本 ($)")
    timestamp: str = Field(..., description="时间戳")


class UsageStatisticsResponse(BaseModel):
    """使用统计响应"""

    global_stats: GlobalUsageStats = Field(..., description="全局统计")
    tasks: list[TaskUsageStats] = Field(default_factory=list, description="任务统计")
    sessions: list[SessionUsageStats] = Field(
        default_factory=list, description="会话统计"
    )
    recent_records: list[UsageRecord] = Field(
        default_factory=list, description="最近记录"
    )
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")


class CostConfigResponse(BaseModel):
    """成本配置响应"""

    daily_token_limit: int = Field(..., description="每日 Token 限制")
    monthly_token_limit: int = Field(..., description="每月 Token 限制")
    per_task_token_limit: int = Field(..., description="单任务 Token 限制")
    per_session_token_limit: int = Field(..., description="单会话 Token 限制")
    warning_threshold: float = Field(..., description="警告阈值")
    critical_threshold: float = Field(..., description="严重阈值")
    auto_save_at_warning: bool = Field(..., description="警告时自动保存")
    auto_pause_at_critical: bool = Field(..., description="严重时自动暂停")
    auto_stop_at_exhausted: bool = Field(..., description="耗尽时自动停止")


class CostReportResponse(BaseModel):
    """成本报表响应"""

    period: str = Field(..., description="统计周期: daily, weekly, monthly")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    total_tokens: int = Field(..., description="总 Token 数")
    total_cost: float = Field(..., description="总成本 ($)")
    by_model: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="按模型统计"
    )
    by_task: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="按任务统计"
    )
    daily_breakdown: list[dict[str, Any]] = Field(
        default_factory=list, description="每日明细"
    )


# ============================================================================
# API 路由
# ============================================================================


@router.get(
    "/budget/status",
    response_model=BudgetStatusResponse,
    summary="获取预算状态",
    description="获取当前预算使用状态",
)
async def get_budget_status(
    task_id: str | None = Query(None, description="任务 ID"),
    session_id: str | None = Query(None, description="会话 ID"),
    current_user=Depends(get_current_user),
) -> BudgetStatusResponse:
    """获取预算状态"""
    budget_manager = get_budget_manager()
    user_id = str(current_user.id)

    status = budget_manager.get_budget_status(
        user_id=user_id,
        task_id=task_id,
        session_id=session_id,
    )

    return BudgetStatusResponse(
        scope=status.scope,
        scope_id=status.scope_id,
        limit=status.limit,
        used=status.used,
        remaining=status.remaining,
        usage_percent=status.usage_percent,
        alert_level=status.alert_level.value,
        estimated_cost=round(status.estimated_cost, 4),
    )


@router.get(
    "/usage/statistics",
    response_model=UsageStatisticsResponse,
    summary="获取使用统计",
    description="获取 Token 使用统计，包括全局、任务、会话维度",
)
async def get_usage_statistics(
    current_user=Depends(get_current_user),
) -> UsageStatisticsResponse:
    """获取使用统计"""
    budget_manager = get_budget_manager()
    stats = budget_manager.get_usage_statistics()

    global_data = stats.get("global", {})
    tasks_data = stats.get("tasks", {})
    sessions_data = stats.get("sessions", {})
    records_data = stats.get("recent_records", [])

    return UsageStatisticsResponse(
        global_stats=GlobalUsageStats(
            daily_tokens=global_data.get("daily_tokens", 0),
            monthly_tokens=global_data.get("monthly_tokens", 0),
            daily_limit=global_data.get("daily_limit", 100000),
            monthly_limit=global_data.get("monthly_limit", 3000000),
            daily_usage_percent=global_data.get("daily_usage_percent", 0),
            monthly_usage_percent=global_data.get("monthly_usage_percent", 0),
            estimated_daily_cost=round(global_data.get("estimated_daily_cost", 0), 4),
            estimated_monthly_cost=round(
                global_data.get("estimated_monthly_cost", 0), 4
            ),
        ),
        tasks=[
            TaskUsageStats(
                task_id=task_id,
                tokens=data.get("tokens", 0),
                limit=data.get("limit", 50000),
                usage_percent=data.get("usage_percent", 0),
            )
            for task_id, data in tasks_data.items()
        ],
        sessions=[
            SessionUsageStats(
                session_id=session_id,
                tokens=data.get("tokens", 0),
                limit=data.get("limit", 100000),
                usage_percent=data.get("usage_percent", 0),
            )
            for session_id, data in sessions_data.items()
        ],
        recent_records=[
            UsageRecord(
                tokens=r.get("tokens", 0),
                model=r.get("model", "unknown"),
                cost=round(r.get("cost", 0), 6),
                timestamp=r.get("timestamp", ""),
            )
            for r in records_data
        ],
        updated_at=datetime.now(),
    )


@router.get(
    "/config",
    response_model=CostConfigResponse,
    summary="获取成本控制配置",
    description="获取当前成本控制配置",
)
async def get_cost_config(
    current_user=Depends(get_current_user),
) -> CostConfigResponse:
    """获取成本控制配置"""
    config = get_cost_control_config()

    return CostConfigResponse(
        daily_token_limit=config.global_budget.daily_token_limit,
        monthly_token_limit=config.global_budget.monthly_token_limit,
        per_task_token_limit=config.global_budget.per_task_token_limit,
        per_session_token_limit=config.global_budget.per_session_token_limit,
        warning_threshold=config.alerts.warning_threshold,
        critical_threshold=config.alerts.critical_threshold,
        auto_save_at_warning=config.protection.auto_save_at_warning,
        auto_pause_at_critical=config.protection.auto_pause_at_critical,
        auto_stop_at_exhausted=config.protection.auto_stop_at_exhausted,
    )


@router.get(
    "/report",
    response_model=CostReportResponse,
    summary="获取成本报表",
    description="获取指定周期的成本报表",
)
async def get_cost_report(
    period: str = Query("daily", description="统计周期: daily, weekly, monthly"),
    current_user=Depends(get_current_user),
) -> CostReportResponse:
    """获取成本报表"""
    budget_manager = get_budget_manager()
    stats = budget_manager.get_usage_statistics()

    now = datetime.now()
    global_data = stats.get("global", {})
    tasks_data = stats.get("tasks", {})

    # 根据周期确定日期范围
    if period == "daily":
        start_date = now.strftime("%Y-%m-%d")
        end_date = start_date
        total_tokens = global_data.get("daily_tokens", 0)
    elif period == "weekly":
        # 简化处理，使用本月数据
        start_date = now.replace(day=max(1, now.day - 7)).strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")
        total_tokens = global_data.get("monthly_tokens", 0) // 4  # 近似
    else:  # monthly
        start_date = now.replace(day=1).strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")
        total_tokens = global_data.get("monthly_tokens", 0)

    # 计算成本
    config = get_cost_control_config()
    cost_rate = config.cost_rates.default
    total_cost = (total_tokens / 1000) * cost_rate

    # 按任务统计
    by_task = {
        task_id: {
            "tokens": data.get("tokens", 0),
            "cost": round((data.get("tokens", 0) / 1000) * cost_rate, 4),
            "usage_percent": data.get("usage_percent", 0),
        }
        for task_id, data in tasks_data.items()
    }

    return CostReportResponse(
        period=period,
        start_date=start_date,
        end_date=end_date,
        total_tokens=total_tokens,
        total_cost=round(total_cost, 4),
        by_model={},  # 需要更详细的记录才能按模型统计
        by_task=by_task,
        daily_breakdown=[],  # 需要持久化存储才能提供每日明细
    )


@router.post(
    "/budget/reset",
    summary="重置预算",
    description="重置指定任务或会话的预算计数",
)
async def reset_budget(
    task_id: str | None = Query(None, description="任务 ID"),
    session_id: str | None = Query(None, description="会话 ID"),
    current_user=Depends(get_current_user),
) -> dict[str, str]:
    """重置预算"""
    budget_manager = get_budget_manager()

    if task_id:
        await budget_manager.reset_task_budget(task_id)
        return {"message": f"任务 {task_id} 预算已重置"}
    elif session_id:
        await budget_manager.reset_session_budget(session_id)
        return {"message": f"会话 {session_id} 预算已重置"}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请指定 task_id 或 session_id",
        )
