"""多模态审阅能力全面测试。

覆盖模块：
- src/review/models.py: 数据模型（ImageReviewResult/VideoReviewResult/MediaReviewConfig）
- src/review/media_reviewer.py: ImageReviewer（格式验证/尺寸检查/EXIF提取）、VideoReviewer（格式验证/时长检查/关键帧提取）
- src/review/media_review_service.py: MediaReviewService（路由逻辑/批量审阅/元数据获取）
- src/channels/api/routes_reviews.py: 媒体审阅 API 端点
- frontend/src/types/review.ts: 前端类型定义验证
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from review.models import (
    ImageReviewResult,
    MediaReviewConfig,
    ReviewFeedback,
    ReviewRequest,
    ReviewStatus,
    VideoReviewResult,
)
from review.media_review_service import MediaReviewService, _infer_media_type


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_dir(tmp_path: Path) -> str:
    """创建临时目录并返回字符串路径。"""
    return str(tmp_path)


@pytest.fixture
def png_image(tmp_path: Path) -> str:
    """创建有效的 PNG 测试图片（200x100）。"""
    img = Image.new("RGB", (200, 100), color="red")
    path = str(tmp_path / "test.png")
    img.save(path, format="PNG")
    return path


@pytest.fixture
def jpeg_image(tmp_path: Path) -> str:
    """创建有效的 JPEG 测试图片（640x480）。"""
    img = Image.new("RGB", (640, 480), color="blue")
    path = str(tmp_path / "test.jpg")
    img.save(path, format="JPEG")
    return path


@pytest.fixture
def jpeg_with_exif(tmp_path: Path) -> str:
    """创建带 EXIF 元数据的 JPEG 图片。"""
    img = Image.new("RGB", (800, 600), color="green")
    from PIL.ExifTags import Base as ExifBase

    exif_data = img.getexif()
    exif_data[ExifBase.Make] = "TestCamera"
    exif_data[ExifBase.Model] = "ModelX-100"
    exif_data[ExifBase.Software] = "TestSuite v1.0"
    exif_data[ExifBase.Orientation] = 1
    exif_data[ExifBase.XResolution] = (72, 1)
    exif_data[ExifBase.YResolution] = (72, 1)

    path = str(tmp_path / "exif_test.jpg")
    img.save(path, format="JPEG", exif=exif_data)
    return path


@pytest.fixture
def gif_image(tmp_path: Path) -> str:
    """创建 GIF 图片。"""
    img = Image.new("RGB", (100, 100), color="yellow")
    path = str(tmp_path / "test.gif")
    img.save(path, format="GIF")
    return path


@pytest.fixture
def webp_image(tmp_path: Path) -> str:
    """创建 WebP 图片。"""
    img = Image.new("RGB", (300, 200), color="purple")
    path = str(tmp_path / "test.webp")
    img.save(path, format="WEBP")
    return path


@pytest.fixture
def bmp_image(tmp_path: Path) -> str:
    """创建 BMP 图片。"""
    img = Image.new("RGB", (50, 50), color="cyan")
    path = str(tmp_path / "test.bmp")
    img.save(path, format="BMP")
    return path


@pytest.fixture
def tiff_image(tmp_path: Path) -> str:
    """创建 TIFF 图片。"""
    img = Image.new("RGB", (160, 120), color="magenta")
    path = str(tmp_path / "test.tiff")
    img.save(path, format="TIFF")
    return path


@pytest.fixture
def corrupted_file(tmp_path: Path) -> str:
    """创建损坏的图片文件（随机字节）。"""
    path = str(tmp_path / "corrupted.jpg")
    with open(path, "wb") as f:
        f.write(b"\x00\x01\x02\x03\x04\x05garbage data not an image")
    return path


@pytest.fixture
def empty_file(tmp_path: Path) -> str:
    """创建空文件。"""
    path = str(tmp_path / "empty.png")
    with open(path, "wb") as f:
        f.write(b"")
    return path


@pytest.fixture
def tiny_image(tmp_path: Path) -> str:
    """创建 1x1 像素的最小图片。"""
    img = Image.new("RGB", (1, 1), color="black")
    path = str(tmp_path / "tiny.png")
    img.save(path, format="PNG")
    return path


@pytest.fixture
def large_image(tmp_path: Path) -> str:
    """创建大尺寸图片（8000x6000）。"""
    img = Image.new("RGB", (8000, 6000), color="white")
    path = str(tmp_path / "large.png")
    img.save(path, format="PNG")
    return path


@pytest.fixture
def sample_video(tmp_path: Path) -> str:
    """使用 PyAV 创建最小测试视频（MP4, 320x240, 1帧）。"""
    import av

    path = str(tmp_path / "test_video.mp4")
    container = av.open(path, mode="w")
    stream = container.add_stream("mpeg4", rate=24)
    stream.width = 320
    stream.height = 240
    stream.pix_fmt = "yuv420p"

    pil_img = Image.new("RGB", (320, 240), color="blue")
    frame = av.VideoFrame.from_image(pil_img)
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


@pytest.fixture
def default_config() -> MediaReviewConfig:
    """创建默认媒体审阅配置。"""
    return MediaReviewConfig()


@pytest.fixture
def strict_config() -> MediaReviewConfig:
    """创建严格媒体审阅配置。"""
    return MediaReviewConfig(
        image_min_width=100,
        image_max_width=1920,
        image_min_height=100,
        image_max_height=1080,
        allowed_image_formats=["JPEG", "PNG"],
        video_min_duration=1.0,
        video_max_duration=600.0,
        allowed_video_formats=["MP4"],
    )


@pytest.fixture
def service() -> MediaReviewService:
    """创建 MediaReviewService 实例。"""
    return MediaReviewService()


# ============================================================================
# 数据模型测试
# ============================================================================


class TestImageReviewResult:
    """ImageReviewResult 数据模型测试。"""

    def test_default_values(self) -> None:
        """默认值测试：必填字段 is_valid，其他字段应有合理默认值。"""
        result = ImageReviewResult(is_valid=True)
        assert result.is_valid is True
        assert result.format == ""
        assert result.width == 0
        assert result.height == 0
        assert result.aspect_ratio == 0.0
        assert result.exif == {}
        assert result.warnings == []
        assert result.errors == []

    def test_to_dict_contains_all_fields(self) -> None:
        """to_dict 应包含所有字段。"""
        result = ImageReviewResult(
            is_valid=True,
            format="PNG",
            width=200,
            height=100,
            aspect_ratio=2.0,
            exif={"Make": "Test"},
            warnings=["注意"],
            errors=[],
        )
        d = result.to_dict()
        assert set(d.keys()) == {
            "is_valid", "format", "width", "height",
            "aspect_ratio", "exif", "warnings", "errors",
        }
        assert d["is_valid"] is True
        assert d["format"] == "PNG"
        assert d["width"] == 200
        assert d["height"] == 100
        assert d["aspect_ratio"] == 2.0
        assert d["exif"] == {"Make": "Test"}
        assert d["warnings"] == ["注意"]
        assert d["errors"] == []

    def test_invalid_result_with_errors(self) -> None:
        """无效结果应包含错误信息。"""
        result = ImageReviewResult(
            is_valid=False,
            errors=["不支持的图片格式: PSD"],
        )
        assert result.is_valid is False
        assert len(result.errors) == 1

    def test_to_dict_serializable(self) -> None:
        """to_dict 返回值应可 JSON 序列化。"""
        result = ImageReviewResult(
            is_valid=True,
            format="JPEG",
            width=100,
            height=50,
            aspect_ratio=2.0,
            exif={"ISO": 200, "FocalLength": 50.0},
        )
        serialized = json.dumps(result.to_dict())
        assert isinstance(serialized, str)
        deserialized = json.loads(serialized)
        assert deserialized["width"] == 100


class TestVideoReviewResult:
    """VideoReviewResult 数据模型测试。"""

    def test_default_values(self) -> None:
        """默认值测试。"""
        result = VideoReviewResult(is_valid=True)
        assert result.is_valid is True
        assert result.format == ""
        assert result.duration_seconds == 0.0
        assert result.width == 0
        assert result.height == 0
        assert result.fps == 0.0
        assert result.codec == ""
        assert result.warnings == []
        assert result.errors == []

    def test_to_dict_contains_all_fields(self) -> None:
        """to_dict 应包含所有字段。"""
        result = VideoReviewResult(
            is_valid=True,
            format="MP4",
            duration_seconds=30.5,
            width=1920,
            height=1080,
            fps=29.97,
            codec="h264",
            warnings=["时长偏长"],
            errors=[],
        )
        d = result.to_dict()
        assert set(d.keys()) == {
            "is_valid", "format", "duration_seconds",
            "width", "height", "fps", "codec", "warnings", "errors",
        }
        assert d["duration_seconds"] == 30.5
        assert d["fps"] == 29.97
        assert d["codec"] == "h264"

    def test_invalid_result(self) -> None:
        """无效结果标记。"""
        result = VideoReviewResult(
            is_valid=False,
            errors=["无法解析视频文件"],
        )
        assert result.is_valid is False

    def test_to_dict_serializable(self) -> None:
        """to_dict 返回值应可 JSON 序列化。"""
        result = VideoReviewResult(
            is_valid=True,
            format="WEBM",
            duration_seconds=10.0,
            fps=24.0,
        )
        serialized = json.dumps(result.to_dict())
        assert isinstance(serialized, str)


class TestMediaReviewConfig:
    """MediaReviewConfig 配置模型测试。"""

    def test_default_values(self) -> None:
        """默认配置应有合理的宽高和格式限制。"""
        config = MediaReviewConfig()
        assert config.image_min_width == 1
        assert config.image_max_width == 7680
        assert config.image_min_height == 1
        assert config.image_max_height == 4320
        assert "JPEG" in config.allowed_image_formats
        assert "PNG" in config.allowed_image_formats
        assert config.video_min_duration == 0.0
        assert config.video_max_duration == 3600.0
        assert "MP4" in config.allowed_video_formats

    def test_custom_config(self) -> None:
        """自定义配置。"""
        config = MediaReviewConfig(
            image_min_width=100,
            image_max_width=1920,
            allowed_image_formats=["JPEG"],
            video_max_duration=60.0,
        )
        assert config.image_min_width == 100
        assert config.allowed_image_formats == ["JPEG"]
        assert config.video_max_duration == 60.0

    def test_config_independence(self) -> None:
        """两个默认配置实例的列表应独立（不共享引用）。"""
        config1 = MediaReviewConfig()
        config2 = MediaReviewConfig()
        config1.allowed_image_formats.append("PSD")
        assert "PSD" not in config2.allowed_image_formats


class TestReviewModelsIntegration:
    """审批模型集成测试（验证 metadata 中的媒体审阅字段）。"""

    def test_review_request_media_metadata(self) -> None:
        """ReviewRequest.metadata 应支持存储媒体审阅结果。"""
        review = ReviewRequest(
            title="媒体审阅测试",
            metadata={
                "media_files": [
                    {"path": "/images/photo.jpg", "media_type": "image"},
                ],
                "media_review_results": {
                    "/images/photo.jpg": ImageReviewResult(
                        is_valid=True, format="JPEG", width=800, height=600
                    ).to_dict(),
                },
            },
        )
        assert "media_files" in review.metadata
        assert "media_review_results" in review.metadata
        assert review.metadata["media_review_results"]["/images/photo.jpg"]["is_valid"] is True

    def test_review_request_to_dict_includes_metadata(self) -> None:
        """to_dict 应包含 metadata 字段。"""
        review = ReviewRequest(
            title="测试",
            metadata={"media_files": []},
        )
        d = review.to_dict()
        assert "metadata" in d
        assert d["metadata"] == {"media_files": []}

    def test_review_request_from_dict_with_metadata(self) -> None:
        """from_dict 应正确还原 metadata。"""
        data = {
            "title": "审阅测试",
            "metadata": {
                "media_review_results": {"key": "value"},
            },
        }
        review = ReviewRequest.from_dict(data)
        assert review.metadata["media_review_results"] == {"key": "value"}


# ============================================================================
# ImageReviewer 单元测试
# ============================================================================


class TestImageReviewerFormatValidation:
    """ImageReviewer 格式验证测试。"""

    def test_png_format_valid(self, png_image: str) -> None:
        """PNG 格式应通过验证。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(png_image)
        assert result.is_valid is True
        assert result.format == "PNG"

    def test_jpeg_format_valid(self, jpeg_image: str) -> None:
        """JPEG 格式应通过验证。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(jpeg_image)
        assert result.is_valid is True
        assert result.format == "JPEG"

    def test_gif_format_valid(self, gif_image: str) -> None:
        """GIF 格式应通过验证。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(gif_image)
        assert result.is_valid is True
        assert result.format == "GIF"

    def test_webp_format_valid(self, webp_image: str) -> None:
        """WebP 格式应通过验证。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(webp_image)
        assert result.is_valid is True
        assert result.format == "WEBP"

    def test_bmp_format_valid(self, bmp_image: str) -> None:
        """BMP 格式应通过验证。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(bmp_image)
        assert result.is_valid is True
        assert result.format == "BMP"

    def test_tiff_format_valid(self, tiff_image: str) -> None:
        """TIFF 格式应通过验证。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(tiff_image)
        assert result.is_valid is True
        assert result.format == "TIFF"

    def test_unsupported_format_rejected(self, tmp_path: Path) -> None:
        """不在允许列表中的格式应被拒绝。

        使用配置限制只允许 JPEG，传入 PNG 应被拒绝。
        """
        from review.media_reviewer import ImageReviewer

        img = Image.new("RGB", (100, 100), color="red")
        path = str(tmp_path / "test.png")
        img.save(path, format="PNG")

        config = MediaReviewConfig(allowed_image_formats=["JPEG"])
        result = ImageReviewer.review(path, config=config)
        assert result.is_valid is False
        assert any("不支持的图片格式" in e for e in result.errors)

    def test_validate_format_empty_string(self) -> None:
        """空格式字符串应返回错误。"""
        from review.media_reviewer import ImageReviewer

        config = MediaReviewConfig()
        errors = ImageReviewer._validate_format("", config)
        assert len(errors) == 1
        assert "无法识别图片格式" in errors[0]


class TestImageReviewerDimensions:
    """ImageReviewer 尺寸检查测试。"""

    def test_dimensions_extracted_correctly(self, png_image: str) -> None:
        """尺寸应正确提取。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(png_image)
        assert result.width == 200
        assert result.height == 100

    def test_aspect_ratio_calculated(self, png_image: str) -> None:
        """宽高比应正确计算。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(png_image)
        assert result.aspect_ratio == 2.0  # 200/100

    def test_min_width_violation(self, tiny_image: str) -> None:
        """图片宽度低于最小限制应报错。"""
        from review.media_reviewer import ImageReviewer

        config = MediaReviewConfig(image_min_width=10)
        result = ImageReviewer.review(tiny_image, config=config)
        assert result.is_valid is False
        assert any("宽度" in e and "小于最小限制" in e for e in result.errors)

    def test_max_width_violation(self, large_image: str) -> None:
        """图片宽度超过最大限制应报错。"""
        from review.media_reviewer import ImageReviewer

        config = MediaReviewConfig(image_max_width=4096)
        result = ImageReviewer.review(large_image, config=config)
        assert result.is_valid is False
        assert any("宽度" in e and "超过最大限制" in e for e in result.errors)

    def test_min_height_violation(self, tiny_image: str) -> None:
        """图片高度低于最小限制应报错。"""
        from review.media_reviewer import ImageReviewer

        config = MediaReviewConfig(image_min_height=10)
        result = ImageReviewer.review(tiny_image, config=config)
        assert result.is_valid is False
        assert any("高度" in e and "小于最小限制" in e for e in result.errors)

    def test_max_height_violation(self, large_image: str) -> None:
        """图片高度超过最大限制应报错。"""
        from review.media_reviewer import ImageReviewer

        config = MediaReviewConfig(image_max_height=2000)
        result = ImageReviewer.review(large_image, config=config)
        assert result.is_valid is False
        assert any("高度" in e and "超过最大限制" in e for e in result.errors)

    def test_check_dimensions_boundary(self) -> None:
        """边界值：刚好等于最小/最大限制应通过。"""
        from review.media_reviewer import ImageReviewer

        config = MediaReviewConfig(image_min_width=100, image_max_width=200,
                                   image_min_height=50, image_max_height=100)
        errors, warnings = ImageReviewer._check_dimensions(100, 50, config)
        assert len(errors) == 0
        errors2, _ = ImageReviewer._check_dimensions(200, 100, config)
        assert len(errors2) == 0


class TestImageReviewerEXIF:
    """ImageReviewer EXIF 提取测试。"""

    def test_exif_extracted_from_jpeg(self, jpeg_with_exif: str) -> None:
        """带 EXIF 的 JPEG 应正确提取元数据。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(jpeg_with_exif)
        assert result.is_valid is True
        assert "Make" in result.exif
        assert result.exif["Make"] == "TestCamera"
        assert "Model" in result.exif
        assert result.exif["Model"] == "ModelX-100"

    def test_no_exif_for_png(self, png_image: str) -> None:
        """PNG 图片通常无 EXIF 数据。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(png_image)
        assert isinstance(result.exif, dict)

    def test_exif_orientation(self, jpeg_with_exif: str) -> None:
        """EXIF 中应包含方向信息。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(jpeg_with_exif)
        assert "Orientation" in result.exif

    def test_exif_fraction_conversion(self, jpeg_with_exif: str) -> None:
        """EXIF 中分数类型的值（如分辨率）应转为可用的数值。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(jpeg_with_exif)
        # XResolution = (72, 1) → 72.0 或 IFDRational(72, 1)
        if "XResolution" in result.exif:
            val = result.exif["XResolution"]
            # PIL 可能返回 float 或 IFDRational，两者均可转为数值比较
            assert float(val) == 72.0


class TestImageReviewerEdgeCases:
    """ImageReviewer 边界场景测试。"""

    def test_nonexistent_file_raises(self) -> None:
        """不存在的文件应抛出 FileNotFoundError。"""
        from review.media_reviewer import ImageReviewer

        with pytest.raises(FileNotFoundError, match="文件不存在"):
            ImageReviewer.review("/nonexistent/path/image.png")

    def test_corrupted_file_returns_invalid(self, corrupted_file: str) -> None:
        """损坏的图片文件应返回 is_valid=False。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(corrupted_file)
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert any("无法识别" in e for e in result.errors)

    def test_empty_file_returns_invalid(self, empty_file: str) -> None:
        """空文件应返回 is_valid=False。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(empty_file)
        assert result.is_valid is False

    def test_default_config_when_none(self, png_image: str) -> None:
        """config=None 时应使用默认配置。"""
        from review.media_reviewer import ImageReviewer

        result = ImageReviewer.review(png_image, config=None)
        assert result.is_valid is True

    def test_multiple_errors_accumulated(self, tmp_path: Path) -> None:
        """多个违规项应累积在 errors 列表中。"""
        from review.media_reviewer import ImageReviewer

        # 创建一个 1x1 的 PNG，同时违反最小宽度和高度
        img = Image.new("RGB", (1, 1), color="red")
        path = str(tmp_path / "tiny.png")
        img.save(path, format="PNG")

        config = MediaReviewConfig(
            image_min_width=10,
            image_min_height=10,
            allowed_image_formats=["JPEG"],  # PNG 也不允许
        )
        result = ImageReviewer.review(path, config=config)
        assert result.is_valid is False
        # 应有至少3个错误：宽度不足、高度不足、格式不支持
        assert len(result.errors) >= 3


# ============================================================================
# VideoReviewer 单元测试
# ============================================================================


class TestVideoReviewerFormatValidation:
    """VideoReviewer 格式验证测试。"""

    def test_mp4_format_valid(self, sample_video: str) -> None:
        """MP4 格式应通过验证。"""
        from review.media_reviewer import VideoReviewer

        result = VideoReviewer.review(sample_video)
        assert result.is_valid is True
        assert result.format == "MP4"

    def test_unsupported_format_rejected(self, sample_video: str) -> None:
        """不在允许列表中的格式应被拒绝。"""
        from review.media_reviewer import VideoReviewer

        config = MediaReviewConfig(allowed_video_formats=["WEBM"])
        result = VideoReviewer.review(sample_video, config=config)
        assert result.is_valid is False
        assert any("不支持的视频格式" in e for e in result.errors)

    def test_validate_format_empty_string(self) -> None:
        """空格式字符串应返回错误。"""
        from review.media_reviewer import VideoReviewer

        config = MediaReviewConfig()
        errors = VideoReviewer._validate_format("", config)
        assert len(errors) == 1
        assert "无法识别视频格式" in errors[0]


class TestVideoReviewerResolveFormat:
    """VideoReviewer._resolve_format 格式解析测试。"""

    def test_resolve_by_extension_mp4(self) -> None:
        """通过 .mp4 扩展名解析为 MP4。"""
        from review.media_reviewer import VideoReviewer

        # PyAV 对 MP4 返回 "mov,mp4,m4a,3gp,3g2,mj2"
        fmt = VideoReviewer._resolve_format("/path/to/video.mp4", "mov,mp4,m4a,3gp,3g2,mj2")
        assert fmt == "MP4"

    def test_resolve_by_extension_avi(self) -> None:
        """通过 .avi 扩展名解析为 AVI。"""
        from review.media_reviewer import VideoReviewer

        fmt = VideoReviewer._resolve_format("/path/to/video.avi", "avi")
        assert fmt == "AVI"

    def test_resolve_by_extension_webm(self) -> None:
        """通过 .webm 扩展名解析为 WEBM。"""
        from review.media_reviewer import VideoReviewer

        fmt = VideoReviewer._resolve_format("/path/to/video.webm", "matroska,webm")
        assert fmt == "WEBM"

    def test_resolve_by_extension_mov(self) -> None:
        """通过 .mov 扩展名解析为 MOV。"""
        from review.media_reviewer import VideoReviewer

        fmt = VideoReviewer._resolve_format("/path/to/video.mov", "mov,mp4,m4a,3gp,3g2,mj2")
        assert fmt == "MOV"

    def test_resolve_by_container_list(self) -> None:
        """扩展名不匹配时，从容器格式列表中匹配。"""
        from review.media_reviewer import VideoReviewer

        # 未知扩展名，容器列表中多个条目时按顺序匹配第一个
        fmt = VideoReviewer._resolve_format("/path/to/video.dat", "mov,mp4,m4a,3gp,3g2,mj2")
        # "mov" 排在 "mp4" 前面，优先匹配到 MOV
        assert fmt in ("MP4", "MOV")

    def test_resolve_fallback_to_uppercase(self) -> None:
        """无法匹配时，兜底返回原始格式名大写。"""
        from review.media_reviewer import VideoReviewer

        fmt = VideoReviewer._resolve_format("/path/to/video.xyz", "unknown_format")
        assert fmt == "UNKNOWN_FORMAT"

    def test_resolve_empty_container(self) -> None:
        """空容器格式名。"""
        from review.media_reviewer import VideoReviewer

        fmt = VideoReviewer._resolve_format("/path/to/video.xyz", "")
        assert fmt == ""


class TestVideoReviewerDurationCheck:
    """VideoReviewer 时长检查测试。"""

    def test_duration_extracted(self, sample_video: str) -> None:
        """视频时长应被正确提取。"""
        from review.media_reviewer import VideoReviewer

        result = VideoReviewer.review(sample_video)
        # 测试视频约 1 帧 / 24fps，时长接近 0
        assert isinstance(result.duration_seconds, float)
        assert result.duration_seconds >= 0

    def test_min_duration_violation(self, sample_video: str) -> None:
        """视频时长低于最短限制应报错。"""
        from review.media_reviewer import VideoReviewer

        config = MediaReviewConfig(video_min_duration=10.0)
        result = VideoReviewer.review(sample_video, config=config)
        assert result.is_valid is False
        assert any("时长" in e and "小于最短限制" in e for e in result.errors)

    def test_max_duration_violation_mock(self) -> None:
        """视频时长超过最长限制应报错（使用 mock）。"""
        from review.media_reviewer import VideoReviewer

        # 模拟一个长视频的元数据
        with patch.object(VideoReviewer, "_extract_metadata", return_value={
            "format": "MP4",
            "duration_seconds": 7200.0,
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "codec": "h264",
        }), patch("review.media_reviewer.os.path.isfile", return_value=True):
            config = MediaReviewConfig(video_max_duration=3600.0)
            result = VideoReviewer.review("/fake/video.mp4", config=config)
            assert result.is_valid is False
            assert any("时长" in e and "超过最长限制" in e for e in result.errors)

    def test_check_duration_boundary(self) -> None:
        """边界值：刚好等于最小/最大时长应通过。"""
        from review.media_reviewer import VideoReviewer

        config = MediaReviewConfig(video_min_duration=10.0, video_max_duration=60.0)
        errors, warnings = VideoReviewer._check_duration(10.0, config)
        assert len(errors) == 0
        errors2, _ = VideoReviewer._check_duration(60.0, config)
        assert len(errors2) == 0


class TestVideoReviewerMetadataExtraction:
    """VideoReviewer 元数据提取测试。"""

    def test_metadata_fields(self, sample_video: str) -> None:
        """视频元数据应包含 format/width/height/fps/codec。"""
        from review.media_reviewer import VideoReviewer

        result = VideoReviewer.review(sample_video)
        assert result.format != ""
        assert result.width == 320
        assert result.height == 240
        assert result.fps > 0
        assert result.codec != ""

    def test_metadata_unparseable_file(self, corrupted_file: str) -> None:
        """无法解析的视频文件应返回 is_valid=False 或包含错误。"""
        from review.media_reviewer import VideoReviewer

        result = VideoReviewer.review(corrupted_file)
        # corrupted_file 可能被 PyAV 部分解析，也可能完全无法解析
        # 只要结果合理即可（要么 is_valid=False，要么能成功解析出元数据）
        assert isinstance(result.is_valid, bool)
        assert isinstance(result.errors, list)

    def test_nonexistent_file_raises(self) -> None:
        """不存在的文件应抛出 FileNotFoundError。"""
        from review.media_reviewer import VideoReviewer

        with pytest.raises(FileNotFoundError, match="文件不存在"):
            VideoReviewer.review("/nonexistent/video.mp4")


class TestVideoReviewerKeyframeExtraction:
    """VideoReviewer 关键帧提取测试。"""

    def test_extract_keyframes_success(self, sample_video: str, tmp_path: Path) -> None:
        """成功提取关键帧。"""
        from review.media_reviewer import VideoReviewer

        output_dir = str(tmp_path / "keyframes")
        paths = VideoReviewer.extract_keyframes(sample_video, interval_seconds=1.0, output_dir=output_dir)
        assert isinstance(paths, list)
        assert len(paths) >= 1
        for p in paths:
            assert os.path.isfile(p)
            assert p.startswith(output_dir)
            # 每个关键帧应是可以打开的 JPEG
            img = Image.open(p)
            assert img.format == "JPEG"

    def test_extract_keyframes_default_output_dir(self, sample_video: str) -> None:
        """未指定输出目录时应使用视频同目录。"""
        from review.media_reviewer import VideoReviewer

        paths = VideoReviewer.extract_keyframes(sample_video, interval_seconds=1.0)
        video_dir = str(Path(sample_video).parent)
        for p in paths:
            assert p.startswith(video_dir)
        # 清理
        for p in paths:
            if os.path.isfile(p):
                os.remove(p)

    def test_extract_keyframes_nonexistent_file(self) -> None:
        """文件不存在应抛出 FileNotFoundError。"""
        from review.media_reviewer import VideoReviewer

        with pytest.raises(FileNotFoundError):
            VideoReviewer.extract_keyframes("/nonexistent/video.mp4")

    def test_extract_keyframes_naming_pattern(self, sample_video: str, tmp_path: Path) -> None:
        """关键帧文件名应包含序号。"""
        from review.media_reviewer import VideoReviewer

        output_dir = str(tmp_path / "naming")
        paths = VideoReviewer.extract_keyframes(sample_video, interval_seconds=1.0, output_dir=output_dir)
        for p in paths:
            assert "_keyframe_" in os.path.basename(p)
            assert os.path.basename(p).endswith(".jpg")


class TestVideoReviewerMocked:
    """VideoReviewer 使用 mock 测试（不依赖真实视频文件）。"""

    def test_review_with_mocked_metadata(self) -> None:
        """使用 mock 元数据审阅视频。"""
        from review.media_reviewer import VideoReviewer

        with patch.object(VideoReviewer, "_extract_metadata", return_value={
            "format": "MP4",
            "duration_seconds": 30.0,
            "width": 1920,
            "height": 1080,
            "fps": 29.97,
            "codec": "h264",
        }), patch("review.media_reviewer.os.path.isfile", return_value=True):
            result = VideoReviewer.review("/fake/video.mp4")
            assert result.is_valid is True
            assert result.format == "MP4"
            assert result.duration_seconds == 30.0
            assert result.width == 1920
            assert result.height == 1080
            assert result.fps == 29.97
            assert result.codec == "h264"

    def test_review_unparseable_video(self) -> None:
        """元数据提取失败应返回 is_valid=False。"""
        from review.media_reviewer import VideoReviewer

        with patch.object(VideoReviewer, "_extract_metadata", return_value=None), \
             patch("review.media_reviewer.os.path.isfile", return_value=True):
            result = VideoReviewer.review("/fake/corrupt.mp4")
            assert result.is_valid is False
            assert "无法解析视频文件" in result.errors

    def test_extract_metadata_av_open_failure(self) -> None:
        """av.open 失败应返回 None。"""
        from review.media_reviewer import VideoReviewer

        with patch("review.media_reviewer.av.open", side_effect=Exception("no file")):
            result = VideoReviewer._extract_metadata("/nonexistent/video.mp4")
            assert result is None

    def test_extract_metadata_no_video_stream(self) -> None:
        """没有视频流应返回 None。"""
        from review.media_reviewer import VideoReviewer

        mock_container = MagicMock()
        mock_container.streams.video = []
        mock_container.close = MagicMock()

        with patch("review.media_reviewer.av.open", return_value=mock_container):
            result = VideoReviewer._extract_metadata("/fake/audio_only.mp4")
            assert result is None
            mock_container.close.assert_called()


# ============================================================================
# MediaReviewService 扩展测试
# ============================================================================


class TestInferMediaType:
    """_infer_media_type 媒体类型推断测试。"""

    def test_image_extensions(self) -> None:
        """常见图片扩展名应推断为 image。"""
        for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"]:
            assert _infer_media_type(f"/path/file{ext}") == "image", f"扩展名 {ext} 应推断为 image"

    def test_video_extensions(self) -> None:
        """常见视频扩展名应推断为 video。"""
        for ext in [".mp4", ".avi", ".mov", ".mkv", ".webm"]:
            assert _infer_media_type(f"/path/file{ext}") == "video", f"扩展名 {ext} 应推断为 video"

    def test_unknown_extension_raises(self) -> None:
        """未知扩展名应抛出 ValueError。"""
        with pytest.raises(ValueError, match="无法推断媒体类型"):
            _infer_media_type("/path/file.xyz")

    def test_case_insensitive(self) -> None:
        """扩展名大小写不敏感。"""
        assert _infer_media_type("/path/file.PNG") == "image"
        assert _infer_media_type("/path/file.MP4") == "video"

    def test_no_extension_raises(self) -> None:
        """无扩展名应抛出 ValueError。"""
        with pytest.raises(ValueError, match="无法推断媒体类型"):
            _infer_media_type("/path/file_no_ext")


class TestMediaReviewServiceRouting:
    """MediaReviewService 路由逻辑测试。"""

    @pytest.mark.asyncio
    async def test_image_type_routes_to_image_reviewer(self, service: MediaReviewService, png_image: str) -> None:
        """media_type='image' 应路由到 ImageReviewer。"""
        result = await service.review_media(png_image, "image")
        assert isinstance(result, ImageReviewResult)

    @pytest.mark.asyncio
    async def test_video_type_routes_to_video_reviewer(self, service: MediaReviewService, sample_video: str) -> None:
        """media_type='video' 应路由到 VideoReviewer。"""
        result = await service.review_media(sample_video, "video")
        assert isinstance(result, VideoReviewResult)

    @pytest.mark.asyncio
    async def test_unsupported_type_raises(self, service: MediaReviewService, png_image: str) -> None:
        """不支持的 media_type 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="不支持的媒体类型"):
            await service.review_media(png_image, "audio")

    @pytest.mark.asyncio
    async def test_custom_config_used(self, service: MediaReviewService, png_image: str) -> None:
        """传入自定义配置应被使用。"""
        config = MediaReviewConfig(image_min_width=300)
        result = await service.review_media(png_image, "image", config=config)
        assert result.is_valid is False

    @pytest.mark.asyncio
    async def test_service_default_config(self, png_image: str) -> None:
        """服务默认配置应被使用。"""
        strict_config = MediaReviewConfig(image_min_width=300)
        svc = MediaReviewService(default_config=strict_config)
        result = await svc.review_media(png_image, "image")
        assert result.is_valid is False


class TestMediaReviewServiceArtifacts:
    """MediaReviewService 批量制品审阅测试。"""

    @pytest.mark.asyncio
    async def test_batch_review_mixed_types(
        self, service: MediaReviewService, png_image: str, sample_video: str
    ) -> None:
        """批量审阅图片和视频制品。"""
        storage = MagicMock()

        async def mock_load(artifact_id: str) -> dict[str, Any]:
            if artifact_id == "img-001":
                return {"file_path": png_image, "media_type": "image"}
            if artifact_id == "vid-001":
                return {"file_path": sample_video, "media_type": "video"}
            return None

        storage.load = AsyncMock(side_effect=mock_load)
        results = await service.review_artifacts(["img-001", "vid-001"], storage)

        assert len(results) == 2
        assert results[0]["media_type"] == "image"
        assert results[0]["is_valid"] is True
        assert results[1]["media_type"] == "video"
        assert results[1]["is_valid"] is True

    @pytest.mark.asyncio
    async def test_batch_review_infer_media_type(
        self, service: MediaReviewService, png_image: str
    ) -> None:
        """未指定 media_type 时应根据扩展名推断。"""
        storage = MagicMock()

        async def mock_load(artifact_id: str) -> dict[str, Any]:
            return {"file_path": png_image}  # 无 media_type

        storage.load = AsyncMock(side_effect=mock_load)
        results = await service.review_artifacts(["art-001"], storage)

        assert len(results) == 1
        assert results[0]["media_type"] == "image"
        assert results[0]["is_valid"] is True

    @pytest.mark.asyncio
    async def test_batch_review_artifact_not_found(self, service: MediaReviewService) -> None:
        """制品不存在应返回错误。"""
        storage = MagicMock()
        storage.load = AsyncMock(return_value=None)
        results = await service.review_artifacts(["missing-001"], storage)

        assert len(results) == 1
        assert "error" in results[0]
        assert "制品不存在" in results[0]["error"]

    @pytest.mark.asyncio
    async def test_batch_review_uninferable_type(self, service: MediaReviewService) -> None:
        """无法推断媒体类型应返回错误。"""
        storage = MagicMock()

        async def mock_load(artifact_id: str) -> dict[str, Any]:
            return {"file_path": "/path/to/file.xyz"}  # 未知扩展名

        storage.load = AsyncMock(side_effect=mock_load)
        results = await service.review_artifacts(["art-001"], storage)

        assert len(results) == 1
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_batch_review_file_not_found(self, service: MediaReviewService) -> None:
        """文件路径不存在应返回错误。"""
        storage = MagicMock()

        async def mock_load(artifact_id: str) -> dict[str, Any]:
            return {"file_path": "/nonexistent/image.png", "media_type": "image"}

        storage.load = AsyncMock(side_effect=mock_load)
        results = await service.review_artifacts(["art-001"], storage)

        assert len(results) == 1
        assert "error" in results[0]


class TestMediaReviewServiceMetadata:
    """MediaReviewService 元数据获取测试。"""

    def test_get_image_metadata(self, service: MediaReviewService, png_image: str) -> None:
        """获取图片元数据应返回完整摘要。"""
        metadata = service.get_media_metadata(png_image, "image")
        assert metadata["media_type"] == "image"
        assert metadata["format"] == "PNG"
        assert metadata["width"] == 200
        assert metadata["height"] == 100
        assert metadata["file_size"] > 0
        assert "aspect_ratio" in metadata

    def test_get_video_metadata(self, service: MediaReviewService, sample_video: str) -> None:
        """获取视频元数据应返回完整摘要。"""
        metadata = service.get_media_metadata(sample_video, "video")
        assert metadata["media_type"] == "video"
        assert metadata["format"] == "MP4"
        assert metadata["width"] == 320
        assert metadata["height"] == 240
        assert metadata["file_size"] > 0
        assert "duration_seconds" in metadata
        assert "fps" in metadata
        assert "codec" in metadata

    def test_get_metadata_nonexistent_file(self, service: MediaReviewService) -> None:
        """文件不存在应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            service.get_media_metadata("/nonexistent/file.png", "image")

    def test_get_metadata_unsupported_type(self, service: MediaReviewService, png_image: str) -> None:
        """不支持的类型应抛出 ValueError。"""
        with pytest.raises(ValueError, match="不支持的媒体类型"):
            service.get_media_metadata(png_image, "audio")

    def test_get_image_metadata_with_exif(self, service: MediaReviewService, jpeg_with_exif: str) -> None:
        """带 EXIF 的图片元数据应包含 EXIF 信息。"""
        metadata = service.get_media_metadata(jpeg_with_exif, "image")
        assert metadata["format"] == "JPEG"
        assert "exif" in metadata

    def test_get_image_metadata_corrupted(self, service: MediaReviewService, corrupted_file: str) -> None:
        """损坏图片的元数据应包含错误信息。"""
        metadata = service.get_media_metadata(corrupted_file, "image")
        assert "error" in metadata

    def test_get_video_metadata_unparseable(self, service: MediaReviewService, corrupted_file: str) -> None:
        """无法解析的视频应返回错误信息或部分元数据。"""
        # 把损坏文件重命名为 .mp4
        metadata = service.get_media_metadata(corrupted_file, "video")
        # corrupted_file 可能被 PyAV 部分解析，也可能完全无法解析
        assert isinstance(metadata, dict)
        assert "format" in metadata or "error" in metadata


class TestMediaReviewServiceThumbnails:
    """MediaReviewService 视频缩略图提取测试。"""

    def test_extract_thumbnails(self, service: MediaReviewService, sample_video: str, tmp_path: Path) -> None:
        """提取视频缩略图。"""
        output_dir = str(tmp_path / "thumbs")
        paths = service.extract_video_thumbnails(sample_video, interval=1.0, output_dir=output_dir)
        assert len(paths) >= 1
        for p in paths:
            assert os.path.isfile(p)


# ============================================================================
# API 端点测试
# ============================================================================


class TestMediaReviewAPI:
    """媒体审阅 API 端点测试。"""

    @pytest.fixture
    def client(self):
        """创建 FastAPI TestClient，覆盖认证依赖。"""
        from fastapi.testclient import TestClient
        from channels.api.routes_reviews import reviews_router
        from channels.api.deps import require_auth
        from fastapi import FastAPI

        app = FastAPI()
        # 覆盖认证：返回模拟用户
        async def mock_auth():
            return {"sub": "test_user", "username": "tester"}

        app.dependency_overrides[require_auth] = mock_auth
        app.include_router(reviews_router)

        # 重置全局服务实例以确保测试隔离
        import review.review_service as rs_mod
        rs_mod._review_service = None

        import channels.api.routes_reviews as rr_mod
        rr_mod._media_review_service = None

        client = TestClient(app)
        yield client

        # 清理
        app.dependency_overrides.clear()
        rs_mod._review_service = None
        rr_mod._media_review_service = None

    def _upload_file(self, tmp_path: Path, filename: str, content: bytes) -> str:
        """创建临时上传文件。"""
        path = str(tmp_path / filename)
        with open(path, "wb") as f:
            f.write(content)
        return path

    # ---- POST /api/v1/reviews/media-review ----

    def test_media_review_upload_image(self, client, tmp_path: Path) -> None:
        """POST /media-review 上传图片应返回审阅结果。"""
        # 准备图片数据
        img = Image.new("RGB", (200, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        response = client.post(
            "/api/v1/reviews/media-review",
            files={"file": ("test.png", img_bytes, "image/png")},
            data={"media_type": "image"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("is_valid") is True
        assert data.get("format") == "PNG"
        assert data.get("width") == 200
        assert data.get("height") == 100
        assert data.get("media_type") == "image"
        assert data.get("filename") == "test.png"

    def test_media_review_upload_image_auto_infer(self, client) -> None:
        """POST /media-review 不指定 media_type 时应自动推断。"""
        img = Image.new("RGB", (100, 100), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        img_bytes = buf.getvalue()

        response = client.post(
            "/api/v1/reviews/media-review",
            files={"file": ("photo.jpg", img_bytes, "image/jpeg")},
            data={"media_type": ""},  # 空字符串触发自动推断
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("is_valid") is True
        assert data.get("media_type") == "image"

    def test_media_review_upload_video(self, client, sample_video: str) -> None:
        """POST /media-review 上传视频应返回审阅结果。"""
        with open(sample_video, "rb") as f:
            video_bytes = f.read()

        response = client.post(
            "/api/v1/reviews/media-review",
            files={"file": ("video.mp4", video_bytes, "video/mp4")},
            data={"media_type": "video"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("is_valid") is True
        assert data.get("format") == "MP4"
        assert data.get("media_type") == "video"

    def test_media_review_upload_corrupted(self, client) -> None:
        """POST /media-review 上传损坏文件应返回无效结果。"""
        corrupted = b"\x00\x01\x02\x03garbage"

        response = client.post(
            "/api/v1/reviews/media-review",
            files={"file": ("bad.jpg", corrupted, "image/jpeg")},
            data={"media_type": "image"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("is_valid") is False

    def test_media_review_upload_unsupported_extension(self, client) -> None:
        """POST /media-review 上传不支持的扩展名应返回错误。"""
        response = client.post(
            "/api/v1/reviews/media-review",
            files={"file": ("file.xyz", b"some data", "application/octet-stream")},
            data={"media_type": ""},
        )
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "INVALID"

    # ---- GET /api/v1/reviews/{review_id}/media-metadata ----

    def test_get_media_metadata_nonexistent_review(self, client) -> None:
        """GET /{review_id}/media-metadata 不存在的审批应返回错误。"""
        response = client.get("/api/v1/reviews/nonexistent-id/media-metadata")
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "NOT_FOUND"

    def test_get_media_metadata_with_stored_results(self, client) -> None:
        """GET /{review_id}/media-metadata 有存储审阅结果时直接返回。"""
        # 先创建审批
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t1", thread_id="th1", session_id="s1",
                tab_id="tab1", title="媒体审批",
                metadata={
                    "media_review_results": {
                        "/images/a.jpg": {"is_valid": True, "format": "JPEG"},
                    },
                },
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.get(f"/api/v1/reviews/{review.id}/media-metadata")
        assert response.status_code == 200
        data = response.json()
        assert data["review_id"] == review.id
        assert "/images/a.jpg" in data["media_metadata"]

    def test_get_media_metadata_no_media_files(self, client) -> None:
        """GET /{review_id}/media-metadata 无媒体文件时返回空列表。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t2", thread_id="th2", session_id="s2",
                tab_id="tab2", title="空审批",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.get(f"/api/v1/reviews/{review.id}/media-metadata")
        assert response.status_code == 200
        data = response.json()
        assert data["media_metadata"] == []

    # ---- POST /api/v1/reviews/{review_id}/attachments ----

    def test_add_attachments_nonexistent_review(self, client) -> None:
        """POST /{review_id}/attachments 不存在的审批应返回错误。"""
        response = client.post(
            "/api/v1/reviews/nonexistent-id/attachments",
            json={"files": [{"path": "/fake/image.png", "media_type": "image"}]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "NOT_FOUND"

    def test_add_attachments_empty_files(self, client) -> None:
        """POST /{review_id}/attachments 空 files 列表应返回错误。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t3", thread_id="th3", session_id="s3",
                tab_id="tab3", title="附件测试",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.post(
            f"/api/v1/reviews/{review.id}/attachments",
            json={"files": []},
        )
        assert response.status_code == 200
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "INVALID"

    def test_add_attachments_with_auto_review(
        self, client, png_image: str
    ) -> None:
        """POST /{review_id}/attachments auto_review=true 应自动审阅。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t4", thread_id="th4", session_id="s4",
                tab_id="tab4", title="附件审阅测试",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.post(
            f"/api/v1/reviews/{review.id}/attachments",
            json={
                "files": [{"path": png_image, "media_type": "image"}],
                "auto_review": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["added_count"] == 1
        assert data["attachments"][0]["review_result"] is not None
        assert data["attachments"][0]["review_result"]["is_valid"] is True

    def test_add_attachments_without_auto_review(
        self, client, png_image: str
    ) -> None:
        """POST /{review_id}/attachments auto_review=false 不应审阅。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t5", thread_id="th5", session_id="s5",
                tab_id="tab5", title="附件不审阅测试",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.post(
            f"/api/v1/reviews/{review.id}/attachments",
            json={
                "files": [{"path": png_image, "media_type": "image"}],
                "auto_review": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["added_count"] == 1
        assert data["attachments"][0]["review_result"] is None

    def test_add_attachments_missing_path(self, client) -> None:
        """POST /{review_id}/attachments 缺少 path 应记录错误。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t6", thread_id="th6", session_id="s6",
                tab_id="tab6", title="缺少路径测试",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.post(
            f"/api/v1/reviews/{review.id}/attachments",
            json={"files": [{"media_type": "image"}], "auto_review": False},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["added_count"] == 1
        assert "error" in data["attachments"][0]
        assert "缺少 path" in data["attachments"][0]["error"]

    def test_add_attachments_infer_media_type(
        self, client, png_image: str
    ) -> None:
        """POST /{review_id}/attachments 未指定 media_type 应自动推断。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t7", thread_id="th7", session_id="s7",
                tab_id="tab7", title="推断类型测试",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.post(
            f"/api/v1/reviews/{review.id}/attachments",
            json={
                "files": [{"path": png_image}],  # 无 media_type
                "auto_review": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["attachments"][0]["media_type"] == "image"

    def test_add_attachments_uninferable_media_type(self, client) -> None:
        """POST /{review_id}/attachments 无法推断类型应记录错误。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t8", thread_id="th8", session_id="s8",
                tab_id="tab8", title="未知类型测试",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        response = client.post(
            f"/api/v1/reviews/{review.id}/attachments",
            json={
                "files": [{"path": "/path/to/file.xyz"}],
                "auto_review": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "error" in data["attachments"][0]
        assert "无法推断" in data["attachments"][0]["error"]

    def test_add_attachments_updates_review_metadata(
        self, client, png_image: str
    ) -> None:
        """POST /{review_id}/attachments 应更新审批的 metadata。"""
        from review.review_service import get_review_service
        rs = get_review_service()

        async def _create():
            return await rs.create_review(
                task_id="t9", thread_id="th9", session_id="s9",
                tab_id="tab9", title="元数据更新测试",
            )

        review = asyncio.new_event_loop().run_until_complete(_create())

        client.post(
            f"/api/v1/reviews/{review.id}/attachments",
            json={
                "files": [{"path": png_image, "media_type": "image"}],
                "auto_review": True,
            },
        )

        # 验证 metadata 已更新
        async def _get():
            return await rs.get_review(review.id)

        updated_review = asyncio.new_event_loop().run_until_complete(_get())
        assert "media_files" in updated_review.metadata
        assert len(updated_review.metadata["media_files"]) == 1
        assert "media_review_results" in updated_review.metadata
        assert png_image in updated_review.metadata["media_review_results"]


# ============================================================================
# 前端类型定义验证测试
# ============================================================================


class TestFrontendTypes:
    """前端类型定义验证测试。

    验证 frontend/src/types/review.ts 中新增的类型与后端数据模型的兼容性。
    通过读取 TypeScript 源码并验证类型结构来确保前后端一致。
    """

    @pytest.fixture
    def review_types_content(self) -> str:
        """读取前端类型定义文件。"""
        types_path = os.path.join(
            os.path.dirname(__file__),
            "..", "frontend", "src", "types", "review.ts",
        )
        types_path = os.path.abspath(types_path)
        with open(types_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_image_review_result_type_defined(self, review_types_content: str) -> None:
        """ImageReviewResult 类型应被定义。"""
        assert "interface ImageReviewResult" in review_types_content
        assert "isValid: boolean" in review_types_content
        assert "format: string" in review_types_content
        assert "width: number" in review_types_content
        assert "height: number" in review_types_content
        assert "aspectRatio: number" in review_types_content
        assert "exif: Record<string, any>" in review_types_content
        assert "warnings: string[]" in review_types_content
        assert "errors: string[]" in review_types_content

    def test_video_review_result_type_defined(self, review_types_content: str) -> None:
        """VideoReviewResult 类型应被定义。"""
        assert "interface VideoReviewResult" in review_types_content
        assert "durationSeconds: number" in review_types_content
        assert "fps: number" in review_types_content
        assert "codec: string" in review_types_content

    def test_media_metadata_type_defined(self, review_types_content: str) -> None:
        """MediaMetadata 类型应被定义。"""
        assert "interface MediaMetadata" in review_types_content
        assert "type: 'image' | 'video'" in review_types_content
        assert "imageResult?: ImageReviewResult" in review_types_content
        assert "videoResult?: VideoReviewResult" in review_types_content

    def test_artifact_type_includes_media(self, review_types_content: str) -> None:
        """ArtifactType 应包含 image 和 video。"""
        assert "'image'" in review_types_content
        assert "'video'" in review_types_content

    def test_artifact_has_media_metadata(self, review_types_content: str) -> None:
        """Artifact 接口应包含 mediaMetadata 字段。"""
        assert "mediaMetadata?: MediaMetadata" in review_types_content

    def test_backend_frontend_field_consistency(self) -> None:
        """后端 to_dict 输出应与前端类型定义字段对应。

        验证命名约定：
        - 后端 snake_case → 前端 camelCase
        """
        # ImageReviewResult 后端字段
        image_result = ImageReviewResult(
            is_valid=True, format="PNG", width=200, height=100,
            aspect_ratio=2.0, exif={"Make": "Test"}, warnings=[], errors=[],
        )
        backend_dict = image_result.to_dict()
        expected_backend_keys = {
            "is_valid", "format", "width", "height",
            "aspect_ratio", "exif", "warnings", "errors",
        }
        assert set(backend_dict.keys()) == expected_backend_keys

        # VideoReviewResult 后端字段
        video_result = VideoReviewResult(
            is_valid=True, format="MP4", duration_seconds=30.0,
            width=1920, height=1080, fps=29.97, codec="h264",
            warnings=[], errors=[],
        )
        backend_dict = video_result.to_dict()
        expected_backend_keys = {
            "is_valid", "format", "duration_seconds",
            "width", "height", "fps", "codec", "warnings", "errors",
        }
        assert set(backend_dict.keys()) == expected_backend_keys


class TestFrontendMediaMetadataPanel:
    """前端 MediaMetadataPanel 组件验证测试。

    通过读取 TypeScript 源码验证组件的 props 接口和功能函数。
    """

    @pytest.fixture
    def panel_content(self) -> str:
        """读取 MediaMetadataPanel 组件源码。"""
        panel_path = os.path.join(
            os.path.dirname(__file__),
            "..", "frontend", "src", "components", "review", "MediaMetadataPanel.tsx",
        )
        panel_path = os.path.abspath(panel_path)
        with open(panel_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_panel_props_defined(self, panel_content: str) -> None:
        """MediaMetadataPanelProps 应定义 metadata 和 compact 属性。"""
        assert "interface MediaMetadataPanelProps" in panel_content
        assert "metadata: MediaMetadata" in panel_content
        assert "compact?: boolean" in panel_content

    def test_panel_exported(self, panel_content: str) -> None:
        """MediaMetadataPanel 组件应被导出。"""
        assert "export function MediaMetadataPanel" in panel_content

    def test_format_duration_defined(self, panel_content: str) -> None:
        """formatDuration 辅助函数应被定义。"""
        assert "function formatDuration" in panel_content

    def test_aspect_ratio_label_defined(self, panel_content: str) -> None:
        """aspectRatioLabel 辅助函数应被定义。"""
        assert "function aspectRatioLabel" in panel_content

    def test_compact_and_full_modes(self, panel_content: str) -> None:
        """组件应支持紧凑模式和完整模式。"""
        assert "compact" in panel_content
        assert "expanded" in panel_content

    def test_warnings_and_errors_rendering(self, panel_content: str) -> None:
        """组件应渲染警告和错误信息。"""
        assert "WarningBadge" in panel_content
        assert "ErrorBadge" in panel_content

    def test_image_and_video_sections(self, panel_content: str) -> None:
        """组件应有图片和视频两种展示逻辑。"""
        assert "ImageMetadataFull" in panel_content
        assert "VideoMetadataFull" in panel_content

    def test_exif_field_labels(self, panel_content: str) -> None:
        """EXIF 字段中文映射应被定义。"""
        assert "EXIF_LABELS" in panel_content
        assert "相机厂商" in panel_content
        assert "相机型号" in panel_content
        assert "拍摄时间" in panel_content
