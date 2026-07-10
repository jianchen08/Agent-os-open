"""多模态媒体审阅核心模块。

提供图片审阅（ImageReviewer）和视频审阅（VideoReviewer）能力，
包括格式验证、尺寸/时长检查、EXIF/元数据提取及关键帧提取。

所有方法均为同步方法，在 IO 密集场景下由调用方决定异步包装。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    import av  # PyAV — 可选依赖，仅视频审阅功能需要
except ImportError:
    av = None  # type: ignore[assignment]
try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

from review.models import (
    ImageReviewResult,
    MediaReviewConfig,
    VideoReviewResult,
)

logger = logging.getLogger(__name__)

# 格式映射：文件扩展名 → 标准格式名称
_EXTENSION_TO_FORMAT: dict[str, str] = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".tiff": "TIFF",
    ".tif": "TIFF",
    ".mp4": "MP4",
    ".avi": "AVI",
    ".mov": "MOV",
    ".mkv": "MKV",
    ".webm": "WEBM",
}

# PyAV 容器格式 → 标准格式名称
_CONTAINER_TO_FORMAT: dict[str, str] = {
    "mp4": "MP4",
    "avi": "AVI",
    "mov": "MOV",
    "mkv": "MKV",
    "webm": "WEBM",
}


class ImageReviewer:
    """图片审阅器。

    对图片文件执行格式验证、尺寸检查和 EXIF 提取，
    返回 ImageReviewResult 结构化结果。
    """

    @staticmethod
    def review(
        file_path: str,
        config: MediaReviewConfig | None = None,
    ) -> ImageReviewResult:
        """审阅图片文件。

        Args:
            file_path: 图片文件路径
            config: 审阅配置，为 None 时使用默认配置

        Returns:
            ImageReviewResult 审阅结果

        Raises:
            FileNotFoundError: 文件不存在
        """
        if not os.path.isfile(file_path):  # noqa: PTH113
            raise FileNotFoundError(f"文件不存在: {file_path}")

        cfg = config or MediaReviewConfig()
        errors: list[str] = []
        warnings: list[str] = []

        # 尝试打开图片
        try:
            img = Image.open(file_path)
            img.load()  # 确保完整加载
        except Exception as exc:
            logger.warning("[ImageReviewer] 无法打开图片 | path=%s | error=%s", file_path, exc)
            return ImageReviewResult(
                is_valid=False,
                errors=[f"无法识别的图片文件: {exc}"],
            )

        # ---- 格式验证 ----
        fmt = img.format or ""
        format_errors = ImageReviewer._validate_format(fmt, cfg)
        errors.extend(format_errors)

        # ---- 尺寸检查 ----
        width, height = img.size
        dim_errors, dim_warnings = ImageReviewer._check_dimensions(width, height, cfg)
        errors.extend(dim_errors)
        warnings.extend(dim_warnings)

        # ---- EXIF 提取 ----
        exif = ImageReviewer._extract_exif(img)

        # 计算宽高比
        aspect_ratio = width / height if height > 0 else 0.0

        is_valid = len(errors) == 0
        return ImageReviewResult(
            is_valid=is_valid,
            format=fmt,
            width=width,
            height=height,
            aspect_ratio=round(aspect_ratio, 4),
            exif=exif,
            warnings=warnings,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_format(fmt: str, config: MediaReviewConfig) -> list[str]:
        """验证图片格式是否在允许列表内。

        Args:
            fmt: Pillow 识别的格式名称（如 JPEG）
            config: 审阅配置

        Returns:
            错误列表
        """
        errors: list[str] = []
        if not fmt:
            errors.append("无法识别图片格式")
            return errors

        allowed = [f.upper() for f in config.allowed_image_formats]
        if fmt.upper() not in allowed:
            errors.append(f"不支持的图片格式: {fmt}，允许的格式: {', '.join(allowed)}")
        return errors

    @staticmethod
    def _check_dimensions(
        width: int,
        height: int,
        config: MediaReviewConfig,
    ) -> tuple[list[str], list[str]]:
        """检查图片尺寸是否符合配置限制。

        Args:
            width: 图片宽度
            height: 图片高度
            config: 审阅配置

        Returns:
            (错误列表, 警告列表) 的元组
        """
        errors: list[str] = []
        warnings: list[str] = []

        if width < config.image_min_width:
            errors.append(f"图片宽度 {width}px 小于最小限制 {config.image_min_width}px")
        if width > config.image_max_width:
            errors.append(f"图片宽度 {width}px 超过最大限制 {config.image_max_width}px")
        if height < config.image_min_height:
            errors.append(f"图片高度 {height}px 小于最小限制 {config.image_min_height}px")
        if height > config.image_max_height:
            errors.append(f"图片高度 {height}px 超过最大限制 {config.image_max_height}px")

        return errors, warnings

    @staticmethod
    def _extract_exif(img: Image.Image) -> dict[str, str | float | int]:
        """提取图片 EXIF 元数据。

        提取字段包括：相机厂商、型号、GPS 坐标、方向、拍摄时间、焦距、ISO 等。

        Args:
            img: PIL Image 对象

        Returns:
            EXIF 键值字典
        """
        exif_dict: dict[str, str | float | int] = {}
        try:
            raw_exif = img.getexif()
            if not raw_exif:
                return exif_dict

            # 直接取所有 EXIF 标签
            from PIL.ExifTags import Base as ExifBase  # noqa: PLC0415

            tag_map: dict[int, str] = {
                ExifBase.Make: "Make",
                ExifBase.Model: "Model",
                ExifBase.Orientation: "Orientation",
                ExifBase.DateTime: "DateTime",
                ExifBase.DateTimeOriginal: "DateTimeOriginal",
                ExifBase.FocalLength: "FocalLength",
                ExifBase.ISOSpeedRatings: "ISO",
                ExifBase.ExposureTime: "ExposureTime",
                ExifBase.FNumber: "FNumber",
                ExifBase.Software: "Software",
                ExifBase.ImageDescription: "ImageDescription",
                ExifBase.XResolution: "XResolution",
                ExifBase.YResolution: "YResolution",
                ExifBase.BitsPerSample: "BitsPerSample",
                ExifBase.ColorSpace: "ColorSpace",
                ExifBase.Flash: "Flash",
            }

            for tag_id, tag_name in tag_map.items():
                value = raw_exif.get(tag_id)
                if value is not None:
                    # 对浮点/分数字典做安全转换
                    if isinstance(value, tuple):
                        try:
                            from fractions import Fraction  # noqa: PLC0415

                            value = float(Fraction(value[0], value[1]))
                        except (ZeroDivisionError, TypeError, IndexError):
                            value = str(value)
                    exif_dict[tag_name] = value

            # GPS 信息单独提取
            from PIL.ExifTags import GPSTags  # noqa: PLC0415

            gps_info = raw_exif.get_ifd(ExifBase.GPSInfo)
            if gps_info:
                for gps_tag_id, gps_value in gps_info.items():
                    tag_name = GPSTags.get(gps_tag_id, f"GPS_{gps_tag_id}")
                    exif_dict[f"GPS_{tag_name}"] = gps_value

        except Exception as exc:
            logger.debug("[ImageReviewer] EXIF 提取异常: %s", exc)

        return exif_dict


class VideoReviewer:
    """视频审阅器。

    对视频文件执行格式验证、时长检查和元数据提取，
    返回 VideoReviewResult 结构化结果。还支持按时间间隔提取关键帧。
    """

    @staticmethod
    def review(
        file_path: str,
        config: MediaReviewConfig | None = None,
    ) -> VideoReviewResult:
        """审阅视频文件。

        Args:
            file_path: 视频文件路径
            config: 审阅配置，为 None 时使用默认配置

        Returns:
            VideoReviewResult 审阅结果

        Raises:
            FileNotFoundError: 文件不存在
        """
        if not os.path.isfile(file_path):  # noqa: PTH113
            raise FileNotFoundError(f"文件不存在: {file_path}")

        cfg = config or MediaReviewConfig()
        errors: list[str] = []
        warnings: list[str] = []

        # 提取元数据
        metadata_result = VideoReviewer._extract_metadata(file_path)

        if metadata_result is None:
            return VideoReviewResult(
                is_valid=False,
                errors=["无法解析视频文件"],
            )

        fmt = metadata_result["format"]
        duration = metadata_result["duration_seconds"]
        width = metadata_result["width"]
        height = metadata_result["height"]
        fps = metadata_result["fps"]
        codec = metadata_result["codec"]

        # ---- 格式验证 ----
        format_errors = VideoReviewer._validate_format(fmt, cfg)
        errors.extend(format_errors)

        # ---- 时长检查 ----
        duration_errors, duration_warnings = VideoReviewer._check_duration(duration, cfg)
        errors.extend(duration_errors)
        warnings.extend(duration_warnings)

        is_valid = len(errors) == 0
        return VideoReviewResult(
            is_valid=is_valid,
            format=fmt,
            duration_seconds=duration,
            width=width,
            height=height,
            fps=fps,
            codec=codec,
            warnings=warnings,
            errors=errors,
        )

    @staticmethod
    def extract_keyframes(
        file_path: str,
        interval_seconds: float = 5.0,
        output_dir: str | None = None,
    ) -> list[str]:
        """按时间间隔从视频中提取关键帧并保存为 JPEG 图片。

        Args:
            file_path: 视频文件路径
            interval_seconds: 提取间隔（秒），默认 5.0
            output_dir: 输出目录，为 None 时与视频同目录

        Returns:
            提取的关键帧图片路径列表

        Raises:
            FileNotFoundError: 文件不存在
        """
        if not os.path.isfile(file_path):  # noqa: PTH113
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 确定输出目录
        if output_dir is None:
            output_dir = str(Path(file_path).parent)
        os.makedirs(output_dir, exist_ok=True)  # noqa: PTH103

        video_stem = Path(file_path).stem
        extracted_paths: list[str] = []

        try:
            container = av.open(file_path)
            stream = container.streams.video[0]

            # 使用 stream.time_base 进行时间换算
            time_base = float(stream.time_base) if stream.time_base else 1.0 / 24.0
            float(stream.average_rate) if stream.average_rate else 24.0

            target_pts = 0.0
            frame_index = 0

            # 使用 decode 迭代
            container.seek(0)

            for frame in container.decode(video=0):
                current_time = float(frame.pts) * time_base if frame.pts is not None else 0.0

                if current_time >= target_pts:
                    output_filename = f"{video_stem}_keyframe_{frame_index:04d}.jpg"
                    output_path = os.path.join(output_dir, output_filename)
                    pil_image = frame.to_image()
                    pil_image.save(output_path, format="JPEG")
                    extracted_paths.append(output_path)

                    frame_index += 1
                    target_pts = current_time + interval_seconds

            container.close()

        except Exception as exc:
            logger.error(
                "[VideoReviewer] 关键帧提取失败 | path=%s | error=%s",
                file_path,
                exc,
            )
            raise

        return extracted_paths

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_format(file_path: str, container_format_name: str) -> str:
        """从文件扩展名或容器格式列表中解析标准格式名称。

        PyAV 对 MP4 容器返回 ``"mov,mp4,m4a,3gp,3g2,mj2"``，
        单纯取第一项会得到 ``mov``。因此优先用文件扩展名匹配，
        其次遍历容器格式列表逐一匹配。

        Args:
            file_path: 视频文件路径
            container_format_name: PyAV 的 ``container.format.name``

        Returns:
            标准格式名称（如 MP4）
        """
        # 优先使用文件扩展名
        ext = Path(file_path).suffix.lower()
        ext_fmt = _EXTENSION_TO_FORMAT.get(ext)
        if ext_fmt and ext_fmt in _CONTAINER_TO_FORMAT.values():
            return ext_fmt

        # 从容器格式列表中匹配
        parts = [p.strip().lower() for p in container_format_name.split(",")]
        for part in parts:
            for container_key, standard_name in _CONTAINER_TO_FORMAT.items():
                if part == container_key:
                    return standard_name

        # 兜底：返回原始格式名（大写）
        return parts[0].upper() if parts else ""

    @staticmethod
    def _validate_format(fmt: str, config: MediaReviewConfig) -> list[str]:
        """验证视频格式是否在允许列表内。

        Args:
            fmt: 标准格式名称（如 MP4）
            config: 审阅配置

        Returns:
            错误列表
        """
        errors: list[str] = []
        if not fmt:
            errors.append("无法识别视频格式")
            return errors

        allowed = [f.upper() for f in config.allowed_video_formats]
        if fmt.upper() not in allowed:
            errors.append(f"不支持的视频格式: {fmt}，允许的格式: {', '.join(allowed)}")
        return errors

    @staticmethod
    def _check_duration(
        duration: float,
        config: MediaReviewConfig,
    ) -> tuple[list[str], list[str]]:
        """检查视频时长是否在配置范围内。

        Args:
            duration: 视频时长（秒）
            config: 审阅配置

        Returns:
            (错误列表, 警告列表) 的元组
        """
        errors: list[str] = []
        warnings: list[str] = []

        if duration < config.video_min_duration:
            errors.append(f"视频时长 {duration:.2f}s 小于最短限制 {config.video_min_duration}s")
        if duration > config.video_max_duration:
            errors.append(f"视频时长 {duration:.2f}s 超过最长限制 {config.video_max_duration}s")

        return errors, warnings

    @staticmethod
    def _extract_metadata(
        file_path: str,
    ) -> dict[str, str | float | int] | None:
        """从视频文件中提取元数据。

        使用 PyAV 解析容器格式、分辨率、帧率、编解码器、时长。

        Args:
            file_path: 视频文件路径

        Returns:
            元数据字典，解析失败返回 None
        """
        try:
            container = av.open(file_path)
        except Exception as exc:
            logger.warning("[VideoReviewer] 无法打开视频 | path=%s | error=%s", file_path, exc)
            return None

        try:
            video_streams = container.streams.video
            if not video_streams:
                container.close()
                return None

            stream = video_streams[0]

            # 格式名称：优先使用文件扩展名匹配，其次从容器格式列表中匹配
            fmt = VideoReviewer._resolve_format(file_path, container.format.name)

            # 时长（秒）
            duration = 0.0
            if container.duration is not None:
                duration = float(container.duration) / av.time_base
            elif stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)

            # 分辨率
            width = stream.width or 0
            height = stream.height or 0

            # 帧率
            fps = 0.0
            if stream.average_rate is not None:
                fps = float(stream.average_rate)

            # 编解码器
            codec = stream.codec_context.name if stream.codec_context else ""

            container.close()

            return {
                "format": fmt,
                "duration_seconds": round(duration, 4),
                "width": width,
                "height": height,
                "fps": round(fps, 4),
                "codec": codec,
            }

        except Exception as exc:
            logger.warning("[VideoReviewer] 元数据提取异常 | path=%s | error=%s", file_path, exc)
            container.close()
            return None
