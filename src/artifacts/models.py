"""制品与批注数据模型。

定义 Artifact、ArtifactType、Annotation、AnnotationTarget 等核心数据结构。
采用 dataclass 定义，纯内存存储，不引入 ORM。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


class ArtifactType(str, Enum):
    """制品类型枚举。"""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    CODE = "code"
    DOCUMENT = "document"
    DATA = "data"
    COMPOSITE = "composite"


class AnnotationTarget(str, Enum):
    """批注目标类型枚举。"""

    TEXT_SELECTION = "text_selection"
    IMAGE_REGION = "image_region"
    VIDEO_TIMELINE = "video_timeline"
    WHOLE_ARTIFACT = "whole_artifact"


class AnnotationStatus(str, Enum):
    """批注状态枚举。"""

    ACTIVE = "active"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


def _new_id() -> str:
    """生成唯一标识（UUID hex 前 12 位）。"""
    return uuid4().hex[:12]


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


@dataclass
class Artifact:
    """制品数据模型。

    Attributes:
        id: 唯一标识
        task_id: 关联任务 ID
        title: 制品标题
        artifact_type: 制品类型
        content: 制品内容（文本类直接存内容；二进制类存文件路径或 URL）
        file_path: 沙盒文件路径（可选）
        version: 版本号，从 1 开始递增
        parent_artifact_id: 前一版本制品 ID（版本链追踪）
        metadata: 扩展元数据
        created_at: 创建时间
        updated_at: 更新时间
    """

    id: str = field(default_factory=_new_id)
    task_id: str = ""
    title: str = ""
    artifact_type: ArtifactType = ArtifactType.TEXT
    content: str = ""
    file_path: str | None = None
    version: int = 1
    parent_artifact_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "title": self.title,
            "artifact_type": self.artifact_type.value,
            "content": self.content,
            "file_path": self.file_path,
            "version": self.version,
            "parent_artifact_id": self.parent_artifact_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        """从字典反序列化。"""
        artifact_type = data.get("artifact_type", "text")
        if isinstance(artifact_type, str):
            artifact_type = ArtifactType(artifact_type)
        return cls(
            id=data.get("id", _new_id()),
            task_id=data.get("task_id", ""),
            title=data.get("title", ""),
            artifact_type=artifact_type,
            content=data.get("content", ""),
            file_path=data.get("file_path"),
            version=data.get("version", 1),
            parent_artifact_id=data.get("parent_artifact_id"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
        )


@dataclass
class Annotation:
    """批注数据模型。

    Attributes:
        id: 唯一标识
        artifact_id: 关联制品 ID
        target_type: 批注目标类型
        target_data: 批注位置数据（结构因 target_type 而异）
        content: 批注文本内容
        author_type: 作者类型（"user" / "agent"）
        author_id: 作者标识
        status: 批注状态
        created_at: 创建时间
        resolved_at: 解决时间
    """

    id: str = field(default_factory=_new_id)
    artifact_id: str = ""
    target_type: AnnotationTarget = AnnotationTarget.WHOLE_ARTIFACT
    target_data: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    author_type: str = "user"
    author_id: str = ""
    status: AnnotationStatus = AnnotationStatus.ACTIVE
    created_at: str = field(default_factory=_now_iso)
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "id": self.id,
            "artifact_id": self.artifact_id,
            "target_type": self.target_type.value,
            "target_data": self.target_data,
            "content": self.content,
            "author_type": self.author_type,
            "author_id": self.author_id,
            "status": self.status.value,
            "created_at": self.created_at,
        }
        if self.resolved_at is not None:
            result["resolved_at"] = self.resolved_at
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Annotation:
        """从字典反序列化。"""
        target_type = data.get("target_type", "whole_artifact")
        if isinstance(target_type, str):
            target_type = AnnotationTarget(target_type)
        status = data.get("status", "active")
        if isinstance(status, str):
            status = AnnotationStatus(status)
        return cls(
            id=data.get("id", _new_id()),
            artifact_id=data.get("artifact_id", ""),
            target_type=target_type,
            target_data=data.get("target_data", {}),
            content=data.get("content", ""),
            author_type=data.get("author_type", "user"),
            author_id=data.get("author_id", ""),
            status=status,
            created_at=data.get("created_at", _now_iso()),
            resolved_at=data.get("resolved_at"),
        )
