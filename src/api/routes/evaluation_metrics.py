"""
评估指标 API 路由

提供评估指标的查询操作：
- 列出指标（支持按分类、状态过滤）
- 查询指标详情
- 获取分类列表

注意：评估指标已迁移到文件存储，不再支持动态创建、更新和删除。
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_current_user
from src.db.connection import get_async_session
from src.db.models import User
from src.evaluation.metric_loader import get_metric_loader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evaluation-metrics", tags=["evaluation_metrics"])


# ============================================================================
# Pydantic 请求/响应模型
# ============================================================================


class EvaluationMetricResponse(BaseModel):
    """评估指标响应"""

    id: str = Field(..., description="指标 ID")
    name: str = Field(..., description="指标名称")
    description: str = Field(..., description="指标描述")
    category: str = Field(..., description="指标分类")
    evaluator_type: str = Field(..., description="评估器类型")
    evaluator_id: str = Field(..., description="评估器 ID")
    default_config: dict[str, Any] | None = Field(
        default_factory=dict, description="默认配置"
    )
    input_schema: dict[str, Any] | None = Field(
        default_factory=dict, description="输入参数 Schema"
    )
    source: str = Field(..., description="来源")
    status: str = Field(..., description="状态")
    tags: list[str] | None = Field(default_factory=list, description="标签")
    includes: list[str] | None = Field(default=None, description="包含的低级指标列表")
    requires: list[str] | None = Field(default=None, description="前置依赖指标列表")
    level: int = Field(1, description="指标层级")
    is_red_line: bool = Field(False, description="是否红线指标")
    default_weight: float = Field(1.0, description="默认权重")


class MetricListResponse(BaseModel):
    """指标列表响应"""

    metrics: list[EvaluationMetricResponse] = Field(..., description="指标列表")
    total: int = Field(..., description="总数")
    category: str | None = Field(None, description="当前分类过滤")
    status: str = Field(..., description="当前状态过滤")


class CategoryListResponse(BaseModel):
    """分类列表响应"""

    categories: list[str] = Field(..., description="分类列表")


# ============================================================================
# API 端点
# ============================================================================


@router.get("", response_model=MetricListResponse)
async def list_metrics(
    category: str | None = Query(None, description="按分类过滤"),
    status: str = Query("active", description="按状态过滤"),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(100, ge=1, le=100, description="返回数量限制"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """列出评估指标

    支持按分类、状态过滤，返回可复用的评估指标列表。
    """
    metric_loader = get_metric_loader()
    metrics = await metric_loader.list_metrics(
        category=category, status=status, limit=limit, offset=skip
    )

    return MetricListResponse(
        metrics=[
            EvaluationMetricResponse(
                id=m.get("id", ""),
                name=m.get("name", ""),
                description=m.get("description", ""),
                category=m.get("category", ""),
                evaluator_type=m.get("evaluator_type", "tool"),
                evaluator_id=m.get("evaluator_id", ""),
                default_config=m.get("default_config", {}),
                input_schema=m.get("input_schema", {}),
                includes=m.get("includes"),
                requires=m.get("requires"),
                level=m.get("level", 1),
                source=m.get("source", "builtin"),
                status=m.get("status", "active"),
                tags=m.get("tags", []),
                is_red_line=m.get("is_red_line", False),
                default_weight=m.get("default_weight", 1.0),
            )
            for m in metrics
        ],
        total=len(metrics),
        category=category,
        status=status,
    )


@router.get("/categories", response_model=CategoryListResponse)
async def get_categories(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """获取所有指标分类

    返回可用的指标分类列表。
    """
    metric_loader = get_metric_loader()
    categories = await metric_loader.get_categories()
    return CategoryListResponse(categories=categories)


@router.get("/popular", response_model=list[EvaluationMetricResponse])
async def get_popular_metrics(
    limit: int = Query(10, ge=1, le=50, description="返回数量限制"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """获取常用指标

    返回所有活跃指标（统计功能已移除）。
    """
    metric_loader = get_metric_loader()
    metrics = await metric_loader.list_metrics(limit=limit)

    return [
        EvaluationMetricResponse(
            id=m.get("id", ""),
            name=m.get("name", ""),
            description=m.get("description", ""),
            category=m.get("category", ""),
            evaluator_type=m.get("evaluator_type", "tool"),
            evaluator_id=m.get("evaluator_id", ""),
            default_config=m.get("default_config", {}),
            input_schema=m.get("input_schema", {}),
            includes=m.get("includes"),
            requires=m.get("requires"),
            level=m.get("level", 1),
            source=m.get("source", "builtin"),
            status=m.get("status", "active"),
            tags=m.get("tags", []),
            is_red_line=m.get("is_red_line", False),
            default_weight=m.get("default_weight", 1.0),
        )
        for m in metrics
    ]


@router.get("/search", response_model=list[EvaluationMetricResponse])
async def search_metrics(
    keyword: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100, description="返回数量限制"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """搜索指标

    按名称或描述搜索评估指标。
    """
    metric_loader = get_metric_loader()
    all_metrics = await metric_loader.list_metrics(limit=1000)

    # 简单的名称匹配
    keyword_lower = keyword.lower()
    matched = [
        m for m in all_metrics
        if keyword_lower in m.get("name", "").lower()
        or keyword_lower in m.get("description", "").lower()
    ]

    return [
        EvaluationMetricResponse(
            id=m.get("id", ""),
            name=m.get("name", ""),
            description=m.get("description", ""),
            category=m.get("category", ""),
            evaluator_type=m.get("evaluator_type", "tool"),
            evaluator_id=m.get("evaluator_id", ""),
            default_config=m.get("default_config", {}),
            input_schema=m.get("input_schema", {}),
            includes=m.get("includes"),
            requires=m.get("requires"),
            level=m.get("level", 1),
            source=m.get("source", "builtin"),
            status=m.get("status", "active"),
            tags=m.get("tags", []),
            is_red_line=m.get("is_red_line", False),
            default_weight=m.get("default_weight", 1.0),
        )
        for m in matched[:limit]
    ]


@router.get("/{metric_id}", response_model=EvaluationMetricResponse)
async def get_metric(
    metric_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """查询评估指标

    获取单个评估指标的详细信息。
    """
    metric_loader = get_metric_loader()
    metric = await metric_loader.get_metric(metric_id)

    if not metric:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="评估指标不存在"
        )

    return EvaluationMetricResponse(
        id=metric.get("id", ""),
        name=metric.get("name", ""),
        description=metric.get("description", ""),
        category=metric.get("category", ""),
        evaluator_type=metric.get("evaluator_type", "tool"),
        evaluator_id=metric.get("evaluator_id", ""),
        default_config=metric.get("default_config", {}),
        input_schema=metric.get("input_schema", {}),
        includes=metric.get("includes"),
        requires=metric.get("requires"),
        level=metric.get("level", 1),
        source=metric.get("source", "builtin"),
        status=metric.get("status", "active"),
        tags=metric.get("tags", []),
        is_red_line=metric.get("is_red_line", False),
        default_weight=metric.get("default_weight", 1.0),
    )


@router.get("/{metric_id}/stats", response_model=dict[str, Any])
async def get_metric_stats(
    metric_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """获取指标使用统计

    注意：统计功能已移除，返回基本信息。
    """
    metric_loader = get_metric_loader()
    metric = await metric_loader.get_metric(metric_id)

    if not metric:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="评估指标不存在"
        )

    return {
        "metric_id": metric_id,
        "metric_name": metric.get("name", ""),
        "usage_count": 0,
        "success_count": 0,
        "success_rate": 0,
        "avg_execution_time": None,
    }
