"""
Agent 调用记录路由

提供 Agent 调用记录的查询 API
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.auth import get_current_user
from src.db.connection import get_async_session
from src.db.repositories.agent_call_repository import AgentCallRepository

router = APIRouter()


# ============================================================================
# 数据模型
# ============================================================================


class AgentCallRecordResponse(BaseModel):
    """Agent 调用记录响应"""

    id: str = Field(..., description="记录 ID")
    execution_id: str = Field(..., description="执行 ID")
    caller_level: str = Field(..., description="调用者层级")
    target_agent_id: str = Field(..., description="目标 Agent ID")
    target_agent_name: str = Field(..., description="目标 Agent 名称")
    operation_type: str = Field(..., description="操作类型")
    instruction_summary: str = Field(..., description="指令摘要")
    status: str = Field(..., description="状态")
    success: bool | None = Field(None, description="是否成功")
    result_summary: str | None = Field(None, description="结果摘要")
    error: str | None = Field(None, description="错误信息")
    start_time: datetime | None = Field(None, description="开始时间")
    end_time: datetime | None = Field(None, description="结束时间")
    duration: float | None = Field(None, description="执行时长（秒）")

    class Config:
        from_attributes = True


class AgentCallRecordDetailResponse(AgentCallRecordResponse):
    """Agent 调用记录详情响应（包含完整信息）"""

    instruction: str = Field(..., description="完整指令")
    context: dict[str, Any] | None = Field(None, description="上下文")
    result: dict[str, Any] | None = Field(None, description="执行结果")
    timeout: int = Field(300, description="超时时间")
    retry_count: int = Field(1, description="重试次数")
    priority: str = Field("normal", description="优先级")
    created_at: datetime = Field(..., description="创建时间")


class AgentCallListResponse(BaseModel):
    """Agent 调用记录列表响应"""

    records: list[AgentCallRecordResponse] = Field(..., description="记录列表")
    total: int = Field(..., description="总数")
    limit: int = Field(..., description="每页数量")
    offset: int = Field(..., description="偏移量")


class AgentCallStatisticsResponse(BaseModel):
    """Agent 调用统计响应"""

    total: int = Field(..., description="总调用次数")
    by_status: dict[str, int] = Field(..., description="按状态统计")
    by_caller_level: dict[str, int] = Field(..., description="按调用者层级统计")
    by_operation_type: dict[str, int] = Field(..., description="按操作类型统计")
    success_rate: float = Field(..., description="成功率 (%)")
    avg_duration: float = Field(..., description="平均执行时长（秒）")


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "",
    response_model=AgentCallListResponse,
    summary="获取 Agent 调用记录列表",
    description="查询 Agent 调用记录，支持多种过滤条件",
)
async def list_agent_calls(
    execution_id: str | None = Query(None, description="执行 ID"),
    target_agent_id: str | None = Query(None, description="目标 Agent ID"),
    caller_level: str | None = Query(None, description="调用者层级 (L1/L2)"),
    status: str | None = Query(None, description="状态"),
    operation_type: str | None = Query(None, description="操作类型"),
    start_time: datetime | None = Query(None, description="开始时间（范围查询）"),
    end_time: datetime | None = Query(None, description="结束时间（范围查询）"),
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AgentCallListResponse:
    """获取 Agent 调用记录列表"""
    repository = AgentCallRepository(db)

    # 查询记录
    records = await repository.query(
        execution_id=execution_id,
        target_agent_id=target_agent_id,
        caller_level=caller_level,
        status=status,
        operation_type=operation_type,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )

    # 统计总数
    total = await repository.count_by_filters(
        target_agent_id=target_agent_id,
        caller_level=caller_level,
        status=status,
        start_time=start_time,
        end_time=end_time,
    )

    return AgentCallListResponse(
        records=[
            AgentCallRecordResponse(
                id=r.id,
                execution_id=r.execution_id,
                caller_level=r.caller_level,
                target_agent_id=r.target_agent_id,
                target_agent_name=r.target_agent_name,
                operation_type=r.operation_type,
                instruction_summary=r.instruction_summary,
                status=r.status,
                success=r.success,
                result_summary=r.result_summary,
                error=r.error,
                start_time=r.start_time,
                end_time=r.end_time,
                duration=r.duration,
            )
            for r in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/statistics",
    response_model=AgentCallStatisticsResponse,
    summary="获取 Agent 调用统计",
    description="获取 Agent 调用的统计信息",
)
async def get_agent_call_statistics(
    target_agent_id: str | None = Query(None, description="目标 Agent ID"),
    start_time: datetime | None = Query(None, description="开始时间"),
    end_time: datetime | None = Query(None, description="结束时间"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AgentCallStatisticsResponse:
    """获取 Agent 调用统计"""
    repository = AgentCallRepository(db)

    stats = await repository.get_statistics(
        target_agent_id=target_agent_id,
        start_time=start_time,
        end_time=end_time,
    )

    return AgentCallStatisticsResponse(
        total=stats["total"],
        by_status=stats["by_status"],
        by_caller_level=stats["by_caller_level"],
        by_operation_type=stats["by_operation_type"],
        success_rate=stats["success_rate"],
        avg_duration=stats["avg_duration"],
    )


@router.get(
    "/{execution_id}",
    response_model=AgentCallRecordDetailResponse,
    summary="获取 Agent 调用记录详情",
    description="根据 execution_id 获取单条调用记录的详细信息",
)
async def get_agent_call_detail(
    execution_id: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AgentCallRecordDetailResponse:
    """获取 Agent 调用记录详情"""
    repository = AgentCallRepository(db)

    record = await repository.get_by_execution_id(execution_id)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"调用记录不存在: {execution_id}",
        )

    return AgentCallRecordDetailResponse(
        id=record.id,
        execution_id=record.execution_id,
        caller_level=record.caller_level,
        target_agent_id=record.target_agent_id,
        target_agent_name=record.target_agent_name,
        operation_type=record.operation_type,
        instruction=record.instruction,
        instruction_summary=record.instruction_summary,
        context=record.context,
        timeout=record.timeout,
        retry_count=record.retry_count,
        priority=record.priority,
        status=record.status,
        success=record.success,
        result=record.result,
        result_summary=record.result_summary,
        error=record.error,
        start_time=record.start_time,
        end_time=record.end_time,
        duration=record.duration,
        created_at=record.created_at,
    )
