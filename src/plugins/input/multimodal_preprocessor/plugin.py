"""多模态预处理 Input 插件。

检测用户输入中的多模态内容（图片URL、本地文件路径），
将其转换为 LLM API 要求的格式（如 OpenAI vision 格式）。

State 命名空间：
    - multimodal_content : 检测到的多模态内容块列表
    - has_multimodal : 是否包含多模态内容
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

# 图片URL正则：匹配 http(s)://...jpg/png/gif/webp/svg
_IMAGE_URL_PATTERN = re.compile(
    r'(https?://\S+\.(?:jpg|jpeg|png|gif|webp|svg)(?:\?\S*)?)',
    re.IGNORECASE,
)

# 本地文件路径正则：匹配以图片/PDF扩展名结尾的路径
_LOCAL_FILE_PATTERN = re.compile(
    r'((?:[A-Za-z]:)?[/\\][\S]+\.(?:jpg|jpeg|png|gif|webp|svg|pdf))',
    re.IGNORECASE,
)

# 扩展名到MIME类型的映射
_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}


class MultimodalPreprocessor(IInputPlugin):
    """多模态预处理 Input 插件。

    扫描用户输入文本，识别其中的图片URL和本地文件路径，
    将多模态内容提取为 OpenAI vision 格式的 content blocks，
    写入管道状态供后续 LLM 调用使用。

    优先级：40（预处理级，在参数注入之前）
    错误策略：SKIP（检测失败不影响管道执行）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化多模态预处理插件。

        Args:
            config: 插件配置字典，支持以下键：
                - priority: 插件优先级（默认 40）
                - max_file_size: 本地文件最大字节数（默认 20MB）
        """
        self._config = config or {}
        self._max_file_size = self._config.get("max_file_size", 20 * 1024 * 1024)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "multimodal_preprocessor"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 40)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行多模态预处理。

        从管道状态中获取用户输入和附件，检测多模态内容并转换为
        OpenAI vision 格式的 content blocks。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含多模态内容状态更新的插件执行结果
        """
        state = ctx.state
        user_input = state.get("user_input", "")
        attachments = state.get("attachments", [])

        # 处理 state 中的附件
        attachment_blocks = await self._process_attachments(attachments)

        # 检测文本中的多模态内容
        text_multimodal = self._detect_multimodal(user_input) if user_input else []

        # 合并附件和文本中的多模态内容
        all_blocks = attachment_blocks + text_multimodal

        if not all_blocks:
            return PluginResult()

        return PluginResult(state_updates={
            "multimodal_content": all_blocks,
            "has_multimodal": True,
        })

    async def _process_attachments(self, attachments: list[dict]) -> list[dict]:
        """处理 state 中的附件列表。

        将附件转换为 OpenAI vision 格式的 content blocks：
        - 图片：转为 base64 data URL 的 image_url 块
        - 音频：当前模型不支持音频输入时，经 ASR 转写为文字 text 块
          （与图片走 image_url 对称，统一在多模态体系内处理音频）

        对于相对路径（如 /uploads/xxx），读取本地文件。

        Args:
            attachments: 附件列表，每个附件包含 url、type 等字段

        Returns:
            多模态内容块列表
        """
        content_blocks: list[dict] = []

        for attachment in attachments:
            url = attachment.get("url")
            mime_type = attachment.get("mime_type") or attachment.get("type", "")

            if not url:
                continue

            # 处理图片类型
            if mime_type.startswith("image/"):
                # 如果是相对路径，转为 base64 data URL
                if url.startswith("/"):
                    image_url = self._local_file_to_data_url(url, mime_type)
                else:
                    image_url = url

                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                })
                continue

            # 处理音频类型：不支持音频的模型，经 ASR 转写为文字
            if mime_type.startswith("audio/"):
                text = await self._audio_to_text(url, mime_type)
                if text:
                    content_blocks.append({"type": "text", "text": text})

        return content_blocks

    async def _audio_to_text(self, url: str, mime_type: str) -> str:
        """将音频附件转写为文本。

        读取音频文件字节（本地路径或 data URL），调用 ASR 服务转写。
        ASR 服务未配置或转写失败时静默跳过（保持插件 SKIP 错误策略）。

        Args:
            url: 音频 URL 或本地路径
            mime_type: 音频 MIME 类型

        Returns:
            转写文本；失败时返回空串
        """
        audio_bytes = self._read_audio_bytes(url, mime_type)
        if not audio_bytes:
            return ""

        try:
            from multimodal import get_asr_service  # noqa: PLC0415

            asr = get_asr_service()
            if not asr.is_available():
                logger.warning("[MultimodalPreprocessor] ASR 服务未配置，跳过音频附件转写")
                return ""
            return await asr.transcribe(audio_bytes, mime_type)
        except Exception as exc:  # noqa: BLE001
            logger.error("[MultimodalPreprocessor] 音频转写失败: %s", exc)
            return ""

    def _read_audio_bytes(self, url: str, mime_type: str) -> bytes:
        """读取音频附件为字节流。

        支持本地路径（如 /uploads/xxx）和 base64 data URL 两种来源。

        Args:
            url: 本地路径或 data URL
            mime_type: 音频 MIME 类型（用于解析 data URL）

        Returns:
            音频字节流；读取失败时返回空 bytes
        """
        # base64 data URL
        if url.startswith("data:"):
            try:
                # data:{mime};base64,{payload}
                header, _, payload = url.partition(",")
                if "base64" in header:
                    return base64.b64decode(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error("[MultimodalPreprocessor] 解析 data URL 失败: %s", exc)
                return ""
            return b""

        # 本地路径
        if url.startswith("/"):
            uploads_dir = os.environ.get("UPLOADS_DIR", "./data/uploads")
            filename = os.path.basename(url)
            full_path = os.path.join(uploads_dir, filename)
            if not os.path.isfile(full_path):
                logger.warning("[MultimodalPreprocessor] 音频文件不存在: %s", full_path)
                return b""
            try:
                with open(full_path, "rb") as f:
                    return f.read()
            except OSError as exc:
                logger.error("[MultimodalPreprocessor] 读取音频文件失败: %s, %s", full_path, exc)
                return b""

        logger.warning("[MultimodalPreprocessor] 不支持的音频来源: %s", url)
        return b""

    def _local_file_to_data_url(self, file_path: str, mime_type: str) -> str:
        """将本地文件转为 base64 data URL。

        Args:
            file_path: 文件路径（如 /uploads/xxx.jpg）
            mime_type: MIME 类型

        Returns:
            base64 data URL 字符串
        """
        try:
            # 从环境变量获取上传目录
            uploads_dir = os.environ.get("UPLOADS_DIR", "./data/uploads")
            # 构建完整路径
            filename = os.path.basename(file_path)
            full_path = os.path.join(uploads_dir, filename)

            if not os.path.isfile(full_path):
                logger.warning("文件不存在: %s", full_path)
                return ""

            with open(full_path, "rb") as f:
                file_data = f.read()

            b64_data = base64.b64encode(file_data).decode("utf-8")
            return f"data:{mime_type};base64,{b64_data}"
        except Exception as e:
            logger.error("读取文件失败: %s, error=%s", file_path, e)
            return ""

    def _detect_multimodal(self, text: str) -> list[dict]:
        """检测文本中的多模态内容。

        依次扫描图片URL和本地文件路径，将匹配到的内容
        转换为 OpenAI vision 格式的 content blocks。
        如果没有匹配到任何多模态内容，返回空列表。

        Args:
            text: 待检测的用户输入文本

        Returns:
            多模态内容块列表，格式为 OpenAI vision content blocks
        """
        content_blocks: list[dict] = []
        matched_spans: list[tuple[int, int]] = []

        # 检测图片URL
        for match in _IMAGE_URL_PATTERN.finditer(text):
            url = match.group(1)
            start, end = match.span(1)
            matched_spans.append((start, end))
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": url},
            })

        # 检测本地文件路径
        for match in _LOCAL_FILE_PATTERN.finditer(text):
            file_path = match.group(1)
            start, end = match.span(1)
            matched_spans.append((start, end))
            content_blocks.append(self._build_local_file_block(file_path))

        if not content_blocks:
            return []

        # 提取剩余的纯文本内容
        remaining_text = self._extract_remaining_text(text, matched_spans)
        if remaining_text.strip():
            text_block: dict[str, Any] = {"type": "text", "text": remaining_text.strip()}
            return [text_block] + content_blocks

        return content_blocks

    def _build_local_file_block(self, file_path: str) -> dict:
        """构建本地文件的内容块。

        检查文件是否存在且大小在限制内，满足条件时
        读取文件并编码为 data URI 格式。

        Args:
            file_path: 本地文件路径

        Returns:
            OpenAI vision 格式的图片内容块，或文本描述块
        """
        if not os.path.isfile(file_path):  # noqa: PTH113
            return {"type": "text", "text": f"[文件不存在: {file_path}]"}

        file_size = os.path.getsize(file_path)  # noqa: PTH202
        if file_size > self._max_file_size:
            return {
                "type": "text",
                "text": f"[文件过大: {file_path} ({file_size} bytes)]",
            }

        _, ext = os.path.splitext(file_path)  # noqa: PTH122
        mime_type = _EXT_TO_MIME.get(ext.lower())
        if not mime_type:
            return {"type": "text", "text": f"[不支持的文件类型: {ext}]"}

        return {"type": "image_url", "image_url": {"url": file_path}}

    def _extract_remaining_text(
        self, text: str, spans: list[tuple[int, int]]
    ) -> str:
        """从原始文本中移除已匹配的多模态片段，返回剩余纯文本。

        Args:
            text: 原始文本
            spans: 已匹配片段的 (start, end) 位置列表

        Returns:
            移除多模态片段后的剩余文本
        """
        sorted_spans = sorted(spans, key=lambda s: s[0])
        parts: list[str] = []
        prev_end = 0
        for start, end in sorted_spans:
            if start > prev_end:
                parts.append(text[prev_end:start])
            prev_end = end
        if prev_end < len(text):
            parts.append(text[prev_end:])
        return " ".join(parts)
