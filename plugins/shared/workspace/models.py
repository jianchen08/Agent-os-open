"""工作空间数据模型。

定义 Workspace 等核心数据结构。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

# 共享层时间戳公共模块（plugins/shared 平铺，stdlib-only）裸名导入
# （先例：task_submit/tool.py 的 _SHARED_ROOT 注入）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from _root_api import now_iso_utc as _now_iso  # noqa: E402


def _new_id() -> str:
    """生成唯一标识（UUID hex 前 12 位）。"""
    return uuid4().hex[:12]


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
        created_at: 创建时间
        updated_at: 更新时间
    """

    id: str = field(default_factory=_new_id)
    container_task_id: str = ""
    session_id: str = ""
    title: str = ""
    description: str = ""
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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Workspace:
        """从字典反序列化。"""
        return cls(
            id=data.get("id", _new_id()),
            container_task_id=data.get("container_task_id", ""),
            session_id=data.get("session_id", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
        )
