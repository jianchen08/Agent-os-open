#!/usr/bin/env python3
"""artifacts 插件 MCP 服务端——制品 + 批注域 HTTP 面（channel_api 拆迁批次 1）。

原 channel_api/routes_artifacts.py（制品/批注 CRUD + 版本 + diff + 多模态上传）
与 channel_api server._handle_artifact_upload（multipart 解包 + 落盘 + 元数据）
迁入本插件，经 ``http.handle`` 按 path 分发（协议与 agent_manager/monitoring 同款）；
plugin.json ``http_endpoints`` 声明（/ext/artifacts/**，auth:user）。

- 服务面 = 本目录 artifact_service / annotation_service（纯内存单例，单一事实源）。
- 上传沿用 DiskFileStorage（多模态存储）+ tenant_data 多租户数据根咽喉点
  （plugins/shared/tenant_data.py，与 multimodal 插件三方对齐不变）。
- artifacts_sidecar 兼容壳（channel_api 内薄 re-export）唯一引用方在 channel_api
  内部，留待收尾轨随 channel_api 整体删除；本插件不依赖它。
[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 1]
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import uuid
from typing import Any

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
_SYSTEM_DIR = os.path.abspath(os.path.join(_PLUGIN_DIR, ".."))
if _SYSTEM_DIR not in sys.path:
    sys.path.insert(0, _SYSTEM_DIR)
# 多租户数据根咽喉点（plugins/shared/tenant_data.py）——参考
# hindsight_memory/wiring.py 的 sys.path 自举模式。
_SHARED_ROOT = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from annotation_service import get_annotation_service  # noqa: E402
from artifact_service import get_artifact_service  # noqa: E402
from tenant_data import DEFAULT_TENANT, tenant_data_root  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("artifacts")


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化制品插件（服务单例懒加载，无启动期外部依赖）。"""
    logger.info("artifacts 插件已加载（制品 + 批注域 HTTP 面）")


# ══ http.handle 响应封装（内核 HttpHandleResponse 约定，与 agent_manager 同款）══


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str, status: int = 503) -> dict[str, Any]:
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _parse_multipart(content_type: str, body_bytes: bytes) -> dict[str, Any]:
    """解析 multipart/form-data（内核透传的 raw_body base64 解码后的字节）。

    返回 {字段名: 值}；文件字段值为 {filename, content_type, data(bytes)}，
    普通字段为 str。用 email.parser 解析（标准库，无需外部依赖）。
    """
    import email  # noqa: PLC0415
    from email.policy import default as default_policy  # noqa: PLC0415

    # 构造一个完整 multipart 消息让 email 解析
    header = f"Content-Type: {content_type}\r\n\r\n".encode()
    msg = email.message_from_bytes(header + body_bytes, policy=default_policy)
    fields: dict[str, Any] = {}
    if not msg.is_multipart():
        return fields
    parts = msg.get_payload()
    if not isinstance(parts, list):  # pragma: no cover —— 防御 typeshed（multipart 时恒 list）
        return fields
    for part in parts:
        if not isinstance(part, email.message.Message):  # pragma: no cover —— 同上防御
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        filename = part.get_filename()
        if filename is not None:
            # 文件字段
            data = part.get_payload(decode=True) or b""
            fields[name] = {
                "filename": filename,
                "content_type": part.get_content_type(),
                "data": data,
            }
        else:
            # 普通字段
            payload = part.get_payload(decode=True)
            fields[name] = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    return fields


# ══ 多模态文件存储单例 + 上传 ══

_file_storage: Any = None


def get_file_storage() -> Any:
    """获取全局文件存储单例（DiskFileStorage）。

    存储目录由环境变量 ``MULTIMODAL_STORAGE_DIR`` 控制（多租户根回退），
    与 channel_api 原实现语义一致。
    """
    global _file_storage  # noqa: PLW0603
    if _file_storage is None:
        from multimodal.storage import DiskFileStorage  # noqa: PLC0415

        _file_storage = DiskFileStorage()
    return _file_storage


_MIME_TO_MEDIA: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
}


def _infer_media_type(mime_type: str) -> str:
    """从 MIME 类型推断媒体类型（image/audio/video/document）。"""
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
    """
    env_dir = os.environ.get("UPLOADS_DIR")
    if env_dir:
        return env_dir
    return str(tenant_data_root(tenant_id or DEFAULT_TENANT, "uploads"))


async def handle_upload(raw_body: str, headers: dict[str, str] | None) -> dict[str, Any]:
    """处理 POST /ext/artifacts/upload（multipart/form-data）。

    内核 dispatcher 透传原始字节（base64 编码在 raw_body）；解 multipart 取
    file + thread_id，落盘 + 存元数据（对齐原 routes_artifacts.upload_file 逻辑，
    _push_upload_event 在 0.2 已是 no-op，不再调用）。
    """
    try:
        body_bytes = base64.b64decode(raw_body) if raw_body else b""
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"invalid upload body: {exc}"}, 400))

    content_type = (headers or {}).get("content-type", "") or (headers or {}).get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return _ok(
            _json_response(
                {"error": "upload requires multipart/form-data", "content_type": content_type},
                400,
            )
        )

    try:
        fields = _parse_multipart(content_type, body_bytes)
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"multipart parse failed: {exc}"}, 400))

    file_field = fields.get("file")
    if not isinstance(file_field, dict):
        return _ok(_json_response({"error": "missing 'file' field in multipart"}, 400))

    content = file_field["data"]
    filename = file_field.get("filename") or "upload"
    mime_type = file_field.get("content_type") or "application/octet-stream"

    file_id = uuid.uuid4().hex[:12]
    media_type = _infer_media_type(mime_type)
    uploads_dir = _get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)  # noqa: PTH103
    ext = os.path.splitext(filename)[1]  # noqa: PTH122
    saved_filename = f"{file_id}{ext}"
    file_path = os.path.join(uploads_dir, saved_filename)
    with open(file_path, "wb") as f:
        f.write(content)
    url = f"/uploads/{saved_filename}"

    # 存储元数据到 DiskFileStorage
    from multimodal.mm_types import AttachmentInfo, MediaType  # noqa: PLC0415

    attachment = AttachmentInfo(
        file_id=file_id,
        filename=filename,
        mime_type=mime_type,
        size=len(content),
        media_type=MediaType(media_type),
        url=url,
    )
    storage = get_file_storage()
    await storage.save(file_id, attachment)

    logger.info(
        "[upload] 文件上传成功 | file_id=%s filename=%s media_type=%s size=%d",
        file_id,
        filename,
        media_type,
        len(content),
    )
    return _ok(
        _json_response(
            {
                "file_id": file_id,
                "filename": filename,
                "mime_type": mime_type,
                "media_type": media_type,
                "size": len(content),
                "url": url,
            }
        )
    )


# ══ 制品 handler（routes_artifacts.py 迁入，剥 FastAPI 装饰器）══


async def list_artifacts(task_id: str, limit: int, offset: int) -> dict[str, Any]:
    """获取任务下的制品列表（query: task_id/limit/offset）。"""
    if not task_id:
        return {"items": [], "total": 0}
    service = get_artifact_service()
    return await service.list_artifacts_by_task(task_id, limit=limit, offset=offset)


async def get_artifact(artifact_id: str) -> dict[str, Any]:
    """获取制品详情。"""
    service = get_artifact_service()
    artifact = await service.get_artifact(artifact_id)
    if not artifact:
        return {"error": {"code": "NOT_FOUND", "message": f"制品不存在: {artifact_id}"}}
    return artifact.to_dict()


async def create_artifact(body: dict[str, Any]) -> dict[str, Any]:
    """创建制品（body: task_id/title/artifact_type/content/file_path/metadata）。"""
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


async def update_artifact(artifact_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """更新制品（创建新版本；body: content/title/metadata）。"""
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


async def delete_artifact(artifact_id: str) -> dict[str, Any]:
    """删除制品。"""
    service = get_artifact_service()
    success = await service.delete_artifact(artifact_id)
    return {"success": success}


async def get_version_history(artifact_id: str) -> dict[str, Any]:
    """获取制品版本历史。"""
    service = get_artifact_service()
    return await service.get_version_history(artifact_id)


async def get_version_diff(artifact_id: str, from_version: int, to_version: int) -> dict[str, Any]:
    """获取两个版本之间的差异。"""
    service = get_artifact_service()
    return await service.get_version_diff(artifact_id, from_version, to_version)


# ══ 批注 handler ══


async def list_annotations(
    artifact_id: str,
    status: str | None,
    limit: int,
) -> dict[str, Any]:
    """获取制品的批注列表（query: status/limit）。"""
    service = get_annotation_service()
    return await service.list_annotations_by_artifact(artifact_id, status=status, limit=limit)


async def create_annotation(artifact_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """添加批注（body: target_type/target_data/content/author_type/author_id）。"""
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


async def update_annotation(annotation_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """更新批注（body: content/target_data）。"""
    service = get_annotation_service()
    annotation = await service.update_annotation(
        annotation_id=annotation_id,
        content=body.get("content"),
        target_data=body.get("target_data"),
    )
    if not annotation:
        return {"error": {"code": "NOT_FOUND", "message": f"批注不存在: {annotation_id}"}}
    return annotation.to_dict()


async def delete_annotation(annotation_id: str) -> dict[str, Any]:
    """删除批注。"""
    service = get_annotation_service()
    success = await service.delete_annotation(annotation_id)
    return {"success": success}


async def resolve_annotation(annotation_id: str) -> dict[str, Any]:
    """标记批注为已解决。"""
    service = get_annotation_service()
    annotation = await service.resolve_annotation(annotation_id)
    if not annotation:
        return {"error": {"code": "NOT_FOUND", "message": f"批注不存在: {annotation_id}"}}
    return annotation.to_dict()


# ══ http.handle 分发（/ext/artifacts/** 入口）══

_PREFIX = "/ext/artifacts"


def _qint(query: dict[str, str], key: str, default: int) -> int:
    try:
        return int(query[key]) if key in query else default
    except (TypeError, ValueError):
        return default


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/artifacts/** (artifacts + annotations domain, channel_api 拆迁批次 1)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 artifacts + annotations 域 13 端点。

    语义对齐原 /ext/channel_api/{artifacts,annotations}/**；业务函数全 async、
    全 dict body；认证由 http_endpoints auth=user 声明（dispatcher 层）。
    """
    try:
        q = query or {}

        # ── upload（multipart，raw_body base64 字节透传）──
        if path == f"{_PREFIX}/upload" and method == "POST":
            return await handle_upload(raw_body, headers or {})

        # ── annotations 独立资源（/ext/artifacts/annotations/{id}[...]）──
        if path.startswith(f"{_PREFIX}/annotations/"):
            ann_rest = path[len(f"{_PREFIX}/annotations/") :]
            if "/" in ann_rest:
                aid, rest = ann_rest.split("/", 1)
                if rest == "resolve" and method == "POST":
                    return _ok(_json_response(await resolve_annotation(aid)))
            else:
                if method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await update_annotation(ann_rest, body)))
                if method == "DELETE":
                    return _ok(_json_response(await delete_annotation(ann_rest)))

        # ── artifacts 集合（/ext/artifacts[?task_id&limit&offset]）──
        if path == _PREFIX and method == "GET":
            return _ok(
                _json_response(
                    await list_artifacts(
                        task_id=q.get("task_id", ""),
                        limit=_qint(q, "limit", 50),
                        offset=_qint(q, "offset", 0),
                    )
                )
            )
        if path == _PREFIX and method == "POST":
            body = _decode_body(raw_body)
            return _ok(_json_response(await create_artifact(body)))

        # ── 子路径（versions/diff/annotations）──
        if path.startswith(_PREFIX + "/"):
            rest = path[len(_PREFIX) + 1 :]  # "{artifact_id}" 或 "{artifact_id}/xxx"
            if "/" in rest:
                art_id, sub_path = rest.split("/", 1)
                if sub_path == "versions" and method == "GET":
                    return _ok(_json_response(await get_version_history(art_id)))
                if sub_path == "diff" and method == "GET":
                    return _ok(
                        _json_response(
                            await get_version_diff(
                                art_id,
                                _qint(q, "from", 1),
                                _qint(q, "to", 2),
                            )
                        )
                    )
                if sub_path == "annotations" and method == "GET":
                    return _ok(
                        _json_response(
                            await list_annotations(
                                art_id,
                                status=q.get("status"),
                                limit=_qint(q, "limit", 100),
                            )
                        )
                    )
                if sub_path == "annotations" and method == "POST":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await create_annotation(art_id, body)))
            else:
                # 单级 /{artifact_id}
                if method == "GET":
                    return _ok(_json_response(await get_artifact(rest)))
                if method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await update_artifact(rest, body)))
                if method == "DELETE":
                    return _ok(_json_response(await delete_artifact(rest)))

        logger.warning("artifacts http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.exception("artifacts http.handle failed: %s", exc)
        return _error(f"artifacts service error: {exc}", 500)


if __name__ == "__main__":
    plugin.run()
