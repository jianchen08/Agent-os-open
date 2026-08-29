#!/usr/bin/env python3
"""Multimodal MCP 服务端——纯接口适配层。

老代码从 0.1 src/multimodal/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

HTTP 面（audio + files 域）：
- POST /ext/multimodal_service/audio/transcriptions（源 routes_asr.py，multipart
  file+language，config/models/asr.yaml 驱动；503 未配置/400 空文件/502 转写失败）
- GET /ext/multimodal_service/files/capabilities（源 routes_missing.py files 域，
  ModelCapabilityRegistry 真实能力，前端 services/api/files.ts 消费形态）
- GET /ext/multimodal_service/files/supported-types（静态宽类型声明）
"""
from __future__ import annotations

import base64
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# http.handle 响应封装 + multipart 解析（内核 HttpHandleResponse/ToolExecutionResult
# 样板）：公共实现 plugins/shared/http_json.py，经共享层自举裸名导入。
# `_error` = protocol_error：本插件上传面协议级错误契约（success:true 包
# HTTP status + 结构化错误体），参数序与 review 一致统一为 (message, status)。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
# capabilities.py 的 get_capability() 直读 config/models/llm.yaml 的
# multimodal 节（mtime 缓存）；llm.yaml 缺失/损坏时返回 degraded=True 空能力。
from capabilities import ModelCapabilityRegistry  # noqa: E402
from http_json import (  # noqa: E402
    json_response as _json_response,
    ok as _ok,
    parse_multipart as _parse_multipart,
    protocol_error as _error,
)
from mm_types import AttachmentInfo, MediaType  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("multimodal_service")


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize multimodal service on load."""
    logger.info("Multimodal service initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup on unload."""
    logger.info("Multimodal service unloaded")


@plugin.tool(
    name="multimodal.convert",
    schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "文本内容"},
            "provider": {"type": "string", "description": "模型提供商（openai/anthropic/zhipu/deepseek等）", "default": "default"},
            "attachments": {
                "type": "array",
                "description": "附件列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "mime_type": {"type": "string"},
                        "media_type": {"type": "string", "enum": ["image", "audio", "video", "document"]},
                        "base64_data": {"type": "string", "description": "Base64编码的文件内容"},
                        "url": {"type": "string", "description": "文件URL（与base64_data二选一）"},
                    },
                    "required": ["filename", "mime_type", "media_type"],
                },
            },
        },
        "required": ["content"],
    },
    description="将文本和附件转换为模型特定格式",
)
async def multimodal_convert(
    content: str,
    provider: str = "default",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert content and attachments to model-specific format."""
    adapter = ModelCapabilityRegistry.get_adapter(provider)
    att_list: list[AttachmentInfo] = []
    if attachments:
        for att in attachments:
            file_id = att.get("filename", "unknown")
            att_list.append(AttachmentInfo(
                file_id=file_id,
                filename=att["filename"],
                mime_type=att["mime_type"],
                size=att.get("size", 0),
                media_type=MediaType(att["media_type"]),
                base64_data=att.get("base64_data"),
                url=att.get("url"),
            ))
    messages = adapter.convert(content, att_list)
    return {"messages": messages, "count": len(messages), "degraded": adapter.degraded}


@plugin.tool(
    name="multimodal.capability",
    schema={
        "type": "object",
        "properties": {
            "model_name": {"type": "string", "description": "模型名称"},
        },
        "required": ["model_name"],
    },
    description="查询模型的多模态能力",
)
async def multimodal_capability(model_name: str) -> dict[str, Any]:
    """Get multimodal capability for a model."""
    capability = ModelCapabilityRegistry.get_capability(model_name)
    return {
        "model_name": capability.model_name,
        "degraded": capability.degraded,
        "supports_image": capability.supports_image,
        "supports_audio": capability.supports_audio,
        "supports_video": capability.supports_video,
        "supports_document": capability.supports_document,
        "supported_image_types": capability.supported_image_types,
        "supported_audio_types": capability.supported_audio_types,
        "supported_video_types": capability.supported_video_types,
        "max_image_size": capability.max_image_size,
        "max_audio_size": capability.max_audio_size,
        "max_video_size": capability.max_video_size,
    }


@plugin.tool(
    name="multimodal.supported",
    schema={
        "type": "object",
        "properties": {
            "model_name": {"type": "string", "description": "模型名称"},
        },
        "required": ["model_name"],
    },
    description="检查模型是否支持多模态",
)
async def multimodal_supported(model_name: str) -> dict[str, Any]:
    """Check if a model supports multimodal input."""
    supported = ModelCapabilityRegistry.is_multimodal_supported(model_name)
    return {"model_name": model_name, "multimodal_supported": supported}


@plugin.tool(
    name="multimodal.transcribe",
    schema={
        "type": "object",
        "properties": {
            "audio_base64": {"type": "string", "description": "Base64编码的音频数据"},
            "mime_type": {"type": "string", "description": "音频MIME类型（如 audio/webm）", "default": "audio/webm"},
            "language": {"type": "string", "description": "识别语言代码（如 zh-CN）", "default": "zh-CN"},
        },
        "required": ["audio_base64"],
    },
    description="语音识别（ASR）：音频转文本",
)
async def multimodal_transcribe(audio_base64: str, mime_type: str = "audio/webm", language: str = "zh-CN") -> dict[str, Any]:
    """Transcribe audio to text using ASR service."""
    from asr import get_asr_service  # noqa: PLC0415
    service = get_asr_service()
    if not service.is_available():
        return {"error": "ASR service not configured (missing API key or asr.yaml)", "text": ""}
    audio_bytes = base64.b64decode(audio_base64)
    text = await service.transcribe(audio_bytes, mime_type, language)
    return {"text": text}


# ── HTTP 端点（http.handle）—— 前端 /ext/multimodal_service/** 入口 ────────
# 响应封装/请求解析助手（json_response/_ok/_error(protocol_error)/_parse_multipart）：
# 公共实现 plugins/shared/http_json.py（文件头已导入）。
# `_error` 参数序统一为 (message, status)——原本地实现为反序 (status, message)。
# 返回 ToolExecutionResult{success,data}，data 为 HttpHandleResponse{status,
# headers,body,body_encoding}（body base64）——与既有 17 插件 http.handle 契约一致。


def _files_capabilities_payload(model_name: str) -> dict[str, Any]:
    """files/capabilities 响应载荷（源 routes_missing.get_model_file_capabilities）。"""
    cap = ModelCapabilityRegistry.get_capability(model_name)
    return {
        "model_name": model_name,
        "degraded": cap.degraded,
        "supports_image": cap.supports_image,
        "supports_audio": cap.supports_audio,
        "supports_video": cap.supports_video,
        "supported_image_types": cap.supported_image_types,
        "supported_audio_types": cap.supported_audio_types,
        "supported_video_types": cap.supported_video_types,
        "max_image_size": cap.max_image_size,
        "max_audio_size": cap.max_audio_size,
        "max_video_size": cap.max_video_size,
        "is_multimodal": cap.supports_image or cap.supports_audio or cap.supports_video,
    }


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
    description="HTTP endpoint handler for /ext/multimodal_service/** (ASR + files capability)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 3 个子端点（audio 1 + files 2）。"""
    del plugin_id

    # ── POST /audio/transcriptions（multipart：file + language）──
    if path == "/ext/multimodal_service/audio/transcriptions" and method == "POST":
        try:
            body_bytes = base64.b64decode(raw_body) if raw_body else b""
        except Exception as exc:  # noqa: BLE001
            return _error(f"invalid upload body: {exc}", 400)

        content_type = ""
        for k, v in (headers or {}).items():
            if isinstance(k, str) and k.lower() == "content-type" and v:
                content_type = str(v)
                break
        if "multipart/form-data" not in content_type:
            return _error("asr requires multipart/form-data", 400)

        try:
            fields = _parse_multipart(content_type, body_bytes)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover —— email.parser 对任意字节几乎不抛
            return _error(f"multipart parse failed: {exc}", 400)  # pragma: no cover

        file_field = fields.get("file")
        if not isinstance(file_field, dict) or not file_field.get("data"):
            return _error("missing or empty 'file' field", 400)

        from asr import get_asr_service  # noqa: PLC0415

        asr = get_asr_service()
        if not asr.is_available():
            # 对齐源 routes_asr：503 + {"code": "asr_not_configured", ...}（前端据此区分未配置）
            return _ok(_json_response(
                {"code": "asr_not_configured", "message": "语音转文字服务未配置"}, 503,
            ))

        audio_bytes: bytes = file_field["data"]
        mime_type = file_field.get("content_type") or "audio/webm"
        language = fields.get("language") or None
        # 注：空串已由上方 `or None` 归一（防御性分支不可达）
        if isinstance(language, str) and language.strip() == "":  # pragma: no cover
            language = None  # pragma: no cover
        try:
            text = await asr.transcribe(audio_bytes, mime_type, language)
        except RuntimeError as exc:
            # 对齐源 routes_asr：502 + {"code": "asr_failed", ...}
            return _ok(_json_response({"code": "asr_failed", "message": str(exc)}, 502))
        except Exception as exc:  # noqa: BLE001
            logger.error("asr transcribe 未预期错误: %s", exc, exc_info=True)
            return _ok(_json_response({"error": "internal server error"}, 500))
        return _ok(_json_response({"text": text}))

    # ── GET /files/capabilities（query: model_name）──
    if path == "/ext/multimodal_service/files/capabilities" and method == "GET":
        model_name = (query or {}).get("model_name", "default")
        return _ok(_json_response(_files_capabilities_payload(model_name)))

    # ── GET /files/supported-types ──
    if path == "/ext/multimodal_service/files/supported-types" and method == "GET":
        return _ok(_json_response({
            "image_types": {"default": ["image/png", "image/jpeg", "image/gif", "image/webp"]},
            "document_types": {
                "default": ["application/pdf", "text/plain", "text/markdown", "text/csv"]
            },
            "max_image_size": 20 * 1024 * 1024,
            "max_document_size": 50 * 1024 * 1024,
        }))

    logger.warning("http.handle: no route for path=%s method=%s", path, method)
    return _error("not found", 404)


if __name__ == "__main__":
    plugin.run()
