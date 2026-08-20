"""多模态预处理 Input 插件。

检测用户输入中的多模态内容（图片URL、本地文件路径），
将其转换为 LLM API 要求的格式（如 OpenAI vision 格式）。

State 命名空间：
    - multimodal_content : 检测到的多模态内容块列表
    - has_multimodal : 是否包含多模态内容

引用语义（ADR 2026-08-21）：本地引用（``/uploads/...``、绝对路径）在
multimodal_content 里**保持引用原样**（不转 base64）——state/trace 恒小；
llm_core 在请求装配时读文件转 base64 data URL（发送前转换，二进制瞬态）。
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sys
from typing import Any

# 共享上传目录解析（plugins/shared/uploads_path.py，ADR 2026-08-21 三方对齐）：
# 本文件位于 plugins/shared/pipeline/input/multimodal_preprocessor/，上溯 3 级到 shared。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult  # noqa: E402
from uploads_path import resolve_uploads_url  # noqa: E402

logger = logging.getLogger(__name__)

# markdown 图片引用（前端发送时并入正文的附件索引，ADR 2026-08-21）：
# ![filename](/uploads/xxx.png)——整 token 匹配（含 ![]() 括号），剩余文本干净。
# 只认 /uploads/ 前缀（平台管理的引用），不劫持用户手打的任意 markdown。
_MD_IMAGE_PATTERN = re.compile(
    r"!\[[^\]]*\]\((/uploads/[^\s)]+\.(?:jpg|jpeg|png|gif|webp|svg))\)",
    re.IGNORECASE,
)

# 图片URL正则：匹配 http(s)://...jpg/png/gif/webp/svg
_IMAGE_URL_PATTERN = re.compile(
    r"(https?://\S+\.(?:jpg|jpeg|png|gif|webp|svg)(?:\?\S*)?)",
    re.IGNORECASE,
)

# 本地文件路径正则：匹配以图片/PDF扩展名结尾的路径
_LOCAL_FILE_PATTERN = re.compile(
    r"((?:[A-Za-z]:)?[/\\][\S]+\.(?:jpg|jpeg|png|gif|webp|svg|pdf))",
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
    检测失败不影响管道执行。

    Attributes:
        _config: 插件配置字典
    """

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

        return PluginResult(
            state_updates={
                "multimodal_content": all_blocks,
                "has_multimodal": True,
            }
        )

    async def _process_attachments(self, attachments: list[dict]) -> list[dict]:
        """处理 state 中的附件列表。

        将附件转换为 OpenAI vision 格式的 content blocks：
        - 图片：转为 base64 data URL 的 image_url 块
        - 音频：当前模型不支持音频输入时，经 ASR 转写为文字 text 块
          （与图片走 image_url 对称，统一在多模态体系内处理音频）
        - 文本/文档/代码：提取文本后作为 text 块，和用户消息一起发给 LLM
          （任何模型都能接收文本，无需多模态能力声明）

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

                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    }
                )
                continue

            # 处理音频类型：不支持音频的模型，经 ASR 转写为文字
            if mime_type.startswith("audio/"):
                text = await self._audio_to_text(url, mime_type)
                if text:
                    content_blocks.append({"type": "text", "text": text})
                continue

            # 处理文本/文档/代码类型：提取文本后拼进用户消息
            # 非图片/音频/视频一律视为可提取文本的附件
            if not mime_type.startswith("video/"):
                text_content = await self._extract_text_from_attachment(url, mime_type)
                if text_content:
                    content_blocks.append({"type": "text", "text": text_content})

        return content_blocks

    async def _audio_to_text(self, url: str, mime_type: str) -> str:
        """将音频附件转写为文本。

        读取音频文件字节（本地路径或 data URL），调用 ASR 服务转写。
        ASR 服务未配置或转写失败时静默跳过。

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

        支持本地 /uploads/ 引用和 base64 data URL 两种来源。

        Args:
            url: /uploads/ 引用或 data URL
            mime_type: 音频 MIME 类型（用于解析 data URL）

        Returns:
            音频字节流；读取失败返回空 bytes
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
                return b""
            return b""

        # /uploads/ 引用（shared/uploads_path.py 统一解析，ADR 2026-08-21）
        full_path = resolve_uploads_url(url)
        if full_path is not None:
            if not full_path.is_file():
                logger.warning("[MultimodalPreprocessor] 音频文件不存在: %s", full_path)
                return b""
            try:
                return full_path.read_bytes()
            except OSError as exc:
                logger.error("[MultimodalPreprocessor] 读取音频文件失败: %s, %s", full_path, exc)
                return b""

        logger.warning("[MultimodalPreprocessor] 不支持的音频来源: %s", url)
        return b""

    async def _extract_text_from_attachment(self, url: str, mime_type: str) -> str:
        """从文本/文档类附件提取文本内容。

        纯文本类（text/*、json、xml、html、代码）直接按 UTF-8 解码；
        二进制文档（pdf/docx/xlsx/pptx 等）经 markitdown 转 Markdown。
        提取的文本会和用户消息一起发给 LLM（任何模型都能接收文本）。

        失败时（文件不存在、markitdown 未安装、文件过大、转换失败）
        记 warning 日志并返回空串，不阻断管道。

        Args:
            url: 附件 URL（如 /uploads/xxx.pdf）
            mime_type: MIME 类型（用于判定提取路径）

        Returns:
            提取的文本内容；失败或无内容时返回空串
        """
        full_path = self._resolve_upload_path(url)
        if not full_path or not os.path.isfile(full_path):
            logger.warning("[MultimodalPreprocessor] 文本附件文件不存在: %s", url)
            return ""

        # 纯文本类：直接 UTF-8 解码
        if self._is_plain_text_mime(mime_type):
            try:
                with open(full_path, "rb") as f:
                    return f.read().decode("utf-8", errors="replace")
            except OSError as exc:
                logger.error("[MultimodalPreprocessor] 读取文本附件失败: %s, %s", full_path, exc)
                return ""

        # 二进制文档（pdf/docx/xlsx/pptx 等）：经 markitdown 转 Markdown
        return self._convert_document_to_text(full_path)

    def _resolve_upload_path(self, url: str) -> str:
        """将附件引用（如 /uploads/xxx）解析为本地磁盘绝对路径。

        经 shared/uploads_path.py 统一解析（UPLOADS_DIR 环境变量 >
        data/{tenant}/uploads，与 channel_api 落盘/内核静态服务三方对齐）。

        Args:
            url: 附件引用 URL

        Returns:
            本地文件路径；非 /uploads/ 形态时返回空串
        """
        resolved = resolve_uploads_url(url)
        return str(resolved) if resolved is not None else ""

    @staticmethod
    def _is_plain_text_mime(mime_type: str) -> bool:
        """判断 MIME 类型是否为可直接 UTF-8 解码的纯文本类。

        包括 text/* 以及常见的结构化文本/代码 MIME（json/xml/html/css/javascript）。
        这些文件无需 markitdown，直接读取即可。

        Args:
            mime_type: MIME 类型

        Returns:
            是纯文本类返回 True
        """
        if mime_type.startswith("text/"):
            return True
        plain_text_mimes = {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
            "application/x-sh",
        }
        return mime_type in plain_text_mimes

    def _convert_document_to_text(self, full_path: str) -> str:
        """将二进制文档（pdf/docx/xlsx/pptx）转为文本。

        复用 binary_converter 工具的 markitdown 转换能力。
        markitdown 未安装、文件过大或转换失败时返回空串。

        Args:
            full_path: 文件绝对路径

        Returns:
            提取的文本；失败时返回空串
        """
        try:
            from pathlib import Path  # noqa: PLC0415

            from tools.builtin.binary_converter.tool import (  # noqa: PLC0415
                convert_binary_to_markdown,
            )
        except ImportError:
            logger.warning("[MultimodalPreprocessor] binary_converter 不可用，跳过文档附件提取")
            return ""

        try:
            result = convert_binary_to_markdown(Path(full_path))
        except Exception as exc:  # noqa: BLE001
            logger.error("[MultimodalPreprocessor] 文档转换异常: %s, %s", full_path, exc)
            return ""

        if not result.success:
            logger.warning(
                "[MultimodalPreprocessor] 文档转换失败: %s, code=%s, error=%s",
                full_path,
                result.error_code,
                result.error,
            )
            return ""

        output = result.output
        if isinstance(output, dict):
            return output.get("content", "") or ""
        return ""

    def _local_file_to_data_url(self, file_path: str, mime_type: str) -> str:
        """将 /uploads/ 引用文件转为 base64 data URL。

        仅供附件分支（state["attachments"]，0.2 链路暂无生产者）使用；
        content 检测出的图片引用保持原样（llm_core 发送前解析，ADR 2026-08-21）。

        Args:
            file_path: /uploads/ 引用（如 /uploads/xxx.jpg）
            mime_type: MIME 类型

        Returns:
            base64 data URL 字符串；解析/读取失败返回空串
        """
        resolved = resolve_uploads_url(file_path)
        if resolved is None:
            logger.warning("[MultimodalPreprocessor] 非 /uploads/ 引用: %s", file_path)
            return ""
        try:
            if not resolved.is_file():
                logger.warning("文件不存在: %s", resolved)
                return ""

            b64_data = base64.b64encode(resolved.read_bytes()).decode("utf-8")
            return f"data:{mime_type};base64,{b64_data}"
        except Exception as e:  # noqa: BLE001
            logger.error("读取文件失败: %s, error=%s", resolved, e)
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

        # markdown 图片引用（![f](/uploads/x.png) 整 token）：最先匹配——
        # 整 token 从剩余文本移除（括号残渣不留给 LLM），只认 /uploads/ 前缀。
        for match in _MD_IMAGE_PATTERN.finditer(text):
            url = match.group(1)
            start, end = match.span(0)
            matched_spans.append((start, end))
            content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url},
                }
            )

        # 检测图片URL
        for match in _IMAGE_URL_PATTERN.finditer(text):
            url = match.group(1)
            start, end = match.span(1)
            matched_spans.append((start, end))
            content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url},
                }
            )

        # 检测本地文件路径（跳过已被 markdown 引用整 token 消费的 span——
        # ![f](/uploads/x.png) 里的 /uploads/x.png 不重复建块）
        def _overlaps_md_span(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e in matched_spans)

        for match in _LOCAL_FILE_PATTERN.finditer(text):
            start, end = match.span(1)
            if _overlaps_md_span(start, end):
                continue
            file_path = match.group(1)
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
        """构建本地文件的内容块（引用，不转 base64——ADR 2026-08-21）。

        检查文件存在且大小在限制内（存在性经 /uploads/ 统一解析或直接判
        绝对路径），满足条件时输出 image_url 引用块，由 llm_core 在请求装配
        时读文件转 base64 data URL。

        Args:
            file_path: /uploads/ 引用或本地绝对路径

        Returns:
            OpenAI vision 格式的图片引用块，或文本描述块
        """
        resolved = resolve_uploads_url(file_path)
        check_path = str(resolved) if resolved is not None else file_path

        if not os.path.isfile(check_path):  # noqa: PTH113
            return {"type": "text", "text": f"[文件不存在: {file_path}]"}

        file_size = os.path.getsize(check_path)  # noqa: PTH202
        if file_size > self._max_file_size:
            return {
                "type": "text",
                "text": f"[文件过大: {file_path} ({file_size} bytes)]",
            }

        _, ext = os.path.splitext(check_path)  # noqa: PTH122
        mime_type = _EXT_TO_MIME.get(ext.lower())
        if not mime_type:
            return {"type": "text", "text": f"[不支持的文件类型: {ext}]"}

        # 引用原样输出（http/绝对路径/uploads 引用统一语义：发送前由 llm_core 解析）
        return {"type": "image_url", "image_url": {"url": file_path}}

    def _extract_remaining_text(self, text: str, spans: list[tuple[int, int]]) -> str:
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
