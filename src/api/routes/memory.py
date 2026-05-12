"""
记忆路由

提供记忆管理相关的 API 端点
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import generate_trace_id
from src.api.errors import create_error_response
from src.api.routes.auth import get_current_user
from src.db.connection import get_async_session
from src.memory.service import MemoryService

logger = logging.getLogger(__name__)

router = APIRouter()


async def get_memory_service(
    session: AsyncSession = Depends(get_async_session),
) -> MemoryService:
    """
    获取记忆服务实例

    Args:
        session: 数据库会话

    Returns:
        MemoryService: 记忆服务实例
    """
    return MemoryService(session=session)


# ============================================================================
# 数据模型
# ============================================================================


class MemorySearchRequest(BaseModel):
    """记忆搜索请求"""

    query: str = Field(..., min_length=1, description="搜索查询")
    memory_types: list[str] | None = Field(None, description="记忆类型过滤")
    top_k: int = Field(default=10, ge=1, le=100, description="返回数量")
    min_score: float = Field(default=0.5, ge=0, le=1, description="最小相关性得分")


class MemoryItem(BaseModel):
    """记忆项"""

    id: UUID = Field(..., description="记忆 ID")
    content: str = Field(..., description="内容")
    memory_type: str = Field(..., description="记忆类型")
    score: float = Field(..., ge=0, le=1, description="相关性得分")
    metadata: dict[str, Any] | None = Field(None, description="元数据")
    created_at: str = Field(..., description="创建时间")


class MemorySearchResponse(BaseModel):
    """记忆搜索响应"""

    items: list[MemoryItem] = Field(..., description="搜索结果")
    total: int = Field(..., ge=0, description="总数量")
    query: str = Field(..., description="搜索查询")


class EpisodeCreateRequest(BaseModel):
    """情景记忆创建请求"""

    intent_text: str = Field(..., min_length=1, description="意图文本")
    plan_dag: dict[str, Any] | None = Field(None, description="执行计划")
    execution_summary: str | None = Field(None, description="执行摘要")
    evaluation_report: dict[str, Any] | None = Field(None, description="评估报告")
    final_score: float | None = Field(None, ge=0, le=1, description="最终得分")
    tags: list[str] = Field(default_factory=list, description="标签")


class EpisodeResponse(BaseModel):
    """情景记忆响应"""

    id: UUID = Field(..., description="记忆 ID")
    intent_text: str = Field(..., description="意图文本")
    plan_dag: dict[str, Any] | None = Field(None, description="执行计划")
    execution_summary: str | None = Field(None, description="执行摘要")
    final_score: float | None = Field(None, description="最终得分")
    tags: list[str] = Field(default_factory=list, description="标签")
    created_at: str = Field(..., description="创建时间")


class KnowledgeCreateRequest(BaseModel):
    """知识创建请求"""

    content: str = Field(..., min_length=1, description="知识内容")
    source_type: str = Field(..., description="来源类型")
    extra_data: dict[str, Any] | None = Field(None, description="额外数据")


class KnowledgeResponse(BaseModel):
    """知识响应"""

    id: UUID = Field(..., description="知识 ID")
    content: str = Field(..., description="知识内容")
    source_type: str = Field(..., description="来源类型")
    extra_data: dict[str, Any] | None = Field(None, description="额外数据")
    created_at: str = Field(..., description="创建时间")


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "/episodes",
    summary="获取情景记忆列表",
    description="获取当前用户的情景记忆列表",
)
async def list_episodes(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
):
    """获取情景记忆列表"""
    try:
        result = await memory_service.list_episodes(
            user_id=current_user.id, page=page, page_size=page_size
        )
        return result
    except Exception as exc:
        # 记录异常但不中断请求，返回空列表
        logger.warning(f"获取情景记忆列表失败: {exc}", exc_info=True)
        return {"items": [], "total": 0, "page": page, "page_size": page_size}


@router.post(
    "/search",
    response_model=MemorySearchResponse,
    summary="搜索记忆",
    description="根据查询搜索相关记忆",
)
async def search_memory(
    request: MemorySearchRequest,
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
) -> MemorySearchResponse:
    """搜索记忆"""
    try:
        result = await memory_service.search(
            user_id=current_user.id,
            query=request.query,
            memory_types=request.memory_types,
            top_k=request.top_k,
            min_score=request.min_score,
        )
        return MemorySearchResponse(**result)
    except Exception as exc:
        # 搜索失败时返回空结果而不是错误，避免中断用户请求
        logger.warning(f"记忆搜索失败: {exc}", exc_info=True)
        return MemorySearchResponse(items=[], total=0, query=request.query)


@router.post(
    "/episodes",
    response_model=EpisodeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建情景记忆",
    description="创建新的情景记忆",
)
async def create_episode(
    request: EpisodeCreateRequest,
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
) -> EpisodeResponse:
    """创建情景记忆"""
    result = await memory_service.create_episode(
        user_id=current_user.id, **request.model_dump()
    )
    return EpisodeResponse(**result)


@router.get(
    "/episodes/{episode_id}",
    response_model=EpisodeResponse,
    summary="获取情景记忆",
    description="获取指定的情景记忆",
)
async def get_episode(
    episode_id: UUID,
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
) -> EpisodeResponse:
    """获取情景记忆"""
    result = await memory_service.get_episode(
        episode_id=episode_id, user_id=current_user.id
    )

    if result is None:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="MEM_001",
            trace_id=trace_id,
            path=f"/api/v1/memory/episodes/{episode_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    return EpisodeResponse(**result)


@router.post(
    "/knowledge",
    response_model=KnowledgeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识",
    description="创建新的语义知识",
)
async def create_knowledge(
    request: KnowledgeCreateRequest,
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
) -> KnowledgeResponse:
    """创建知识"""
    result = await memory_service.create_knowledge(
        user_id=current_user.id, **request.model_dump()
    )
    return KnowledgeResponse(**result)


@router.delete(
    "/episodes/{episode_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除情景记忆",
    description="删除指定的情景记忆",
)
async def delete_episode(
    episode_id: UUID,
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
) -> None:
    """删除情景记忆"""
    success = await memory_service.delete_episode(
        episode_id=episode_id, user_id=current_user.id
    )

    if not success:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="MEM_001",
            trace_id=trace_id,
            path=f"/api/v1/memory/episodes/{episode_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )


@router.get(
    "/semantic",
    summary="获取语义记忆列表",
    description="获取当前用户的语义知识列表",
)
async def list_semantic_memory(
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
):
    """获取语义记忆列表"""
    try:
        result = await memory_service.list_semantic_memory(user_id=current_user.id)
        return result
    except Exception as exc:
        # 记录异常但不中断请求，返回空列表
        logger.warning(f"获取语义记忆列表失败: {exc}", exc_info=True)
        return {"items": [], "total": 0}


@router.post(
    "/consolidate",
    summary="记忆整合",
    description="整合用户的情景记忆和语义知识",
)
async def consolidate_memory(
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
):
    """记忆整合"""
    try:
        result = await memory_service.consolidate(user_id=current_user.id)
        return result
    except Exception as exc:
        # 记录整合失败的异常
        logger.error(f"记忆整合失败: {exc}", exc_info=True)
        return {"success": False, "message": "记忆整合失败", "consolidated_count": 0}


@router.get(
    "/stats",
    summary="获取记忆统计",
    description="获取当前用户的记忆统计数据",
)
async def get_memory_stats(
    current_user=Depends(get_current_user),
    memory_service: MemoryService = Depends(get_memory_service),
):
    """获取记忆统计"""
    try:
        result = await memory_service.get_stats(user_id=current_user.id)
        return result
    except Exception as exc:
        # 记录异常但不中断请求，返回空统计
        logger.warning(f"获取记忆统计失败: {exc}", exc_info=True)
        return {
            "episode_count": 0,
            "knowledge_count": 0,
            "total_count": 0,
            "last_updated": "",
        }
