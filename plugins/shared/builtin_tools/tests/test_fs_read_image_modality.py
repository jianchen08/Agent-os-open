# @feature: FP-0.2.spill_guard 图片模态读取 | @vision: V1 可进化 | @ci: python-coverage
"""file_read 二进制按模型模态分流测试（2026-09-14 用户裁定）。

图片模态文件：不再"Binary file or encoding issue"拒绝，改走
base64_data + mime_type 契约（tool_core inject_multimodal 按
state.llm_supports_vision——模型 multimodal 声明——决定注图或文本引导）。
非模态二进制维持干净拒绝；超平台内联上限的图片拒绝并带体量。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agentos_builtin_tools.fs_tools import _MAX_IMAGE_EMBED_BYTES, file_read

pytestmark = pytest.mark.unit

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff\xe0"
# RIFF 容器体量字段：嗅探只看偏移 8-12，值本身无关
NULL4 = b"\x00\x00\x00\x00"


class TestImageModalityRead:
    async def test_png_read_returns_base64_contract(self, tmp_path: Path) -> None:
        """PNG：成功返回 base64_data + mime_type 契约键，内容无损往返。"""
        raw = _PNG_MAGIC + b"\x00\x01image-body"
        f = tmp_path / "pic.png"
        f.write_bytes(raw)

        result = await file_read(path=str(f), workspace=str(tmp_path))
        assert result.success, f"图片读取不应拒绝: {result.error}"
        assert result.output["mime_type"] == "image/png"
        assert base64.b64decode(result.output["base64_data"]) == raw, "base64 无损往返"
        assert result.output["size"] == len(raw)
        assert Path(result.output["path"]) == f.resolve()

    async def test_jpeg_magic_also_routes_to_contract(self, tmp_path: Path) -> None:
        """JPEG 魔数同走模态契约（≥2 组有区分度输入）。"""
        raw = _JPEG_MAGIC + b"body"
        f = tmp_path / "pic.jpg"
        f.write_bytes(raw)

        result = await file_read(path=str(f), workspace=str(tmp_path))
        assert result.success
        assert result.output["mime_type"] == "image/jpeg"

    async def test_non_image_binary_still_rejected(self, tmp_path: Path) -> None:
        """非模态二进制维持干净拒绝（错误文案不变）。"""
        f = tmp_path / "blob.bin"
        f.write_bytes(b"\x00\x01\x02not-an-image")

        result = await file_read(path=str(f), workspace=str(tmp_path))
        assert result.success is False
        assert "Binary file or encoding issue" in (result.error or "")

    async def test_oversize_image_rejected_with_size(self, tmp_path: Path) -> None:
        """超平台内联上限的图片：拒绝并带体量（不静默截断 base64）。"""
        f = tmp_path / "huge.png"
        f.write_bytes(_PNG_MAGIC + b"\x00" * (_MAX_IMAGE_EMBED_BYTES + 1))

        result = await file_read(path=str(f), workspace=str(tmp_path))
        assert result.success is False
        assert "Image too large" in (result.error or "")


class TestSniffImageMimeFamilies:
    """魔数嗅探覆盖各图片家族（fs_tools.py:193-201）。

    嗅探是模态契约的入口判据：家族识别错会让 tool_core 注错 MIME，
    模型按错误内容类型解释 base64。
    """

    @pytest.mark.parametrize(
        ("magic", "expected_mime", "ext"),
        [
            (b"GIF87a", "image/gif", ".gif"),
            (b"GIF89a", "image/gif", ".gif"),
            (b"RIFF" + NULL4 + b"WEBP", "image/webp", ".webp"),
            (b"BM", "image/bmp", ".bmp"),
        ],
        ids=["gif87a", "gif89a", "webp", "bmp"],
    )
    async def test_family_magic_routes_to_mime_contract(
        self, tmp_path: Path, magic: bytes, expected_mime: str, ext: str
    ) -> None:
        """GIF（两版本）/ WEBP / BMP 魔数 → 对应 MIME 的 base64 契约。"""
        raw = magic + NULL4 + NULL4 + b"payload"
        f = tmp_path / f"pic{ext}"
        f.write_bytes(raw)

        result = await file_read(path=str(f), workspace=str(tmp_path))

        assert result.success, f"应走模态契约: {result.error}"
        assert result.output["mime_type"] == expected_mime
        assert base64.b64decode(result.output["base64_data"]) == raw

    async def test_riff_without_webp_tag_is_not_image(self, tmp_path: Path) -> None:
        """RIFF 容器但非 WEBP 标记（如 WAV）→ 不误判为图片，维持干净拒绝。"""
        f = tmp_path / "sound.bin"
        f.write_bytes(b"RIFF" + NULL4 + b"WAVEfmt " + NULL4 + NULL4)

        result = await file_read(path=str(f), workspace=str(tmp_path))

        assert result.success is False
        assert "Binary file or encoding issue" in (result.error or "")


class TestHeadReadIoFailure:
    """二进制闸的头部读取 IO 故障（fs_tools.py:265-269）。

    头部读取是嗅探前置：读失败必须干净报错（不得继续按文本读而把 IO 故障
    伪装成编码问题）。
    """

    async def test_head_open_oserror_is_clean_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """file_path.open("rb") 抛 OSError → Read error 干净失败（268-269）。"""
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")

        real_open = Path.open

        def _boom(self: Path, *args: object, **kwargs: object):
            if str(self) == str(f) and args and args[0] == "rb":
                raise OSError("device stuck")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _boom)

        result = await file_read(path=str(f), workspace=str(tmp_path))

        assert result.success is False
        assert "Read error" in result.error
        assert "device stuck" in result.error
