"""工作空间服务。"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from workspace.models import FileTreeNode, Workspace

logger = logging.getLogger(__name__)

# ── GAP-1 统一：state 聚合读取器（on_load 注入）──
# 约定签名：``() -> list[dict]``（sync 或 async，管道 state 聚合行，行为扁平点号键
# 如 {"pipeline_id": ..., "task.scope": ..., "lineage.parent_pipeline_id": ...}）。
# None = 未注入（回退 task_service 只读镜像）。
_state_reader: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    """获取 state 聚合读取器（None = 未注入）。"""
    return _state_reader

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

    async def resolve_workspace_from_state(self, pipeline_id: str) -> str | None:
        """按 pipeline_id 从 state 聚合行解析工作区坐标（非任务管道关联通道）。

        行为扁平键（内核 STATE_SUMMARY_KEYS 出口 ``workspace``/``ws_meta``）：
        ``ws_meta`` 对象取 ``.path``（worktree 副本或 plain 目录，非 project_root），
        回退 ``workspace`` 标量。读面未注入/无命中/命中行无工作区键 → None。
        """
        reader = _get_state_reader()
        if reader is None:
            return None
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
            if not isinstance(rows, list):
                return None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("pipeline_id") or "") != pipeline_id:
                    continue
                ws_meta = row.get("ws_meta")
                if isinstance(ws_meta, dict) and ws_meta.get("path"):
                    return str(ws_meta["path"])
                ws = row.get("workspace")
                if isinstance(ws, str) and ws:
                    return ws
                return None
            return None
        except Exception as exc:  # noqa: BLE001 — 解析失败回退 task_service 镜像
            logger.warning(
                "[WorkspaceService] state 行工作区解析失败 | pipeline=%s | err=%s",
                pipeline_id,
                exc,
            )
            return None

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
        """解析任务到容器任务（GAP-1 统一：父链 = lineage.parent_pipeline_id）。

        读 state 聚合行（task = pipeline）：无父（根形式）→ 自身；task.scope ==
        container → 自身；否则沿 lineage.parent_pipeline_id 向上找最近的容器
        任务。读面未注入回退 task_service 只读镜像。
        """
        try:
            rows = await self._read_state_rows()
            if rows is None:
                return await self._resolve_container_task_legacy(task_id)

            by_id = {str(r.get("pipeline_id") or ""): r for r in rows}
            current_id = task_id
            visited: set[str] = set()
            while current_id and current_id not in visited:
                visited.add(current_id)
                row = by_id.get(current_id)
                if row is None:
                    break
                if not str(row.get("lineage.parent_pipeline_id") or ""):
                    return current_id  # 根形式：自身即根/容器任务
                if str(row.get("task.scope") or "") == "container":
                    return current_id
                current_id = str(row.get("lineage.parent_pipeline_id") or "")
            return task_id
        except Exception:
            logger.warning("[WorkspaceService] 解析容器任务失败 | task_id=%s", task_id)
            return task_id

    async def _resolve_container_task_legacy(self, task_id: str) -> str:
        """回退：task_service 只读镜像路径（读面未注入时的存量兼容）。"""
        try:
            from tasks.service_access import get_task_service  # noqa: PLC0415

            task_service = get_task_service()
            if task_service is None:
                return task_id
            task = await asyncio.to_thread(task_service.get_task, task_id)
            if not task:
                return task_id
            if not task.parent_task_id:
                return task_id
            if task.metadata.get("is_container"):
                return task_id
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
            return task_id

    async def _get_child_task_ids(self, container_task_id: str) -> set[str]:
        """获取容器任务下所有子任务 ID（GAP-1 统一：子链 = lineage 分组）。"""
        try:
            rows = await self._read_state_rows()
            if rows is None:
                return await self._get_child_task_ids_legacy(container_task_id)

            children_of: dict[str, list[str]] = {}
            for r in rows:
                pid = str(r.get("pipeline_id") or "")
                parent = str(r.get("lineage.parent_pipeline_id") or "")
                if pid and parent:
                    children_of.setdefault(parent, []).append(pid)

            child_ids: set[str] = set()
            visited: set[str] = set()
            queue = [container_task_id]
            while queue:
                parent_id = queue.pop(0)
                if parent_id in visited:
                    continue
                visited.add(parent_id)
                for cid in children_of.get(parent_id, []):
                    child_ids.add(cid)
                    queue.append(cid)
            return child_ids
        except Exception as e:
            # 子任务聚合失败静默空集会让容器制品列表只剩自身条目——留痕可观测
            logger.warning(
                "[workspace] 子任务聚合失败（返回空集，制品列表可能不完整）| container_task_id=%s | error=%s",
                container_task_id,
                e,
            )
            return set()

    async def _get_child_task_ids_legacy(self, container_task_id: str) -> set[str]:
        """回退：task_service 只读镜像路径。"""
        try:
            from tasks.service_access import get_task_service  # noqa: PLC0415

            task_service = get_task_service()
            if task_service is None:
                return {container_task_id}
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
        except Exception as e:
            # legacy 路径同款留痕（218/241 两条聚合路径口径一致）
            logger.warning(
                "[workspace] 子任务聚合失败 legacy（返回空集，制品列表可能不完整）| container_task_id=%s | error=%s",
                container_task_id,
                e,
            )
            return set()

    async def _read_state_rows(self) -> list[dict[str, Any]] | None:
        """读管道 state 聚合行（pipeline-state.list；None = 桥未就绪）。"""
        reader = _get_state_reader()
        if reader is None:
            return None
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001 — 读面降级不崩
            logger.warning("[WorkspaceService] state 聚合读取失败: %s", exc)
            return None

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

    # 文件树递归深度的安全兜底：仅用于防范符号链接环、超深嵌套导致的死循环或栈溢出，
    # 不作为用户可见的嵌套层级限制。
    _SCAN_MAX_DEPTH_SAFETY_CAP = 50

    def _scan_directory(
        self,
        path: str,
        base_path: str,
        max_depth: int = _SCAN_MAX_DEPTH_SAFETY_CAP,
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
