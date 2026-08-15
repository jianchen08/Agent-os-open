"""制品与批注 API 路由。

提供制品 CRUD、版本管理、差异对比，以及批注 CRUD 的 REST API 端点。
包含多模态文件上传端点。
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Any

from artifacts_sidecar.annotation_service import get_annotation_service
from artifacts_sidecar.artifact_service import get_artifact_service
from deps import require_auth
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from multimodal.mm_types import AttachmentInfo, MediaType  # noqa: F811
from multimodal.storage import DiskFileStorage  # noqa: F811

# 多租户数据根咽喉点（plugins/shared/tenant_data.py）。本文件位于
# plugins/shared/system/channel_api/routes_artifacts.py，上溯 2 级到
# plugins/shared/。参考 hindsight_memory/wiring.py 的 sys.path 自举模式。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from tenant_data import DEFAULT_TENANT, tenant_data_root  # noqa: E402

logger = logging.getLogger(__name__)

artifacts_router = APIRouter(
    prefix="/api/v1/artifacts",
    tags=["制品"],
    dependencies=[Depends(require_auth)],
)
annotations_router_v1 = APIRouter(
    prefix="/api/v1",
    tags=["批注"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# 多模态文件存储单例
# ---------------------------------------------------------------------------

_file_storage: DiskFileStorage | None = None


def get_file_storage() -> DiskFileStorage:
    """获取全局文件存储单例（DiskFileStorage）。

    存储目录由环境变量 ``MULTIMODAL_STORAGE_DIR`` 控制，默认 ``./data/multimodal``。
    """
    global _file_storage  # noqa: PLW0603
    if _file_storage is None:
        _file_storage = DiskFileStorage()
    return _file_storage


# ---------------------------------------------------------------------------
# 多模态文件上传端点
# ---------------------------------------------------------------------------

_MIME_TO_MEDIA: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
}


def _infer_media_type(mime_type: str) -> str:
    """从 MIME 类型推断媒体类型。

    Args:
        mime_type: 文件 MIME 类型（如 image/jpeg）

    Returns:
        MediaType 字符串值（image/audio/video/document）
    """
    if not mime_type:
        return "document"
    category = mime_type.split("/", maxsplit=1)[0]
    return _MIME_TO_MEDIA.get(category, "document")


def _get_uploads_dir(tenant_id: str | None = None) -> str:
    """获取上传文件目录。

    解析优先级（高 → 低）：

    1. 环境变量 ``UPLOADS_DIR``（兼容存量部署覆盖，最高优先级）；
    2. 多租户数据根 ``tenant_data_root(tenant_id or default, "uploads")``
       （方案 B 目录隔离默认值，即 ``data/{tenant_id}/uploads``）。

    Args:
        tenant_id: 租户 ID。未设 UPLOADS_DIR 时目录落在
            ``data/{tenant_id}/uploads``；None 则用 ``DEFAULT_TENANT``。

    Returns:
        上传文件目录（str 路径）。
    """
    env_dir = os.environ.get("UPLOADS_DIR")
    if env_dir:
        return env_dir
    return str(tenant_data_root(tenant_id or DEFAULT_TENANT, "uploads"))


@artifacts_router.post("/upload", summary="上传多模态文件")
async def upload_file(
    file: UploadFile = File(..., description="上传的文件"),
    thread_id: str = Form(default="", description="关联的会话ID"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """上传多模态文件，返回文件信息和可访问 URL。

    支持 multipart/form-data 上传，文件持久化到磁盘。
    上传成功后通过 WebSocket 推送 ``multimedia_uploaded`` 事件。

    Returns:
        包含 file_id, filename, mime_type, media_type, size, url 的字典
    """
    content = await file.read()
    file_id = uuid.uuid4().hex[:12]
    mime_type = file.content_type or "application/octet-stream"
    media_type = _infer_media_type(mime_type)
    user_id = _user.get("sub", "")

    # 1. 持久化文件二进制到 uploads 目录
    uploads_dir = _get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)  # noqa: PTH103
    ext = os.path.splitext(file.filename or "")[1]  # noqa: PTH122
    saved_filename = f"{file_id}{ext}"
    file_path = os.path.join(uploads_dir, saved_filename)
    with open(file_path, "wb") as f:
        f.write(content)

    # 2. 构造可访问 URL（前端通过静态文件服务访问）
    url = f"/uploads/{saved_filename}"

    # 3. 存储元数据到 DiskFileStorage
    attachment = AttachmentInfo(
        file_id=file_id,
        filename=file.filename or saved_filename,
        mime_type=mime_type,
        size=len(content),
        media_type=MediaType(media_type),
        url=url,
    )
    storage = get_file_storage()
    await storage.save(file_id, attachment)

    # 4. 推送 multimedia_uploaded WS 事件
    await _push_upload_event(
        user_id=user_id,
        file_id=file_id,
        filename=file.filename or saved_filename,
        mime_type=mime_type,
        media_type=media_type,
        size=len(content),
        url=url,
        thread_id=thread_id,
    )

    logger.info(
        "[upload] 文件上传成功 | file_id=%s filename=%s media_type=%s size=%d",
        file_id,
        file.filename,
        media_type,
        len(content),
    )

    return {
        "file_id": file_id,
        "filename": file.filename or saved_filename,
        "mime_type": mime_type,
        "media_type": media_type,
        "size": len(content),
        "url": url,
    }


async def _push_upload_event(
    user_id: str,
    file_id: str,
    filename: str,
    mime_type: str,
    media_type: str,
    size: int,
    url: str,
    thread_id: str = "",
) -> None:
    """推送 multimedia_uploaded 事件给用户。

    0.2 推送改走 frontend.emit capability（ADR §3.5），SDK 暂未实现该 capability；
    当前推送静默跳过，0.2 栈不再依赖 0.1 的 src/channels/websocket/
    ws_interaction_notifier（task_11 P2-7）。待 SDK 实现后在此用
    ctx.frontend.emit(event="multimedia_uploaded", scope=...) 恢复。
    """
    return


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
