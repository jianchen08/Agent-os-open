"""MultimodalPreprocessor 文本类附件注入测试。

验证文本/文档/代码类附件被正确提取为 text content block，
和用户消息一起发给 LLM（核心需求：能解析出文本的文件都应能发送）。
"""

from __future__ import annotations

import os
from unittest.mock import patch
from dataclasses import dataclass

import pytest

from pipeline.plugin import PluginContext
from plugins.input.multimodal_preprocessor.plugin import MultimodalPreprocessor


@dataclass
class _FakeResult:
    """模拟 ToolExecutionResult 的最小结构。"""

    success: bool = True
    output: object = None
    error: str | None = None
    error_code: str | None = None


def _make_ctx(attachments: list[dict]) -> PluginContext:
    """构造带附件的插件上下文。"""
    return PluginContext(state={"user_input": "分析这个文件", "attachments": attachments})


def _make_plugin() -> MultimodalPreprocessor:
    return MultimodalPreprocessor()


class TestIsPlainTextMime:
    """_is_plain_text_mime 判定。"""

    @pytest.mark.parametrize(
        "mime,expected",
        [
            ("text/plain", True),
            ("text/markdown", True),
            ("text/x-python", True),
            ("application/json", True),
            ("application/xml", True),
            ("application/javascript", True),
            ("application/x-yaml", True),
            ("application/pdf", False),
            ("application/vnd.openxmlformats", False),
            ("image/png", False),
            ("", False),
        ],
    )
    def test_mime_classification(self, mime: str, expected: bool) -> None:
        assert MultimodalPreprocessor._is_plain_text_mime(mime) is expected


class TestPlainTextAttachment:
    """纯文本类附件直接 UTF-8 解码。"""

    def test_txt_file_extracted_as_text_block(self, tmp_path) -> None:
        """text/plain 附件读取后产出 {type:text} 块。"""
        upload_dir = str(tmp_path)
        file_path = tmp_path / "notes.txt"
        file_path.write_text("hello world", encoding="utf-8")

        plugin = _make_plugin()
        with patch.dict(os.environ, {"UPLOADS_DIR": upload_dir}):
            import asyncio

            blocks = asyncio.run(
                plugin._process_attachments([
                    {"url": "/uploads/notes.txt", "mime_type": "text/plain"},
                ])
            )

        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "hello world"

    def test_code_file_treated_as_text(self, tmp_path) -> None:
        """text/x-python 代码文件按纯文本提取。"""
        upload_dir = str(tmp_path)
        (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")

        plugin = _make_plugin()
        import asyncio

        with patch.dict(os.environ, {"UPLOADS_DIR": upload_dir}):
            blocks = asyncio.run(
                plugin._process_attachments([
                    {"url": "/uploads/app.py", "mime_type": "text/x-python"},
                ])
            )

        assert len(blocks) == 1
        assert blocks[0]["text"] == "print('hi')"

    def test_missing_file_returns_empty(self, tmp_path) -> None:
        """文件不存在时返回空列表，不抛异常。"""
        plugin = _make_plugin()
        import asyncio

        with patch.dict(os.environ, {"UPLOADS_DIR": str(tmp_path)}):
            blocks = asyncio.run(
                plugin._process_attachments([
                    {"url": "/uploads/nope.txt", "mime_type": "text/plain"},
                ])
            )
        assert blocks == []


class TestBinaryDocumentAttachment:
    """二进制文档（pdf/docx）经 markitdown 转换。"""

    def test_pdf_converted_via_markitdown(self, tmp_path) -> None:
        """application/pdf 附件走 convert_binary_to_markdown。"""
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")

        fake_result = _FakeResult(
            success=True, output={"content": "# 提取的标题\n正文内容"}
        )
        plugin = _make_plugin()
        import asyncio

        with (
            patch.dict(os.environ, {"UPLOADS_DIR": str(tmp_path)}),
            patch(
                "tools.builtin.binary_converter.tool.convert_binary_to_markdown",
                return_value=fake_result,
            ),
        ):
            blocks = asyncio.run(
                plugin._process_attachments([
                    {"url": "/uploads/doc.pdf", "mime_type": "application/pdf"},
                ])
            )

        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert "提取的标题" in blocks[0]["text"]

    def test_markitdown_failure_returns_empty(self, tmp_path) -> None:
        """markitdown 转换失败（如未安装/文件过大）时静默返回空。"""
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")

        fake_result = _FakeResult(
            success=False, error="markitdown not installed", error_code="MARKITDOWN_NOT_INSTALLED"
        )
        plugin = _make_plugin()
        import asyncio

        with (
            patch.dict(os.environ, {"UPLOADS_DIR": str(tmp_path)}),
            patch(
                "tools.builtin.binary_converter.tool.convert_binary_to_markdown",
                return_value=fake_result,
            ),
        ):
            blocks = asyncio.run(
                plugin._process_attachments([
                    {"url": "/uploads/doc.pdf", "mime_type": "application/pdf"},
                ])
            )
        assert blocks == []

    def test_markitdown_raises_returns_empty(self, tmp_path) -> None:
        """convert_binary_to_markdown 抛异常时静默返回空。"""
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
        plugin = _make_plugin()
        import asyncio

        with (
            patch.dict(os.environ, {"UPLOADS_DIR": str(tmp_path)}),
            patch(
                "tools.builtin.binary_converter.tool.convert_binary_to_markdown",
                side_effect=RuntimeError("boom"),
            ),
        ):
            blocks = asyncio.run(
                plugin._process_attachments([
                    {"url": "/uploads/doc.pdf", "mime_type": "application/pdf"},
                ])
            )
        assert blocks == []


class TestImageAudioVideoUnchanged:
    """图片/音频/视频分支不受文本分支影响。"""

    def test_image_still_image_url_block(self) -> None:
        """image/* 仍走 image_url 块，不进文本分支。"""
        plugin = _make_plugin()
        import asyncio

        with patch.object(
            plugin, "_local_file_to_data_url", return_value="data:image/png;base64,xxx"
        ):
            blocks = asyncio.run(
                plugin._process_attachments([
                    {"url": "/uploads/img.png", "mime_type": "image/png"},
                ])
            )
        assert len(blocks) == 1
        assert blocks[0]["type"] == "image_url"

    def test_video_not_processed_as_text(self) -> None:
        """video/* 不进文本提取分支（保持现状，视频暂不支持）。"""
        plugin = _make_plugin()
        import asyncio

        blocks = asyncio.run(
            plugin._process_attachments([
                {"url": "/uploads/clip.mp4", "mime_type": "video/mp4"},
            ])
        )
        assert blocks == []


class TestExecuteIntegration:
    """execute 全流程：文本附件产出汇入 state['multimodal_content']。"""

    def test_text_attachment_reaches_multimodal_content(self, tmp_path) -> None:
        """选中文本附件后 execute 写入 multimodal_content。"""
        (tmp_path / "readme.md").write_text("# 标题", encoding="utf-8")
        ctx = _make_ctx([
            {"url": "/uploads/readme.md", "mime_type": "text/markdown"},
        ])
        plugin = _make_plugin()
        import asyncio

        with patch.dict(os.environ, {"UPLOADS_DIR": str(tmp_path)}):
            result = asyncio.run(plugin.execute(ctx))

        assert result.state_updates.get("has_multimodal") is True
        blocks = result.state_updates.get("multimodal_content", [])
        assert any(b.get("text") == "# 标题" for b in blocks)
