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


class TestImageSplitInjection:
    """R20 定稿：原图+分块一次注入（规格经服务端注入，非 LLM 可见参数）。

    无规格（max_pixels 缺省/非法）→ 原样单图契约且切分服务零调用（向后兼容，
    行为与 R14 既往逐键一致）；有规格 → 调 multimodal.split_image，tiled 时
    原图 base64_data 保持 + 分块 images[]（tool_core 注图随原图片）+ 切分标注
    content；服务降级（异常/ error 键）→ 原样单图（warn 留痕）。
    """

    @staticmethod
    def _stub_tiled_caller(
        monkeypatch: pytest.MonkeyPatch, tiled: bool = True, error: str | None = None
    ) -> list[tuple[str, str, int]]:
        from agentos_builtin_tools import fs_tools

        calls: list[tuple[str, str, int]] = []

        async def _caller(base64_data: str, mime_type: str, max_pixels: int) -> dict[str, object]:
            calls.append((base64_data, mime_type, max_pixels))
            if error is not None:
                return {"error": error, "tiled": False}
            if not tiled:
                return {"tiled": False, "tiles": []}
            return {
                "tiled": True,
                "width": 2000,
                "height": 1000,
                "grid": {"rows": 1, "cols": 2},
                "tiles": [
                    {
                        "base64_data": "dGl6MA==",
                        "mime_type": "image/png",
                        "width": 1000,
                        "height": 1000,
                        "row": 1,
                        "col": 1,
                        "x0": 0,
                        "y0": 0,
                        "x1": 1000,
                        "y1": 1000,
                    },
                    {
                        "base64_data": "dGl6MQ==",
                        "mime_type": "image/png",
                        "width": 1000,
                        "height": 1000,
                        "row": 1,
                        "col": 2,
                        "x0": 1000,
                        "y0": 0,
                        "x1": 2000,
                        "y1": 1000,
                    },
                ],
                "annotation": "[图像切分] 原图 2000×1000 … 块(行1,列1) 覆盖 x=0..1000",
            }

        fs_tools.set_split_image_caller(_caller)
        monkeypatch.setattr(fs_tools, "set_split_image_caller", fs_tools.set_split_image_caller)
        return calls

    @pytest.fixture(autouse=True)
    def _reset_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentos_builtin_tools import fs_tools

        monkeypatch.setattr(fs_tools, "_split_image_caller", None)

    async def test_tiled_injects_original_plus_tiles_with_annotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _PNG_MAGIC + b"\x00\x01image-body"
        f = tmp_path / "poster.png"
        f.write_bytes(raw)
        calls = self._stub_tiled_caller(monkeypatch)

        result = await file_read(
            path=str(f), workspace=str(tmp_path), llm_vision_max_pixels=1048576
        )
        assert result.success, result.error
        out = result.output
        # 原图契约键原样保持（前端卡片 + tool_core 注图首图数据源）
        assert base64.b64decode(out["base64_data"]) == raw
        assert out["mime_type"] == "image/png"
        assert Path(out["path"]) == f.resolve()
        assert out["size"] == len(raw)
        # 分块面：tool_core images[].base64 契约键映射（tile base64_data → base64）
        assert [img["base64"] for img in out["images"]] == ["dGl6MA==", "dGl6MQ=="]
        assert all(img["mime_type"] == "image/png" for img in out["images"])
        assert all(img["path"] == out["path"] for img in out["images"])
        assert out["image_grid"] == {"rows": 1, "cols": 2}
        assert out["tile_count"] == 2
        # 切分标注走 content 文本直通通道（serialize_for_content verbatim）
        assert "块(行1,列1)" in out["content"]
        # 服务调用参数：原图 base64 + mime + 注入规格
        assert calls == [(out["base64_data"], "image/png", 1048576)]

    async def test_within_spec_returns_exact_original_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _PNG_MAGIC + b"tiny"
        f = tmp_path / "small.png"
        f.write_bytes(raw)
        calls = self._stub_tiled_caller(monkeypatch, tiled=False)

        result = await file_read(
            path=str(f), workspace=str(tmp_path), llm_vision_max_pixels=1048576
        )
        assert result.success
        out = result.output
        assert set(out) == {"path", "mime_type", "base64_data", "size"}, "未切分=原样契约"
        assert base64.b64decode(out["base64_data"]) == raw
        assert len(calls) == 1, "服务被调用但 tiled=false → 原样返回"

    async def test_no_spec_skips_split_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无规格（未注入/非视觉模型常态）→ 原样契约，切分服务零调用。"""
        raw = _PNG_MAGIC + b"body"
        f = tmp_path / "pic.png"
        f.write_bytes(raw)
        calls = self._stub_tiled_caller(monkeypatch)

        result = await file_read(path=str(f), workspace=str(tmp_path))
        assert result.success
        assert set(result.output) == {"path", "mime_type", "base64_data", "size"}
        assert calls == [], "无规格不得触达切分服务"

    @pytest.mark.parametrize("bad", [0, -3, "1568", True])
    async def test_invalid_spec_treated_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
    ) -> None:
        raw = _PNG_MAGIC + b"body"
        f = tmp_path / "pic.png"
        f.write_bytes(raw)
        calls = self._stub_tiled_caller(monkeypatch)

        result = await file_read(
            path=str(f), workspace=str(tmp_path), llm_vision_max_pixels=bad  # type: ignore[arg-type]
        )
        assert result.success
        assert set(result.output) == {"path", "mime_type", "base64_data", "size"}
        assert calls == []

    async def test_service_error_falls_back_to_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _PNG_MAGIC + b"body"
        f = tmp_path / "pic.png"
        f.write_bytes(raw)
        self._stub_tiled_caller(monkeypatch, error="图像解码失败（PIL）")

        result = await file_read(
            path=str(f), workspace=str(tmp_path), llm_vision_max_pixels=1048576
        )
        assert result.success, "切分失败不得拖垮读图主功能"
        assert set(result.output) == {"path", "mime_type", "base64_data", "size"}

    async def test_service_unavailable_falls_back_to_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _PNG_MAGIC + b"body"
        f = tmp_path / "pic.png"
        f.write_bytes(raw)
        from agentos_builtin_tools import fs_tools

        async def _boom(b64: str, mime: str, max_pixels: int) -> dict[str, object]:
            raise RuntimeError("capability not connected")

        fs_tools.set_split_image_caller(_boom)

        result = await file_read(
            path=str(f), workspace=str(tmp_path), llm_vision_max_pixels=1048576
        )
        assert result.success, "服务不可用不得拖垮读图主功能"
        assert set(result.output) == {"path", "mime_type", "base64_data", "size"}
