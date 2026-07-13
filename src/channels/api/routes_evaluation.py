"""评估指标查询 API 路由。

提供评估指标的列表和详情查询接口（只读），
数据来源于 MetricLoader 从 YAML 文件加载的指标定义。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import APIError, require_auth
from channels.api.models import (
    MetricDetailResponse,
    MetricListResponse,
    MetricResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/metrics", tags=["评估指标"])


def _get_metric_loader() -> Any:
    """惰性获取或创建 MetricLoader 实例。"""
    try:
        from evaluation.loader import MetricLoader  # noqa: PLC0415

        loader = MetricLoader()
        if not loader.metrics:
            loader.load_all()
        return loader
    except Exception as exc:
        logger.warning("评估指标加载器初始化失败: %s", exc)
        return None


def _metric_to_response(m: Any) -> MetricResponse:
    """将 MetricDefinition 转为 MetricResponse。"""
    return MetricResponse(
        id=m.id,
        name=m.name,
        description=m.description,
        metric_type=m.metric_type.value if hasattr(m.metric_type, "value") else str(m.metric_type),
        evaluator_id=m.evaluator_id,
        is_red_line=m.is_red_line,
        default_weight=m.default_weight,
        level=m.level,
        tags=m.tags,
        status=m.status,
    )


def _metric_to_detail(m: Any) -> MetricDetailResponse:
    """将 MetricDefinition 转为 MetricDetailResponse。"""
    return MetricDetailResponse(
        id=m.id,
        name=m.name,
        description=m.description,
        metric_type=m.metric_type.value if hasattr(m.metric_type, "value") else str(m.metric_type),
        evaluator_id=m.evaluator_id,
        is_red_line=m.is_red_line,
        default_weight=m.default_weight,
        level=m.level,
        tags=m.tags,
        status=m.status,
        default_config=m.default_config,
        input_schema=m.input_schema,
        includes=m.includes,
        requires=m.requires,
    )


@router.get(
    "",
    response_model=MetricListResponse,
    summary="获取评估指标列表",
)
def list_metrics(
    metric_type: str | None = Query(
        default=None,
        description="按类型筛选 (tool/agent/human)",
    ),
    tag: str | None = Query(default=None, description="按标签筛选"),
    is_red_line: bool | None = Query(
        default=None,
        description="是否红线指标",
    ),
    limit: int = Query(default=50, ge=1, le=200, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _user: dict = Depends(require_auth),
) -> MetricListResponse:
    """获取所有已加载的评估指标列表。

    支持按类型、标签、红线标识筛选，结果分页返回。

    Returns:
        MetricListResponse 包含 items 和 total
    """
    loader = _get_metric_loader()

    if loader is None:
        return MetricListResponse(items=[], total=0)

    metrics = list(loader.metrics.values())

    # 筛选
    if metric_type:
        metrics = [
            m
            for m in metrics
            if (m.metric_type.value if hasattr(m.metric_type, "value") else str(m.metric_type)) == metric_type
        ]
    if tag:
        metrics = [m for m in metrics if tag in m.tags]
    if is_red_line is not None:
        metrics = [m for m in metrics if m.is_red_line == is_red_line]

    total = len(metrics)
    end = offset + limit
    page = metrics[offset:end]
    items = [_metric_to_response(m) for m in page]

    return MetricListResponse(items=items, total=total)


@router.get(
    "/{metric_id}",
    response_model=MetricDetailResponse,
    summary="获取评估指标详情",
)
def get_metric(
    metric_id: str,
    _user: dict = Depends(require_auth),
) -> MetricDetailResponse:
    """根据指标 ID 获取评估指标详情。

    Args:
        metric_id: 指标唯一标识

    Returns:
        MetricDetailResponse 包含完整的指标定义

    Raises:
        APIError: 指标不存在 (404)
    """
    loader = _get_metric_loader()

    if loader is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="评估指标加载器未初始化",
        )

    metric = loader.get(metric_id)
    if metric is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message=f"评估指标 '{metric_id}' 不存在",
        )

    return _metric_to_detail(metric)
