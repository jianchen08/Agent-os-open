"""工作空间服务。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

# 裸名 models = 本目录 models.py（插件目录已入 sys.path）；全名 workspace.models
# 在合宿平铺环境下被非包同名模块遮蔽 → 成员装载失败 fail-fast 拖垮 light 组
from models import Workspace

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
# None = 未注入（读面缺失即无坐标，如实 no_workspace，无兜底通道）。
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

    async def resolve_workspace_from_state(self, pipeline_id: str) -> tuple[str, str, bool]:
        """按 pipeline_id 从 state 聚合行解析工作区坐标与任务归属（非任务管道关联通道）。

        任务管道坐标优先取任务域镜像 ``task.ws_meta``（任务 init 即写，不受
        会话工作区投影污染——任务管道的 ``ws_meta`` 键曾会被写成会话目录，
        打开工作空间落到会话默认文件夹）；主会话管道无任务镜像，回退
        ``ws_meta``。worktree 模式合并后副本即删，按 ``resolve_meta_workspace_path``
        重定位到 project_root。最后回退 ``workspace`` 标量。读面未注入/无命中/
        命中行无工作区键 → ("", "", False)。

        Returns:
            ``(workspace_path, owner_sub, is_task_row)``——owner 为同行
            ``task.submitted_by``（任务归属权威字段，task_submit 出生协议写全；
            会话投影行无该键 → 空串）。is_task_row 标记命中行是否携带任务身份
            （``task.id`` / ``task.submitted_by`` / ``task.ws_meta`` 任一在场）：
            归属闸只对任务行要求 submitted_by；会话行（无任何任务身份标记）
            坐标即服务端 state 真值，不构成缺归属。单次读取同源返回，避免
            二次读行的归属错位。
        """
        reader = _get_state_reader()
        if reader is None:
            return "", "", False
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
            if not isinstance(rows, list):
                return "", "", False
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("pipeline_id") or "") != pipeline_id:
                    continue
                owner = str(row.get("task.submitted_by") or "")
                is_task_row = bool(
                    row.get("task.id") or owner or row.get("task.ws_meta")
                )
                for key in ("task.ws_meta", "ws_meta"):
                    meta = row.get(key)
                    if not isinstance(meta, dict):
                        continue
                    path = resolve_meta_workspace_path(meta)
                    if path:
                        return path, owner, is_task_row
                ws = row.get("workspace")
                if isinstance(ws, str) and ws:
                    return ws, owner, is_task_row
                return "", owner, is_task_row
            return "", "", False
        except Exception as exc:  # noqa: BLE001 — 解析失败按无坐标处理（无兜底通道）
            logger.warning(
                "[WorkspaceService] state 行工作区解析失败 | pipeline=%s | err=%s",
                pipeline_id,
                exc,
            )
            return "", "", False

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
        sub_path: str = "",
    ) -> dict[str, Any]:
        """列出工作空间指定层级的一层直接子项（懒加载文件树供数）。

        每次现列（不落 Workspace 模型——曾经的扫描回写会让 get_workspace
        详情返回"上次扫描冻结值"，与专树端点不同源，B11-S7 已删）。
        只列一层、不递归：目录下钻由前端按 path 逐层拉取，单层条目数受
        _LAYER_MAX_ENTRIES 封顶。曾经的全量递归扫描在 worktree junction
        依赖链接场景会穿透进主仓依赖森林且不可收敛（2026-10-16 失控实证：
        单次请求 45 分钟 + 2.7 GB），单层列举让该问题类别整体消失。
        """
        if base_path and os.path.isdir(base_path):  # noqa: PTH112
            return await asyncio.to_thread(self._list_layer, base_path, sub_path)
        return {"entries": [], "truncated": False}

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

    # Windows 保留名（CON 等）在文件树中不可见：条目名命中即跳过。
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

    # 单层列举条目数上限：一次响应内可见条目数封顶，超限截断并打 truncated
    # 标记（跳过的隐藏条目不计入）。防御性预算而非展示限制——单层也可能被
    # node_modules 等巨目录撑爆内存与响应体。
    _LAYER_MAX_ENTRIES = 500

    @staticmethod
    def _normalize_sub_path(sub_path: str) -> str | None:
        """子路径词法归一：仅接受工作区内的正向下钻段，输入侧逃逸即拒绝。

        只做词法校验、不做 realpath 解析——junction/symlink 物理指向工作区
        之外是链接继承的预期形态（worktree 依赖链接），物理越界合法；要拦的
        是 ``..`` / 绝对路径 / 盘符这类输入侧逃逸。空串/点归一为根层。
        返回 posix 分隔的规范化相对路径；非法返回 None。
        """
        raw = (sub_path or "").strip()
        if not raw or raw == ".":
            return ""
        if re.match(r"^[A-Za-z]:", raw) or raw.startswith(("/", "\\")):
            return None
        segments: list[str] = []
        for seg in raw.replace("\\", "/").split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                return None
            segments.append(seg)
        return "/".join(segments)

    @staticmethod
    def _entry_type(full_path: str) -> str:
        """条目类型：link（symlink/junction）判定优先于 directory/file。

        顺序即语义：指向目录的链接按链接呈现（前端展开时列目标第一层，由
        单层列举天然封顶），不再被当作普通目录下钻——os.path.isdir 会穿透
        junction，类型判定必须先于它。
        """
        if os.path.islink(full_path):  # noqa: PTH114
            return "link"
        # os.path.isjunction 为 3.12+ 且仅 ntpath 定义（junction 是 Windows
        # 概念），getattr 守卫保持 POSIX 侧（Linux 部署）直接可运行。
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(full_path):
            return "link"
        if os.path.isdir(full_path):  # noqa: PTH112
            return "directory"
        return "file"

    def _list_layer(self, base_path: str, sub_path: str = "") -> dict[str, Any]:
        """列出 base_path 下 sub_path（工作区内相对路径，posix）的一层子项。

        返回 ``{"entries": [{name, type, path}...], "truncated": bool}``，
        path 为自工作区根起的 posix 相对路径（前端懒加载按它回传 path 参数，
        文件操作端点同源消费）。目录不可读降级为空层并留痕。
        """
        rel = self._normalize_sub_path(sub_path)
        if rel is None:
            return {
                "entries": [],
                "truncated": False,
                "error": f"非法子路径（仅接受工作区内相对下钻）: {sub_path}",
            }
        target = os.path.join(base_path, *rel.split("/")) if rel else base_path
        try:
            names = sorted(os.listdir(target))  # noqa: PTH208
        except (PermissionError, OSError) as exc:
            logger.warning("[workspace] 目录不可读（返回空层）: %s | %s", target, exc)
            return {"entries": [], "truncated": False}

        entries: list[dict[str, Any]] = []
        truncated = False
        for name in names:
            if name == "__pycache__" or name.startswith("."):
                continue
            if name.split(".")[0].upper() in self._WINDOWS_RESERVED_NAMES:
                continue
            if len(entries) >= self._LAYER_MAX_ENTRIES:
                truncated = True
                break
            entries.append(
                {
                    "name": name,
                    "type": self._entry_type(os.path.join(target, name)),
                    "path": f"{rel}/{name}" if rel else name,
                }
            )
        return {"entries": entries, "truncated": truncated}
