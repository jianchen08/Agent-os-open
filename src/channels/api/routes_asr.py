"""语音识别（ASR）转写路由。

提供音频转文字的 HTTP 接口，供前端在浏览器 Web Speech API 不可用（如 Edge/Chrome
的云端服务不可达报 network 错误）时降级使用。

采用 OpenAI 兼容契约 ``POST /api/v1/audio/transcriptions``：
- 请求：multipart 上传音频文件（字段名 file）
- 响应：``{"text": "转写文本"}``

ASR 服务商由 ``config/models/asr.yaml`` 配置驱动（默认智谱 GLM-ASR）。
未配置时返回 503，前端可据此区分"未配置"与"转写失败"。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from channels.api.deps import require_auth
from multimodal import get_asr_service

logger = logging.getLogger(__name__)

asr_router = APIRouter(
    prefix="/api/v1/audio",
    tags=["语音识别"],
    dependencies=[Depends(require_auth)],
)


@asr_router.post("/transcriptions")
async def transcribe_audio(
    _user: dict = Depends(require_auth),
    file: UploadFile = File(..., description="音频文件"),
    language: str | None = Form(None, description="识别语言代码（如 zh-CN），可选"),
) -> dict[str, Any]:
    """音频转文字。

    将上传的音频文件转写为文本。前端语音输入在浏览器识别不可用时降级调用此接口。

    Args:
        file: 音频文件（webm/wav/mp3 等）
        language: 识别语言代码，覆盖默认配置

    Returns:
        ``{"text": "转写文本"}``

    Raises:
        HTTPException 503: ASR 服务未配置
        HTTPException 400: 文件为空或读取失败
        HTTPException 502: 转写失败
    """
    asr = get_asr_service()
    if not asr.is_available():
        logger.warning("[ASR Route] ASR 服务未配置或未启用")
        raise HTTPException(
            status_code=503,
            detail={"code": "asr_not_configured", "message": "语音转文字服务未配置"},
        )

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="音频文件为空")

    mime_type = file.content_type or "audio/webm"
    logger.info(
        "[ASR Route] 收到转写请求: filename=%s, mime=%s, size=%d",
        file.filename,
        mime_type,
        len(audio_bytes),
    )

    try:
        text = await asr.transcribe(audio_bytes, mime_type, language)
    except RuntimeError as exc:
        logger.error("[ASR Route] 转写失败: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"code": "asr_failed", "message": str(exc)},
        ) from exc

    return {"text": text}


__all__ = ["asr_router"]
