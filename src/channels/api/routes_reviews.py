"""审批 API 路由。

提供审批请求的创建、状态查询、反馈提交等 REST API 端点。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import require_auth
from review.review_service import get_review_service

logger = logging.getLogger(__name__)

reviews_router = APIRouter(prefix="/api/v1/reviews", tags=["审批"])


@reviews_router.post("", summary="创建审批请求")
async def create_review(
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """创建审批请求。

    请求体: {"task_id": str, "thread_id": str, "session_id": str,
             "tab_id": str, "title": str, "description": str,
             "artifact_ids": [str], "priority": str, "timeout_seconds": float}
    """
    service = get_review_service()
    review = await service.create_review(
        task_id=body.get("task_id", ""),
        thread_id=body.get("thread_id", ""),
        session_id=body.get("session_id", ""),
        tab_id=body.get("tab_id", ""),
        title=body.get("title", ""),
        description=body.get("description", ""),
        artifact_ids=body.get("artifact_ids"),
        priority=body.get("priority", "normal"),
        timeout_seconds=body.get("timeout_seconds"),
        metadata=body.get("metadata"),
    )
    return review.to_dict()


@reviews_router.get("/{review_id}", summary="获取审批详情")
async def get_review(
    review_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取审批详情。"""
    service = get_review_service()
    review = await service.get_review(review_id)
    if not review:
        return {"error": {"code": "NOT_FOUND", "message": f"审批不存在: {review_id}"}}
    return review.to_dict()


@reviews_router.get("", summary="获取审批列表")
async def list_reviews(
    task_id: str = Query(default="", description="按任务 ID 过滤"),
    limit: int = Query(default=50, ge=1, le=200),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取任务的审批列表。"""
    if not task_id:
        return {"items": [], "total": 0}
    service = get_review_service()
    return await service.list_reviews_by_task(task_id, limit=limit)


@reviews_router.post("/{review_id}/feedback", summary="提交审批反馈")
async def submit_feedback(
    review_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """提交审批反馈。

    请求体: {"response_type": str, "overall_comment": str,
             "annotations": [{artifact_id, target_type, target_data, content}]}
    """
    service = get_review_service()
    feedback = await service.submit_feedback(
        review_id=review_id,
        response_type=body.get("response_type", "approved"),
        overall_comment=body.get("overall_comment", ""),
        annotations=body.get("annotations"),
        user_id=body.get("user_id"),
    )
    if not feedback:
        return {"error": {"code": "INVALID", "message": "审批不存在或状态不允许反馈"}}
    return feedback.to_dict()


@reviews_router.post("/{review_id}/viewed", summary="标记已查看")
async def mark_as_viewed(
    review_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """标记审批为已查看（状态变为 in_review）。"""
    service = get_review_service()
    success = await service.mark_as_viewed(review_id)
    return {"id": review_id, "viewed": success}


@reviews_router.post("/{review_id}/cancel", summary="取消审批")
async def cancel_review(
    review_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """取消审批。

    请求体: {"reason": str}
    """
    service = get_review_service()
    reason = (body or {}).get("reason")
    success = await service.cancel_review(review_id, reason=reason)
    return {"id": review_id, "cancelled": success}
