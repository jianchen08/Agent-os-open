"""工作空间服务。"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from workspace.models import FileTreeNode, Workspace

logger = logging.getLogger(__name__)

# 全局单例
_workspace_service: WorkspaceService | None = None


def get_workspace_service() -> WorkspaceService:
    """获取全局工作空间服务单例。"""
    global _workspace_service  # noqa: PLW0603
    if _workspace_service is None:
        _workspace_service = WorkspaceService()
    return _workspace_service


def reset_workspace_service() -> None:
    """重置全局单例（测试用）。"""
    global _workspace_service  # noqa: PLW0603
    _workspace_service = None


class WorkspaceService:
    """工作空间服务（纯内存版）。"""

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}

    async def get_or_create_workspace(
        self,
        container_task_id: str,
        session_id: str = "",
        title: str = "",
        description: str = "",
    ) -> Workspace:
        """获取或创建工作空间。"""
        ws = self._workspaces.get(container_task_id)
        if ws:
            return ws

        workspace = Workspace(
            container_task_id=container_task_id,
            session_id=session_id,
            title=title or f"工作空间-{container_task_id[:8]}",
            description=description,
        )

        self._workspaces[container_task_id] = workspace
        logger.info(
            "[WorkspaceService] 创建工作空间 | id=%s | container_task_id=%s",
            workspace.id,
            container_task_id,
        )
        return workspace

    async def get_workspace(self, container_task_id: str) -> Workspace | None:
        """获取工作空间。"""
        return self._workspaces.get(container_task_id)

    async def list_artifacts_by_workspace(
        self,
        container_task_id: str,
    ) -> dict[str, Any]:
        """聚合工作空间下所有制品。"""
        ws = self._workspaces.get(container_task_id)
        if not ws:
            return {"items": [], "total": 0}

        # 延迟导入避免循环依赖
        from artifacts.artifact_service import get_artifact_service  # noqa: PLC0415

        artifact_service = get_artifact_service()

        items: list[dict[str, Any]] = []

        # 获取容器任务关联的所有子任务
        task_ids = await self._get_child_task_ids(container_task_id)
        # 包含容器任务自身
        task_ids.add(container_task_id)

        for task_id in task_ids:
            result = await artifact_service.list_artifacts_by_task(task_id, limit=100)
            items.extend(result["items"])

        return {"items": items, "total": len(items)}

    async def get_file_tree(
        self,
        container_task_id: str,
        base_path: str | None = None,
    ) -> dict[str, Any]:
        """生成文件目录树。"""
        if base_path and os.path.isdir(base_path):  # noqa: PTH112
            tree = await asyncio.to_thread(self._scan_directory, base_path, base_path)
        else:
            tree = []

        # 更新工作空间的文件树缓存
        ws = self._workspaces.get(container_task_id)
        if ws:
            ws.file_tree = tree
            from datetime import UTC, datetime  # noqa: PLC0415

            ws.updated_at = datetime.now(UTC).isoformat()

        return {"tree": [n.to_dict() for n in tree]}

    async def resolve_container_task(self, task_id: str) -> str:
        """解析任务到容器任务。"""
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
            task_service = provider.get_or_create(
                "task_service",
                lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
            )

            task = await asyncio.to_thread(task_service.get_task, task_id)
            if not task:
                return task_id

            # 策略 1: 无父任务 → 本身就是根任务/容器任务
            if not task.parent_task_id:
                return task_id

            # 策略 2: 有容器标记
            if task.metadata.get("is_container"):
                return task_id

            # 策略 3: 向上递归
            current = task
            visited = {task_id}
            while current.parent_task_id and current.parent_task_id not in visited:
                visited.add(current.parent_task_id)
                parent = await asyncio.to_thread(task_service.get_task, current.parent_task_id)
                if not parent:
                    break
                current = parent

            return current.id

        except Exception:
            logger.warning("[WorkspaceService] 解析容器任务失败 | task_id=%s", task_id)
            return task_id

    async def _get_child_task_ids(self, container_task_id: str) -> set[str]:
        """获取容器任务下所有子任务 ID。"""
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
            task_service = provider.get_or_create(
                "task_service",
                lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
            )

            child_ids: set[str] = set()
            visited: set[str] = set()
            queue = [container_task_id]

            while queue:
                parent_id = queue.pop(0)
                if parent_id in visited:
                    continue
                visited.add(parent_id)
                subtasks = task_service.list_subtasks(parent_id)
                for t in subtasks:
                    child_ids.add(t.id)
                    queue.append(t.id)

            return child_ids
        except Exception:
            return set()

    _WINDOWS_RESERVED_NAMES = frozenset(
        {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM2",
            "COM3",
            "COM4",
            "COM5",
            "COM6",
            "COM7",
            "COM8",
            "COM9",
            "LPT1",
            "LPT2",
            "LPT3",
            "LPT4",
            "LPT5",
            "LPT6",
            "LPT7",
            "LPT8",
            "LPT9",
        }
    )

    def _scan_directory(
        self,
        path: str,
        base_path: str,
        max_depth: int = 5,
        current_depth: int = 0,
    ) -> list[FileTreeNode]:
        """扫描目录生成文件树。"""
        if current_depth >= max_depth:
            return []

        nodes: list[FileTreeNode] = []
        try:
            entries = sorted(os.listdir(path))  # noqa: PTH208
        except (PermissionError, OSError):
            return []

        for entry in entries:
            if entry == "__pycache__" or entry.startswith("."):
                continue

            stem = entry.split(".")[0].upper()
            if stem in self._WINDOWS_RESERVED_NAMES:
                continue

            full_path = os.path.join(path, entry)

            if full_path.startswith("\\\\.\\"):
                continue

            try:
                rel_path = os.path.relpath(full_path, base_path)
            except ValueError:
                continue

            if os.path.isdir(full_path):  # noqa: PTH112
                children = self._scan_directory(full_path, base_path, max_depth, current_depth + 1)
                nodes.append(
                    FileTreeNode(
                        name=entry,
                        type="directory",
                        path=rel_path,
                        children=children,
                    )
                )
            else:
                nodes.append(
                    FileTreeNode(
                        name=entry,
                        type="file",
                        path=rel_path,
                    )
                )

        return nodes
