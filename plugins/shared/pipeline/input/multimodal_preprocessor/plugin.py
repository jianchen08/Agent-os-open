"""多模态预处理 Input 插件。

检测用户消息中的多模态内容（图片URL、本地文件路径），
转换为 LLM API 要求的格式（如 OpenAI vision 格式）。

State 命名空间：
    - multimodal_content : 本轮待发送的多模态内容块列表（每轮全量重建）
    - has_multimodal     : 本轮是否有待发送的多模态内容
    - multimodal_seen    : 已检出的 (消息键, 引用) 登记集（防同图重发）

输入面（0.2）：引擎把用户输入 append 进 ``state["messages"]``（route_user_input
只写 messages 键，``user_input`` 键在 0.2 无生产者）；本插件扫描其中
role=user 的消息文本。

引用语义（ADR 2026-08-21）：本地引用（``/uploads/...``、绝对路径）在
multimodal_content 里**保持引用原样**（不转 base64）——state/trace 恒小；
llm_core 在请求装配时读文件转 base64 data URL（发送前转换，二进制瞬态）。
消息文本不改写：markdown 引用原样保留（前端渲染与历史回看依赖它），
解析失败由 llm_core 产出占位块（禁静默丢内容）。
"""

from __future__ import annotations

import base64
import hashlib
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

class MultimodalPreprocessor(IInputPlugin):
    """多模态预处理 Input 插件。

    扫描对话历史中 role=user 的消息文本，识别其中的图片URL和本地文件路径，
    将新检出的多模态内容以 OpenAI vision 格式 content blocks 写入管道状态，
    供 llm_core 请求装配时合并。

    优先级：40（预处理级，在参数注入之前）
    检测失败不影响管道执行。

    Attributes:
        _config: 插件配置字典
    """

    # seen 登记集上限：条目 = 消息键 × 图片引用，量级跟随会话历史中的图片
    # token 数（正常会话远不可达），超限逐最旧防长会话无界增长。
    _MAX_SEEN_ENTRIES = 4096

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化多模态预处理插件。

        Args:
            config: 插件配置字典，支持以下键：
                - priority: 插件优先级（默认 40）
        """
        self._config = config or {}

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

        扫描对话历史中 role=user 的消息与附件列表，检出未登记过的多模态
        内容并转换为 OpenAI vision 格式的 content blocks。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含多模态内容状态更新的插件执行结果。multimodal_content 每轮
            全量重建（prepare 在主循环里反复执行）：无新块也显式写空列表，
            清掉上一轮残留块——否则 llm_core 会把旧块再次合并进新的最后
            一条用户消息，同一张图随每个工具循环重复进请求。
        """
        state = ctx.state
        attachments = state.get("attachments", [])

        attachment_blocks = await self._process_attachments(attachments)

        seen = self._load_seen(state.get("multimodal_seen"))
        message_blocks, seen = self._detect_messages_multimodal(
            state.get("messages", []), seen
        )

        all_blocks = attachment_blocks + message_blocks

        return PluginResult(
            state_updates={
                "multimodal_content": all_blocks,
                "has_multimodal": bool(all_blocks),
                "multimodal_seen": self._dump_seen(seen),
            }
        )

    async def _process_attachments(self, attachments: list[dict]) -> list[dict]:
        """处理 state 中的附件列表。

        将附件转换为 OpenAI vision 格式的 content blocks：
        - 图片：转为 base64 data URL 的 image_url 块（本地引用读取失败 →
          解析失败占位块，不产出空 url 图块）
        - 音频：当前模型不支持音频输入时，经 ASR 转写为文字 text 块
          （与图片走 image_url 对称，统一在多模态体系内处理音频）
        - 文本/文档/代码：提取文本后作为 text 块，和用户消息一起发给 LLM
          （任何模型都能接收文本，无需多模态能力声明）；提取失败 → 解析
          失败占位块

        每附件独立 try：单附件处理异常只对该附件产出占位块，不拖垮其他
        附件/文本（2026-09-08 附件解析失败显式化裁定）。

        Args:
            attachments: 附件列表，每个附件包含 url、type 等字段

        Returns:
            多模态内容块列表
        """
        content_blocks: list[dict] = []

        for attachment in attachments:
            try:
                block = await self._process_single_attachment(attachment)
            except Exception as exc:  # noqa: BLE001
                logger.error("[MultimodalPreprocessor] 附件处理异常: %r | %s", attachment, exc)
                url = attachment.get("url", attachment) if isinstance(attachment, dict) else attachment
                block = {
                    "type": "text",
                    "text": self._attachment_failure_notice(str(url), "处理异常"),
                }
            if block is not None:
                content_blocks.append(block)

        return content_blocks

    async def _process_single_attachment(self, attachment: dict) -> dict | None:
        """处理单个附件，产出至多一个 content block；无可产出内容返回 None。

        Args:
            attachment: 附件字段（url、mime_type/type）

        Returns:
            OpenAI vision content block；附件无 url 或视频类型返回 None
        """
        url = attachment.get("url")
        mime_type = attachment.get("mime_type") or attachment.get("type", "")

        if not url:
            return None

        # 处理图片类型
        if mime_type.startswith("image/"):
            # 如果是相对路径，转为 base64 data URL；解析失败 → 显式占位块
            if url.startswith("/"):
                image_url = self._local_file_to_data_url(url, mime_type)
                if not image_url:
                    return {
                        "type": "text",
                        "text": self._attachment_failure_notice(
                            url, "文件读取失败或来源不支持"
                        ),
                    }
            else:
                image_url = url

            return {"type": "image_url", "image_url": {"url": image_url}}

        # 处理音频类型：不支持音频的模型，经 ASR 转写为文字
        if mime_type.startswith("audio/"):
            text = await self._audio_to_text(url, mime_type)
            return {"type": "text", "text": text} if text else None

        # 处理文本/文档/代码类型：提取文本后拼进用户消息
        # 非图片/音频/视频一律视为可提取文本的附件
        if not mime_type.startswith("video/"):
            text_content = await self._extract_text_from_attachment(url, mime_type)
            if text_content:
                return {"type": "text", "text": text_content}

        return None

    @staticmethod
    def _attachment_failure_notice(url: str, reason: str) -> str:
        """构造附件解析失败的 LLM 可见占位文本（禁静默丢内容，同音频降级）。"""
        return f"[附件 {url} 解析失败：{reason}]"

    async def _audio_to_text(self, url: str, mime_type: str) -> str:
        """将音频附件转写为文本。

        读取音频文件字节（本地路径或 data URL），调用 ASR 服务转写。
        任一环节失败时降级为用户可见提示文本（非核心降级须有提示——
        音频内容不能静默丢失），调用方将其作为 text 块拼进消息。

        Args:
            url: 音频 URL 或本地路径
            mime_type: 音频 MIME 类型

        Returns:
            转写文本；失败/不可用时返回非空的降级提示文本
        """
        audio_bytes = self._read_audio_bytes(url, mime_type)
        if not audio_bytes:
            return self._audio_degrade_notice(url, "音频文件读取失败或来源不支持")

        try:
            from multimodal import get_asr_service  # noqa: PLC0415

            asr = get_asr_service()
            if not asr.is_available():
                logger.warning(
                    "[MultimodalPreprocessor] ASR 服务未配置，音频附件降级为提示文本 | url=%s",
                    url,
                )
                return self._audio_degrade_notice(url, "ASR 服务未配置")
            return await asr.transcribe(audio_bytes, mime_type)
        except Exception as exc:  # noqa: BLE001
            logger.error("[MultimodalPreprocessor] 音频转写失败: %s", exc)
            return self._audio_degrade_notice(url, "转写失败")

    @staticmethod
    def _audio_degrade_notice(url: str, reason: str) -> str:
        """构造音频转写降级的用户可见提示（禁静默丢内容）。"""
        return f"[音频附件 {url} 未能转为文字：{reason}]"

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
        二进制文档（pdf/docx/xlsx/pptx 等）转换未支持，返回显式失败占位。
        提取的文本会和用户消息一起发给 LLM（任何模型都能接收文本）。

        解析失败（文件不存在、读取错误、文档转换未支持）
        返回非空的 `[附件 … 解析失败：原因]` 占位文本（禁静默丢内容）；
        成功但内容为空返回空串（非失败）。

        Args:
            url: 附件 URL（如 /uploads/xxx.pdf）
            mime_type: MIME 类型（用于判定提取路径）

        Returns:
            提取的文本内容；解析失败返回占位文本；成功无内容返回空串
        """
        full_path = self._resolve_upload_path(url)
        if not full_path or not os.path.isfile(full_path):
            logger.warning("[MultimodalPreprocessor] 文本附件文件不存在: %s", url)
            return self._attachment_failure_notice(url, "文件不存在")

        # 纯文本类：直接 UTF-8 解码
        if self._is_plain_text_mime(mime_type):
            try:
                with open(full_path, "rb") as f:
                    return f.read().decode("utf-8", errors="replace")
            except OSError as exc:
                logger.error("[MultimodalPreprocessor] 读取文本附件失败: %s, %s", full_path, exc)
                return self._attachment_failure_notice(url, "文件读取失败")

        # 二进制文档（pdf/docx/xlsx/pptx 等）：转换未支持，显式占位
        return self._convert_document_to_text(url)

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
        这些文件不走文档转换分支，直接读取即可。

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

    def _convert_document_to_text(self, url: str) -> str:
        """二进制文档（pdf/docx/xlsx/pptx）转换未支持，返回显式失败占位。

        文档转换器（markitdown 链路）尚未在 0.2 重建；按禁静默丢内容契约
        （2026-09-08 用户裁定：附件失败要显式说明）返回占位文本，
        LLM 与 trace 均可见，不静默跳过。

        Args:
            url: 附件引用 URL（占位文本回指来源）

        Returns:
            `[附件 … 解析失败：文档转换未支持]` 占位文本
        """
        logger.warning("[MultimodalPreprocessor] 文档转换未支持: %s", url)
        return self._attachment_failure_notice(url, "文档转换未支持")

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

    def _detect_messages_multimodal(
        self, messages: Any, seen: dict[tuple[str, str], None]
    ) -> tuple[list[dict], dict[tuple[str, str], None]]:
        """扫描对话历史里的用户消息，收集未登记过的图片引用块。

        检出即登记 (消息键, 引用)：prepare 每轮重跑，不登记则同一条带图
        消息在每次工具循环后都会重新合并进 LLM 请求（token 爆炸）。

        Args:
            messages: state["messages"] 对话历史
            seen: 已登记集（插入序 dict，键 (msg_key, url)）

        Returns:
            (新增图片块列表, 更新后的登记集)
        """
        blocks: list[dict] = []
        if not isinstance(messages, list):
            return blocks, seen
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            msg_key = self._message_key(msg)
            for url in self._extract_image_refs(content):
                entry = (msg_key, url)
                if entry in seen:
                    continue
                seen[entry] = None
                blocks.append({"type": "image_url", "image_url": {"url": url}})

        overflow = len(seen) - self._MAX_SEEN_ENTRIES
        if overflow > 0:
            for entry in list(seen)[:overflow]:
                seen.pop(entry, None)
        return blocks, seen

    @staticmethod
    def _message_key(msg: dict) -> str:
        """消息防重键：client_message_id 优先（路由器随消息 metadata 落的
        幂等键），缺失退化内容指纹（触发器注入等无幂等键路径）。"""
        metadata = msg.get("metadata")
        client_message_id = (
            metadata.get("client_message_id") if isinstance(metadata, dict) else ""
        )
        if client_message_id:
            return f"id:{client_message_id}"
        content = msg.get("content") or ""
        return "sha1:" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _extract_image_refs(text: str) -> list[str]:
        """按优先级提取文本中的图片引用，同 span 不重复建块。

        markdown 图片引用（![f](/uploads/x.png) 整 token）最先匹配——只认
        /uploads/ 前缀（平台管理的引用），不劫持用户手打的任意 markdown；
        http(s) 图片 URL 次之；本地图片/PDF 路径兜底（跳过已被前两类消费
        的 span）。文件存在性/大小不在此预检——llm_core 装配时统一解析，
        失败产出占位块（禁静默丢内容，2026-09-08 裁定）。
        """
        refs: list[str] = []
        matched_spans: list[tuple[int, int]] = []

        def _overlaps(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e in matched_spans)

        for match in _MD_IMAGE_PATTERN.finditer(text):
            matched_spans.append(match.span(0))
            refs.append(match.group(1))

        for match in _IMAGE_URL_PATTERN.finditer(text):
            start, end = match.span(1)
            if _overlaps(start, end):
                continue
            matched_spans.append((start, end))
            refs.append(match.group(1))

        for match in _LOCAL_FILE_PATTERN.finditer(text):
            start, end = match.span(1)
            if _overlaps(start, end):
                continue
            ref = match.group(1)
            # 盘符分支会把 URL 的 "s:/" 当盘符（https:// → s://…）——含
            # scheme 分隔符的引用不是本地路径（http 引用已由前两类处理）
            if "://" in ref:
                continue
            matched_spans.append((start, end))
            refs.append(ref)

        return refs

    @staticmethod
    def _load_seen(raw: Any) -> dict[tuple[str, str], None]:
        """还原上轮登记集（state 经 JSON 往返，条目为 [msg_key, url] 列表）。"""
        if not isinstance(raw, list):
            return {}
        seen: dict[tuple[str, str], None] = {}
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                seen[(entry[0], entry[1])] = None
        return seen

    @staticmethod
    def _dump_seen(seen: dict[tuple[str, str], None]) -> list[list[str]]:
        """登记集落 state 形态（JSON 可序列化，插入序保持）。"""
        return [[key, url] for (key, url) in seen]
