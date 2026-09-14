# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""multimodal_preprocessor plugin.py 单元测试（模块目录内自持，行覆盖 ≥90%）。

覆盖决策链（ADR 2026-08-21）：附件/文本中的多模态内容 → OpenAI vision 格式
content blocks 写入管道状态；本地引用保持原样（不转 base64，state/trace 恒小），
llm_core 发送前解析。

用例分组：
- 构造/属性：priority 默认与覆盖、max_file_size、name；
- execute：附件+文本合并、仅附件、无多模态空结果、无 url 附件跳过；
- 附件图片：/uploads/ 相对路径转 base64 data URL、http URL 原样透传；
- 附件音频：ASR 可用转写为 text 块、ASR 未配置/转写异常/导入失败/文件缺失/
  来源不支持/非法 data URL 降级为用户可见提示块（U22 禁静默丢内容）、
  data URL 字节透传、读文件 OSError；
- 附件文本/文档/图片：纯文本 UTF-8 提取、json 按纯文本、octet-stream 走文档转换、
  文档转换未支持（真实代码路径）；任一环节解析失败（文件缺失/读错误/
  转换未支持）产出 `[附件 … 解析失败：原因]` 占位块——
  禁静默跳过（2026-09-08 用户裁定：后端要报错，LLM 与 trace 可见）；
  坏附件条目逐个隔离不拖垮其他附件；
- 附件视频：跳过；
- 文本检测：markdown 引用整 token 消费、与裸路径正则去重、http URL（含 query/
  大写扩展名）、本地路径存在/缺失/过大/不支持类型、pdf 引用、盘符路径、
  远程 pdf URL 现状行为、组合多来源、无多模态空列表；
- 工具方法：_extract_remaining_text 排序/全消费、_is_plain_text_mime 枚举、
  _local_file_to_data_url 成功/非 uploads/缺失/读错误、_resolve_upload_path。

外部依赖 mock 边界：ASR 服务（multimodal 跨进程 capability）以 sys.modules 桩注入；
文件系统经真实 tmp 文件（读错误分支 mock Path.read_bytes / builtins.open）。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _load_plugin_under_test() -> ModuleType:
    """唯一名动态加载 plugin.py（裸名 `import plugin` 会被兄弟插件目录串扰）。"""
    path = Path(__file__).resolve().parent / "plugin.py"
    name = "_mm_preprocessor_plugin_ut"
    sys.modules.pop(name, None)
    sys.modules.pop("plugin", None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_under_test()

MultimodalPreprocessor = _mod.MultimodalPreprocessor


def _make_ctx(
    text: str = "",
    attachments: list | None = None,
    messages: list | None = None,
) -> SimpleNamespace:
    """构造最小 PluginContext 形状（execute 只读 state）。

    text 便捷参数按 0.2 输入面包装成单条 user 消息（引擎 route_user_input
    把用户输入 append 进 state["messages"]，无独立 user_input 键）。
    """
    if messages is None:
        messages = [{"role": "user", "content": text}] if text else []
    state: dict = {"messages": messages}
    if attachments is not None:
        state["attachments"] = attachments
    return SimpleNamespace(state=state)


def _refs(pre: Any, text: str) -> list[str]:
    # 单测直访私有法（引用提取器是被测单元）
    return pre._extract_image_refs(text)  # noqa: SLF001


# ── 构造与属性 ────────────────────────────────────────────────


def test_name():
    assert MultimodalPreprocessor().name == "multimodal_preprocessor"


@pytest.mark.parametrize(
    ("config", "expected"),
    [({}, 40), ({"priority": 10}, 10), ({"priority": 0}, 0)],
)
def test_priority_default_and_config_override(config: dict, expected: int):
    pre = MultimodalPreprocessor(config)
    assert pre.priority == expected


# ── execute 状态输出 ─────────────────────────────────────────


async def test_execute_no_multimodal_rebuilds_empty():
    """无多模态也要全量重建：显式写空列表清残留块（幂等契约）。"""
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("纯文本消息"))
    assert result.state_updates == {
        "multimodal_content": [],
        "has_multimodal": False,
        "multimodal_seen": [],
    }


async def test_execute_merges_attachment_and_message_blocks():
    pre = MultimodalPreprocessor()
    result = await pre.execute(
        _make_ctx("图 https://example.com/b.jpg", [{"url": "https://x.com/a.png", "mime_type": "image/png"}])
    )
    assert result.state_updates["has_multimodal"] is True
    blocks = result.state_updates["multimodal_content"]
    urls = [b["image_url"]["url"] for b in blocks if b.get("type") == "image_url"]
    # 附件在前、消息检出在后；文本不再单独产 text 块（消息原文保留在 messages）
    assert urls == ["https://x.com/a.png", "https://example.com/b.jpg"]
    assert all(b.get("type") != "text" for b in blocks)


async def test_execute_attachments_only():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "https://x.com/a.png", "mime_type": "image/png"}]))
    assert result.state_updates["has_multimodal"] is True
    assert len(result.state_updates["multimodal_content"]) == 1


async def test_execute_attachment_without_url_skipped():
    pre = MultimodalPreprocessor()
    result = await pre.execute(
        _make_ctx("", [{"type": "image/png"}, {"url": "https://x.com/a.png", "mime_type": "image/png"}])
    )
    assert len(result.state_updates["multimodal_content"]) == 1


# ── 附件图片 ──────────────────────────────────────────────────


async def test_attachment_image_relative_path_converted_to_data_url(tmp_path, monkeypatch):
    payload = b"\x89PNG\r\n\x1a\n"
    (tmp_path / "cat.png").write_bytes(payload)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/cat.png", "mime_type": "image/png"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    url = blocks[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # 性质断言：data URL 载荷可逆回原字节
    assert base64.b64decode(url.split(",", 1)[1]) == payload


async def test_attachment_image_http_url_passthrough():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "https://x.com/a.png", "mime_type": "image/png"}]))
    assert result.state_updates["multimodal_content"] == [
        {"type": "image_url", "image_url": {"url": "https://x.com/a.png"}}
    ]


# ── 附件音频（ASR 为跨进程 capability，sys.modules 桩注入）──────


class _FakeASR:
    def __init__(self, available: bool = True, text: str = "", error: Exception | None = None) -> None:
        self._available = available
        self._text = text
        self._error = error

    def is_available(self) -> bool:
        return self._available

    async def transcribe(self, audio_bytes: bytes, mime_type: str) -> str:
        if self._error is not None:
            raise self._error
        return self._text


class _RecordingASR(_FakeASR):
    def __init__(self) -> None:
        super().__init__(text="转写文本")
        self.received: bytes | None = None

    async def transcribe(self, audio_bytes: bytes, mime_type: str) -> str:
        self.received = audio_bytes
        return await super().transcribe(audio_bytes, mime_type)


def _make_asr_module(service: Any) -> ModuleType:
    mod = ModuleType("multimodal")
    mod.get_asr_service = lambda: service
    return mod


async def test_attachment_audio_transcribed_to_text_block(tmp_path, monkeypatch):
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "multimodal", _make_asr_module(_FakeASR(text="转写文本")))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/voice.wav", "mime_type": "audio/wav"}]))
    assert result.state_updates["multimodal_content"] == [{"type": "text", "text": "转写文本"}]


async def test_attachment_audio_asr_unavailable_degrades_with_notice(tmp_path, monkeypatch):
    """ASR 未配置：降级提示必须作为用户可见 text 块进入消息（禁静默丢内容）。"""
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "multimodal", _make_asr_module(_FakeASR(available=False)))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/voice.wav", "mime_type": "audio/wav"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "voice.wav" in blocks[0]["text"]
    assert "未能转为文字" in blocks[0]["text"]
    assert "ASR 服务未配置" in blocks[0]["text"]


async def test_attachment_audio_transcribe_error_degrades_with_notice(tmp_path, monkeypatch):
    """转写异常：降级提示可见且区分于其他失败原因。"""
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "multimodal", _make_asr_module(_FakeASR(error=RuntimeError("boom"))))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/voice.wav", "mime_type": "audio/wav"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "未能转为文字" in blocks[0]["text"]
    assert "转写失败" in blocks[0]["text"]


async def test_attachment_audio_asr_import_error_degrades_with_notice(tmp_path, monkeypatch):
    """ASR 模块导入失败：同样降级为可见提示，不静默。"""
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/voice.wav", "mime_type": "audio/wav"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "未能转为文字" in blocks[0]["text"]


async def test_attachment_audio_data_url_read(monkeypatch):
    payload = b"RIFFxxxxWAVE"
    url = "data:audio/wav;base64," + base64.b64encode(payload).decode("ascii")
    recorder = _RecordingASR()
    monkeypatch.setitem(sys.modules, "multimodal", _make_asr_module(recorder))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": url, "mime_type": "audio/wav"}]))
    assert result.state_updates["multimodal_content"] == [{"type": "text", "text": "转写文本"}]
    # 性质断言：ASR 收到的是解码后的原始字节
    assert recorder.received == payload


async def test_attachment_audio_missing_file_degrades_with_notice(tmp_path, monkeypatch):
    """音频文件缺失：降级提示可见。"""
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/nope.wav", "mime_type": "audio/wav"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "nope.wav" in blocks[0]["text"]
    assert "未能转为文字" in blocks[0]["text"]


async def test_attachment_audio_unsupported_source_degrades_with_notice():
    """远程音频来源不支持：降级提示可见。"""
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "https://x.com/a.wav", "mime_type": "audio/wav"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "a.wav" in blocks[0]["text"]
    assert "未能转为文字" in blocks[0]["text"]


async def test_attachment_audio_invalid_data_url_degrades_with_notice():
    # 载荷长度非 4 的倍数 → base64 解码抛 binascii.Error → 降级提示可见
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "data:audio/wav;base64,abc", "mime_type": "audio/wav"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "未能转为文字" in blocks[0]["text"]


async def test_attachment_audio_data_url_without_base64_degrades_with_notice():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "data:audio/wav,raw", "mime_type": "audio/wav"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "未能转为文字" in blocks[0]["text"]


async def test_audio_to_text_degrade_returns_notice_not_empty(tmp_path, monkeypatch):
    """U22 契约：音频无法转写时返回非空降级提示，绝不返回静默空串。"""
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    text = await pre._audio_to_text("/uploads/nope.wav", "audio/wav")
    assert text != ""
    assert "未能转为文字" in text


def test_read_audio_bytes_oserror_returns_empty(tmp_path, monkeypatch):
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))

    def _raise(self: Path) -> bytes:
        raise OSError("io error")

    monkeypatch.setattr(Path, "read_bytes", _raise)
    pre = MultimodalPreprocessor()
    assert pre._read_audio_bytes("/uploads/voice.wav", "audio/wav") == b""


# ── 附件文本/文档 ────────────────────────────────────────────


async def test_attachment_plain_text_extracted(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_bytes("hello 世界".encode("utf-8"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/note.txt", "mime_type": "text/plain"}]))
    assert result.state_updates["multimodal_content"] == [{"type": "text", "text": "hello 世界"}]


async def test_attachment_json_treated_as_plain_text(tmp_path, monkeypatch):
    (tmp_path / "data.json").write_bytes(b'{"k": 1}')
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/data.json", "mime_type": "application/json"}]))
    assert result.state_updates["multimodal_content"] == [{"type": "text", "text": '{"k": 1}'}]


def _failure_notices(blocks: list[dict]) -> list[str]:
    """提取附件解析失败占位块文本（禁静默跳过契约的可观察面）。"""
    return [b["text"] for b in blocks if b.get("type") == "text" and "解析失败" in b.get("text", "")]


async def test_attachment_plain_text_missing_file_emits_failure_notice(tmp_path, monkeypatch):
    """文本附件文件不存在 → `[附件 … 解析失败：文件不存在]` 占位块（禁静默跳过）。"""
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/nope.txt", "mime_type": "text/plain"}]))
    blocks = result.state_updates["multimodal_content"]
    assert blocks == [{"type": "text", "text": "[附件 /uploads/nope.txt 解析失败：文件不存在]"}]


async def test_attachment_plain_text_read_error_emits_failure_notice(tmp_path, monkeypatch):
    """文本附件读取 OSError → 显式占位块（禁静默跳过）。"""
    (tmp_path / "note.txt").write_bytes(b"hello")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk error")

    monkeypatch.setattr("builtins.open", _raise)
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/note.txt", "mime_type": "text/plain"}]))
    blocks = result.state_updates["multimodal_content"]
    assert blocks == [{"type": "text", "text": "[附件 /uploads/note.txt 解析失败：文件读取失败]"}]


async def test_attachment_octet_stream_routed_to_document_converter(tmp_path, monkeypatch):
    (tmp_path / "data.bin").write_bytes(b"\x00\x01")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/data.bin", "mime_type": "application/octet-stream"}]))
    # 非纯文本 MIME → 走文档转换分支；转换未支持 → 显式占位（禁静默跳过）
    blocks = result.state_updates["multimodal_content"]
    notices = _failure_notices(blocks)
    assert len(notices) == 1
    assert "/uploads/data.bin" in notices[0]


@pytest.mark.parametrize("mime_type", ["application/pdf", "application/octet-stream"])
async def test_attachment_document_conversion_unsupported_emits_notice(tmp_path, monkeypatch, mime_type):
    """文档转换分支走真实代码路径 → 显式"文档转换未支持"占位块（禁静默跳过）。"""
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/doc.pdf", "mime_type": mime_type}]))
    notices = _failure_notices(result.state_updates["multimodal_content"])
    assert len(notices) == 1
    assert "/uploads/doc.pdf" in notices[0]
    assert "文档转换未支持" in notices[0]


# ── 附件图片（本地引用读取失败 → 显式占位，禁空 url 图块） ────


async def test_attachment_image_missing_file_emits_failure_notice(tmp_path, monkeypatch):
    """图片附件文件不存在 → 占位块替代空 url 的 image_url 块（禁静默丢图）。"""
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/nope.png", "mime_type": "image/png"}]))
    assert result.state_updates["multimodal_content"] == [
        {"type": "text", "text": "[附件 /uploads/nope.png 解析失败：文件读取失败或来源不支持]"}
    ]


async def test_attachment_image_read_error_emits_failure_notice(tmp_path, monkeypatch):
    """图片附件读取 OSError → 占位块（禁静默丢图）。"""
    (tmp_path / "cat.png").write_bytes(b"png")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))

    def _raise(self: Path) -> bytes:
        raise OSError("io error")

    monkeypatch.setattr(Path, "read_bytes", _raise)
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/cat.png", "mime_type": "image/png"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(_failure_notices(blocks)) == 1
    assert "/uploads/cat.png" in _failure_notices(blocks)[0]


async def test_attachment_image_success_keeps_image_block(tmp_path, monkeypatch):
    """成功路径不变：图片附件读取成功 → 正常 image_url data URL 块。"""
    (tmp_path / "cat.png").write_bytes(b"png-bytes")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/cat.png", "mime_type": "image/png"}]))
    blocks = result.state_updates["multimodal_content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_attachment_malformed_entry_isolated_with_notice():
    """坏附件条目（非 dict）逐个隔离 → 该附件显式占位，好附件照常处理
    （每附件独立 try，单附件失败不拖垮其他附件/文本）。"""
    pre = MultimodalPreprocessor()
    result = await pre.execute(
        _make_ctx("", ["not-a-dict", {"url": "https://x.com/a.png", "mime_type": "image/png"}])
    )
    blocks = result.state_updates["multimodal_content"]
    image_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert image_blocks == [{"type": "image_url", "image_url": {"url": "https://x.com/a.png"}}]
    notices = _failure_notices(blocks)
    assert len(notices) == 1
    assert "not-a-dict" in notices[0]


# ── 附件视频 ──────────────────────────────────────────────────


async def test_attachment_video_skipped():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/v.mp4", "mime_type": "video/mp4"}]))
    # 视频附件跳过、无消息检出：全量重建为空（不再有旧"空结果不写键"分支）
    assert result.state_updates["multimodal_content"] == []
    assert result.state_updates["has_multimodal"] is False


# ── 消息检测：markdown 图片引用 ────────────────────────────────


def test_detect_md_image_ref_extracted():
    pre = MultimodalPreprocessor()
    assert _refs(pre, "看看这张 ![cat.png](/uploads/cat.png) 怎么样") == ["/uploads/cat.png"]


def test_detect_md_ref_not_duplicated_by_bare_path():
    # ![f](/uploads/x.png) 里的 /uploads/x.png 同时匹配 _LOCAL_FILE_PATTERN——
    # span 重叠守卫必须阻止重复提取
    pre = MultimodalPreprocessor()
    assert _refs(pre, "![图](/uploads/a.png)") == ["/uploads/a.png"]


def test_detect_md_ref_uppercase_ext():
    pre = MultimodalPreprocessor()
    assert _refs(pre, "![A](/uploads/A.PNG)") == ["/uploads/A.PNG"]


def test_detect_md_ref_with_http_url_fallback():
    # 只认平台管理的 /uploads/ 引用；http 图片由 _IMAGE_URL_PATTERN 兜底
    pre = MultimodalPreprocessor()
    assert _refs(pre, "![x](https://evil.com/a.png) 详见链接") == ["https://evil.com/a.png"]


def test_detect_md_ref_missing_file_keeps_ref():
    # 引用不做存在性预检——原样输出，由 llm_core 发送前解析（失败产占位块）
    pre = MultimodalPreprocessor()
    assert _refs(pre, "![f](/uploads/nope.png)") == ["/uploads/nope.png"]


# ── 消息检测：http 图片 URL ──────────────────────────────────


def test_detect_http_url_with_query():
    pre = MultimodalPreprocessor()
    assert _refs(pre, "图 https://example.com/pic.jpg?size=1 完") == [
        "https://example.com/pic.jpg?size=1"
    ]


def test_detect_http_url_uppercase_ext():
    pre = MultimodalPreprocessor()
    assert _refs(pre, "https://example.com/PIC.PNG") == ["https://example.com/PIC.PNG"]


def test_detect_remote_pdf_url_not_extracted():
    # 远程 PDF URL 非图片引用：_IMAGE_URL_PATTERN 只认图片扩展名；
    # _LOCAL_FILE_PATTERN 的盘符分支曾把 "https://" 残缺提取成 "s://…"，
    # 含 scheme 分隔符的命中一律跳过（不产坏块）
    pre = MultimodalPreprocessor()
    assert _refs(pre, "看 https://x.com/a.pdf") == []


# ── 消息检测：本地路径 ────────────────────────────────────────


def test_detect_local_path_existing_emits_ref(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"png")
    pre = MultimodalPreprocessor()
    # 引用原样（llm_core 发送前读文件转 base64 data URL）
    assert _refs(pre, f"截图在 {f} 请分析") == [str(f)]


def test_detect_local_path_missing_still_emits_ref():
    # 无存在性预检：缺失文件同样产引用块，解析失败占位由 llm_core 兜底
    # （禁静默丢内容，2026-09-08 裁定）
    pre = MultimodalPreprocessor()
    missing = str(Path(sys.executable).parent / "definitely_missing_xyz.png")
    assert _refs(pre, f"看 {missing}") == [missing]


def test_detect_local_path_oversize_still_emits_ref(tmp_path):
    # 大小上限预检已删（与 llm_core 的 20MB 上限双层重复）——引用块照产，
    # 超限占位由 llm_core 统一产出
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 100)
    pre = MultimodalPreprocessor()
    assert _refs(pre, f"看 {f}") == [str(f)]


def test_detect_pdf_path_existing_emits_ref(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    pre = MultimodalPreprocessor()
    assert _refs(pre, f"见 {f}") == [str(f)]


def test_detect_drive_letter_path_missing_placeholder():
    # 盘符形态路径（C:\...）由 _LOCAL_FILE_PATTERN 的 (?:[A-Za-z]:)? 分支匹配
    pre = MultimodalPreprocessor()
    assert _refs(pre, "看 C:\\tmp\\nope.png") == ["C:\\tmp\\nope.png"]


def test_detect_uploads_ref_emits_ref(tmp_path, monkeypatch):
    (tmp_path / "cat.png").write_bytes(b"png")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    assert _refs(pre, "看 /uploads/cat.png") == ["/uploads/cat.png"]


def test_detect_no_multimodal_returns_empty():
    pre = MultimodalPreprocessor()
    assert _refs(pre, "纯文本消息") == []


def test_detect_combined_multiple_sources():
    # 三类来源各提一块：md 引用 + http URL + 裸本地路径
    pre = MultimodalPreprocessor()
    assert _refs(pre, "![a](/uploads/a.png) 和 https://x.com/b.jpg 和 /c/d.png") == [
        "/uploads/a.png",
        "https://x.com/b.jpg",
        "/c/d.png",
    ]


# ── 消息扫描：登记与防重发 ────────────────────────────────────


def test_scan_user_messages_only():
    """assistant/tool 消息里的图片语法不检出（防历史回复回灌重发）。"""
    pre = MultimodalPreprocessor()
    blocks, seen = pre._detect_messages_multimodal(
        [
            {"role": "user", "content": "看 ![a](/uploads/a.png)"},
            {"role": "assistant", "content": "好的 ![b](/uploads/b.png)"},
            {"role": "tool", "content": "result ![c](/uploads/c.png)"},
        ],
        {},
    )
    assert [b["image_url"]["url"] for b in blocks] == ["/uploads/a.png"]
    # 无 client_message_id → 内容指纹键
    assert len(seen) == 1
    (seen_key, seen_url), = seen
    assert seen_key.startswith("sha1:")
    assert seen_url == "/uploads/a.png"


def test_scan_registered_entry_not_re_emitted():
    """检出即登记：已登记 (消息键, 引用) 不再产块（防工具循环每轮重发）。"""
    pre = MultimodalPreprocessor()
    msg = {"role": "user", "content": "看 ![a](/uploads/a.png)"}
    blocks1, seen = pre._detect_messages_multimodal([msg], {})
    assert len(blocks1) == 1
    blocks2, seen = pre._detect_messages_multimodal([msg], seen)
    assert blocks2 == []
    assert len(seen) == 1


def test_scan_new_message_detected_after_previous_registered():
    """同会话新带图消息正常检出（登记只挡旧条目，不挡新消息）。"""
    pre = MultimodalPreprocessor()
    blocks1, seen = pre._detect_messages_multimodal(
        [{"role": "user", "content": "一 ![a](/uploads/a.png)"}], {}
    )
    assert len(blocks1) == 1
    blocks2, _ = pre._detect_messages_multimodal(
        [
            {"role": "user", "content": "一 ![a](/uploads/a.png)"},
            {"role": "user", "content": "二 ![b](/uploads/b.png)"},
        ],
        seen,
    )
    assert [b["image_url"]["url"] for b in blocks2] == ["/uploads/b.png"]


def test_message_key_client_message_id_preferred_over_content_hash():
    """幂等键优先：同 id 不同内容同键；无 id 退化内容指纹。"""
    key_a = MultimodalPreprocessor._message_key(
        {"metadata": {"client_message_id": "mc-1"}, "content": "aaa"}
    )
    key_b = MultimodalPreprocessor._message_key(
        {"metadata": {"client_message_id": "mc-1"}, "content": "改写过的内容"}
    )
    assert key_a == key_b == "id:mc-1"

    no_id = MultimodalPreprocessor._message_key({"role": "user", "content": "触发器注入"})
    assert no_id.startswith("sha1:")
    assert (
        MultimodalPreprocessor._message_key({"role": "user", "content": "触发器注入"})
        == no_id
    )
    other = MultimodalPreprocessor._message_key({"role": "user", "content": "另一条"})
    assert other != no_id


async def test_scan_seen_roundtrip_through_state_shapes():
    """seen 经 execute 输出（列表形态）回灌下一轮 execute，防重发全链成立。"""
    pre = MultimodalPreprocessor()

    async def run(state_extra: dict) -> dict:
        state = {"messages": [{"role": "user", "content": "看 ![a](/uploads/a.png)"}]}
        state.update(state_extra)
        result = await pre.execute(SimpleNamespace(state=state))
        return result.state_updates

    first = await run({})
    assert [b["image_url"]["url"] for b in first["multimodal_content"]] == ["/uploads/a.png"]
    assert first["has_multimodal"] is True

    second = await run({"multimodal_seen": first["multimodal_seen"]})
    assert second["multimodal_content"] == []
    assert second["has_multimodal"] is False
    # 登记集保持（不因本轮无新块而清空）
    assert second["multimodal_seen"] == first["multimodal_seen"]


def test_scan_seen_cap_drops_oldest():
    """登记集超上限逐最旧（防御性有界，正常会话远不可达）。"""
    pre = MultimodalPreprocessor()
    seen: dict[tuple[str, str], None] = {(f"k{i}", "/uploads/x.png"): None for i in range(10)}
    pre._MAX_SEEN_ENTRIES = 5
    _, capped = pre._detect_messages_multimodal(
        [{"role": "user", "content": "![n](/uploads/n.png)"}], seen
    )
    assert len(capped) == 5
    assert ("k0", "/uploads/x.png") not in capped
    assert ("k6", "/uploads/x.png") in capped  # 11 条超 5 → 逐最旧删 6 条
    # 新检出的条目不因截断丢失
    assert [url for _, url in capped if url == "/uploads/n.png"] == ["/uploads/n.png"]


def test_load_seen_accepts_state_shapes_and_skips_malformed():
    raw = [["id:a", "/uploads/a.png"], ("id:b", "/uploads/b.png"), "junk", ["only-one"], 42]
    seen = MultimodalPreprocessor._load_seen(raw)
    assert ("id:a", "/uploads/a.png") in seen
    assert ("id:b", "/uploads/b.png") in seen
    assert len(seen) == 2
    assert MultimodalPreprocessor._load_seen(None) == {}


def test_detect_messages_non_list_returns_empty():
    pre = MultimodalPreprocessor()
    blocks, seen = pre._detect_messages_multimodal(None, {})
    assert blocks == []
    assert seen == {}


# ── 工具方法 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("text/plain", True),
        ("text/markdown", True),
        ("application/json", True),
        ("application/xml", True),
        ("application/javascript", True),
        ("application/x-yaml", True),
        ("application/x-sh", True),
        ("application/pdf", False),
        ("image/png", False),
        ("application/octet-stream", False),
    ],
)
def test_is_plain_text_mime(mime: str, expected: bool):
    assert MultimodalPreprocessor._is_plain_text_mime(mime) is expected


def test_local_file_to_data_url_success(tmp_path, monkeypatch):
    payload = b"\x89PNG"
    (tmp_path / "cat.png").write_bytes(payload)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    url = pre._local_file_to_data_url("/uploads/cat.png", "image/png")
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == payload


def test_local_file_to_data_url_non_uploads_returns_empty():
    pre = MultimodalPreprocessor()
    assert pre._local_file_to_data_url("C:/tmp/x.png", "image/png") == ""


def test_local_file_to_data_url_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    assert pre._local_file_to_data_url("/uploads/nope.png", "image/png") == ""


def test_local_file_to_data_url_read_error_returns_empty(tmp_path, monkeypatch):
    (tmp_path / "cat.png").write_bytes(b"png")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))

    def _raise(self: Path) -> bytes:
        raise OSError("io error")

    monkeypatch.setattr(Path, "read_bytes", _raise)
    pre = MultimodalPreprocessor()
    assert pre._local_file_to_data_url("/uploads/cat.png", "image/png") == ""


def test_resolve_upload_path_non_uploads_returns_empty():
    pre = MultimodalPreprocessor()
    assert pre._resolve_upload_path("C:/tmp/x.png") == ""


# ── _detect_messages_multimodal / _extract_image_refs 缺口分支 ──────────


class TestHistoryImageDetectionGaps:
    """历史图片块收集与引用提取的残余分支（coverage.xml 缺口靶行）。"""

    def _pre(self) -> Any:
        return MultimodalPreprocessor(config={})

    @pytest.mark.parametrize(
        ("content", "label"),
        [
            ({"text": "x"}, "dict-content"),
            ("", "empty-str"),
            (None, "none-content"),
            (123, "int-content"),
        ],
    )
    def test_non_string_content_skipped(self, content: Any, label: str) -> None:
        """user 消息 content 非非空字符串（dict/空串/None/数字）→ 跳过该条。"""
        pre = self._pre()
        messages = [{"role": "user", "content": content}]

        blocks, seen = pre._detect_messages_multimodal(messages, {})

        assert blocks == [] and seen == {}, f"{label} 不得产生引用块"

    def test_non_user_and_non_dict_messages_skipped(self) -> None:
        """非 user 角色与非 dict 条目跳过；user 消息照常收集（互不影响）。"""
        pre = self._pre()
        messages = [
            "junk",
            None,
            42,
            {"role": "assistant", "content": "![a](/uploads/a.png)"},
            {"role": "system", "content": "![s](/uploads/s.png)"},
            {"role": "user", "content": "![keep](/uploads/keep.png)"},
        ]

        blocks, seen = pre._detect_messages_multimodal(messages, {})

        assert [b["image_url"]["url"] for b in blocks] == ["/uploads/keep.png"]
        assert len(seen) == 1

    def test_non_list_messages_returns_empty(self) -> None:
        """messages 非 list（None/dict/字符串）→ 空结果，不抛。"""
        pre = self._pre()
        for messages in (None, {"role": "user"}, "junk", 7):
            blocks, seen = pre._detect_messages_multimodal(messages, {})
            assert blocks == [] and seen == {}

    def test_markdown_ref_consumes_span_so_url_not_duplicated(self) -> None:
        """markdown 图引用与 http URL 同 span → 同 span 不重复建块（去重靠 span）。"""
        pre = self._pre()
        text = "![pic](https://cdn.example.com/p.png)"
        messages = [{"role": "user", "content": text}]

        blocks, _seen = pre._detect_messages_multimodal(messages, {})

        assert len(blocks) == 1, "同一图片不得因两类模式各产一块"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("纯文本无引用", []),
            ("![a](/uploads/a.png)", ["/uploads/a.png"]),
            ("![a](/uploads/a.png) 和 https://x.com/b.jpg", ["/uploads/a.png", "https://x.com/b.jpg"]),
            # 本地路径模式要求以 / 或盘符起头：裸相对名（d.pdf）不匹配
            ("看这个 /abs/path/c.jpeg 和 d.pdf", ["/abs/path/c.jpeg"]),
            ("C:/tmp/e.png", ["C:/tmp/e.png"]),  # 盘符路径形态
        ],
    )
    def test_extract_refs_matrix(self, text: str, expected: list[str]) -> None:
        """引用提取矩阵：markdown → http → 本地路径三优先级，去重不重复消费 span。"""
        assert self._pre()._extract_image_refs(text) == expected

    def test_markdown_ref_with_http_url_inside_not_double_counted(self) -> None:
        """markdown 内嵌 http 图 URL：整 token 先匹配，http 模式不重复取。"""
        refs = self._pre()._extract_image_refs("![x](https://cdn.example.com/a.png)")

        assert refs == ["https://cdn.example.com/a.png"]
        assert len(refs) == len(set(refs))

    def test_http_url_span_inside_markdown_token_is_skipped(self) -> None:
        """markdown 链接文本里含 http 图 URL（span 被整 token 覆盖）→ http 模式
        跳过该 span，只保留 markdown 的目标（span 去重防止同图两取）。"""
        text = "![https://cdn.example.com/inner.png](/uploads/outer.png)"

        refs = self._pre()._extract_image_refs(text)

        assert refs == ["/uploads/outer.png"]
        assert not any("cdn.example.com" in r for r in refs)

    def test_local_path_already_consumed_by_md_not_repeated(self) -> None:
        """本地路径与前序模式重叠 span → 不重复进 refs（span 去重生效）。"""
        refs = self._pre()._extract_image_refs("![y](/uploads/mirror.jpg)")

        assert refs.count("/uploads/mirror.jpg") == 1

    def test_seen_entries_overflow_evicts_oldest(self) -> None:
        """登记集超 _MAX_SEEN_ENTRIES → 逐出最旧条目（防长会话无界增长）。"""
        pre = self._pre()
        seen: dict[tuple[str, str], None] = {
            (f"old-{i}", f"/uploads/old-{i}.png"): None for i in range(pre._MAX_SEEN_ENTRIES)
        }

        blocks, trimmed = pre._detect_messages_multimodal(
            [{"role": "user", "content": "![new](/uploads/new.png)"}], seen,
        )

        assert [b["image_url"]["url"] for b in blocks] == ["/uploads/new.png"]
        assert len(trimmed) == pre._MAX_SEEN_ENTRIES, "登记集恒有界"
        assert ("old-0", "/uploads/old-0.png") not in trimmed, "最旧条目被逐出"
        assert ("new-msg", "/uploads/new.png") in trimmed or any(
            url == "/uploads/new.png" for _k, url in trimmed
        )


# ── 模块自举行（plugins/shared 入 sys.path）──────────────────────────


class TestSharedRootBootstrap:
    """模块顶部自举行：plugins/shared 不在路径时按文件路径装载会补插。

    触发场景 = 插件经文件路径装载（spec_from_file_location 不依赖 sys.path），
    随后模块内 ``from uploads_path import`` 要求 shared 在路径上——自举行即兜底。
    """

    def test_shared_root_reinserted_when_load_by_file_path(self) -> None:
        import importlib.util as _ilu
        from pathlib import Path as _Path

        plugin_file = _Path(__file__).resolve().parent / "plugin.py"
        shared_root = str(_Path(__file__).resolve().parents[3])
        original = sys.path[:]
        mod_name = "multimodal_preprocessor_bootstrap_probe"
        sys.modules.pop(mod_name, None)
        try:
            sys.path[:] = [p for p in original if p != shared_root]
            spec = _ilu.spec_from_file_location(mod_name, str(plugin_file))
            assert spec is not None and spec.loader is not None
            mod = _ilu.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)

            assert shared_root in sys.path, "装载必须把 plugins/shared 推回 sys.path"
            assert hasattr(mod, "MultimodalPreprocessor")
            assert callable(mod.resolve_uploads_url), "兄弟裸名模块经自举行后可用"
        finally:
            sys.path[:] = original
            sys.modules.pop(mod_name, None)
