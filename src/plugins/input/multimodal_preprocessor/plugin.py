"""多模态预处理 Input 插件。

检测用户输入中的多模态内容（图片URL、本地文件路径），
将其转换为 LLM API 要求的格式（如 OpenAI vision 格式）。

State 命名空间：
    - multimodal_content : 检测到的多模态内容块列表
    - has_multimodal : 是否包含多模态内容
"""

from __future__ import annotations

import os
import re
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

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

        从管道状态中获取用户输入，检测多模态内容并转换为
        OpenAI vision 格式的 content blocks。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含多模态内容状态更新的插件执行结果
        """
        state = ctx.state
        user_input = state.get("user_input", "")

        if not user_input:
            return PluginResult()

        multimodal_content = self._detect_multimodal(user_input)
        if not multimodal_content:
            return PluginResult()

        return PluginResult(state_updates={
            "multimodal_content": multimodal_content,
            "has_multimodal": True,
        })

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
