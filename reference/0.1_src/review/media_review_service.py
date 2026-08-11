"""媒体审阅服务。

整合 ImageReviewer 和 VideoReviewer，提供统一的媒体审阅入口。
支持单文件审阅、批量制品审阅、元数据摘要和视频缩略图提取。

公共接口：
    - MediaReviewService: 媒体审阅服务类
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from review.media_reviewer import ImageReviewer, VideoReviewer
from review.models import (
    ImageReviewResult,
    MediaReviewConfig,
    VideoReviewResult,
)

logger = logging.getLogger(__name__)

# 文件扩展名 → 媒体类型映射
_IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
_VIDEO_EXTENSIONS: set[str] = {".mp4", ".avi", ".mov", ".mkv", ".webm"}


def _infer_media_type(file_path: str) -> str:
    """根据文件扩展名推断媒体类型。

    Args:
        file_path: 文件路径

    Returns:
        "image" 或 "video"

    Raises:
        ValueError: 无法识别的文件扩展名
    """
    ext = Path(file_path).suffix.lower()
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    raise ValueError(f"无法推断媒体类型: {ext}")


class MediaReviewService:
    """媒体审阅服务。

    整合 ImageReviewer 和 VideoReviewer，提供统一的审阅 API。
    根据传入的 media_type 自动路由到对应的审阅器。

    用法::

        service = MediaReviewService()
        result = await service.review_media("/path/to/image.png", "image")
        metadata = service.get_media_metadata("/path/to/video.mp4", "video")
    """

    def __init__(self, default_config: MediaReviewConfig | None = None) -> None:
        """初始化媒体审阅服务。

        Args:
            default_config: 默认审阅配置，为 None 时使用 MediaReviewConfig 默认值
        """
        self._default_config = default_config or MediaReviewConfig()

    async def review_media(
        self,
        file_path: str,
        media_type: str,
        config: MediaReviewConfig | None = None,
    ) -> ImageReviewResult | VideoReviewResult:
        """审阅单个媒体文件。

        根据 media_type 路由到 ImageReviewer 或 VideoReviewer，
        在线程池中执行同步审阅操作。

        Args:
            file_path: 媒体文件路径
            media_type: 媒体类型（"image" 或 "video"）
            config: 审阅配置，为 None 时使用默认配置

        Returns:
            ImageReviewResult 或 VideoReviewResult

        Raises:
            ValueError: 不支持的 media_type
            FileNotFoundError: 文件不存在
        """
        cfg = config or self._default_config

        if media_type == "image":
            return await asyncio.to_thread(ImageReviewer.review, file_path, cfg)
        if media_type == "video":
            return await asyncio.to_thread(VideoReviewer.review, file_path, cfg)

        raise ValueError(f"不支持的媒体类型: {media_type}，仅支持 'image' 和 'video'")

    async def review_artifacts(
        self,
        artifact_ids: list[str],
        storage: Any,
    ) -> list[dict[str, Any]]:
        """批量审阅制品。

        从存储中逐个加载制品信息，根据 media_type 自动路由审阅。
        如果制品信息中缺少 media_type，会尝试根据 file_path 扩展名推断。

        Args:
            artifact_ids: 制品 ID 列表
            storage: 实现 IFileStorage 接口的存储实例

        Returns:
            审阅结果字典列表，每个字典包含：
            - artifact_id: 制品 ID
            - media_type: 媒体类型
            - result: 审阅结果字典（成功时）
            - error: 错误信息（失败时）
        """
        results: list[dict[str, Any]] = []

        for artifact_id in artifact_ids:
            try:
                artifact_data = await storage.load(artifact_id)
                if artifact_data is None:
                    results.append(
                        {
                            "artifact_id": artifact_id,
                            "error": f"制品不存在: {artifact_id}",
                        }
                    )
                    continue

                file_path = artifact_data.get("file_path")
                if not file_path:
                    results.append(
                        {
                            "artifact_id": artifact_id,
                            "error": f"制品缺少 file_path: {artifact_id}",
                        }
                    )
                    continue

                # 确定 media_type：优先使用显式指定，否则推断
                media_type = artifact_data.get("media_type")
                if not media_type:
                    try:
                        media_type = _infer_media_type(file_path)
                    except ValueError:
                        results.append(
                            {
                                "artifact_id": artifact_id,
                                "error": f"无法推断媒体类型: {file_path}",
                            }
                        )
                        continue

                review_result = await self.review_media(file_path, media_type)

                results.append(
                    {
                        "artifact_id": artifact_id,
                        "media_type": media_type,
                        "file_path": file_path,
                        **review_result.to_dict(),
                    }
                )

            except FileNotFoundError as exc:
                results.append(
                    {
                        "artifact_id": artifact_id,
                        "error": f"文件不存在: {exc}",
                    }
                )
            except Exception as exc:
                logger.error(
                    "[MediaReviewService] 制品审阅失败 | artifact_id=%s | error=%s",
                    artifact_id,
                    exc,
                )
                results.append(
                    {
                        "artifact_id": artifact_id,
                        "error": str(exc),
                    }
                )

        return results

    def get_media_metadata(
        self,
        file_path: str,
        media_type: str,
    ) -> dict[str, Any]:
        """获取媒体文件元数据摘要。

        Args:
            file_path: 媒体文件路径
            media_type: 媒体类型（"image" 或 "video"）

        Returns:
            元数据摘要字典，包含：
            - media_type: 媒体类型
            - file_path: 文件路径
            - file_size: 文件大小（字节）
            - format: 媒体格式
            - width/height: 分辨率
            - 图片特有: aspect_ratio, exif
            - 视频特有: duration_seconds, fps, codec

        Raises:
            ValueError: 不支持的 media_type
            FileNotFoundError: 文件不存在
        """
        if not os.path.isfile(file_path):  # noqa: PTH113
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = os.path.getsize(file_path)  # noqa: PTH202
        base: dict[str, Any] = {
            "media_type": media_type,
            "file_path": file_path,
            "file_size": file_size,
        }

        if media_type == "image":
            return self._get_image_metadata(file_path, base)
        if media_type == "video":
            return self._get_video_metadata(file_path, base)

        raise ValueError(f"不支持的媒体类型: {media_type}，仅支持 'image' 和 'video'")

    def extract_video_thumbnails(
        self,
        video_path: str,
        interval: float = 5.0,
        output_dir: str | None = None,
    ) -> list[str]:
        """从视频中提取缩略图。

        委托给 VideoReviewer.extract_keyframes 执行。

        Args:
            video_path: 视频文件路径
            interval: 提取间隔（秒），默认 5.0
            output_dir: 输出目录，为 None 时与视频同目录

        Returns:
            缩略图文件路径列表

        Raises:
            FileNotFoundError: 视频文件不存在
        """
        return VideoReviewer.extract_keyframes(
            file_path=video_path,
            interval_seconds=interval,
            output_dir=output_dir,
        )

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_image_metadata(file_path: str, base: dict[str, Any]) -> dict[str, Any]:
        """提取图片元数据并合并到 base 字典。

        Args:
            file_path: 图片文件路径
            base: 基础元数据字典

        Returns:
            合并后的元数据字典
        """
        from PIL import Image  # noqa: PLC0415

        try:
            img = Image.open(file_path)
            img.load()
            width, height = img.size
            fmt = img.format or ""
            aspect_ratio = round(width / height, 4) if height > 0 else 0.0

            # 提取 EXIF
            exif: dict[str, Any] = {}
            try:
                raw_exif = img.getexif()
                if raw_exif:
                    for tag_id, value in raw_exif.items():
                        from PIL.ExifTags import Base as ExifBase  # noqa: PLC0415

                        tag_name = ExifBase(tag_id).name
                        if isinstance(value, bytes):
                            continue
                        exif[tag_name] = value
            except Exception:
                pass

            base.update(
                {
                    "format": fmt,
                    "width": width,
                    "height": height,
                    "aspect_ratio": aspect_ratio,
                    "exif": exif,
                }
            )
        except Exception as exc:
            logger.warning(
                "[MediaReviewService] 图片元数据提取失败 | path=%s | error=%s",
                file_path,
                exc,
            )
            base["error"] = f"无法读取图片: {exc}"

        return base

    @staticmethod
    def _get_video_metadata(file_path: str, base: dict[str, Any]) -> dict[str, Any]:
        """提取视频元数据并合并到 base 字典。

        Args:
            file_path: 视频文件路径
            base: 基础元数据字典

        Returns:
            合并后的元数据字典
        """
        metadata = VideoReviewer._extract_metadata(file_path)

        if metadata is None:
            base["error"] = "无法解析视频文件"
            return base

        base.update(
            {
                "format": metadata.get("format", ""),
                "duration_seconds": metadata.get("duration_seconds", 0.0),
                "width": metadata.get("width", 0),
                "height": metadata.get("height", 0),
                "fps": metadata.get("fps", 0.0),
                "codec": metadata.get("codec", ""),
            }
        )
        return base
