"""工作空间统一生命周期管理"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

# DEBT: _workspace_git_ops 和 _workspace_merge_ops 模块在 0.1 src/isolation/ 中即不存在（源码遗留缺陷）。
# ceiling: workspace_lifecycle 任何实例化路径都会触发 ModuleNotFoundError；当前 server.py 未触发该路径故可加载。
# upgrade: 当任何 server.py tool 需要调用 WorkspaceLifecycleManager（git ops / merge ops）时，必须先补齐这两个 mixin 模块。
from _workspace_git_ops import _force_rmtree, _GitOpsMixin, _safe_ws_name  # noqa: E402
from _workspace_merge_ops import _MergeOpsMixin  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "WorkspaceLifecycleManager",
    "_safe_ws_name",
    "_force_rmtree",
]


class WorkspaceLifecycleManager(_GitOpsMixin, _MergeOpsMixin):
    """工作空间统一生命周期管理器"""

    def __init__(self, resource_merge: Any, config: dict[str, Any], task_tree: Any, ws_meta_store: Any, base_path: str):
        """初始化工作空间生命周期管理器"""
        self._resource_merge = resource_merge
        self._config = config
        self._task_tree = task_tree
        self._ws_meta_store = ws_meta_store
        self._base_path = Path(base_path)
        # 按 project_root 粒度的并发锁
        self._merge_locks: dict[str, Any] = {}
        self._global_lock = __import__("threading").Lock()
        # 项目大小计算缓存 {project_root: (mtime, size)}
        self._size_cache: dict[str, tuple[float, int]] = {}
        # 记录项目根目录的主分支，用于守卫 auto-save 不写入错误分支
        self._main_branch: str = ""
        try:
            self._record_main_branch()
        except Exception:
            logger.warning("[WorkspaceLifecycle] __init__ 中记录主分支失败", exc_info=True)

    # ── 任务启动 ──────────────────────────────────────────────

    def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """任务启动时的生命周期钩子，根据 is_root 分发到子任务或根任务处理"""
        self._record_main_branch()
        self.restore_ws_meta(task_id)
        existing = self._ws_meta_store.get(task_id)
        if existing and existing.get("mode"):
            ws_path = existing.get("path", "")
            if ws_path and Path(ws_path).exists():
                logger.debug(
                    "[WorkspaceLifecycle] 复用已有工作空间: task_id=%s, mode=%s, path=%s",
                    task_id,
                    existing.get("mode"),
                    ws_path,
                )
                self._copy_skills_to_workspace(ws_path)
                return existing
            logger.debug(
                "[WorkspaceLifecycle] 已有 ws_meta 但路径不存在，重新创建: task_id=%s, path=%s",
                task_id,
                ws_path,
            )
        if not task_data.get("is_root", True):
            meta = self._start_subtask(task_id, workspace, task_data)
        else:
            meta = self._start_root_task(task_id, workspace, task_data)
        self._copy_skills_to_workspace(meta["path"])
        self._persist_ws_meta(task_id)
        return meta

    def _start_subtask(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """子任务启动：共享父工作空间。

        拓扑由 workspace_mode 决定（与隔离解耦）：
        - plain → 直接共享宿主目录（挂项目任务的 workspace 已解析为项目文件夹）
        - 其他（默认 worktree 语义的父链共享）→ 共享父任务工作空间
        """
        _ws_mode = task_data.get("workspace_mode", "") or self._config.get("workspace", {}).get(
            "default_mode", "worktree"
        )
        if _ws_mode == "plain":
            if workspace:
                meta = {"mode": "shared", "path": workspace}
                self._ws_meta_store[task_id] = meta
                logger.debug(
                    "[WorkspaceLifecycle] plain 拓扑(子任务): 共享目录 task_id=%s, path=%s",
                    task_id,
                    workspace,
                )
                return meta

        parent_path = workspace
        parent_meta: dict = {}
        try:
            task = self._task_tree.get_task(task_id)
            if task and task.parent_task_id:
                parent_id = task.parent_task_id
                self.restore_ws_meta(parent_id)
                parent_meta = self._ws_meta_store.get(parent_id, {})
                parent_path = parent_meta.get("path", workspace)
        except Exception as e:
            logger.warning("[WorkspaceLifecycle] _start_subtask 查找父任务失败: task_id=%s, error=%s", task_id, e)

        meta = {
            "mode": "shared",
            "path": parent_path,
            "parent_workspace": workspace,
            "project_root": parent_meta.get("project_root", ""),
        }
        self._ws_meta_store[task_id] = meta
        return meta

    # ── 技能文件复制 ──────────────────────────────────────────────

    def _copy_skills_to_workspace(self, ws_path: str) -> None:
        """将项目 skills/ 目录复制到工作空间（按技能粒度增量同步）。

        任务启动时调用一次，让 Agent 在 host / worktree / Docker 容器
        所有模式下都能通过 skills/<技能名>/scripts/*.py 访问技能脚本。
        """
        skills_src = self._base_path / "skills"
        if not skills_src.exists() or not skills_src.is_dir():
            logger.debug(
                "[WorkspaceLifecycle] skills/ 目录不存在，跳过复制: %s",
                skills_src,
            )
            return
        skills_dst = Path(ws_path) / "skills"
        # 工作空间就是项目目录本身时，源和目标相同，无需复制
        if skills_src.resolve() == skills_dst.resolve():
            logger.debug(
                "[WorkspaceLifecycle] 工作空间即为项目目录，skills/ 已在原位，跳过复制: %s",
                skills_dst,
            )
            return
        skills_dst.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for skill_src in skills_src.iterdir():
            if not skill_src.is_dir():
                continue
            skill_dst = skills_dst / skill_src.name
            if skill_dst.exists():
                continue  # 已有技能保持原样，仅补齐缺失项
            try:
                shutil.copytree(skill_src, skill_dst, symlinks=True)
                copied.append(skill_src.name)
            except Exception as exc:
                logger.warning(
                    "[WorkspaceLifecycle] 技能复制失败: %s → %s | error=%s",
                    skill_src,
                    skill_dst,
                    exc,
                )
        if copied:
            logger.debug(
                "[WorkspaceLifecycle] 技能已增量同步: %s → %s | new=%s",
                skills_src,
                skills_dst,
                copied,
            )

    def _start_root_task(self, task_id: str, workspace: str, task_data: dict) -> dict:  # noqa: PLR0912,PLR0915
        """根任务启动：场景A(新项目) / 场景B(无.git) / 场景C(有.git)"""
        # ── inherit_workspace_from：直接复用旧任务的工作空间 ──
        # 继承原任务的 ws_meta（mode/branch/project_root），保持 worktree 生命周期
        if task_data.get("_inherit_workspace_resolved"):
            source_ws_meta = task_data.get("_source_ws_meta") or {}
            source_mode = source_ws_meta.get("mode", "shared")
            meta = {
                "mode": source_mode,
                "path": workspace,
                "branch": source_ws_meta.get("branch", ""),
                "project_root": source_ws_meta.get("project_root", ""),
            }
            logger.debug(
                "[WorkspaceLifecycle] inherit: 复用旧工作空间 task_id=%s, workspace=%s, mode=%s, branch=%s",
                task_id,
                workspace,
                source_mode,
                meta.get("branch"),
            )
            self._ws_meta_store[task_id] = meta
            return meta

        # ── workspace_mode 拓扑决策（与隔离解耦）──
        # plain 模式不建 worktree、不切分支，但通过 task_id + project_root
        # 让 on_before_evaluate 把产出 commit 到当前分支，
        # 避免改动被后续任务的 auto-save 混入错误的 commit message。
        # 默认（未指定/配置缺省）→ worktree 拓扑。
        _ws_mode = task_data.get("workspace_mode", "") or self._config.get("workspace", {}).get(
            "default_mode", "worktree"
        )
        if _ws_mode == "plain":
            if workspace:
                meta = {
                    "mode": "plain",
                    "path": workspace,
                    "task_id": task_id,
                    "project_root": workspace,
                }
                self._ws_meta_store[task_id] = meta
                logger.debug(
                    "[WorkspaceLifecycle] plain 拓扑: 直接操作目录 "
                    "task_id=%s, path=%s（无 git worktree/branch）",
                    task_id,
                    workspace,
                )
                return meta

        # PLAIN 拓扑：直接操作项目目录，不创建 worktree 隔离
        # 同上：保留 task_id + project_root，供 on_before_evaluate 用准确的
        # commit message 提交，避免被其他任务的 auto-save 顺手带走。
        _ws_mode = task_data.get("workspace_mode", "") or self._config.get("workspace", {}).get(
            "default_mode", "worktree"
        )
        if _ws_mode == "plain":
            scenario, project_root = self._detect_scenario(workspace, task_data)
            root_path = Path(project_root)
            if not root_path.exists():
                root_path.mkdir(parents=True, exist_ok=True)
            meta = {
                "mode": "shared",
                "path": str(root_path),
                "project_root": str(root_path),
                "task_id": task_id,
            }
            self._ws_meta_store[task_id] = meta
            logger.debug(
                "[WorkspaceLifecycle] PLAIN拓扑: task_id=%s, 直接操作项目目录: %s",
                task_id,
                root_path,
            )
            return meta

        # 无显式 workspace：
        # - 显式 plain → plain 空目录（只创建目录，不做 git 操作）
        # - 默认/显式 worktree → 落到下方 worktree 建立分支，源 = 调用方传入的
        #   workspace（plugin._bootstrap 已把无显式场景的 workspace 解析为项目根，
        #   mode=worktree 即「在项目根上建 worktree」）。项目根非 git 仓库或
        #   worktree 建不起来时由下方分支的 git 前置步骤 fail-honest 抛错，
        #   上层（plugin._bootstrap）捕获后降级为 plain。
        has_explicit_workspace = task_data.get("_has_explicit_workspace", False)
        if _ws_mode == "plain" and not has_explicit_workspace:
            ws_base = self._get_workspace_root()
            plain_path = ws_base / task_id
            plain_path.mkdir(parents=True, exist_ok=True)
            meta = {"mode": "plain", "path": str(plain_path)}
            self._ws_meta_store[task_id] = meta
            logger.debug(
                "[WorkspaceLifecycle] plain 模式: task_id=%s, path=%s（无 git 操作）",
                task_id,
                plain_path,
            )
            return meta

        scenario, project_root = self._detect_scenario(workspace, task_data)
        root_path = Path(project_root)
        # 无显式 workspace（源=项目根）的 worktree 前置守卫：项目根必须是已提交的
        # git 仓库——不能在项目根上自动 git init / initial commit（会把普通目录
        # 改写成仓库，污染用户态）。非 git 仓库或无提交 → warn 降级 plain 空目录
        # （fail-honest 不 panic，保持既有降级风格）。
        if not has_explicit_workspace:
            rc_wt, out_wt, _ = self._run_git("rev-parse", "--is-inside-work-tree", cwd=root_path)
            rc_head, _, _ = self._run_git("rev-parse", "HEAD", cwd=root_path)
            if rc_wt != 0 or out_wt.strip().lower() != "true" or rc_head != 0:
                ws_base = self._get_workspace_root()
                plain_path = ws_base / task_id
                plain_path.mkdir(parents=True, exist_ok=True)
                meta = {"mode": "plain", "path": str(plain_path)}
                self._ws_meta_store[task_id] = meta
                logger.warning(
                    "[WorkspaceLifecycle] 项目根非有效 git 仓库，worktree 无法建立，"
                    "降级 plain 模式: task_id=%s, root=%s, rc_worktree=%d, rc_head=%d, path=%s",
                    task_id,
                    project_root,
                    rc_wt,
                    rc_head,
                    plain_path,
                )
                return meta
        logger.debug(
            "[WorkspaceLifecycle] _start_root_task: task_id=%s, scenario=%s, workspace=%s, root_path=%s",
            task_id,
            scenario,
            workspace,
            root_path,
        )

        ws_base = self._get_workspace_root()

        if not root_path.exists():
            root_path.mkdir(parents=True, exist_ok=True)
        if not (root_path / ".git").exists():
            if not self._git_init_and_initial_commit(root_path, "chore: initial project"):
                raise RuntimeError(f"项目空间初始化失败（git init）: task_id={task_id}, path={root_path}")
        else:
            self._ensure_git_user(root_path)
            rc_head, _, _ = self._run_git("rev-parse", "HEAD", cwd=root_path)
            if rc_head != 0:
                logger.debug(
                    "[WorkspaceLifecycle] .git 存在但无提交，执行 initial commit: task_id=%s, path=%s",
                    task_id,
                    root_path,
                )
                if not self._git_init_and_initial_commit(root_path, "chore: initial project"):
                    raise RuntimeError(
                        f"项目空间初始化失败（已有 .git 但无提交记录）: task_id={task_id}, path={root_path}"
                    )
            elif self._guard_root_branch(root_path):
                # 此 auto-save 提交的是项目根目录上残留的脏改动，
                # 来源可能是用户、其他 host 任务、上一次中断的执行——不是当前任务。
                # 用中性 message，避免给本任务"贴上"不属于它的改动。
                self._autosave_before_worktree(
                    root_path, "chore: auto-save dirty working tree before worktree creation", task_id
                )
            else:
                logger.warning("[WorkspaceLifecycle] 跳过项目根目录 auto-save: 分支守卫检测到变更, task_id=%s", task_id)

        branch = f"task/{task_id}"
        ws_dir = ws_base / _safe_ws_name(root_path.name, task_id)
        project_size = self._calc_project_size(str(root_path), task_id)
        threshold = self._config.get("workspace", {}).get("sparse_threshold_mb", 50) * 1024 * 1024

        if project_size > threshold:
            self._setup_sparse_worktree(ws_dir, root_path, branch)
        else:
            self._worktree_add_with_repair(root_path, branch, ws_dir, task_id)
        self._ensure_git_user(ws_dir)
        meta = {"mode": "worktree", "path": str(ws_dir), "branch": branch, "project_root": str(root_path)}

        self._ws_meta_store[task_id] = meta
        return meta

    # ── ws_meta 持久化与恢复 ────────────────────────────────────

    def _persist_ws_meta(self, task_id: str):
        """将 ws_meta 持久化到 task.metadata["ws_meta"]"""
        meta = self._ws_meta_store.get(task_id)
        if not meta:
            return
        try:
            task = self._task_tree.get_task(task_id)
            if task and task.metadata is not None:
                task.metadata["ws_meta"] = meta
                coro = self._task_tree.save_task(task)
                try:
                    loop = asyncio.get_running_loop()
                    t = loop.create_task(coro)
                    t.add_done_callback(self._log_persist_failure)
                except RuntimeError:
                    try:
                        loop = asyncio.get_event_loop()
                        if not loop.is_closed():
                            loop.call_soon_threadsafe(loop.create_task, coro)
                    except Exception:
                        logger.warning(
                            "[WorkspaceLifecycle] _persist_ws_meta: 无法调度 save_task, task_id=%s",
                            task_id,
                        )
        except Exception as e:
            logger.warning("[WorkspaceLifecycle] _persist_ws_meta 失败: task_id=%s, error=%s", task_id, e)

    @staticmethod
    def _log_persist_failure(fut: asyncio.Task) -> None:
        """记录 create_task 调度的 save_task 协程异常。"""
        try:
            fut.result()
        except Exception as exc:
            logger.warning("[WorkspaceLifecycle] save_task 协程执行失败: %s", exc)

    def restore_ws_meta(self, task_id: str):
        """从 task.metadata["ws_meta"] 恢复到 ws_meta_store"""
        if task_id in self._ws_meta_store:
            return
        try:
            task = self._task_tree.get_task(task_id)
            if task and task.metadata:
                saved = task.metadata.get("ws_meta")
                if saved:
                    self._ws_meta_store[task_id] = saved
        except Exception as e:
            logger.warning("[WorkspaceLifecycle] restore_ws_meta 失败: task_id=%s, error=%s", task_id, e)

    # ── 工作空间清理 ──────────────────────────────────────────

    def cleanup_workspace(self, task_id: str) -> dict[str, Any]:
        """清理单个任务关联的工作空间（worktree/分支/目录），不递归子任务"""
        self.restore_ws_meta(task_id)
        meta = self._ws_meta_store.get(task_id)
        if not meta:
            return {"worktree_removed": False, "branch_deleted": False, "dir_removed": False}

        mode = meta.get("mode", "")
        workspace = meta.get("path", "")
        result: dict[str, Any] = {"worktree_removed": False, "branch_deleted": False, "dir_removed": False}

        if mode == "worktree":
            project_root = Path(meta.get("project_root", "")).resolve()
            branch = meta.get("branch", "")
            ws_path = Path(workspace).resolve()
            if project_root.exists():
                if ws_path.exists():
                    rc, _, _ = self._run_git("worktree", "remove", str(ws_path), "--force", cwd=project_root)
                    result["worktree_removed"] = rc == 0
                if branch:
                    rc, _, _ = self._run_git("branch", "-D", branch, cwd=project_root)
                    result["branch_deleted"] = rc == 0
            if ws_path.exists() and "__wt_" in ws_path.name:
                try:
                    _force_rmtree(str(ws_path))
                    result["dir_removed"] = True
                except OSError as e:
                    logger.warning("[WorkspaceLifecycle] cleanup_workspace rmtree 失败: %s, %s", workspace, e)
        elif mode == "plain":
            ws_path = Path(workspace)
            if not ws_path.is_absolute():
                ws_path = ws_path.resolve()
            logger.debug("[WorkspaceLifecycle] plain 模式保留工作空间目录: %s", ws_path)

        self._ws_meta_store.pop(task_id, None)
        logger.debug("[WorkspaceLifecycle] cleanup_workspace: task_id=%s, mode=%s, result=%s", task_id, mode, result)
        return result
