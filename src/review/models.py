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
        metadata: 扩展元数据
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
