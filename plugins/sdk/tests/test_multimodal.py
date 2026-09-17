# @feature: FP-0.2.一 第三方插件协议 | @ci: python-test
"""multimodal 通用多模态归一通道单测。

MCP image content（标准多模态字段）→ AgentOS 多模态契约（落盘 file_path +
metadata.multimodal_content）的转换全链真实执行：真实 PNG 字节、真实落盘
（tmp_path），无 mock（错误路径用坏 base64 / 超限常量注入）。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from agentos_plugin_sdk import multimodal
from agentos_plugin_sdk.multimodal import (
    error_text,
    mcp_content_parts,
    multimodal_content_from_file,
    normalize_mcp_result,
    save_image_with_multimodal,
)


def _png_bytes(width: int = 8, height: int = 6, color: tuple[int, int, int] = (90, 160, 220)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── content 解析 ──────────────────────────────────────────


class TestMcpContentParts:
    def test_text_image_resource_mixed(self):
        texts, b64, mime = mcp_content_parts(
            {
                "content": [
                    {"type": "text", "text": "a"},
                    "garbage",
                    None,
                    {"type": "resource", "resource": {"text": "res-body"}},
                    {"type": "image", "data": "QUJD", "mimeType": "image/jpeg"},
                    {"type": "text", "text": "b"},
                ]
            }
        )
        assert texts == ["a", "res-body", "b"]
        assert (b64, mime) == ("QUJD", "image/jpeg")

    def test_empty_and_missing_content(self):
        assert mcp_content_parts({}) == ([], "", "")
        assert mcp_content_parts({"content": []}) == ([], "", "")

    def test_error_text_joins_and_falls_back(self):
        assert error_text({"content": [{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]}) == "x\ny"
        assert error_text({"content": []}) == "上游工具执行失败"


# ── 落盘 + 多模态块 ──────────────────────────────────────


class TestSaveImageWithMultimodal:
    def test_saves_bytes_and_builds_data_url(self, tmp_path):
        raw = _png_bytes()
        path, blocks = save_image_with_multimodal(raw, "image/png", str(tmp_path), stem="computer")
        assert Path(path).exists() and Path(path).read_bytes() == raw
        assert Path(path).name.startswith("computer-") and Path(path).suffix == ".png"
        url = blocks[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == raw

    def test_jpeg_extension_and_cwd_fallback(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path, _ = save_image_with_multimodal(b"ff-d8-fake", "image/jpeg", "", stem="browser")
        assert Path(path).suffix == ".jpg"
        assert Path(path).parent.resolve() == tmp_path.resolve()

    def test_creates_missing_workspace_dir(self, tmp_path):
        ws = tmp_path / "not-yet"
        path, _ = save_image_with_multimodal(b"x", "image/png", str(ws))
        assert Path(path).exists()


class TestMultimodalContentFromFile:
    def test_real_file_roundtrip(self, tmp_path):
        raw = _png_bytes()
        f = tmp_path / "gen.png"
        f.write_bytes(raw)
        blocks = multimodal_content_from_file(str(f))
        assert base64.b64decode(blocks[0]["image_url"]["url"].split(",", 1)[1]) == raw

    def test_missing_and_unreadable_return_none(self, tmp_path):
        assert multimodal_content_from_file(str(tmp_path / "nope.png")) is None
        assert multimodal_content_from_file("") is None
        assert multimodal_content_from_file(str(tmp_path)) is None  # 目录：isfile 拦截


# ── normalize_mcp_result 全链 ────────────────────────────────


class TestNormalizeMcpResult:
    def test_text_content_to_snapshot_text(self):
        r = normalize_mcp_result({"content": [{"type": "text", "text": "- page"}]}, tool_name="t")
        assert r.success
        assert r.output == {"status": 200, "tool": "t", "snapshot_text": "- page"}
        assert r.metadata == {"action": "t"}

    def test_is_error_maps_to_upstream_failure(self):
        r = normalize_mcp_result({"isError": True, "content": [{"type": "text", "text": "boom"}]}, tool_name="t")
        assert not r.success
        assert r.error_code == "UPSTREAM_TOOL_ERROR"
        assert "boom" in r.error

    def test_is_error_empty_content_falls_back(self):
        r = normalize_mcp_result({"isError": True, "content": []}, tool_name="t")
        assert "上游工具执行失败" in r.error

    def test_image_full_envelope(self, tmp_path):
        raw = _png_bytes()
        r = normalize_mcp_result(
            {
                "content": [
                    {"type": "text", "text": "shot"},
                    {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/png"},
                ]
            },
            tool_name="computer_take_screenshot",
            workspace=str(tmp_path),
            stem="computer",
        )
        assert r.success
        out = r.output
        assert out["snapshot_text"] == "shot"
        assert Path(out["file_path"]).read_bytes() == raw
        assert out["image_mime"] == "image/png"
        url = r.metadata["multimodal_content"][0]["image_url"]["url"]
        assert base64.b64decode(url.split(",", 1)[1]) == raw

    def test_extra_data_merged(self):
        r = normalize_mcp_result(
            {"content": []}, tool_name="browser_navigate", extra_data={"url": "https://abc.example/"}
        )
        assert r.output["url"] == "https://abc.example/"

    def test_bad_base64_clean_failure(self, tmp_path):
        r = normalize_mcp_result(
            {"content": [{"type": "image", "data": "!!!not-base64!!!", "mimeType": "image/png"}]},
            tool_name="t",
            workspace=str(tmp_path),
        )
        assert not r.success
        assert r.error_code == "IMAGE_DECODE_FAILED"
        assert "base64" in r.error

    def test_oversized_image_clean_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(multimodal, "MAX_IMAGE_BYTES", 10)
        r = normalize_mcp_result(
            {"content": [{"type": "image", "data": base64.b64encode(b"x" * 64).decode("ascii")}]},
            tool_name="t",
            workspace=str(tmp_path),
        )
        assert not r.success
        assert r.error_code == "IMAGE_TOO_LARGE"
        assert "超限" in r.error
