"""MediaReviewService 单元测试。

覆盖 MediaReviewService 的所有公共方法：
- review_media: 单文件媒体审阅
- review_artifacts: 批量制品审阅
- get_media_metadata: 媒体元数据摘要
- extract_video_thumbnails: 视频缩略图提取
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from review.models import (
    ImageReviewResult,
    MediaReviewConfig,
    VideoReviewResult,
)
from review.media_review_service import MediaReviewService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service() -> MediaReviewService:
    """创建 MediaReviewService 实例。"""
    return MediaReviewService()


@pytest.fixture
def sample_image(tmp_path: Path) -> str:
    """创建一个有效的测试 PNG 图片文件。"""
    from PIL import Image

    img = Image.new("RGB", (200, 100), color="red")
    path = str(tmp_path / "test_image.png")
    img.save(path, format="PNG")
    return path


@pytest.fixture
def sample_video(tmp_path: Path) -> str:
    """创建一个最小的测试 MP4 视频文件。

    使用 PyAV 生成一帧视频。
    """
    import av

    path = str(tmp_path / "test_video.mp4")
    container = av.open(path, mode="w")
    stream = container.add_stream("mpeg4", rate=24)
    stream.width = 320
    stream.height = 240
    stream.pix_fmt = "yuv420p"

    from PIL import Image as PILImage

    img = PILImage.new("RGB", (320, 240), color="blue")
    frame = av.VideoFrame.from_image(img)
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


@pytest.fixture
def mock_storage() -> MagicMock:
    """创建一个 Mock IFileStorage。"""
    storage = MagicMock()
    storage.save = AsyncMock(return_value=None)
    storage.load = AsyncMock(return_value=None)
    storage.exists = AsyncMock(return_value=True)
    return storage


# ---------------------------------------------------------------------------
# review_media 测试
# ---------------------------------------------------------------------------


class TestReviewMedia:
    """review_media 方法测试。"""

    @pytest.mark.asyncio
    async def test_review_media_image_returns_image_result(
        self, service: MediaReviewService, sample_image: str
    ) -> None:
        """审阅图片应返回 ImageReviewResult。"""
        result = await service.review_media(sample_image, "image")
        assert isinstance(result, ImageReviewResult)
        assert result.is_valid is True
        assert result.format == "PNG"
        assert result.width == 200
        assert result.height == 100

    @pytest.mark.asyncio
    async def test_review_media_video_returns_video_result(
        self, service: MediaReviewService, sample_video: str
    ) -> None:
        """审阅视频应返回 VideoReviewResult。"""
        result = await service.review_media(sample_video, "video")
        assert isinstance(result, VideoReviewResult)
        assert result.is_valid is True
        assert result.format == "MP4"
        assert result.width == 320
        assert result.height == 240

    @pytest.mark.asyncio
    async def test_review_media_image_with_config(
        self, service: MediaReviewService, sample_image: str
    ) -> None:
        """使用自定义配置审阅图片。"""
        config = MediaReviewConfig(image_min_width=300)
        result = await service.review_media(sample_image, "image", config=config)
        assert isinstance(result, ImageReviewResult)
        assert result.is_valid is False  # 200 < 300
        assert any("宽度" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_review_media_unsupported_type_raises(
        self, service: MediaReviewService, sample_image: str
    ) -> None:
        """不支持的 media_type 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="不支持的媒体类型"):
            await service.review_media(sample_image, "audio")

    @pytest.mark.asyncio
    async def test_review_media_nonexistent_file_raises(
        self, service: MediaReviewService
    ) -> None:
        """文件不存在应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            await service.review_media("/nonexistent/file.png", "image")

    @pytest.mark.asyncio
    async def test_review_media_video_with_config(
        self, service: MediaReviewService, sample_video: str
    ) -> None:
        """使用自定义配置审阅视频。"""
        config = MediaReviewConfig(video_max_duration=0.001)
        result = await service.review_media(sample_video, "video", config=config)
        assert isinstance(result, VideoReviewResult)
        assert result.is_valid is False
        assert any("时长" in e for e in result.errors)


# ---------------------------------------------------------------------------
# review_artifacts 测试
# ---------------------------------------------------------------------------


class TestReviewArtifacts:
    """review_artifacts 方法测试。"""

    @pytest.mark.asyncio
    async def test_review_artifacts_empty_list(
        self, service: MediaReviewService, mock_storage: MagicMock
    ) -> None:
        """空制品列表应返回空结果列表。"""
        results = await service.review_artifacts([], mock_storage)
        assert results == []

    @pytest.mark.asyncio
    async def test_review_artifacts_image_artifact(
        self, service: MediaReviewService, sample_image: str, mock_storage: MagicMock
    ) -> None:
        """审阅图片制品应返回正确结果。"""
        artifact_data = {
            "file_path": sample_image,
            "media_type": "image",
        }
        # mock storage.load 返回制品数据
        mock_storage.load = AsyncMock(return_value=artifact_data)
        results = await service.review_artifacts(["art-001"], mock_storage)
        assert len(results) == 1
        assert results[0]["media_type"] == "image"
        assert results[0]["is_valid"] is True

    @pytest.mark.asyncio
    async def test_review_artifacts_missing_file_path(
        self, service: MediaReviewService, mock_storage: MagicMock
    ) -> None:
        """制品缺少 file_path 应记录错误。"""
        artifact_data = {"media_type": "image"}
        mock_storage.load = AsyncMock(return_value=artifact_data)
        results = await service.review_artifacts(["art-001"], mock_storage)
        assert len(results) == 1
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_review_artifacts_missing_media_type(
        self, service: MediaReviewService, sample_image: str, mock_storage: MagicMock
    ) -> None:
        """制品缺少 media_type 应根据扩展名自动推断。"""
        artifact_data = {"file_path": sample_image}
        mock_storage.load = AsyncMock(return_value=artifact_data)
        results = await service.review_artifacts(["art-001"], mock_storage)
        assert len(results) == 1
        assert results[0]["media_type"] == "image"
        assert results[0]["is_valid"] is True

    @pytest.mark.asyncio
    async def test_review_artifacts_null_load(
        self, service: MediaReviewService, mock_storage: MagicMock
    ) -> None:
        """storage.load 返回 None 时应记录错误。"""
        mock_storage.load = AsyncMock(return_value=None)
        results = await service.review_artifacts(["art-001"], mock_storage)
        assert len(results) == 1
        assert "error" in results[0]


# ---------------------------------------------------------------------------
# get_media_metadata 测试
# ---------------------------------------------------------------------------


class TestGetMediaMetadata:
    """get_media_metadata 方法测试。"""

    def test_get_media_metadata_image(
        self, service: MediaReviewService, sample_image: str
    ) -> None:
        """获取图片元数据应返回正确摘要。"""
        metadata = service.get_media_metadata(sample_image, "image")
        assert metadata["media_type"] == "image"
        assert metadata["format"] == "PNG"
        assert metadata["width"] == 200
        assert metadata["height"] == 100
        assert "file_size" in metadata

    def test_get_media_metadata_video(
        self, service: MediaReviewService, sample_video: str
    ) -> None:
        """获取视频元数据应返回正确摘要。"""
        metadata = service.get_media_metadata(sample_video, "video")
        assert metadata["media_type"] == "video"
        assert metadata["format"] == "MP4"
        assert metadata["width"] == 320
        assert metadata["height"] == 240
        assert "duration_seconds" in metadata

    def test_get_media_metadata_unsupported_type(
        self, service: MediaReviewService, sample_image: str
    ) -> None:
        """不支持的类型应抛出 ValueError。"""
        with pytest.raises(ValueError, match="不支持的媒体类型"):
            service.get_media_metadata(sample_image, "audio")

    def test_get_media_metadata_nonexistent_file(
        self, service: MediaReviewService
    ) -> None:
        """文件不存在应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            service.get_media_metadata("/nonexistent/file.png", "image")


# ---------------------------------------------------------------------------
# extract_video_thumbnails 测试
# ---------------------------------------------------------------------------


class TestExtractVideoThumbnails:
    """extract_video_thumbnails 方法测试。"""

    def test_extract_video_thumbnails_default_interval(
        self, service: MediaReviewService, sample_video: str
    ) -> None:
        """使用默认间隔提取缩略图。"""
        paths = service.extract_video_thumbnails(sample_video)
        assert isinstance(paths, list)
        # 至少有一个缩略图
        assert len(paths) >= 1
        for p in paths:
            assert os.path.isfile(p)

    def test_extract_video_thumbnails_custom_interval(
        self, service: MediaReviewService, sample_video: str, tmp_path: Path
    ) -> None:
        """使用自定义间隔和输出目录。"""
        output_dir = str(tmp_path / "thumbnails")
        paths = service.extract_video_thumbnails(
            sample_video, interval=1.0, output_dir=output_dir
        )
        assert isinstance(paths, list)
        assert len(paths) >= 1
        for p in paths:
            assert p.startswith(output_dir)

    def test_extract_video_thumbnails_nonexistent_file(
        self, service: MediaReviewService
    ) -> None:
        """文件不存在应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            service.extract_video_thumbnails("/nonexistent/video.mp4")
