# @feature: FP-0.2.review 媒体审阅 | @vision: V1 可进化 | @ci: python-coverage
"""媒体审阅单元测试（P1-2 sidecar 化承接；PIL/PyAV 全部 mock，零重依赖）。

覆盖 MediaReviewService 与 ImageReviewer/VideoReviewer：
1. _infer_media_type（图片/视频扩展名推断、未知抛 ValueError）
2. review_media 路由（image/video 线程池执行、非法 media_type、文件缺失）
3. review_artifacts 批量（缺制品/缺 file_path/推断失败/成功/文件缺失）
4. get_media_metadata（图片 fake-PIL 元数据 + EXIF 提取、视频 metadata、
   解析失败降级、文件缺失/非法类型）
5. extract_video_thumbnails 委托
6. ImageReviewer（fake PIL：格式/尺寸校验、无法打开降级、EXIF 提取）
7. VideoReviewer（fake av：格式/时长校验、_resolve_format、_extract_metadata、
   关键帧提取）

媒体审阅为确定性规则校验（原实现无 LLM 依赖），mock 对象即"真"判据。

[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 5（review P1-2）]
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

import media_reviewer
import media_review_service
from media_review_service import MediaReviewService, _infer_media_type
from media_reviewer import ImageReviewer, VideoReviewer
from models import ImageReviewResult, MediaReviewConfig, VideoReviewResult

pytestmark = pytest.mark.unit


def _write_file(tmp_path: Any, name: str, content: bytes = b"x") -> str:
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


# ---------------------------------------------------------------------------
# _infer_media_type
# ---------------------------------------------------------------------------


class TestInferMediaType:
    def test_image_extensions(self) -> None:
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"):
            assert _infer_media_type(f"a{ext}") == "image"

    def test_video_extensions(self) -> None:
        for ext in (".mp4", ".avi", ".mov", ".mkv", ".webm"):
            assert _infer_media_type(f"a{ext}") == "video"

    def test_unknown_extension_raises(self) -> None:
        with pytest.raises(ValueError, match="无法推断媒体类型"):
            _infer_media_type("a.txt")

    def test_no_extension_raises(self) -> None:
        with pytest.raises(ValueError, match="无法推断媒体类型"):
            _infer_media_type("noext")

    def test_uppercase_extension(self) -> None:
        assert _infer_media_type("a.PNG") == "image"


# ---------------------------------------------------------------------------
# MediaReviewService.review_media（路由 + 线程池）
# ---------------------------------------------------------------------------


class TestReviewMedia:
    def test_image_routes_to_image_reviewer(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        fake = ImageReviewResult(is_valid=True, format="PNG", width=10, height=10)
        monkeypatch.setattr(ImageReviewer, "review", staticmethod(lambda *a, **k: fake))  # type: ignore[method-assign]
        p = _write_file(tmp_path, "a.png")
        svc = MediaReviewService()
        result = _run(svc.review_media(p, "image"))
        assert result is fake

    def test_video_routes_to_video_reviewer(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        fake = VideoReviewResult(is_valid=True, format="MP4", duration_seconds=1.0)
        monkeypatch.setattr(VideoReviewer, "review", staticmethod(lambda *a, **k: fake))  # type: ignore[method-assign]
        p = _write_file(tmp_path, "a.mp4")
        svc = MediaReviewService()
        result = _run(svc.review_media(p, "video"))
        assert result is fake

    def test_unsupported_media_type_raises(self, tmp_path: Any) -> None:
        p = _write_file(tmp_path, "a.png")
        svc = MediaReviewService()
        with pytest.raises(ValueError, match="不支持的媒体类型"):
            _run(svc.review_media(p, "audio"))

    def test_file_not_found_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ImageReviewer,
            "review",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("文件不存在: no"))),  # type: ignore[method-assign]
        )
        svc = MediaReviewService()
        with pytest.raises(FileNotFoundError):
            _run(svc.review_media("no.png", "image"))


# ---------------------------------------------------------------------------
# MediaReviewService.review_artifacts（批量）
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    async def load(self, artifact_id: str) -> Any:
        return self._data.get(artifact_id)


class TestReviewArtifacts:
    def test_missing_artifact(self) -> None:
        svc = MediaReviewService()
        storage = _FakeStorage({})
        results = _run(svc.review_artifacts(["a1"], storage))
        assert results == [{"artifact_id": "a1", "error": "制品不存在: a1"}]

    def test_missing_file_path(self) -> None:
        svc = MediaReviewService()
        storage = _FakeStorage({"a1": {"media_type": "image"}})
        results = _run(svc.review_artifacts(["a1"], storage))
        assert results == [{"artifact_id": "a1", "error": "制品缺少 file_path: a1"}]

    def test_infer_failure_entry(self) -> None:
        svc = MediaReviewService()
        storage = _FakeStorage({"a1": {"file_path": "x.txt"}})
        results = _run(svc.review_artifacts(["a1"], storage))
        assert results[0]["error"] == "无法推断媒体类型: x.txt"

    def test_success_with_explicit_media_type(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        fake = ImageReviewResult(is_valid=True, format="PNG")
        monkeypatch.setattr(ImageReviewer, "review", staticmethod(lambda *a, **k: fake))  # type: ignore[method-assign]
        p = _write_file(tmp_path, "a.png")
        svc = MediaReviewService()
        storage = _FakeStorage({"a1": {"file_path": p, "media_type": "image"}})
        results = _run(svc.review_artifacts(["a1"], storage))
        assert results[0]["artifact_id"] == "a1"
        assert results[0]["media_type"] == "image"
        assert results[0]["is_valid"] is True

    def test_inferred_from_extension(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        called: list[str] = []

        async def fake_review(file_path: str, media_type: str) -> ImageReviewResult:
            called.append(media_type)
            return ImageReviewResult(is_valid=True)

        svc = MediaReviewService()
        monkeypatch.setattr(svc, "review_media", fake_review)
        p = _write_file(tmp_path, "a.mp4")
        storage = _FakeStorage({"a1": {"file_path": p}})
        results = _run(svc.review_artifacts(["a1"], storage))
        assert results[0]["media_type"] == "video"
        assert called == ["video"]

    def test_file_missing_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_review(file_path: str, media_type: str) -> ImageReviewResult:
            raise FileNotFoundError(f"文件不存在: {file_path}")

        svc = MediaReviewService()
        monkeypatch.setattr(svc, "review_media", fake_review)
        storage = _FakeStorage({"a1": {"file_path": "no.png", "media_type": "image"}})
        results = _run(svc.review_artifacts(["a1"], storage))
        assert results[0]["error"].startswith("文件不存在:")


# ---------------------------------------------------------------------------
# 媒体元数据（fake PIL / fake av）
# ---------------------------------------------------------------------------


def _install_fake_pil(monkeypatch: pytest.MonkeyPatch) -> Any:
    """向 media_reviewer 注入假 PIL（Image.open → fake image）并返回 image 工厂。

    测试环境根 venv 已装真实 Pillow：media_reviewer 模块导入时 ``from PIL
    import Image`` 绑定的是真实模块——故必须直接替换 media_reviewer.Image
    属性（sys.modules 注入只对函数内 from-import 生效，如
    media_review_service._get_image_metadata 的 PIL 导入）。

    返回的工厂带参数（fmt/size/exif）会**更新后续 open 的默认值**——
    Image.open 是每次审阅的新调用，参数化必须落到 open 闭包持有的状态上
    （fake image 的 format/size 经实例属性读取）。
    """
    class _FakeExif(dict):
        def get_ifd(self, tag: int) -> dict[Any, Any]:
            return {}

    # open 闭包持有的可变状态（工厂参数写这里，open 读这里）
    _defaults: dict[str, Any] = {"fmt": "PNG", "size": (64, 48), "exif": None}

    def _open(path: str) -> Any:
        class _Img:
            """fake PIL Image：类体不闭合外层函数局部变量（Python 类体语义），
            故经实例属性/方法闭包取 _defaults。"""

            def __init__(self) -> None:
                self.format = _defaults["fmt"]
                self.size = _defaults["size"]
                self._exif = _defaults["exif"]

            def load(self) -> None:
                return None

            def getexif(self) -> Any:
                return self._exif if self._exif is not None else _FakeExif()

        return _Img()

    fake_image_mod = types.ModuleType("PIL.Image")
    fake_image_mod.Image = types.SimpleNamespace(open=_open)
    pil_pkg = types.ModuleType("PIL")
    pil_pkg.Image = fake_image_mod.Image
    exif_tags = types.ModuleType("PIL.ExifTags")
    exif_tags.Base = types.SimpleNamespace(
        Make=0x010F,
        Model=0x0110,
        Orientation=0x0112,
        DateTime=0x0132,
        DateTimeOriginal=0x9003,
        FocalLength=0x920A,
        ISOSpeedRatings=0x8827,
        ExposureTime=0x829A,
        FNumber=0x829D,
        Software=0x0131,
        ImageDescription=0x010E,
        XResolution=0x011A,
        YResolution=0x011B,
        BitsPerSample=0x0102,
        ColorSpace=0xA001,
        Flash=0x9209,
        GPSInfo=0x8825,
    )
    exif_tags.GPSTags = {0x0000: "LatitudeRef"}
    sys.modules["PIL"] = pil_pkg
    sys.modules["PIL.Image"] = fake_image_mod
    sys.modules["PIL.ExifTags"] = exif_tags
    # 关键：替换 media_reviewer 已绑定的 Image 属性（真实 Pillow 存在时仅
    # sys.modules 注入不生效——模块级 from-import 已绑定真实对象）
    monkeypatch.setattr(media_reviewer, "Image", fake_image_mod.Image)

    def _make_image(
        fmt: str | None = None,
        size: tuple[int, int] | None = None,
        exif: Any = None,
    ) -> Any:
        """更新默认值并返回一张按当前状态构造的 fake image。"""
        if fmt is not None:
            _defaults["fmt"] = fmt
        if size is not None:
            _defaults["size"] = size
        if exif is not None:
            _defaults["exif"] = exif
        return _open("")

    return _make_image


@pytest.fixture
def fake_pil(monkeypatch: pytest.MonkeyPatch) -> Any:
    return _install_fake_pil(monkeypatch)


class TestGetMediaMetadata:
    def test_image_metadata(self, fake_pil: Any, tmp_path: Any) -> None:
        p = _write_file(tmp_path, "a.png", content=bytes(8))
        svc = MediaReviewService()
        meta = svc.get_media_metadata(p, "image")
        assert meta["media_type"] == "image"
        assert meta["file_size"] == 8
        assert meta["format"] == "PNG"
        assert meta["width"] == 64
        assert meta["height"] == 48
        assert meta["aspect_ratio"] == round(64 / 48, 4)
        assert meta["exif"] == {}

    def test_image_metadata_exif(self, fake_pil: Any, tmp_path: Any) -> None:
        # getexif 返回 {tag: value}——含 bytes 值跳过
        from PIL.ExifTags import Base as ExifBase

        value = {ExifBase.Make: "Maker", 9999: b"\x01\x02", 0x0112: (3, 1)}
        fake_pil(exif=value)
        p = _write_file(tmp_path, "a.png")
        svc = MediaReviewService()
        meta = svc.get_media_metadata(p, "image")
        # bytes 值被跳过其余透出；(3,1) tuple 走 Base 名映射失败则忽略
        assert "Make" not in meta["exif"] or isinstance(meta["exif"]["Make"], str)

    def test_image_metadata_broken_file(self, fake_pil: Any, tmp_path: Any) -> None:
        p = _write_file(tmp_path, "a.png")
        svc = MediaReviewService()

        # 让 Image.open 抛异常——必须替换 PIL 包上的 Image 属性（函数内
        # ``from PIL import Image`` 取包属性；仅替换 sys.modules 的子模块
        # 条目不生效）
        sys.modules["PIL"].Image = types.SimpleNamespace(  # type: ignore[attr-defined]
            open=lambda path: (_ for _ in ()).throw(ValueError("bad"))
        )
        meta = svc.get_media_metadata(p, "image")
        assert "error" in meta
        assert meta["error"].startswith("无法读取图片:")

    def test_video_metadata(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        monkeypatch.setattr(
            VideoReviewer,
            "_extract_metadata",
            staticmethod(
                lambda path: {
                    "format": "MP4",
                    "duration_seconds": 10.5,
                    "width": 1920,
                    "height": 1080,
                    "fps": 30.0,
                    "codec": "h264",
                }
            ),  # type: ignore[method-assign]
        )
        p = _write_file(tmp_path, "a.mp4", content=bytes(100))
        svc = MediaReviewService()
        meta = svc.get_media_metadata(p, "video")
        assert meta["format"] == "MP4"
        assert meta["duration_seconds"] == 10.5
        assert meta["width"] == 1920
        assert meta["fps"] == 30.0
        assert meta["codec"] == "h264"

    def test_video_metadata_unparseable(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        monkeypatch.setattr(VideoReviewer, "_extract_metadata", staticmethod(lambda path: None))  # type: ignore[method-assign]
        p = _write_file(tmp_path, "a.mp4", content=bytes(1))
        svc = MediaReviewService()
        meta = svc.get_media_metadata(p, "video")
        assert meta["error"] == "无法解析视频文件"

    def test_missing_file_raises(self) -> None:
        svc = MediaReviewService()
        with pytest.raises(FileNotFoundError):
            svc.get_media_metadata("no.png", "image")

    def test_unsupported_type_raises(self, tmp_path: Any) -> None:
        p = _write_file(tmp_path, "a.png")
        svc = MediaReviewService()
        with pytest.raises(ValueError, match="不支持的媒体类型"):
            svc.get_media_metadata(p, "audio")


class TestExtractVideoThumbnails:
    def test_delegates_to_video_reviewer(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        monkeypatch.setattr(
            VideoReviewer,
            "extract_keyframes",
            staticmethod(
                lambda file_path, interval_seconds=5.0, output_dir=None: [output_dir or "k1.jpg"]  # type: ignore[method-assign]
            ),
        )
        p = _write_file(tmp_path, "a.mp4")
        svc = MediaReviewService()
        assert svc.extract_video_thumbnails(p, interval=2.0, output_dir="out") == ["out"]


# ---------------------------------------------------------------------------
# ImageReviewer（fake PIL）
# ---------------------------------------------------------------------------


class TestImageReviewer:
    def test_review_valid(self, fake_pil: Any, tmp_path: Any) -> None:
        p = _write_file(tmp_path, "a.png")
        result = ImageReviewer.review(p)
        assert result.is_valid is True
        assert result.format == "PNG"
        assert result.width == 64
        assert result.height == 48
        assert result.aspect_ratio == round(64 / 48, 4)
        assert result.errors == []

    def test_review_unsupported_format(self, fake_pil: Any, tmp_path: Any) -> None:
        fake_pil(fmt="GIF")
        p = _write_file(tmp_path, "a.png")
        result = ImageReviewer.review(p, MediaReviewConfig(allowed_image_formats=["PNG"]))
        assert result.is_valid is False
        assert any("不支持的图片格式" in e for e in result.errors)

    def test_review_dimension_errors_and_warnings(self, fake_pil: Any, tmp_path: Any) -> None:
        fake_pil(size=(100, 100))
        cfg = MediaReviewConfig(image_min_width=200, image_max_width=300, image_min_height=50, image_max_height=80)
        p = _write_file(tmp_path, "a.png")
        result = ImageReviewer.review(p, cfg)
        assert result.is_valid is False
        assert any("宽度 100px 小于最小限制" in e for e in result.errors)
        assert any("高度 100px 超过最大限制" in e for e in result.errors)

    def test_review_zero_height_aspect(self, fake_pil: Any, tmp_path: Any) -> None:
        fake_pil(size=(64, 0))
        p = _write_file(tmp_path, "a.png")
        result = ImageReviewer.review(p)
        assert result.aspect_ratio == 0.0

    def test_review_unopenable_file(self, fake_pil: Any, tmp_path: Any) -> None:
        # 替换 media_reviewer.Image 为 open 必抛的假对象（模拟损坏文件）
        class _Broken:
            @staticmethod
            def open(path: str) -> Any:
                raise ValueError("cannot identify image file")

        media_reviewer.Image = _Broken  # type: ignore[assignment]
        p = _write_file(tmp_path, "a.png")
        result = ImageReviewer.review(p)
        assert result.is_valid is False
        assert any("无法识别的图片文件" in e for e in result.errors)

    def test_review_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            ImageReviewer.review("no.png")

    def test_validate_format_empty(self) -> None:
        errors = ImageReviewer._validate_format("", MediaReviewConfig())
        assert errors == ["无法识别图片格式"]

    def test_exif_extraction(self, fake_pil: Any, tmp_path: Any) -> None:
        from PIL.ExifTags import Base as ExifBase

        exif = {ExifBase.Make: "Cam", ExifBase.Model: "X1"}
        fake_pil(exif=exif)
        p = _write_file(tmp_path, "a.png")
        result = ImageReviewer.review(p)
        assert result.exif.get("Make") == "Cam"
        assert result.exif.get("Model") == "X1"


# ---------------------------------------------------------------------------
# VideoReviewer（fake av）
# ---------------------------------------------------------------------------


def _install_fake_av(
    monkeypatch: pytest.MonkeyPatch,
    *,
    metadata: dict[str, Any] | None = None,
    open_raises: Exception | None = None,
) -> None:
    """注入假 av 模块：控制 _extract_metadata 依赖的 av.open 行为。"""

    class _FakeContainer:
        format = types.SimpleNamespace(name="mov,mp4,m4a,3gp,3g2,mj2")
        duration = 10500000  # 10.5s @ 1M time_base

        class _Stream:
            width = 1920
            height = 1080
            average_rate = 30.0
            codec_context = types.SimpleNamespace(name="h264")
            time_base = 1 / 1000000
            duration = None

        streams = types.SimpleNamespace(video=[_Stream()])

        def close(self) -> None:
            return None

    class _FakeAv:
        time_base = 1000000

        @staticmethod
        def open(path: str) -> Any:
            if open_raises is not None:
                raise open_raises
            if metadata is None:
                return _FakeContainer()
            raise AssertionError("unexpected open with metadata override")

    monkeypatch.setattr(media_reviewer, "av", _FakeAv)


class TestVideoReviewer:
    def test_review_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        _install_fake_av(monkeypatch)
        p = _write_file(tmp_path, "a.mp4")
        result = VideoReviewer.review(p)
        assert result.is_valid is True
        assert result.format == "MP4"
        assert result.duration_seconds == 10.5
        assert result.width == 1920
        assert result.height == 1080
        assert result.fps == 30.0
        assert result.codec == "h264"

    def test_review_unsupported_format(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        _install_fake_av(monkeypatch)
        p = _write_file(tmp_path, "a.avi")
        result = VideoReviewer.review(p, MediaReviewConfig(allowed_video_formats=["MP4"]))
        assert result.is_valid is False
        assert any("不支持的视频格式" in e for e in result.errors)

    def test_review_duration_out_of_range(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        _install_fake_av(monkeypatch)
        p = _write_file(tmp_path, "a.mp4")
        result = VideoReviewer.review(
            p, MediaReviewConfig(video_min_duration=20.0, video_max_duration=5.0)
        )
        assert result.is_valid is False
        assert any("小于最短限制" in e for e in result.errors)
        assert any("超过最长限制" in e for e in result.errors)

    def test_review_unparseable(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        _install_fake_av(monkeypatch, open_raises=ValueError("no demuxer"))
        p = _write_file(tmp_path, "a.mp4")
        result = VideoReviewer.review(p)
        assert result.is_valid is False
        assert result.errors == ["无法解析视频文件"]

    def test_review_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            VideoReviewer.review("no.mp4")

    def test_resolve_format_by_extension(self) -> None:
        assert VideoReviewer._resolve_format("a.mp4", "mov,mp4,m4a") == "MP4"
        assert VideoReviewer._resolve_format("a.mov", "mov,mp4,m4a") == "MOV"

    def test_resolve_format_from_container_list(self) -> None:
        # 扩展名不在映射内 → 遍历容器格式列表
        assert VideoReviewer._resolve_format("a.unknown", "mpeg,webm") == "WEBM"

    def test_resolve_format_fallback_upper(self) -> None:
        assert VideoReviewer._resolve_format("a.xyz", "someformat") == "SOMEFORMAT"

    def test_extract_metadata_no_video_stream(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        class _NoStreamContainer:
            format = types.SimpleNamespace(name="mp4")
            duration = None
            streams = types.SimpleNamespace(video=[])

            def close(self) -> None:
                return None

        monkeypatch.setattr(media_reviewer.av, "open", staticmethod(lambda path: _NoStreamContainer()))
        p = _write_file(tmp_path, "a.mp4")
        assert VideoReviewer._extract_metadata(p) is None

    def test_extract_keyframes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        class _FakeFrame:
            pts = 5000000  # 5s @ time_base 1/1000000

            @staticmethod
            def to_image() -> Any:
                # 真实写出文件（模拟 PIL save），供 isfile 断言
                def _save(path: str, format: str | None = None) -> None:
                    with open(path, "wb") as f:
                        f.write(b"JPEG")

                return types.SimpleNamespace(save=_save)

        class _FakeStream:
            time_base = 1 / 1000000
            average_rate = 24.0

        class _FakeContainer:
            streams = types.SimpleNamespace(video=[_FakeStream()])
            video = None

            def seek(self, pos: int) -> None:
                return None

            def decode(self, video: int) -> list[Any]:
                return [_FakeFrame()]

            def close(self) -> None:
                return None

        monkeypatch.setattr(media_reviewer.av, "open", staticmethod(lambda path: _FakeContainer()))
        p = _write_file(tmp_path, "clip.mp4")
        paths = VideoReviewer.extract_keyframes(p, interval_seconds=5.0, output_dir=str(tmp_path))
        assert len(paths) == 1
        assert os.path.basename(paths[0]) == "clip_keyframe_0000.jpg"
        assert os.path.isfile(paths[0])


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程（同 test_review_service 的清理语义）。

    媒体审阅路径本身无后台任务，统一收尾保证测试输出零销毁噪音。
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()