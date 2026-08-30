"""工作空间服务。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from workspace.models import FileTreeNode, Workspace

logger = logging.getLogger(__name__)


def _relative_under(target: Path, root_dir: str) -> Path | None:
    """target 位于 root_dir 之下时返回其相对路径，否则 None。

    两侧路径同源（agent 注入的 workspace 与 ws_meta.path 同一产出链），
    字符串形态一致，做精确前缀匹配即可，不做大小写猜测。
    """
    try:
        rel = target.relative_to(root_dir)
    except ValueError:
        return None
    return rel if str(rel) not in ("", ".") else None


def resolve_meta_workspace_path(meta: dict) -> str | None:
    """从 ws_meta 形态字典解析可用工作区坐标。

    worktree 模式：副本目录存在返回副本（运行中任务的真实工作区）；副本已删
    （合并清理）而 project_root 存在 → 返回 project_root（合并目标，产物所在
    地）。其余模式取 path。
    """
    path = str(meta.get("path") or "")
    if not path:
        return None
    if meta.get("mode") == "worktree":
        project_root = str(meta.get("project_root") or "")
        if project_root and not Path(path).is_dir() and Path(project_root).is_dir():
            return project_root
    return path

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

        task_ids = await self._get_child_task_ids(container_task_id)
        # 包含容器任务自身
        task_ids.add(container_task_id)

        for task_id in task_ids:
            result = await artifact_service.list_artifacts_by_task(task_id, limit=100)
            items.extend(result["items"])

        return {"items": items, "total": len(items)}

    async def resolve_workspace_from_state(self, pipeline_id: str) -> str | None:
        """按 pipeline_id 从 state 聚合行解析工作区坐标（非任务管道关联通道）。

        任务管道坐标优先取任务域镜像 ``task.ws_meta``（任务 init 即写，不受
        会话工作区投影污染——任务管道的 ``ws_meta`` 键曾会被写成会话目录，
        打开工作空间落到会话默认文件夹）；主会话管道无任务镜像，回退
        ``ws_meta``。worktree 模式合并后副本即删，按 ``resolve_meta_workspace_path``
        重定位到 project_root。最后回退 ``workspace`` 标量。读面未注入/无命中/
        命中行无工作区键 → None。
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
                for key in ("task.ws_meta", "ws_meta"):
                    meta = row.get(key)
                    if not isinstance(meta, dict):
                        continue
                    path = resolve_meta_workspace_path(meta)
                    if path:
                        return path
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

    async def resolve_merged_worktree_target(self, path: str) -> str | None:
        """已合并 worktree 的死路径重定位：路径落在某任务 worktree 副本内
        → project_root + 相对后缀。

        worktree 任务合并后副本即删，任务过程中产出的绝对路径（工具结果/
        文件卡片）随之失效；state 行的 worktree 元数据（ws_meta / task.ws_meta）
        合并后仍持久，是重定位真值源。仅匹配绝对路径；无命中返回 None。
        """
        if "__wt_" not in path or not Path(path).is_absolute():
            return None
        reader = _get_state_reader()
        if reader is None:
            return None
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
            if not isinstance(rows, list):
                return None
            target = Path(path)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                meta = row.get("ws_meta")
                if not isinstance(meta, dict):
                    meta = row.get("task.ws_meta")
                if not isinstance(meta, dict) or meta.get("mode") != "worktree":
                    continue
                wt_root = str(meta.get("path") or "")
                project_root = str(meta.get("project_root") or "")
                if not wt_root or not project_root:
                    continue
                rel = _relative_under(target, wt_root)
                if rel is None:
                    continue
                return str(Path(project_root) / rel)
            return None
        except Exception as exc:  # noqa: BLE001 — 重定位失败按无命中处理，不阻断读取
            logger.warning(
                "[WorkspaceService] 合并 worktree 路径重定位失败 | path=%s | err=%s",
                path,
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

        ws = self._workspaces.get(container_task_id)
        if ws:
            ws.file_tree = tree
            from datetime import UTC, datetime  # noqa: PLC0415

            ws.updated_at = datetime.now(UTC).isoformat()

        return {"tree": [n.to_dict() for n in tree]}

    async def _get_child_task_ids(self, project_id: str) -> set[str]:
        """获取项目名下所有子任务 ID（挂靠键 = state 行 task.parent_project_id）。"""
        try:
            rows = await self._read_state_rows()
            if rows is None:
                # 读面未注入 → fail-closed 空集并留痕（静默空集会让制品列表
                # 不完整，须可观测）
                logger.warning(
                    "[workspace] 子任务聚合失败（state 读面未注入，返回空集，制品列表可能不完整）| project_id=%s",
                    project_id,
                )
                return set()

            return {
                str(r.get("pipeline_id") or "")
                for r in rows
                if str(r.get("task.parent_project_id") or "") == project_id
                and str(r.get("pipeline_id") or "")
            }
        except Exception as e:
            # 子任务聚合失败静默空集会让制品列表不完整——留痕可观测
            logger.warning(
                "[workspace] 子任务聚合失败（返回空集，制品列表可能不完整）| project_id=%s | error=%s",
                project_id,
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
