"""Worktree 自动清理模块。

管理工作树（worktree）的创建和自动清理，确保在任务完成或取消后
释放工作树资源。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class WorktreeEntry:
    """工作树条目。"""

    def __init__(
        self,
        tree_id: str = "",
        path: str = "",
        task_id: str = "",
        status: str = "active",
        created_at: float = 0.0,
    ) -> None:
        self.tree_id = tree_id or str(uuid.uuid4())
        self.path = path
        self.task_id = task_id
        self.status = status
        self.created_at = created_at or time.time()
        self.cleaned_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tree_id": self.tree_id,
            "path": self.path,
            "task_id": self.task_id,
            "status": self.status,
            "created_at": self.created_at,
            "cleaned_at": self.cleaned_at,
        }


class WorktreeManager:
    """工作树管理器。

    管理工作树的创建、查询和自动清理。
    支持按任务完成/取消事件自动清理对应的工作树。
    """

    CLEANUP_DELAY = 60  # 清理延迟（秒）
    MAX_AGE = 86400     # 最大存活时间（秒）

    def __init__(self) -> None:
        self._entries: dict[str, WorktreeEntry] = {}
        self._task_map: dict[str, str] = {}  # task_id -> tree_id

    def create(self, task_id: str, path: str = "") -> WorktreeEntry:
        """创建工作树。"""
        entry = WorktreeEntry(
            path=path or f"/tmp/worktree/{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            status="active",
        )
        self._entries[entry.tree_id] = entry
        self._task_map[task_id] = entry.tree_id
        logger.info("[Worktree] 创建: tree=%s task=%s", entry.tree_id[:12], task_id[:12])
        return entry

    def get_by_task(self, task_id: str) -> WorktreeEntry | None:
        """按任务 ID 查找工作树。"""
        tree_id = self._task_map.get(task_id)
        if tree_id:
            return self._entries.get(tree_id)
        return None

    def get(self, tree_id: str) -> WorktreeEntry | None:
        """按 ID 查找工作树。"""
        return self._entries.get(tree_id)

    def cleanup(self, tree_id: str) -> bool:
        """清理指定工作树。"""
        entry = self._entries.get(tree_id)
        if entry is None:
            return False
        if entry.status == "cleaned":
            return True

        entry.status = "cleaned"
        entry.cleaned_at = time.time()
        logger.info("[Worktree] 清理: tree=%s", tree_id[:12])
        return True

    def cleanup_by_task(self, task_id: str) -> bool:
        """按任务 ID 清理工作树。"""
        entry = self.get_by_task(task_id)
        if entry:
            return self.cleanup(entry.tree_id)
        return False

    def auto_cleanup_stale(self) -> list[str]:
        """自动清理超时的工作树。

        Returns:
            被清理的工作树 ID 列表。
        """
        now = time.time()
        cleaned: list[str] = []
        for entry in list(self._entries.values()):
            if entry.status == "active" and (now - entry.created_at) > self.MAX_AGE:
                self.cleanup(entry.tree_id)
                cleaned.append(entry.tree_id)
        return cleaned

    @property
    def active_count(self) -> int:
        """活跃工作树数量。"""
        return sum(1 for e in self._entries.values() if e.status == "active")

    @property
    def total_count(self) -> int:
        """总工作树数量。"""
        return len(self._entries)

    def list_active(self) -> list[WorktreeEntry]:
        """列出所有活跃工作树。"""
        return [e for e in self._entries.values() if e.status == "active"]
