"""媒体审阅模块测试。

覆盖 ImageReviewer 和 VideoReviewer 的核心功能：
- 图片格式验证、尺寸检查、EXIF 提取
- 视频格式验证、时长检查、元数据提取、关键帧提取
- 边界场景与异常处理
"""

from __future__ import annotations

import os
from pathlib import Path

import av
import pytest
from PIL import Image

from review.media_reviewer import ImageReviewer, VideoReviewer
from review.models import (
    ImageReviewResult,
    MediaReviewConfig,
    VideoReviewResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path: Path) -> str:
    """返回临时目录的字符串路径。"""
    return str(tmp_path)


@pytest.fixture
def sample_jpeg(tmp_dir: str) -> str:
    """创建一张简单的 JPEG 测试图片。"""
    path = os.path.join(tmp_dir, "test.jpg")
    img = Image.new("RGB", (640, 480), color="red")
    img.save(path, format="JPEG")
    return path


@pytest.fixture
def sample_png(tmp_dir: str) -> str:
    """创建一张 PNG 测试图片。"""
    path = os.path.join(tmp_dir, "test.png")
    img = Image.new("RGBA", (800, 600), color="blue")
    img.save(path, format="PNG")
    return path


@pytest.fixture
def sample_gif(tmp_dir: str) -> str:
    """创建一张 GIF 测试图片。"""
    path = os.path.join(tmp_dir, "test.gif")
    img = Image.new("P", (100, 100), color=0)
    img.save(path, format="GIF")
    return path


@pytest.fixture
def sample_webp(tmp_dir: str) -> str:
    """创建一张 WebP 测试图片。"""
    path = os.path.join(tmp_dir, "test.webp")
    img = Image.new("RGB", (320, 240), color="green")
    img.save(path, format="WEBP")
    return path


@pytest.fixture
def sample_mp4(tmp_dir: str) -> str:
    """使用 PyAV 创建一个极短的 MP4 测试视频。"""
    path = os.path.join(tmp_dir, "test.mp4")
    _create_test_video(path, width=320, height=240, duration=1.0, fps=24)
    return path


@pytest.fixture
def sample_avi(tmp_dir: str) -> str:
    """创建一个 AVI 测试视频。"""
    path = os.path.join(tmp_dir, "test.avi")
    _create_test_video(path, width=160, height=120, duration=0.5, fps=10, codec_name="mpeg4")
    return path


@pytest.fixture
def default_config() -> MediaReviewConfig:
    """返回默认审阅配置。"""
    return MediaReviewConfig()


@pytest.fixture
def strict_image_config() -> MediaReviewConfig:
    """返回严格的图片审阅配置。"""
    return MediaReviewConfig(
        image_min_width=100,
        image_max_width=1920,
        image_min_height=100,
        image_max_height=1080,
        allowed_image_formats=["JPEG", "PNG"],
    )


@pytest.fixture
def strict_video_config() -> MediaReviewConfig:
    """返回严格的视频审阅配置。"""
    return MediaReviewConfig(
        video_min_duration=1.0,
        video_max_duration=60.0,
        allowed_video_formats=["MP4"],
    )


def _create_test_video(
    path: str,
    width: int = 320,
    height: int = 240,
    duration: float = 1.0,
    fps: int = 24,
    codec_name: str = "mpeg4",
) -> None:
    """使用 PyAV 辅助创建测试视频文件。"""
    container = av.open(path, mode="w")
    stream = container.add_stream(codec_name, rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"

    total_frames = int(duration * fps)
    for i in range(total_frames):
        img = Image.new("RGB", (width, height), color=(i % 256, 128, 64))
        frame = av.VideoFrame.from_image(img)
        for packet in stream.encode(frame):
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)

    container.close()


# ===========================================================================
# ImageReviewer 测试
# ===========================================================================


class TestImageReviewerFormatValidation:
    """图片格式验证测试。"""

    def test_jpeg_format_valid(self, sample_jpeg: str, default_config: MediaReviewConfig) -> None:
        result = ImageReviewer.review(sample_jpeg, default_config)
        assert result.is_valid is True
        assert result.format == "JPEG"

    def test_png_format_valid(self, sample_png: str, default_config: MediaReviewConfig) -> None:
        result = ImageReviewer.review(sample_png, default_config)
        assert result.is_valid is True
        assert result.format == "PNG"

    def test_gif_format_valid(self, sample_gif: str, default_config: MediaReviewConfig) -> None:
        result = ImageReviewer.review(sample_gif, default_config)
        assert result.is_valid is True
        assert result.format == "GIF"

    def test_webp_format_valid(self, sample_webp: str, default_config: MediaReviewConfig) -> None:
        result = ImageReviewer.review(sample_webp, default_config)
        assert result.is_valid is True
        assert result.format == "WEBP"

    def test_disallowed_format_reports_error(
        self, sample_gif: str, strict_image_config: MediaReviewConfig
    ) -> None:
        """GIF 不在严格配置的允许列表中，应报错。"""
        result = ImageReviewer.review(sample_gif, strict_image_config)
        assert result.is_valid is False
        assert any("格式" in e or "format" in e.lower() for e in result.errors)

    def test_file_not_found_raises_error(self, default_config: MediaReviewConfig) -> None:
        with pytest.raises(FileNotFoundError):
            ImageReviewer.review("/nonexistent/path.jpg", default_config)

    def test_non_image_file_reports_error(
        self, tmp_dir: str, default_config: MediaReviewConfig
    ) -> None:
        """非图片文件应报告错误。"""
        txt_path = os.path.join(tmp_dir, "not_image.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("hello")
        result = ImageReviewer.review(txt_path, default_config)
        assert result.is_valid is False
        assert len(result.errors) > 0


class TestImageReviewerDimensions:
    """图片尺寸检查测试。"""

    def test_dimensions_populated(self, sample_jpeg: str, default_config: MediaReviewConfig) -> None:
        result = ImageReviewer.review(sample_jpeg, default_config)
        assert result.width == 640
        assert result.height == 480

    def test_aspect_ratio(self, sample_jpeg: str, default_config: MediaReviewConfig) -> None:
        result = ImageReviewer.review(sample_jpeg, default_config)
        assert abs(result.aspect_ratio - (640 / 480)) < 0.01

    def test_width_too_small(
        self, tmp_dir: str, strict_image_config: MediaReviewConfig
    ) -> None:
        """宽 50 < min 100，应报错。"""
        path = os.path.join(tmp_dir, "small.jpg")
        img = Image.new("RGB", (50, 200))
        img.save(path, format="JPEG")
        result = ImageReviewer.review(path, strict_image_config)
        assert result.is_valid is False

    def test_height_too_large(
        self, tmp_dir: str, strict_image_config: MediaReviewConfig
    ) -> None:
        """高 2000 > max 1080，应报错。"""
        path = os.path.join(tmp_dir, "tall.jpg")
        img = Image.new("RGB", (200, 2000))
        img.save(path, format="JPEG")
        result = ImageReviewer.review(path, strict_image_config)
        assert result.is_valid is False


class TestImageReviewerExif:
    """EXIF 提取测试。"""

    def test_exif_returns_dict(self, sample_jpeg: str, default_config: MediaReviewConfig) -> None:
        result = ImageReviewer.review(sample_jpeg, default_config)
        assert isinstance(result.exif, dict)

    def test_exif_with_embedded_data(
        self, tmp_dir: str, default_config: MediaReviewConfig
    ) -> None:
        """嵌入 EXIF 数据后应能提取关键字段。"""
        path = os.path.join(tmp_dir, "exif.jpg")
        img = Image.new("RGB", (100, 100))
        from PIL.ExifTags import Base as ExifBase

        exif_data = img.getexif()
        # 设置相机型号 (Make)
        exif_data[ExifBase.Make] = "TestCamera"
        exif_data[ExifBase.Model] = "TestModel"
        img.save(path, format="JPEG", exif=exif_data)

        result = ImageReviewer.review(path, default_config)
        assert result.exif.get("Make") == "TestCamera"
        assert result.exif.get("Model") == "TestModel"


class TestImageReviewerNoConfig:
    """不传配置时使用默认值。"""

    def test_review_without_config(self, sample_jpeg: str) -> None:
        result = ImageReviewer.review(sample_jpeg)
        assert result.is_valid is True
        assert result.format == "JPEG"

    def test_review_with_none_config(self, sample_png: str) -> None:
        result = ImageReviewer.review(sample_png, config=None)
        assert result.is_valid is True
        assert result.format == "PNG"


# ===========================================================================
# VideoReviewer 测试
# ===========================================================================


class TestVideoReviewerFormatValidation:
    """视频格式验证测试。"""

    def test_mp4_format_valid(self, sample_mp4: str, default_config: MediaReviewConfig) -> None:
        result = VideoReviewer.review(sample_mp4, default_config)
        assert result.is_valid is True
        assert result.format == "MP4"

    def test_avi_format_valid(self, sample_avi: str, default_config: MediaReviewConfig) -> None:
        result = VideoReviewer.review(sample_avi, default_config)
        assert result.is_valid is True
        assert result.format == "AVI"

    def test_disallowed_format_reports_error(
        self, sample_avi: str, strict_video_config: MediaReviewConfig
    ) -> None:
        """AVI 不在严格配置允许的 MP4 列表中。"""
        result = VideoReviewer.review(sample_avi, strict_video_config)
        assert result.is_valid is False
        assert any("格式" in e or "format" in e.lower() for e in result.errors)

    def test_file_not_found_raises_error(self, default_config: MediaReviewConfig) -> None:
        with pytest.raises(FileNotFoundError):
            VideoReviewer.review("/nonexistent/video.mp4", default_config)

    def test_non_video_file_reports_error(
        self, tmp_dir: str, default_config: MediaReviewConfig
    ) -> None:
        txt_path = os.path.join(tmp_dir, "not_video.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("hello")
        result = VideoReviewer.review(txt_path, default_config)
        assert result.is_valid is False
        assert len(result.errors) > 0


class TestVideoReviewerMetadata:
    """视频元数据提取测试。"""

    def test_duration_populated(self, sample_mp4: str, default_config: MediaReviewConfig) -> None:
        result = VideoReviewer.review(sample_mp4, default_config)
        assert result.duration_seconds > 0

    def test_dimensions_populated(self, sample_mp4: str, default_config: MediaReviewConfig) -> None:
        result = VideoReviewer.review(sample_mp4, default_config)
        assert result.width == 320
        assert result.height == 240

    def test_fps_populated(self, sample_mp4: str, default_config: MediaReviewConfig) -> None:
        result = VideoReviewer.review(sample_mp4, default_config)
        assert result.fps > 0

    def test_codec_populated(self, sample_mp4: str, default_config: MediaReviewConfig) -> None:
        result = VideoReviewer.review(sample_mp4, default_config)
        assert len(result.codec) > 0


class TestVideoReviewerDuration:
    """视频时长检查测试。"""

    def test_duration_too_short(
        self, tmp_dir: str, strict_video_config: MediaReviewConfig
    ) -> None:
        """0.5 秒视频 < 最短 1.0 秒，应报错。"""
        path = os.path.join(tmp_dir, "short.mp4")
        _create_test_video(path, duration=0.5, fps=24)
        result = VideoReviewer.review(path, strict_video_config)
        assert result.is_valid is False

    def test_duration_within_range(self, sample_mp4: str, strict_video_config: MediaReviewConfig) -> None:
        result = VideoReviewer.review(sample_mp4, strict_video_config)
        assert result.is_valid is True


class TestVideoReviewerNoConfig:
    """不传配置时使用默认值。"""

    def test_review_without_config(self, sample_mp4: str) -> None:
        result = VideoReviewer.review(sample_mp4)
        assert result.is_valid is True
        assert result.format == "MP4"


class TestVideoReviewerKeyframes:
    """关键帧提取测试。"""

    def test_extract_keyframes_returns_paths(
        self, sample_mp4: str, tmp_dir: str
    ) -> None:
        output_dir = os.path.join(tmp_dir, "keyframes")
        os.makedirs(output_dir, exist_ok=True)
        paths = VideoReviewer.extract_keyframes(
            sample_mp4, interval_seconds=0.5, output_dir=output_dir
        )
        assert isinstance(paths, list)
        for p in paths:
            assert os.path.isfile(p)
            assert p.endswith(".jpg")

    def test_extract_keyframes_default_output_dir(
        self, sample_mp4: str, tmp_dir: str
    ) -> None:
        """不指定 output_dir 时，应在视频同目录下生成关键帧。"""
        paths = VideoReviewer.extract_keyframes(
            sample_mp4, interval_seconds=0.5
        )
        assert isinstance(paths, list)
        for p in paths:
            assert os.path.isfile(p)

    def test_extract_keyframes_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            VideoReviewer.extract_keyframes("/nonexistent/video.mp4")


# ===========================================================================
# MediaReviewConfig 默认值测试
# ===========================================================================


class TestMediaReviewConfigDefaults:
    """配置默认值测试。"""

    def test_default_image_formats(self) -> None:
        cfg = MediaReviewConfig()
        assert "JPEG" in cfg.allowed_image_formats
        assert "PNG" in cfg.allowed_image_formats
        assert len(cfg.allowed_image_formats) == 6

    def test_default_video_formats(self) -> None:
        cfg = MediaReviewConfig()
        assert "MP4" in cfg.allowed_video_formats
        assert "AVI" in cfg.allowed_video_formats
        assert len(cfg.allowed_video_formats) == 5

    def test_default_dimensions(self) -> None:
        cfg = MediaReviewConfig()
        assert cfg.image_min_width == 1
        assert cfg.image_max_width == 7680
        assert cfg.video_max_duration == 3600.0


# ===========================================================================
# 数据模型序列化测试
# ===========================================================================


class TestResultSerialization:
    """结果序列化测试。"""

    def test_image_result_to_dict(self) -> None:
        result = ImageReviewResult(
            is_valid=True,
            format="JPEG",
            width=800,
            height=600,
            aspect_ratio=1.33,
        )
        d = result.to_dict()
        assert d["is_valid"] is True
        assert d["format"] == "JPEG"
        assert d["width"] == 800
        assert d["warnings"] == []

    def test_video_result_to_dict(self) -> None:
        result = VideoReviewResult(
            is_valid=False,
            format="MP4",
            duration_seconds=10.5,
            errors=["时长超出限制"],
        )
        d = result.to_dict()
        assert d["is_valid"] is False
        assert d["duration_seconds"] == 10.5
        assert "时长超出限制" in d["errors"]
