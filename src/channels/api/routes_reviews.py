"""审批 API 路由。

提供审批请求的创建、状态查询、反馈提交等 REST API 端点。
包含媒体审阅相关的文件上传、元数据查询和附件管理端点。
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from channels.api.deps import require_auth
from review.media_review_service import MediaReviewService
from review.review_service import get_review_service

logger = logging.getLogger(__name__)

reviews_router = APIRouter(
    prefix="/api/v1/reviews",
    tags=["审批"],
    dependencies=[Depends(require_auth)],
)

# 全局媒体审阅服务实例
_media_review_service: MediaReviewService | None = None


def get_media_review_service() -> MediaReviewService:
    """获取全局媒体审阅服务单例。"""
    global _media_review_service  # noqa: PLW0603
    if _media_review_service is None:
        _media_review_service = MediaReviewService()
    return _media_review_service


# ---------------------------------------------------------------------------
# 原有审批端点
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 媒体审阅端点
# ---------------------------------------------------------------------------


@reviews_router.post("/media-review", summary="上传文件并执行媒体审阅")
async def media_review(
    file: UploadFile = File(..., description="上传的媒体文件"),
    media_type: str = Form(default="", description="媒体类型: image 或 video（留空自动推断）"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """上传文件并执行媒体审阅，返回审阅结果。

    根据 media_type 路由到图片或视频审阅器。
    如果未指定 media_type，根据文件扩展名自动推断。

    Returns:
        ImageReviewResult 或 VideoReviewResult 的字典形式
    """
    # 保存上传文件到临时目录
    suffix = os.path.splitext(file.filename or "")[1] if file.filename else ""  # noqa: PTH122
    tmp_dir = tempfile.mkdtemp(prefix="media_review_")
    tmp_path = os.path.join(tmp_dir, file.filename or f"upload{suffix}")

    try:
        content = await file.read()
        with open(tmp_path, "wb") as f:
            f.write(content)

        # 确定 media_type
        effective_media_type = media_type
        if not effective_media_type:
            from review.media_review_service import _infer_media_type  # noqa: PLC0415

            try:
                effective_media_type = _infer_media_type(tmp_path)
            except ValueError:
                return {
                    "error": {
                        "code": "INVALID",
                        "message": f"无法推断媒体类型，请显式指定 media_type（文件: {file.filename}）",
                    }
                }

        media_svc = get_media_review_service()
        result = await media_svc.review_media(tmp_path, effective_media_type)
        result_dict = result.to_dict()
        result_dict["media_type"] = effective_media_type
        result_dict["filename"] = file.filename
        return result_dict

    except FileNotFoundError as exc:
        return {"error": {"code": "NOT_FOUND", "message": str(exc)}}
    except ValueError as exc:
        return {"error": {"code": "INVALID", "message": str(exc)}}
    except Exception as exc:
        logger.error("[routes_reviews] 媒体审阅失败 | error=%s", exc)
        return {"error": {"code": "INTERNAL", "message": f"媒体审阅失败: {exc}"}}
    finally:
        # 清理临时文件
        if os.path.isfile(tmp_path):  # noqa: PTH113
            os.remove(tmp_path)  # noqa: PTH107
        if os.path.isdir(tmp_dir):  # noqa: PTH112
            os.rmdir(tmp_dir)  # noqa: PTH106


@reviews_router.get("/{review_id}/media-metadata", summary="获取审批关联的媒体元数据")
async def get_media_metadata(
    review_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取审批请求关联的媒体元数据。

    从审批请求的 metadata.media_review_results 字段中读取审阅结果，
    并从 metadata.media_files 中读取文件路径列表生成元数据摘要。

    Returns:
        包含 media_metadata 列表的字典
    """
    service = get_review_service()
    review = await service.get_review(review_id)
    if not review:
        return {"error": {"code": "NOT_FOUND", "message": f"审批不存在: {review_id}"}}

    metadata = review.metadata
    review_results = metadata.get("media_review_results", {})
    media_files = metadata.get("media_files", [])

    # 如果有存储的审阅结果，直接返回
    if review_results:
        return {
            "review_id": review_id,
            "media_metadata": review_results,
        }

    # 否则尝试从 media_files 重新生成元数据
    if not media_files:
        return {
            "review_id": review_id,
            "media_metadata": [],
        }

    media_svc = get_media_review_service()
    metadata_list: list[dict[str, Any]] = []

    for file_info in media_files:
        file_path = file_info if isinstance(file_info, str) else file_info.get("path", "")
        media_type = ""
        if isinstance(file_info, dict):
            media_type = file_info.get("media_type", "")

        if not file_path or not os.path.isfile(file_path):  # noqa: PTH113
            metadata_list.append(
                {
                    "file_path": file_path,
                    "error": "文件不存在或路径无效",
                }
            )
            continue

        try:
            if not media_type:
                from review.media_review_service import _infer_media_type  # noqa: PLC0415

                media_type = _infer_media_type(file_path)
            meta = media_svc.get_media_metadata(file_path, media_type)
            metadata_list.append(meta)
        except (ValueError, FileNotFoundError) as exc:
            metadata_list.append(
                {
                    "file_path": file_path,
                    "error": str(exc),
                }
            )

    return {
        "review_id": review_id,
        "media_metadata": metadata_list,
    }


@reviews_router.post("/{review_id}/attachments", summary="为审批添加媒体附件")
async def add_attachments(
    review_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """为已有审批添加媒体附件。

    请求体::

        {
            "files": [
                {"path": "/path/to/file.png", "media_type": "image"},
                {"path": "/path/to/video.mp4", "media_type": "video"}
            ],
            "auto_review": true
        }

    当 auto_review 为 true 时，自动对所有附件执行媒体审阅，
    并将结果存储到审批请求的 metadata.media_review_results 中。

    Returns:
        包含附件信息和审阅结果（如有）的字典
    """
    service = get_review_service()
    review = await service.get_review(review_id)
    if not review:
        return {"error": {"code": "NOT_FOUND", "message": f"审批不存在: {review_id}"}}

    files = body.get("files", [])
    auto_review = body.get("auto_review", False)

    if not files:
        return {"error": {"code": "INVALID", "message": "files 列表不能为空"}}

    # 更新 media_files
    media_files = review.metadata.get("media_files", [])
    review_results = review.metadata.get("media_review_results", {})

    added: list[dict[str, Any]] = []
    media_svc = get_media_review_service()

    for file_info in files:
        file_path = file_info.get("path", "") if isinstance(file_info, dict) else file_info
        media_type = file_info.get("media_type", "") if isinstance(file_info, dict) else ""

        if not file_path:
            added.append({"error": "缺少 path 字段"})
            continue

        # 如果未指定 media_type，尝试推断
        if not media_type:
            try:
                from review.media_review_service import _infer_media_type  # noqa: PLC0415

                media_type = _infer_media_type(file_path)
            except ValueError:
                added.append(
                    {
                        "file_path": file_path,
                        "error": "无法推断媒体类型",
                    }
                )
                continue

        entry = {"path": file_path, "media_type": media_type}
        media_files.append(entry)

        # 可选自动审阅
        review_result_dict: dict[str, Any] | None = None
        if auto_review and os.path.isfile(file_path):  # noqa: PTH113
            try:
                result = await media_svc.review_media(file_path, media_type)
                review_result_dict = result.to_dict()
                review_results[file_path] = review_result_dict
            except Exception as exc:
                logger.warning(
                    "[routes_reviews] 附件审阅失败 | path=%s | error=%s",
                    file_path,
                    exc,
                )
                review_result_dict = {"error": str(exc)}
                review_results[file_path] = review_result_dict

        added.append(
            {
                **entry,
                "review_result": review_result_dict,
            }
        )

    # 更新审批的 metadata
    review.metadata["media_files"] = media_files
    review.metadata["media_review_results"] = review_results

    return {
        "review_id": review_id,
        "added_count": len(added),
        "attachments": added,
    }
