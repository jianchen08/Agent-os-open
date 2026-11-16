# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""file_read 目录模式与文档 markdown 回退测试（list_directory 合并件）。

锁定契约（ADR 2026-10-16-file-read-absorb-list-directory）：
1. 目录模式：path 指向目录 → 只给 items（name/type/size，名称序）+
   count，不产出文件内容字段；include_hidden / pattern 过滤语义同原
   list_directory；未注入 workspace 时 fail-closed（同文件模式）；
2. 文档回退：docx → markdown 文本（converted=true + source_format）；
   无文本层 pdf → 显式失败（禁静默空内容）；非文档二进制维持原拒绝；
3. 模态不回归：图片（NUL 头）仍走 base64 模态契约，不经文档转换。

打桩边界：零 mock——docx 用 python-docx 真实生成，pdf 用 pypdf 真实生成，
其余走 tmp_path 真实文件操作。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfWriter

from agentos_builtin_tools.fs_tools import file_read
from agentos_builtin_tools.server import TOOL_REGISTRY

pytestmark = pytest.mark.unit


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# ─────────────────────── 目录模式（list_directory 合并件） ───────────────────────


class TestFileReadDirectoryMode:
    async def test_directory_returns_items_only(self, tmp_path: Path) -> None:
        ws = _ws(tmp_path)
        (ws / "a.txt").write_text("x", encoding="utf-8")
        (ws / "pkg").mkdir()
        (ws / "pkg" / "inner.py").write_text("y", encoding="utf-8")

        result = await file_read(str(ws), workspace=str(ws))

        assert result.success is True, result.error
        items = result.output["items"]
        names = [i["name"] for i in items]
        assert names == sorted(names), "条目按名称序（性质断言）"
        by_name = {i["name"]: i for i in items}
        assert by_name["a.txt"]["type"] == "file"
        assert by_name["a.txt"]["size"] == 1
        assert by_name["pkg"]["type"] == "directory"
        assert result.output["count"] == len(items) == 2
        assert result.output["file"] == str(ws.resolve())
        # 目录模式只给目录列表：不产出文件内容字段
        assert "content" not in result.output
        assert "lines" not in result.output

    async def test_directory_hidden_and_pattern_filters(self, tmp_path: Path) -> None:
        ws = _ws(tmp_path)
        (ws / ".hidden").write_text("x", encoding="utf-8")
        (ws / "a.py").write_text("x", encoding="utf-8")
        (ws / "b.txt").write_text("y", encoding="utf-8")

        default = await file_read(str(ws), workspace=str(ws))
        names = {i["name"] for i in default.output["items"]}
        assert names == {"a.py", "b.txt"}, "隐藏项默认剔除"

        with_hidden = await file_read(str(ws), include_hidden=True, workspace=str(ws))
        assert ".hidden" in {i["name"] for i in with_hidden.output["items"]}

        only_py = await file_read(str(ws), pattern="*.py", workspace=str(ws))
        assert {i["name"] for i in only_py.output["items"]} == {"a.py"}

    async def test_directory_without_injection_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = await file_read(str(tmp_path))
        assert result.success is False
        assert "未注入" in result.error

    async def test_file_mode_unchanged(self, tmp_path: Path) -> None:
        ws = _ws(tmp_path)
        (ws / "f.txt").write_text("hello", encoding="utf-8")
        result = await file_read("f.txt", workspace=str(ws))
        assert result.success is True
        assert result.output["content"] == "hello"
        assert "items" not in result.output, "文件模式不产出目录字段"


# ─────────────────────── 文档 → markdown 回退 ───────────────────────


class TestDocumentMarkdownFallback:
    async def test_docx_converted_to_markdown(self, tmp_path: Path) -> None:
        ws = _ws(tmp_path)
        doc = Document()
        doc.add_heading("立项背景", level=1)
        doc.add_paragraph("正文第一段。")
        target = ws / "spec.docx"
        doc.save(str(target))

        result = await file_read(str(target), workspace=str(ws))

        assert result.success is True, result.error
        assert result.output["converted"] is True
        assert result.output["source_format"] == "docx"
        assert "# 立项背景" in result.output["content"]
        assert "正文第一段。" in result.output["content"]
        assert result.output["size"] == target.stat().st_size

    async def test_pdf_without_text_layer_fails_explicitly(self, tmp_path: Path) -> None:
        ws = _ws(tmp_path)
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        target = ws / "scan.pdf"
        with target.open("wb") as fh:
            writer.write(fh)

        result = await file_read(str(target), workspace=str(ws))

        assert result.success is False
        assert "转换失败" in result.error, "无文本层显式报错（禁静默空内容）"

    async def test_unsupported_binary_still_rejected(self, tmp_path: Path) -> None:
        ws = _ws(tmp_path)
        target = ws / "data.bin"
        target.write_bytes(b"\x00\x01\x02not-an-image-or-document")

        result = await file_read(str(target), workspace=str(ws))

        assert result.success is False
        assert "Binary file or encoding issue" in result.error

    async def test_image_still_routes_to_modality(self, tmp_path: Path) -> None:
        ws = _ws(tmp_path)
        target = ws / "pic.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        result = await file_read(str(target), workspace=str(ws))

        assert result.success is True
        assert result.output["mime_type"] == "image/png"
        assert "base64_data" in result.output, "图片仍走模态契约，不经文档转换"


# ─────────────────────── 声明面契约 ───────────────────────


class TestMergedDeclaration:
    def test_list_directory_retired_from_registry(self) -> None:
        assert "list_directory" not in TOOL_REGISTRY
        assert "file_read" in TOOL_REGISTRY

    def test_file_read_schema_declares_directory_params(self) -> None:
        schema = TOOL_REGISTRY["file_read"][0]
        props = schema["properties"]
        assert "include_hidden" in props
        assert "pattern" in props
        assert "path" in schema.get("required", [])
