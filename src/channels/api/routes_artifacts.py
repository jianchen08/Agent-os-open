"""制品与批注 API 路由。

提供制品 CRUD、版本管理、差异对比，以及批注 CRUD 的 REST API 端点。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import require_auth
from artifacts.artifact_service import get_artifact_service
from artifacts.annotation_service import get_annotation_service

logger = logging.getLogger(__name__)

artifacts_router = APIRouter(prefix="/api/v1/artifacts", tags=["制品"])
annotations_router_v1 = APIRouter(prefix="/api/v1", tags=["批注"])


# ---------------------------------------------------------------------------
# 制品端点
# ---------------------------------------------------------------------------


@artifacts_router.get("", summary="获取制品列表")
async def list_artifacts(
    task_id: str = Query(default="", description="按任务 ID 过滤"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取任务下的制品列表。"""
    if not task_id:
        return {"items": [], "total": 0}
    service = get_artifact_service()
    return await service.list_artifacts_by_task(task_id, limit=limit, offset=offset)


@artifacts_router.get("/{artifact_id}", summary="获取制品详情")
async def get_artifact(
    artifact_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取制品详情。"""
    service = get_artifact_service()
    artifact = await service.get_artifact(artifact_id)
    if not artifact:
        return {"error": {"code": "NOT_FOUND", "message": f"制品不存在: {artifact_id}"}}
    return artifact.to_dict()


@artifacts_router.post("", summary="创建制品")
async def create_artifact(
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """创建制品。

    请求体: {"task_id": str, "title": str, "artifact_type": str,
             "content": str, "file_path": str|null, "metadata": dict}
    """
    service = get_artifact_service()
    artifact = await service.create_artifact(
        task_id=body.get("task_id", ""),
        title=body.get("title", ""),
        artifact_type=body.get("artifact_type", "text"),
        content=body.get("content", ""),
        file_path=body.get("file_path"),
        metadata=body.get("metadata"),
    )
    return artifact.to_dict()


@artifacts_router.put("/{artifact_id}", summary="更新制品（创建新版本）")
async def update_artifact(
    artifact_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """更新制品（创建新版本）。

    请求体: {"content": str, "title": str|null, "metadata": dict|null}
    """
    service = get_artifact_service()
    artifact = await service.update_artifact(
        artifact_id=artifact_id,
        content=body.get("content"),
        title=body.get("title"),
        metadata=body.get("metadata"),
    )
    if not artifact:
        return {"error": {"code": "NOT_FOUND", "message": f"制品不存在: {artifact_id}"}}
    return artifact.to_dict()


@artifacts_router.delete("/{artifact_id}", summary="删除制品")
async def delete_artifact(
    artifact_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """删除制品。"""
    service = get_artifact_service()
    success = await service.delete_artifact(artifact_id)
    return {"success": success}


@artifacts_router.get("/{artifact_id}/versions", summary="获取制品版本历史")
async def get_version_history(
    artifact_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取制品版本历史。"""
    service = get_artifact_service()
    return await service.get_version_history(artifact_id)


@artifacts_router.get("/{artifact_id}/diff", summary="获取版本差异")
async def get_version_diff(
    artifact_id: str,
    from_version: int = Query(default=1, description="起始版本号"),
    to_version: int = Query(default=2, description="目标版本号"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取两个版本之间的差异。"""
    service = get_artifact_service()
    return await service.get_version_diff(artifact_id, from_version, to_version)


# ---------------------------------------------------------------------------
# 批注端点（制品子资源）
# ---------------------------------------------------------------------------


@artifacts_router.get("/{artifact_id}/annotations", summary="获取制品批注列表")
async def list_annotations(
    artifact_id: str,
    status: str | None = Query(default=None, description="按状态过滤"),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取制品的批注列表。"""
    service = get_annotation_service()
    return await service.list_annotations_by_artifact(artifact_id, status=status, limit=limit)


@artifacts_router.post("/{artifact_id}/annotations", summary="添加批注")
async def create_annotation(
    artifact_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """添加批注。

    请求体: {"target_type": str, "target_data": dict, "content": str,
             "author_type": str, "author_id": str}
    """
    service = get_annotation_service()
    annotation = await service.create_annotation(
        artifact_id=artifact_id,
        target_type=body.get("target_type", "whole_artifact"),
        target_data=body.get("target_data", {}),
        content=body.get("content", ""),
        author_type=body.get("author_type", "user"),
        author_id=body.get("author_id", ""),
    )
    return annotation.to_dict()


# ---------------------------------------------------------------------------
# 批注端点（独立资源）
# ---------------------------------------------------------------------------


@annotations_router_v1.put("/annotations/{annotation_id}", summary="更新批注")
async def update_annotation(
    annotation_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """更新批注。"""
    service = get_annotation_service()
    annotation = await service.update_annotation(
        annotation_id=annotation_id,
        content=body.get("content"),
        target_data=body.get("target_data"),
    )
    if not annotation:
        return {"error": {"code": "NOT_FOUND", "message": f"批注不存在: {annotation_id}"}}
    return annotation.to_dict()


@annotations_router_v1.delete("/annotations/{annotation_id}", summary="删除批注")
async def delete_annotation(
    annotation_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """删除批注。"""
    service = get_annotation_service()
    success = await service.delete_annotation(annotation_id)
    return {"success": success}


@annotations_router_v1.post("/annotations/{annotation_id}/resolve", summary="标记批注为已解决")
async def resolve_annotation(
    annotation_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """标记批注为已解决。"""
    service = get_annotation_service()
    annotation = await service.resolve_annotation(annotation_id)
    if not annotation:
        return {"error": {"code": "NOT_FOUND", "message": f"批注不存在: {annotation_id}"}}
    return annotation.to_dict()
