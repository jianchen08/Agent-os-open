"""工作空间数据模型。

定义 Workspace、FileTreeNode 等核心数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _new_id() -> str:
    """生成唯一标识（UUID hex 前 12 位）。"""
    return uuid4().hex[:12]


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


@dataclass
class FileTreeNode:
    """文件树节点。

    Attributes:
        name: 文件/目录名称
        type: 节点类型（file / directory）
        path: 完整路径
        artifact_id: 关联制品 ID（可选，如果该文件对应一个制品）
        children: 子节点列表
        metadata: 扩展元数据
    """

    name: str = ""
    type: str = "file"
    path: str = ""
    artifact_id: str | None = None
    children: list[FileTreeNode] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "path": self.path,
        }
        if self.artifact_id is not None:
            result["artifact_id"] = self.artifact_id
        if self.children:
            result["children"] = [c.to_dict() for c in self.children]
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileTreeNode:
        """从字典反序列化。"""
        children = [cls.from_dict(c) for c in data.get("children", [])]
        return cls(
            name=data.get("name", ""),
            type=data.get("type", "file"),
            path=data.get("path", ""),
            artifact_id=data.get("artifact_id"),
            children=children,
            metadata=data.get("metadata", {}),
        )


@dataclass
class Workspace:
    """工作空间数据模型。

    与容器任务 1:1 关系，聚合其下所有子任务的制品。

    Attributes:
        id: 唯一标识
        container_task_id: 容器任务 ID
        session_id: 关联会话 ID
        title: 工作空间标题
        description: 工作空间描述
        file_tree: 文档目录结构
        created_at: 创建时间
        updated_at: 更新时间
    """

    id: str = field(default_factory=_new_id)
    container_task_id: str = ""
    session_id: str = ""
    title: str = ""
    description: str = ""
    file_tree: list[FileTreeNode] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "id": self.id,
            "container_task_id": self.container_task_id,
            "session_id": self.session_id,
            "title": self.title,
            "description": self.description,
            "file_tree": [n.to_dict() for n in self.file_tree],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Workspace:
        """从字典反序列化。"""
        file_tree = [FileTreeNode.from_dict(n) for n in data.get("file_tree", [])]
        return cls(
            id=data.get("id", _new_id()),
            container_task_id=data.get("container_task_id", ""),
            session_id=data.get("session_id", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            file_tree=file_tree,
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
        )
