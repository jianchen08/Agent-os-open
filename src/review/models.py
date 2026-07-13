"""审批数据模型。

定义 ReviewStatus、ReviewRequest、ReviewFeedback 等核心数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


class ReviewStatus(str, Enum):
    """审批状态枚举。"""

    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PARTIALLY_APPROVED = "partially_approved"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _new_id() -> str:
    """生成唯一标识（UUID hex 前 12 位）。"""
    return uuid4().hex[:12]


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


@dataclass
class ReviewRequest:
    """审批请求数据模型。

    Attributes:
        id: 唯一标识
        task_id: 关联任务 ID
        thread_id: 关联会话线程 ID
        session_id: 关联会话 ID
        tab_id: 前端目标 Tab ID
        title: 审批标题
        description: 审批描述
        artifact_ids: 关联制品 ID 列表
        status: 审批状态
        priority: 优先级
        timeout_seconds: 超时时间（秒）
        created_at: 创建时间
        updated_at: 更新时间
        reviewed_at: 用户开始审查时间
        completed_at: 审批完成时间
        metadata: 扩展元数据，支持以下子字段：
            - media_review_results: dict，媒体审阅结果（文件路径 → 审阅结果字典）
            - media_files: list[dict]，媒体附件列表（每项含 path 和 media_type）
            - cancel_reason: str，取消原因
    """

    id: str = field(default_factory=_new_id)
    task_id: str = ""
    thread_id: str = ""
    session_id: str = ""
    tab_id: str = ""
    title: str = ""
    description: str = ""
    artifact_ids: list[str] = field(default_factory=list)
    status: ReviewStatus = ReviewStatus.PENDING
    priority: str = "normal"
    timeout_seconds: float = 86400.0
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    reviewed_at: str | None = None
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "id": self.id,
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "tab_id": self.tab_id,
            "title": self.title,
            "description": self.description,
            "artifact_ids": self.artifact_ids,
            "status": self.status.value,
            "priority": self.priority,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }
        if self.reviewed_at is not None:
            result["reviewed_at"] = self.reviewed_at
        if self.completed_at is not None:
            result["completed_at"] = self.completed_at
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewRequest:
        """从字典反序列化。"""
        status = data.get("status", "pending")
        if isinstance(status, str):
            status = ReviewStatus(status)
        return cls(
            id=data.get("id", _new_id()),
            task_id=data.get("task_id", ""),
            thread_id=data.get("thread_id", ""),
            session_id=data.get("session_id", ""),
            tab_id=data.get("tab_id", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            artifact_ids=data.get("artifact_ids", []),
            status=status,
            priority=data.get("priority", "normal"),
            timeout_seconds=data.get("timeout_seconds", 86400.0),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
            reviewed_at=data.get("reviewed_at"),
            completed_at=data.get("completed_at"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ReviewFeedback:
    """审批反馈数据模型。

    Attributes:
        id: 唯一标识
        review_request_id: 关联审批请求 ID
        response_type: 响应类型（approved/denied/answered 等）
        overall_comment: 整体评论
        annotations: 批注列表
        user_id: 用户标识
        created_at: 创建时间
    """

    id: str = field(default_factory=_new_id)
    review_request_id: str = ""
    response_type: str = "approved"
    overall_comment: str = ""
    annotations: list[dict[str, Any]] = field(default_factory=list)
    user_id: str | None = None
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "id": self.id,
            "review_request_id": self.review_request_id,
            "response_type": self.response_type,
            "overall_comment": self.overall_comment,
            "annotations": self.annotations,
            "created_at": self.created_at,
        }
        if self.user_id is not None:
            result["user_id"] = self.user_id
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewFeedback:
        """从字典反序列化。"""
        return cls(
            id=data.get("id", _new_id()),
            review_request_id=data.get("review_request_id", ""),
            response_type=data.get("response_type", "approved"),
            overall_comment=data.get("overall_comment", ""),
            annotations=data.get("annotations", []),
            user_id=data.get("user_id"),
            created_at=data.get("created_at", _now_iso()),
        )


# ---------------------------------------------------------------------------
# 媒体审阅数据模型
# ---------------------------------------------------------------------------

_DEFAULT_IMAGE_FORMATS: list[str] = ["JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF"]
_DEFAULT_VIDEO_FORMATS: list[str] = ["MP4", "AVI", "MOV", "MKV", "WEBM"]


@dataclass
class ImageReviewResult:
    """图片审阅结果。

    Attributes:
        is_valid: 是否通过审阅（无 error）
        format: 图片格式（JPEG / PNG / GIF / WebP / BMP / TIFF）
        width: 图片宽度（像素）
        height: 图片高度（像素）
        aspect_ratio: 宽高比（width / height）
        exif: EXIF 元数据字典
        warnings: 审阅警告列表
        errors: 审阅错误列表
    """

    is_valid: bool
    format: str = ""
    width: int = 0
    height: int = 0
    aspect_ratio: float = 0.0
    exif: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "is_valid": self.is_valid,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "aspect_ratio": self.aspect_ratio,
            "exif": self.exif,
            "warnings": self.warnings,
            "errors": self.errors,
        }


@dataclass
class VideoReviewResult:
    """视频审阅结果。

    Attributes:
        is_valid: 是否通过审阅（无 error）
        format: 容器格式（MP4 / AVI / MOV / MKV / WebM）
        duration_seconds: 时长（秒）
        width: 视频宽度（像素）
        height: 视频高度（像素）
        fps: 帧率
        codec: 视频编解码器名称
        warnings: 审阅警告列表
        errors: 审阅错误列表
    """

    is_valid: bool
    format: str = ""
    duration_seconds: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "is_valid": self.is_valid,
            "format": self.format,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "codec": self.codec,
            "warnings": self.warnings,
            "errors": self.errors,
        }


@dataclass
class MediaReviewConfig:
    """媒体审阅配置。

    Attributes:
        image_min_width: 图片最小宽度（像素）
        image_max_width: 图片最大宽度（像素）
        image_min_height: 图片最小高度（像素）
        image_max_height: 图片最大高度（像素）
        allowed_image_formats: 允许的图片格式列表
        video_min_duration: 视频最短时长（秒）
        video_max_duration: 视频最长时长（秒）
        allowed_video_formats: 允许的视频格式列表
    """

    image_min_width: int = 1
    image_max_width: int = 7680
    image_min_height: int = 1
    image_max_height: int = 4320
    allowed_image_formats: list[str] = field(default_factory=lambda: list(_DEFAULT_IMAGE_FORMATS))
    video_min_duration: float = 0.0
    video_max_duration: float = 3600.0
    allowed_video_formats: list[str] = field(default_factory=lambda: list(_DEFAULT_VIDEO_FORMATS))
