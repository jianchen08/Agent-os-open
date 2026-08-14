#!/usr/bin/env python3
"""Multimodal MCP 服务端——纯接口适配层。

老代码从 0.1 src/multimodal/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import base64
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# capabilities.py 的 get_capability() / get_adapter_for_model() 已改为可选导入：
# 先尝试 from llm_config / from router_factory（本地未提供时 ImportError），
# 找不到则走 fallback（返回默认空能力 / DefaultAdapter）。
# 0.1 时代曾用 sys.modules 注入 src.* stub 的兼容层，现已移除——src/ 不再存在。
from adapter import DefaultAdapter, MultimodalAdapter  # noqa: E402
from capabilities import ModelCapabilityRegistry  # noqa: E402
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
    return {"messages": messages, "count": len(messages)}


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


if __name__ == "__main__":
    plugin.run()
