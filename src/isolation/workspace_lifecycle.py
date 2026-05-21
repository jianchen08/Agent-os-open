"""工作空间统一生命周期管理

封装完整生命周期：初始化 -> 开发 -> 评估前保存 -> 评估通过合并/不通过重试 -> 清理。

暴露接口：
- WorkspaceLifecycleManager：工作空间生命周期管理器

从 _workspace_git_ops._GitOpsMixin 和 _workspace_merge_ops._MergeOpsMixin 继承
Git 操作和合并操作方法，本文件仅保留业务编排层代码。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from isolation._workspace_git_ops import _GitOpsMixin, _force_rmtree, _safe_ws_name
from isolation._workspace_merge_ops import _MergeOpsMixin

logger = logging.getLogger(__name__)

__all__ = [
    "WorkspaceLifecycleManager",
    "_safe_ws_name",
    "_force_rmtree",
]


class WorkspaceLifecycleManager(_GitOpsMixin, _MergeOpsMixin):
    """工作空间统一生命周期管理器

    场景A: 新项目（workspace 为空或路径不存在）
    场景B: 已有项目无 .git（初始化 git 并提交现有文件）
    场景C: 已有项目有 .git（通过 worktree 或 sparse-checkout 隔离）

    四种工作模式: project_root / branch / worktree / shared

    继承:
        _GitOpsMixin: Git 命令封装和分支管理方法
        _MergeOpsMixin: 合并、验证和清理方法
    """

    def __init__(self, resource_merge: Any, config: dict[str, Any],
                 task_tree: Any, ws_meta_store: Any, base_path: str):
        """初始化工作空间生命周期管理器

        Args:
            resource_merge: ResourceMerge 工具实例，用于合并和回滚操作
            config: isolation_config 配置字典
            task_tree: 任务父子关系查询接口，需提供 get_parent_info(task_id) 方法
            ws_meta_store: 工作空间元数据存储，需提供 get/set 方法
            base_path: 主仓库根目录路径
        """
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

    # ── 内部工具方法 ──────────────────────────────────────────────

    def _ensure_dir_and_git(self, path: Path) -> None:
        """确保目录存在且有 git 初始化。"""
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        if not (path / ".git").exists():
            if not self._git_init_and_initial_commit(path, "chore: initial container project"):
                raise RuntimeError(f"容器空间初始化失败（git init）: {path}")
        else:
            self._ensure_git_user(path)

    # ── 容器空间初始化 ──────────────────────────────────────────

    def init_container_workspace(self, container_task_id: str, workspace: str | None,
                                 task_data: dict) -> dict:
        """容器任务的空间初始化（由 TaskWorker 在跳过执行前调用）

        BUG-FIX-fix_20260519_container_workspace_path:
        规则：
        - 有 workspace + host 模式 → 直接用 workspace（原空间）
        - 有 workspace + 非 host 模式 → 在 ws_root 创建容器空间，从 workspace 复制
        - 无 workspace → 在 ws_root 创建容器空间，空空间 + git init
        """
        isolation_mode = task_data.get("isolation_mode", "") or ""
        ws_base = self._get_workspace_root()

        if workspace and isolation_mode == "host":
            path = Path(workspace)
            self._ensure_dir_and_git(path)
            logger.info("[WorkspaceLifecycle] host模式复用原空间: task_id=%s, path=%s",
                        container_task_id, path)
        else:
            path = ws_base / f"container_{container_task_id}"
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                if workspace:
                    src_path = Path(workspace)
                    if not src_path.is_absolute():
                        src_path = self._base_path / src_path
                    copied = self._copy_project_to_container(path, src=src_path)
                    logger.info("[WorkspaceLifecycle] 容器空间已复制文件: task_id=%s, files=%d",
                                container_task_id, copied)
                if not self._git_init_and_initial_commit(path, "chore: initial container project"):
                    raise RuntimeError(f"容器空间初始化失败（git init）: {path}")
            else:
                self._ensure_dir_and_git(path)

        meta = {"mode": "project_root", "path": str(path),
                "branch": "main", "project_root": str(path),
                "is_container_workspace": True}
        self._ws_meta_store[container_task_id] = meta
        self._persist_ws_meta(container_task_id)
        logger.info("[WorkspaceLifecycle] 容器空间已初始化: task_id=%s, path=%s",
                     container_task_id, path)
        return meta

    # ── 任务启动 ──────────────────────────────────────────────

    def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """任务启动时的生命周期钩子，根据 is_root 分发到子任务或根任务处理"""
        self._record_main_branch()
        self.restore_ws_meta(task_id)
        existing = self._ws_meta_store.get(task_id)
        if existing and existing.get("mode"):
            ws_path = existing.get("path", "")
            if ws_path and Path(ws_path).exists():
                logger.info(
                    "[WorkspaceLifecycle] 复用已有工作空间: task_id=%s, mode=%s, path=%s",
                    task_id, existing.get("mode"), ws_path,
                )
                return existing
            logger.info(
                "[WorkspaceLifecycle] 已有 ws_meta 但路径不存在，重新创建: task_id=%s, path=%s",
                task_id, ws_path,
            )
        if not task_data.get("is_root", True):
            meta = self._start_subtask(task_id, workspace, task_data)
        else:
            meta = self._start_root_task(task_id, workspace, task_data)
        self._persist_ws_meta(task_id)
        return meta

    def _start_subtask(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """子任务启动：通过 TaskService API 查找父任务，共享父工作空间"""
        _isolation_mode = (task_data.get("isolation_mode", "")
                           or self._config.get("coordinator", {}).get("default_level", ""))
        if _isolation_mode == "host":
            container_ws = self._find_container_workspace(task_id)
            host_path = container_ws or workspace
            if host_path:
                meta = {"mode": "shared", "path": host_path}
                self._ws_meta_store[task_id] = meta
                logger.info(
                    "[WorkspaceLifecycle] host 隔离模式(子任务): 共享目录 "
                    "task_id=%s, path=%s, container_ws=%s",
                    task_id, host_path, container_ws,
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
            logger.warning("[WorkspaceLifecycle] _start_subtask 查找父任务失败: task_id=%s, error=%s",
                           task_id, e)

        meta = {"mode": "shared", "path": parent_path,
                "parent_workspace": workspace,
                "project_root": parent_meta.get("project_root", "")}
        self._ws_meta_store[task_id] = meta
        return meta

    def _start_root_task(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """根任务启动：场景A(新项目) / 场景B(无.git) / 场景C(有.git)

        BUG-FIX-fix_20260422_scenario_detect_base_path:
        问题根因: _detect_scenario 用 workspace（.ai_workspaces/xxx）做检测，
                 该目录总是空的，导致总是走场景A（new_project），创建空 git 仓库，
                 Agent 在空目录中无法读取项目文件，路径不对齐。
        修复方案: 用 self._base_path（项目根目录）做场景检测，
                 workspace 仅作为 worktree 的目标路径。

        BUG-FIX-fix_20260423_index_lock:
        问题根因: 场景C中 ws_dir 可能有残留的 .git（无提交）和 index.lock，
                 导致 git add/commit 失败。
        修复方案: 使用 _git_init_and_initial_commit 统一处理 init/add/commit，
                 自动清理 index.lock 和空的 .git 目录。
        """
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
            logger.info(
                "[WorkspaceLifecycle] inherit: 复用旧工作空间 "
                "task_id=%s, workspace=%s, mode=%s, branch=%s",
                task_id, workspace, source_mode, meta.get("branch"),
            )
            self._ws_meta_store[task_id] = meta
            return meta

        # ── Host isolation mode ──
        _isolation_mode = (task_data.get("isolation_mode", "")
                           or self._config.get("coordinator", {}).get("default_level", ""))
        if _isolation_mode == "host":
            container_ws = self._find_container_workspace(task_id)
            host_path = container_ws or workspace
            if host_path:
                meta = {"mode": "plain", "path": host_path}
                self._ws_meta_store[task_id] = meta
                logger.info(
                    "[WorkspaceLifecycle] host 隔离模式: 直接操作目录 "
                    "task_id=%s, path=%s, container_ws=%s（无 git worktree/branch）",
                    task_id, host_path, container_ws,
                )
                return meta
        # BUG-FIX-fix_20260425_container_workspace_init:
        # 容器子任务优先查找容器空间，基于容器空间做 worktree/copy
        # 但当 _inherit_workspace_resolved 时跳过容器查找，使用继承的工作空间
        container_ws = None
        if not task_data.get("_inherit_workspace_resolved"):
            container_ws = self._find_container_workspace(task_id)
        if container_ws:
            container_path = Path(container_ws).resolve()
            if not (container_path / ".git").exists():
                if not self._git_init_and_initial_commit(container_path, "chore: init container repo"):
                    raise RuntimeError(f"容器空间 git 初始化失败: {container_path}")
            self._ensure_git_user(container_path)
            rc_head, _, _ = self._run_git("rev-parse", "HEAD", cwd=container_path)
            if rc_head != 0:
                logger.info(
                    "[WorkspaceLifecycle] 容器空间 .git 存在但无提交，执行 initial commit: "
                    "task_id=%s, path=%s", task_id, container_path)
                if not self._git_init_and_initial_commit(
                        container_path, "chore: init container repo"):
                    raise RuntimeError(
                        f"容器空间初始化失败（已有 .git 但无提交记录）: {container_path}")
            elif self._guard_root_branch(container_path):
                self._git_add_commit_if_dirty(container_path,
                                              f"chore: auto-save before subtask {task_id}")
            else:
                logger.warning("[WorkspaceLifecycle] 跳过容器空间 auto-save: 分支守卫检测到变更")

            branch = f"task/{task_id}"
            ws_dir = container_path.parent / _safe_ws_name(
                container_path.name, task_id)
            project_size = self._calc_project_size(str(container_path), task_id)
            threshold = (self._config.get("workspace", {})
                         .get("sparse_threshold_mb", 50) * 1024 * 1024)

            if project_size > threshold:
                self._setup_sparse_worktree(ws_dir, container_path, branch)
            else:
                self._worktree_add_with_repair(container_path, branch, ws_dir, task_id)
            self._ensure_git_user(ws_dir)
            meta = {"mode": "worktree", "path": str(ws_dir),
                    "branch": branch, "project_root": str(container_path)}
            self._ws_meta_store[task_id] = meta
            return meta

        # 检测到父任务是容器但找不到工作空间时，报错而非静默降级
        # 但 inherit_workspace_from 场景跳过此检查——继承的任务有自己指定的工作空间
        if not task_data.get("_inherit_workspace_resolved"):
            try:
                task = self._task_tree.get_task(task_id)
                if task and task.parent_task_id:
                    parent_task = self._task_tree.get_task(task.parent_task_id)
                    if parent_task and parent_task.metadata.get("task_scope") == "container":
                        raise RuntimeError(
                            f"父任务 {task.parent_task_id} 是容器任务，"
                            f"但未找到容器工作空间（可能初始化失败）。"
                            f"子任务 {task_id} 无法创建工作空间。"
                        )
            except RuntimeError:
                raise
            except Exception:
                logger.warning("[WorkspaceLifecycle] 工作空间初始化异常", exc_info=True)

        # HOST 模式：直接操作项目目录，不创建 worktree 隔离
        isolation_level = task_data.get("isolation_level", "")
        if isolation_level == "host":
            scenario, project_root = self._detect_scenario(workspace, task_data)
            root_path = Path(project_root)
            if not root_path.exists():
                root_path.mkdir(parents=True, exist_ok=True)
            meta = {
                "mode": "shared",
                "path": str(root_path),
                "project_root": str(root_path),
            }
            self._ws_meta_store[task_id] = meta
            logger.info(
                "[WorkspaceLifecycle] HOST模式: task_id=%s, 直接操作项目目录: %s",
                task_id,
                root_path,
            )
            return meta

        # 无显式 workspace 且无容器 → plain 模式：只创建目录，不做 git 操作
        has_explicit_workspace = task_data.get("_has_explicit_workspace", False)
        if not has_explicit_workspace and not container_ws:
            ws_base = self._get_workspace_root()
            plain_path = ws_base / task_id
            plain_path.mkdir(parents=True, exist_ok=True)
            meta = {"mode": "plain", "path": str(plain_path)}
            self._ws_meta_store[task_id] = meta
            logger.info(
                "[WorkspaceLifecycle] plain 模式: task_id=%s, path=%s（无 git 操作）",
                task_id, plain_path,
            )
            return meta

        scenario, project_root = self._detect_scenario(workspace, task_data)
        root_path = Path(project_root)
        logger.info("[WorkspaceLifecycle] _start_root_task: task_id=%s, scenario=%s, "
                     "workspace=%s, root_path=%s",
                     task_id, scenario, workspace, root_path)

        ws_base = self._get_workspace_root()

        # BUG-FIX-fix_20260521_existing_project_copy:
        # 问题根因: existing_project 分支将整个项目复制到 .ai_workspaces/taskid_repo，
        #          每次任务都复制一份完整项目，导致磁盘空间爆炸。
        # 修复方案: 直接从目标项目（root_path）创建 worktree，
        #          worktree 放在配置的工作空间基目录（ws_base）下，不复制文件。
        if not root_path.exists():
            root_path.mkdir(parents=True, exist_ok=True)
        if not (root_path / ".git").exists():
            if not self._git_init_and_initial_commit(root_path, "chore: initial project"):
                raise RuntimeError(
                    f"项目空间初始化失败（git init）: task_id={task_id}, path={root_path}")
        else:
            self._ensure_git_user(root_path)
            rc_head, _, _ = self._run_git("rev-parse", "HEAD", cwd=root_path)
            if rc_head != 0:
                logger.info(
                    "[WorkspaceLifecycle] .git 存在但无提交，执行 initial commit: "
                    "task_id=%s, path=%s", task_id, root_path)
                if not self._git_init_and_initial_commit(
                        root_path, "chore: initial project"):
                    raise RuntimeError(
                        f"项目空间初始化失败（已有 .git 但无提交记录）: "
                        f"task_id={task_id}, path={root_path}")
            elif self._guard_root_branch(root_path):
                self._git_add_commit_if_dirty(
                    root_path,
                    f"chore: auto-save before worktree for task {task_id}")
            else:
                logger.warning(
                    "[WorkspaceLifecycle] 跳过项目根目录 auto-save: "
                    "分支守卫检测到变更, task_id=%s",
                    task_id)

        branch = f"task/{task_id}"
        ws_dir = ws_base / _safe_ws_name(root_path.name, task_id)
        project_size = self._calc_project_size(str(root_path), task_id)
        threshold = (self._config.get("workspace", {})
                     .get("sparse_threshold_mb", 50) * 1024 * 1024)

        if project_size > threshold:
            self._setup_sparse_worktree(ws_dir, root_path, branch)
        else:
            self._worktree_add_with_repair(root_path, branch, ws_dir, task_id)
        self._ensure_git_user(ws_dir)
        meta = {"mode": "worktree", "path": str(ws_dir),
                "branch": branch, "project_root": str(root_path)}

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
                self._task_tree.save_task(task)
        except Exception as e:
            logger.warning("[WorkspaceLifecycle] _persist_ws_meta 失败: task_id=%s, error=%s",
                           task_id, e)

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
            logger.warning("[WorkspaceLifecycle] restore_ws_meta 失败: task_id=%s, error=%s",
                           task_id, e)

    # ── 工作空间清理 ──────────────────────────────────────────

    def cleanup_workspace(self, task_id: str) -> dict[str, Any]:
        """清理单个任务关联的工作空间（worktree/分支/目录），不递归子任务"""
        self.restore_ws_meta(task_id)
        meta = self._ws_meta_store.get(task_id)
        if not meta:
            return {"worktree_removed": False, "branch_deleted": False, "dir_removed": False}

        mode = meta.get("mode", "")
        workspace = meta.get("path", "")
        result: dict[str, Any] = {
            "worktree_removed": False, "branch_deleted": False, "dir_removed": False}

        if mode == "worktree":
            project_root = Path(meta.get("project_root", "")).resolve()
            branch = meta.get("branch", "")
            ws_path = Path(workspace).resolve()
            if project_root.exists():
                if ws_path.exists():
                    rc, _, _ = self._run_git("worktree", "remove", str(ws_path),
                                             "--force", cwd=project_root)
                    result["worktree_removed"] = rc == 0
                if branch:
                    rc, _, _ = self._run_git("branch", "-D", branch, cwd=project_root)
                    result["branch_deleted"] = rc == 0
            if ws_path.exists() and "__wt_" in ws_path.name:
                try:
                    _force_rmtree(str(ws_path))
                    result["dir_removed"] = True
                except OSError as e:
                    logger.warning("[WorkspaceLifecycle] cleanup_workspace rmtree 失败: %s, %s",
                                   workspace, e)
        elif mode == "plain":
            ws_path = Path(workspace)
            if not ws_path.is_absolute():
                ws_path = ws_path.resolve()
            logger.info("[WorkspaceLifecycle] plain 模式保留工作空间目录: %s", ws_path)

        self._ws_meta_store.pop(task_id, None)
        logger.info("[WorkspaceLifecycle] cleanup_workspace: task_id=%s, mode=%s, result=%s",
                     task_id, mode, result)
        return result
