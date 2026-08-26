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
  来源不支持/非法 data URL 静默跳过、data URL 字节透传、读文件 OSError；
- 附件文本/文档：纯文本 UTF-8 提取、json 按纯文本、octet-stream 走文档转换、
  文档经 binary_converter 转 markdown、转换失败/异常/无内容跳过、转换器缺失跳过、
  读文件 OSError；
- 附件视频：跳过；
- 文本检测：markdown 引用整 token 消费、与裸路径正则去重、http URL（含 query/
  大写扩展名）、本地路径存在/缺失/过大/不支持类型、pdf 引用、盘符路径、
  远程 pdf URL 现状行为、组合多来源、无多模态空列表；
- 工具方法：_extract_remaining_text 排序/全消费、_is_plain_text_mime 枚举、
  _local_file_to_data_url 成功/非 uploads/缺失/读错误、_resolve_upload_path。

外部依赖 mock 边界：ASR 服务（multimodal 跨进程 capability）与 binary_converter
（tools.builtin 第三方转换能力）以 sys.modules 桩注入；文件系统经真实 tmp 文件
（读错误分支 mock Path.read_bytes / builtins.open）。
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


def _make_ctx(user_input: str, attachments: list | None = None) -> SimpleNamespace:
    """构造最小 PluginContext 形状（execute 只读 state）。"""
    state: dict = {"user_input": user_input}
    if attachments is not None:
        state["attachments"] = attachments
    return SimpleNamespace(state=state)


def _detect(pre: Any, text: str) -> list[dict]:
    # 单测直访私有法（检测器是被测单元）
    return pre._detect_multimodal(text)  # noqa: SLF001


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


def test_max_file_size_default_and_override():
    assert MultimodalPreprocessor()._max_file_size == 20 * 1024 * 1024
    assert MultimodalPreprocessor({"max_file_size": 100})._max_file_size == 100


# ── execute 状态输出 ─────────────────────────────────────────


async def test_execute_no_multimodal_returns_empty():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("纯文本消息"))
    assert result.state_updates == {}


async def test_execute_merges_attachment_and_text_blocks():
    pre = MultimodalPreprocessor()
    result = await pre.execute(
        _make_ctx("图 https://example.com/b.jpg", [{"url": "https://x.com/a.png", "mime_type": "image/png"}])
    )
    assert result.state_updates["has_multimodal"] is True
    blocks = result.state_updates["multimodal_content"]
    urls = [b["image_url"]["url"] for b in blocks if b.get("type") == "image_url"]
    assert urls == ["https://x.com/a.png", "https://example.com/b.jpg"]
    assert any(b.get("type") == "text" for b in blocks)


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


async def test_attachment_audio_asr_unavailable_skipped(tmp_path, monkeypatch):
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "multimodal", _make_asr_module(_FakeASR(available=False)))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/voice.wav", "mime_type": "audio/wav"}]))
    assert result.state_updates == {}


async def test_attachment_audio_transcribe_error_skipped(tmp_path, monkeypatch):
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "multimodal", _make_asr_module(_FakeASR(error=RuntimeError("boom"))))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/voice.wav", "mime_type": "audio/wav"}]))
    assert result.state_updates == {}


async def test_attachment_audio_asr_import_error_skipped(tmp_path, monkeypatch):
    (tmp_path / "voice.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/voice.wav", "mime_type": "audio/wav"}]))
    assert result.state_updates == {}


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


async def test_attachment_audio_missing_file_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/nope.wav", "mime_type": "audio/wav"}]))
    assert result.state_updates == {}


async def test_attachment_audio_unsupported_source_skipped():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "https://x.com/a.wav", "mime_type": "audio/wav"}]))
    assert result.state_updates == {}


async def test_attachment_audio_invalid_data_url_skipped():
    # 载荷长度非 4 的倍数 → base64 解码抛 binascii.Error → 记日志返回空
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "data:audio/wav;base64,abc", "mime_type": "audio/wav"}]))
    assert result.state_updates == {}


async def test_attachment_audio_data_url_without_base64_skipped():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "data:audio/wav,raw", "mime_type": "audio/wav"}]))
    assert result.state_updates == {}


async def test_audio_to_text_empty_bytes_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    assert await pre._audio_to_text("/uploads/nope.wav", "audio/wav") == ""


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


async def test_attachment_plain_text_missing_file_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/nope.txt", "mime_type": "text/plain"}]))
    assert result.state_updates == {}


async def test_attachment_plain_text_read_error_skipped(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_bytes(b"hello")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk error")

    monkeypatch.setattr("builtins.open", _raise)
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/note.txt", "mime_type": "text/plain"}]))
    assert result.state_updates == {}


async def test_attachment_octet_stream_routed_to_document_converter(tmp_path, monkeypatch):
    (tmp_path / "data.bin").write_bytes(b"\x00\x01")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/data.bin", "mime_type": "application/octet-stream"}]))
    # 非纯文本 MIME → 走 markitdown 转换；binary_converter 未安装 → 跳过
    assert result.state_updates == {}


def _install_binary_converter(monkeypatch: pytest.MonkeyPatch, converter: Any) -> None:
    """以 sys.modules 桩注入 tools.builtin.binary_converter.tool（第三方转换能力）。"""
    tool_mod = ModuleType("tools.builtin.binary_converter.tool")
    tool_mod.convert_binary_to_markdown = converter
    bc_mod = ModuleType("tools.builtin.binary_converter")
    bc_mod.tool = tool_mod
    builtin_mod = ModuleType("tools.builtin")
    builtin_mod.binary_converter = bc_mod
    tools_mod = ModuleType("tools")
    tools_mod.builtin = builtin_mod
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.builtin", builtin_mod)
    monkeypatch.setitem(sys.modules, "tools.builtin.binary_converter", bc_mod)
    monkeypatch.setitem(sys.modules, "tools.builtin.binary_converter.tool", tool_mod)


def _ok_result(content: Any) -> SimpleNamespace:
    return SimpleNamespace(success=True, error_code=None, error=None, output=content)


async def test_attachment_document_converted_via_markitdown(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    _install_binary_converter(monkeypatch, lambda path: _ok_result({"content": "markdown 正文"}))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/doc.pdf", "mime_type": "application/pdf"}]))
    assert result.state_updates["multimodal_content"] == [{"type": "text", "text": "markdown 正文"}]


@pytest.mark.parametrize("output", [{}, "plain string", None])
async def test_attachment_document_output_without_content_skipped(tmp_path, monkeypatch, output):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    _install_binary_converter(monkeypatch, lambda path: _ok_result(output))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/doc.pdf", "mime_type": "application/pdf"}]))
    assert result.state_updates == {}


async def test_attachment_document_conversion_failure_skipped(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    _install_binary_converter(
        monkeypatch, lambda path: SimpleNamespace(success=False, error_code="E1", error="boom", output=None)
    )
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/doc.pdf", "mime_type": "application/pdf"}]))
    assert result.state_updates == {}


async def test_attachment_document_conversion_exception_skipped(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))

    def _boom(path: Path) -> SimpleNamespace:
        raise RuntimeError("convert failed")

    _install_binary_converter(monkeypatch, _boom)
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/doc.pdf", "mime_type": "application/pdf"}]))
    assert result.state_updates == {}


async def test_attachment_document_converter_missing_skipped(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/doc.pdf", "mime_type": "application/pdf"}]))
    assert result.state_updates == {}


# ── 附件视频 ──────────────────────────────────────────────────


async def test_attachment_video_skipped():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("", [{"url": "/uploads/v.mp4", "mime_type": "video/mp4"}]))
    assert result.state_updates == {}


# ── 文本检测：markdown 图片引用 ────────────────────────────────


def test_detect_md_image_ref_clean_text():
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看看这张 ![cat.png](/uploads/cat.png) 怎么样")
    assert [b for b in blocks if b.get("type") == "image_url"] == [
        {"type": "image_url", "image_url": {"url": "/uploads/cat.png"}}
    ]
    # 整 token 消费：剩余文本无 markdown 残渣（join 语义下空白可能翻倍，只断性质）
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    assert len(text_blocks) == 1
    text = text_blocks[0]["text"]
    assert "看看这张" in text and "怎么样" in text
    assert "![" not in text and "cat.png" not in text


def test_detect_md_ref_not_duplicated_by_bare_path():
    # ![f](/uploads/x.png) 里的 /uploads/x.png 同时匹配 _LOCAL_FILE_PATTERN——
    # span 重叠守卫必须阻止重复建块
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "![图](/uploads/a.png)")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image_url"


def test_detect_md_ref_uppercase_ext():
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "![A](/uploads/A.PNG)")
    assert len(blocks) == 1
    assert blocks[0]["image_url"]["url"] == "/uploads/A.PNG"


def test_detect_md_ref_with_http_url_fallback():
    # 只认平台管理的 /uploads/ 引用；http 图片由 _IMAGE_URL_PATTERN 兜底
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "![x](https://evil.com/a.png) 详见链接")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["image_url"]["url"] == "https://evil.com/a.png"


def test_detect_md_ref_missing_file_keeps_ref():
    # markdown 引用不做存在性检查——引用原样输出，由 llm_core 发送前解析
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "![f](/uploads/nope.png)")
    assert len(blocks) == 1
    assert blocks[0]["image_url"]["url"] == "/uploads/nope.png"


# ── 文本检测：http 图片 URL ──────────────────────────────────


def test_detect_http_url_with_query():
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "图 https://example.com/pic.jpg?size=1 完")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["image_url"]["url"] == "https://example.com/pic.jpg?size=1"


def test_detect_http_url_uppercase_ext():
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "https://example.com/PIC.PNG")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1


def test_detect_remote_pdf_url_placeholder():
    # 远程 PDF URL：_IMAGE_URL_PATTERN 只认图片扩展名，_LOCAL_FILE_PATTERN 命中
    # 其路径段 → 按本地路径判存在性 → 文本占位（现状行为，见报告疑似 bug）
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看 https://x.com/a.pdf")
    placeholders = [b for b in blocks if b.get("type") == "text" and "文件不存在" in b["text"]]
    assert placeholders


# ── 文本检测：本地路径 ────────────────────────────────────────


def test_detect_local_path_existing_emits_ref(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"png")
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, f"截图在 {f} 请分析")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    # 引用原样（llm_core 发送前读文件转 base64 data URL）
    assert img_blocks[0]["image_url"]["url"] == str(f)


def test_detect_local_path_missing_text_placeholder():
    pre = MultimodalPreprocessor()
    missing = str(Path(sys.executable).parent / "definitely_missing_xyz.png")
    blocks = _detect(pre, f"看 {missing}")
    placeholders = [b for b in blocks if b.get("type") == "text" and "文件不存在" in b["text"]]
    assert placeholders


def test_detect_local_path_oversize_text_placeholder(tmp_path):
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 100)
    pre = MultimodalPreprocessor({"max_file_size": 10})
    blocks = _detect(pre, f"看 {f}")
    placeholders = [b for b in blocks if b.get("type") == "text" and "文件过大" in b["text"]]
    assert placeholders
    assert "100 bytes" in placeholders[0]["text"]


def test_build_local_file_block_unsupported_ext_text_placeholder(tmp_path):
    # _LOCAL_FILE_PATTERN 只匹配图片/pdf 扩展名，故"不支持的文件类型"分支
    # 只能经 _build_local_file_block 直调触达（存在但扩展名不在映射表）
    f = tmp_path / "notes.xyz"
    f.write_bytes(b"hello")
    pre = MultimodalPreprocessor()
    block = pre._build_local_file_block(str(f))
    assert block == {"type": "text", "text": "[不支持的文件类型: .xyz]"}


def test_detect_pdf_path_existing_emits_ref(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, f"见 {f}")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["image_url"]["url"] == str(f)


def test_detect_drive_letter_path_missing_placeholder():
    # 盘符形态路径（C:\...）由 _LOCAL_FILE_PATTERN 的 (?:[A-Za-z]:)? 分支匹配
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看 C:\\tmp\\nope.png")
    placeholders = [b for b in blocks if b.get("type") == "text" and "文件不存在" in b["text"]]
    assert placeholders


def test_detect_uploads_ref_existing_emits_ref(tmp_path, monkeypatch):
    (tmp_path / "cat.png").write_bytes(b"png")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看 /uploads/cat.png")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["image_url"]["url"] == "/uploads/cat.png"


def test_detect_uploads_ref_missing_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看 /uploads/nope.png")
    placeholders = [b for b in blocks if b.get("type") == "text" and "文件不存在" in b["text"]]
    assert placeholders


def test_detect_no_multimodal_returns_empty():
    pre = MultimodalPreprocessor()
    assert _detect(pre, "纯文本消息") == []


def test_detect_combined_multiple_sources():
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "![a](/uploads/a.png) 和 https://x.com/b.jpg 和 /c/d.png")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 2


# ── 工具方法 ─────────────────────────────────────────────────


def test_extract_remaining_text_removes_spans():
    pre = MultimodalPreprocessor()
    assert pre._extract_remaining_text("abcXYZdef", [(3, 6)]) == "abc def"


def test_extract_remaining_text_multiple_spans_sorted():
    # 乱序 spans 先排序再切分
    pre = MultimodalPreprocessor()
    assert pre._extract_remaining_text("abCDefGHij", [(6, 8), (2, 4)]) == "ab ef ij"


def test_extract_remaining_text_full_consumption():
    pre = MultimodalPreprocessor()
    assert pre._extract_remaining_text("abcXYZdef", [(0, 9)]) == ""


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
