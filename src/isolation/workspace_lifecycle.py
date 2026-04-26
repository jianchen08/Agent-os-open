"""工作空间统一生命周期管理

封装完整生命周期：初始化 -> 开发 -> 评估前保存 -> 评估通过合并/不通过重试 -> 清理。

暴露接口：
- WorkspaceLifecycleManager：工作空间生命周期管理器
"""
import logging
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 排除的目录（不参与场景检测和大小计算）
_SKIP_DIRS = frozenset({".git", ".ai_workspaces", "__pycache__", ".pytest_cache"})
_SKIP_EXTENSIONS = frozenset({".bak", ".pyc", ".pyo"})
_SPARSE_THRESHOLD_BYTES = 50 * 1024 * 1024  # sparse checkout 大小阈值（50MB）
_GIT_TIMEOUT = 30  # git 命令执行超时（秒）
_GIT_INIT_TIMEOUT = 120  # git init/add/commit 超时（秒），初始化操作耗时更长


def _safe_ws_name(project_name: str, task_id: str, name_limit: int = 15) -> str:
    """生成安全的 worktree 目录名，项目名截断到 name_limit 字符避免 Windows 路径超限"""
    safe = project_name.replace(" ", "_")
    if len(safe) > name_limit:
        safe = safe[:name_limit]
    return f"{safe}__wt_{task_id[:8]}"


def _force_rmtree(path: str) -> None:
    """强制删除目录树，兼容 Windows 下 .git 只读文件。

    Windows 上 git objects 文件为只读属性，shutil.rmtree 默认无法删除。
    通过 onerror 回调去除只读属性后重试。
    """
    def _on_error(func, filepath, exc_info):
        if os.name == "nt":
            os.chmod(filepath, stat.S_IWRITE)
            func(filepath)
        else:
            raise

    try:
        shutil.rmtree(path, onerror=_on_error)
    except OSError:
        shutil.rmtree(path, onerror=_on_error)


class WorkspaceLifecycleManager:
    """工作空间统一生命周期管理器

    场景A: 新项目（workspace 为空或路径不存在）
    场景B: 已有项目无 .git（初始化 git 并提交现有文件）
    场景C: 已有项目有 .git（通过 worktree 或 sparse-checkout 隔离）

    四种工作模式: project_root / branch / worktree / shared
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
        self._merge_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        # 项目大小计算缓存 {project_root: (mtime, size)}
        self._size_cache: dict[str, tuple[float, int]] = {}

    # ── 内部工具方法 ──────────────────────────────────────────────

    def _run_git(self, *args: str, cwd: Path, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
        """执行 git 命令（同步，使用 subprocess）"""
        cmd = ["git"] + list(args)
        try:
            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            # BUG-FIX-fix_20260425_git_error_log:
            # 问题根因: 原条件 `r.returncode != 0 and r.stderr` 在 stderr 为空时跳过日志，
            #           导致 git commit 失败时无法追踪原因（Windows 环境下 git 可能通过 stdout 输出错误）。
            # 修复方案: returncode != 0 时始终记录，同时包含 stdout 和 stderr。
            if r.returncode != 0:
                err_parts = []
                if r.stderr.strip():
                    err_parts.append(f"stderr={r.stderr[:200]}")
                if r.stdout.strip():
                    err_parts.append(f"stdout={r.stdout[:200]}")
                detail = " | ".join(err_parts) if err_parts else "(no output)"
                logger.warning("[WorkspaceLifecycle] git %s failed (rc=%d): %s", " ".join(args), r.returncode, detail)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"命令执行超时（{timeout}秒）"
        except FileNotFoundError:
            return -1, "", "未找到 git 命令"

    def _get_merge_lock(self, project_root: str) -> threading.Lock:
        """获取指定 project_root 的合并锁，确保同一项目目录的合并操作串行执行"""
        with self._global_lock:
            if project_root not in self._merge_locks:
                self._merge_locks[project_root] = threading.Lock()
            return self._merge_locks[project_root]

    def _ensure_git_user(self, cwd: Path):
        """配置 workspace 的 git 用户信息"""
        self._run_git("config", "user.email", "agent@agent-os.local", cwd=cwd)
        self._run_git("config", "user.name", "Agent OS", cwd=cwd)

    def _remove_index_lock(self, cwd: Path) -> bool:
        """Remove stale git index.lock if it exists. Returns True if a lock was removed."""
        lock_path = cwd / ".git" / "index.lock"
        if lock_path.exists():
            try:
                lock_path.unlink()
                logger.info("[WorkspaceLifecycle] Removed stale index.lock: %s", lock_path)
                return True
            except OSError as e:
                logger.warning("[WorkspaceLifecycle] Failed to remove index.lock %s: %s", lock_path, e)
                return False
        return False

    def _resolve_main_branch(self, cwd: Path) -> str:
        """动态检测仓库的主分支名，优先尝试 main，回退到实际 HEAD 所在分支。

        BUG-FIX-fix_20260425_main_branch:
        git init 在不同平台/版本下默认分支名不同（main 或 master），
        硬编码 'main' 会导致 checkout/merge 失败。
        """
        rc, out, _ = self._run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
        if rc == 0 and out.strip():
            current = out.strip()
            if current in ("main", "master"):
                return current
        rc2, _, _ = self._run_git("rev-parse", "--verify", "main", cwd=cwd)
        if rc2 == 0:
            return "main"
        return "master"

    def _assert_on_branch(self, expected: str, cwd: Path) -> bool:
        """验证 cwd 当前处于期望分支，绝不 checkout 切换。

        主仓库 / 容器空间都不允许 git checkout，修改任何分支应通过 worktree。
        Returns:
            True if on expected branch, False otherwise.
        """
        rc, current, _ = self._run_git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
        if rc == 0 and current.strip() == expected:
            return True
        logger.warning(
            "[WorkspaceLifecycle] EXPECT %s but on %s — "
            "不允许 checkout 切换分支: cwd=%s",
            expected, current.strip() if rc == 0 else "(unknown)", cwd)
        return False

    def _git_init_and_initial_commit(self, cwd: Path, message: str) -> bool:
        """Initialize a new git repo and make the initial commit with all files.

        Handles edge cases: stale index.lock, pre-existing but empty .git directory,
        and ensures the init -> add -> commit sequence completes atomically.

        Returns:
            True if the repo was successfully initialized with a commit, False otherwise.
        """
        git_dir = cwd / ".git"
        needs_init = True
        if git_dir.exists():
            # .git exists — check if it is a valid repo with at least one commit
            rc, _, _ = self._run_git("rev-parse", "HEAD", cwd=cwd)
            if rc == 0:
                # Valid repo with commits — no need to re-init
                needs_init = False
            else:
                # Empty/corrupt .git — remove it and start fresh
                logger.info("[WorkspaceLifecycle] Existing .git is empty/corrupt, removing: %s", git_dir)
                try:
                    _force_rmtree(str(git_dir))
                except OSError as e:
                    logger.warning("[WorkspaceLifecycle] Failed to remove corrupt .git: %s", e)
                    return False

        if needs_init:
            rc, _, stderr = self._run_git("init", "--initial-branch=main", cwd=cwd)
            if rc != 0:
                logger.warning("[WorkspaceLifecycle] git init --initial-branch=main failed: %s, retry without flag", stderr)
                rc, _, stderr = self._run_git("init", cwd=cwd)
                if rc != 0:
                    logger.warning("[WorkspaceLifecycle] git init failed: %s", stderr)
                    return False
                self._run_git("checkout", "-b", "main", cwd=cwd)

        self._ensure_git_user(cwd)

        gitignore = cwd / ".gitignore"
        if not gitignore.exists():
            try:
                gitignore.write_text("\n".join([
                    "__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/",
                    "*.bak", "*.egg-info/", ".mypy_cache/",
                    "node_modules/", ".env", "*.log", ".tox/",
                ]) + "\n", encoding="utf-8")
            except OSError:
                pass

        self._remove_index_lock(cwd)

        rc, _, stderr = self._run_git("add", "-A", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
        if rc != 0:
            # If add failed due to index.lock, remove it and retry once
            if "index.lock" in (stderr or ""):
                if self._remove_index_lock(cwd):
                    rc, _, stderr = self._run_git("add", "-A", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
            if rc != 0:
                logger.warning("[WorkspaceLifecycle] git add -A failed after retry: %s", stderr)
                return False

        rc, out, stderr = self._run_git("commit", "-m", message, "--allow-empty", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
        if rc != 0:
            if "index.lock" in (stderr or ""):
                if self._remove_index_lock(cwd):
                    rc, out, stderr = self._run_git("commit", "-m", message, "--allow-empty", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
            if rc != 0:
                logger.warning("[WorkspaceLifecycle] git commit failed after retry: %s | stdout: %s", stderr, out)
                return False

        return True

    def _git_add_commit_if_dirty(self, cwd: Path, message: str) -> str | None:
        """暂存并提交变更（如果有），返回 commit hash 或 None"""
        # Remove stale index.lock before any git operations
        self._remove_index_lock(cwd)

        rc, _, _ = self._run_git("add", "-A", cwd=cwd)
        if rc != 0:
            # Retry after removing index.lock
            self._remove_index_lock(cwd)
            rc, _, _ = self._run_git("add", "-A", cwd=cwd)
            if rc != 0:
                return None

        rc, status, _ = self._run_git("status", "--porcelain", cwd=cwd)
        if rc == 0 and status.strip():
            commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
            if commit_rc != 0:
                self._remove_index_lock(cwd)
                commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
                if commit_rc != 0:
                    return None
            _, h, _ = self._run_git("rev-parse", "HEAD", cwd=cwd)
            return h.strip() if h else None
        return None

    # ── 1. 场景检测 ──────────────────────────────────────────────

    @staticmethod
    def _detect_scenario(workspace: str, task_data: dict) -> tuple[str, str]:
        """检测工作空间场景

        workspace 为空 -> new_project，path = {ws_root}/{task_id}
        路径存在且有文件（排除 .git/.ai_workspaces/__pycache__/.pytest_cache）-> existing_project
        路径不存在或无文件 -> new_project

        Returns:
            (scenario: "existing_project"|"new_project", project_root: str)
        """
        task_id = task_data.get("task_id", "")
        ws_root = task_data.get("workspace_root", ".ai_workspaces")
        if not workspace:
            return "new_project", str(Path(ws_root) / task_id)
        path = Path(workspace)
        if not path.exists():
            return "new_project", str(path)
        has_files = False
        for item in path.iterdir():
            if item.name in _SKIP_DIRS:
                continue
            if item.is_file():
                has_files = True
                break
            if item.is_dir():
                try:
                    if any(item.rglob("*")):
                        has_files = True
                        break
                except PermissionError:
                    pass
        return ("existing_project" if has_files else "new_project"), str(path)

    # ── 2. 任务启动 ──────────────────────────────────────────────

    def on_task_start(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """任务启动时的生命周期钩子，根据 is_root 分发到子任务或根任务处理"""
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

        meta = {"mode": "shared", "path": parent_path,
                "parent_workspace": workspace,
                "project_root": parent_meta.get("project_root", "")}
        self._ws_meta_store[task_id] = meta
        return meta

    def init_container_workspace(self, container_task_id: str, workspace: str | None, task_data: dict) -> dict:
        """容器任务的空间初始化（由 TaskWorker 在跳过执行前调用）

        BUG-FIX-fix_20260425_container_workspace_init:
        容器任务必须先初始化工作空间（mkdir + git init），
        否则后续子任务找不到容器空间，各自创建空目录。
        """
        ws_root = task_data.get("workspace_root", ".ai_workspaces")
        if workspace:
            container_path = workspace
        else:
            container_path = str(Path(ws_root) / container_task_id)

        path = Path(container_path)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            if not self._git_init_and_initial_commit(path, "chore: initial container project"):
                raise RuntimeError(f"容器空间初始化失败（git init）: {path}")
        elif not (path / ".git").exists():
            if not self._git_init_and_initial_commit(path, "chore: initial commit for container workspace"):
                raise RuntimeError(f"容器空间初始化失败（git init）: {path}")
        else:
            self._ensure_git_user(path)

        meta = {"mode": "project_root", "path": str(path),
                "branch": "main", "project_root": str(path),
                "is_container_workspace": True}
        self._ws_meta_store[container_task_id] = meta
        self._persist_ws_meta(container_task_id)
        logger.info("[WorkspaceLifecycle] 容器空间已初始化: task_id=%s, path=%s", container_task_id, path)
        return meta

    def _find_container_workspace(self, task_id: str) -> str | None:
        """查找父容器任务的工作空间路径

        BUG-FIX-fix_20260425_container_workspace_init:
        先尝试 restore_ws_meta 从持久化恢复，再查找。
        """
        try:
            task = self._task_tree.get_task(task_id)
            if not task or not task.parent_task_id:
                return None
            parent_task = self._task_tree.get_task(task.parent_task_id)
            if not parent_task:
                return None
            if parent_task.metadata.get("task_scope") != "container":
                return None

            self.restore_ws_meta(parent_task.id)

            parent_meta = self._ws_meta_store.get(parent_task.id, {})
            container_ws = parent_meta.get("path", "")
            if not container_ws:
                container_ws = parent_task.metadata.get("container_workspace", "")
            return container_ws if container_ws else None
        except Exception as e:
            logger.warning("[WorkspaceLifecycle] _find_container_workspace 失败: task_id=%s, error=%s", task_id, e)
            return None

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
        # BUG-FIX-fix_20260425_container_workspace_init:
        # 容器子任务优先查找容器空间，基于容器空间做 worktree/copy
        container_ws = self._find_container_workspace(task_id)
        if container_ws:
            container_path = Path(container_ws).resolve()
            if not (container_path / ".git").exists():
                if not self._git_init_and_initial_commit(container_path, "chore: init container repo"):
                    raise RuntimeError(f"容器空间 git 初始化失败: {container_path}")
            self._ensure_git_user(container_path)
            self._git_add_commit_if_dirty(container_path, f"chore: auto-save before subtask {task_id}")

            branch = f"task/{task_id}"
            ws_dir = container_path.parent / _safe_ws_name(
                container_path.name, task_id)
            project_size = self._calc_project_size(str(container_path), task_id)
            threshold = self._config.get("workspace", {}).get("sparse_threshold_mb", 50) * 1024 * 1024

            if project_size > threshold:
                self._setup_sparse_worktree(ws_dir, container_path, branch)
            else:
                rc, _, stderr = self._run_git(
                    "worktree", "add", "-b", branch, str(ws_dir), cwd=container_path)
                if rc != 0:
                    raise RuntimeError(
                        f"容器子任务 worktree 创建失败（不降级）: task_id={task_id}, error={stderr}")
            self._ensure_git_user(ws_dir)
            meta = {"mode": "worktree", "path": str(ws_dir),
                    "branch": branch, "project_root": str(container_path)}
            self._ws_meta_store[task_id] = meta
            return meta

        # 检测到父任务是容器但找不到工作空间时，报错而非静默降级
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
            pass

        scenario, project_root = self._detect_scenario(workspace, task_data)
        root_path = Path(project_root)
        logger.info("[WorkspaceLifecycle] _start_root_task: task_id=%s, scenario=%s, workspace=%s, root_path=%s",
                     task_id, scenario, workspace, root_path)

        if scenario == "new_project":
            root_path.mkdir(parents=True, exist_ok=True)
            if not self._git_init_and_initial_commit(root_path, "chore: initial project"):
                raise RuntimeError(f"新项目空间初始化失败（git init）: task_id={task_id}, path={root_path}")
            meta = {"mode": "project_root", "path": str(root_path),
                    "branch": "main", "project_root": str(root_path)}
        elif not (root_path / ".git").exists():
            if not self._git_init_and_initial_commit(root_path, "chore: initial commit for workspace isolation"):
                raise RuntimeError(f"已有项目 git 初始化失败: task_id={task_id}, path={root_path}")
            meta = {"mode": "project_root", "path": str(root_path),
                    "branch": "main", "project_root": str(root_path)}
        else:
            self._ensure_git_user(root_path)
            self._git_add_commit_if_dirty(
                root_path,
                f"chore: auto-save before worktree for task {task_id}")
            branch = f"task/{task_id}"
            ws_dir = Path(
                task_data.get("workspace_root", ".ai_workspaces")
            ) / _safe_ws_name(root_path.name, task_id)
            project_size = self._calc_project_size(str(root_path), task_id)
            threshold = self._config.get("workspace", {}).get("sparse_threshold_mb", 50) * 1024 * 1024

            if project_size > threshold:
                self._setup_sparse_worktree(ws_dir, root_path, branch)
            else:
                rc, _, stderr = self._run_git(
                    "worktree", "add", "-b", branch, str(ws_dir), cwd=root_path)
                if rc != 0:
                    raise RuntimeError(f"git worktree add 失败（不降级）: task_id={task_id}, error={stderr}")
            self._ensure_git_user(ws_dir)
            meta = {"mode": "worktree", "path": str(ws_dir),
                    "branch": branch, "project_root": str(root_path)}

        self._ws_meta_store[task_id] = meta
        return meta

    # ── 3. Sparse Worktree 设置 ──────────────────────────────────

    def _setup_sparse_worktree(self, ws_dir: Path, project_root: Path, branch: str):
        """为大项目设置 sparse-checkout worktree，排除目录通过符号链接关联（Windows 用 junction point 降级）"""
        self._run_git("worktree", "add", "--no-checkout", "-b", branch, str(ws_dir), cwd=project_root)
        self._run_git("sparse-checkout", "init", "--cone", cwd=ws_dir)
        whitelist = self._config.get("workspace", {}).get("worktree_include_patterns", ["src", "config"])
        if whitelist:
            self._run_git("sparse-checkout", "set", *whitelist, cwd=ws_dir)
        link_patterns = self._config.get("workspace", {}).get("worktree_link_patterns", [])
        for link_name in link_patterns:
            src, dst = project_root / link_name, ws_dir / link_name
            if src.exists() and not dst.exists():
                try:
                    if os.name == "nt":
                        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                                       capture_output=True, timeout=10)
                    else:
                        dst.symlink_to(src)
                except Exception as e:
                    logger.warning("[WorkspaceLifecycle] 创建符号链接失败: %s -> %s, error=%s", src, dst, e)

    # ── 4. 项目大小计算 ──────────────────────────────────────────

    def _calc_project_size(self, project_root: str, task_id: str) -> int:
        """计算项目工作文件总大小（不含 .git），两轮扫描策略 + 增量缓存"""
        root = Path(project_root)
        # 检查缓存
        if project_root in self._size_cache:
            cached_mtime, cached_size = self._size_cache[project_root]
            git_dir = root / ".git"
            cur = git_dir.stat().st_mtime if git_dir.exists() else root.stat().st_mtime
            if cur == cached_mtime:
                return cached_size
        total = 0
        for item in root.iterdir():
            if item.name in _SKIP_DIRS:
                continue
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
            elif item.is_dir():
                for f in item.rglob("*"):
                    if not f.is_file():
                        continue
                    if any(p in _SKIP_DIRS for p in f.relative_to(root).parts):
                        continue
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        git_dir = root / ".git"
        mtime = git_dir.stat().st_mtime if git_dir.exists() else root.stat().st_mtime
        self._size_cache[project_root] = (mtime, total)
        logger.debug("[WorkspaceLifecycle] 项目大小: root=%s, size=%d, task=%s", project_root, total, task_id)
        return total

    # ── 5. 评估前保存 ────────────────────────────────────────────

    def on_before_evaluate(self, workspace: str) -> dict:
        """评估前保存：git add -A + git commit -m "checkpoint: before evaluate" """
        ws_path = Path(workspace)
        if not ws_path.exists():
            return {"success": False, "error": f"工作空间不存在: {workspace}"}
        self._ensure_git_user(ws_path)
        commit_hash = self._git_add_commit_if_dirty(ws_path, "checkpoint: before evaluate")
        rc, status, _ = self._run_git("status", "--porcelain", cwd=ws_path)
        return {"success": True, "commit_hash": commit_hash,
                "has_changes": bool(status and status.strip())}

    # ── 6. 评估通过 ──────────────────────────────────────────────

    def on_eval_passed(self, task_id: str, workspace: str, ws_meta: dict) -> dict:
        """评估通过后按 mode 分发合并逻辑，并发安全：按 project_root 粒度加锁"""
        mode = ws_meta.get("mode", "")
        project_root = ws_meta.get("project_root", "")
        lock = self._get_merge_lock(project_root)
        with lock:
            if mode == "project_root":
                result = self._merge_project_root(workspace, ws_meta)
                if result.get("success"):
                    self._cleanup_project_root(workspace)
                return result
            if mode == "branch":
                return self._merge_branch(workspace, ws_meta)
            if mode == "worktree":
                result = self._safe_merge(workspace, ws_meta)
                self._cleanup_worktree(workspace, ws_meta)
                return result
            if mode == "shared":
                return self._safe_merge(workspace, ws_meta)
            logger.warning("[WorkspaceLifecycle] 未知 mode: %s, task_id=%s", mode, task_id)
            return {"success": False, "error": f"未知工作模式: {mode}"}

    def _merge_project_root(self, workspace: str, ws_meta: dict) -> dict:
        """project_root 模式合并：将 workspace 中新增/修改的文件复制回主项目"""
        import shutil
        ws_path = Path(workspace)
        project_root = Path(ws_meta.get("project_root", workspace))
        self._ensure_git_user(ws_path)
        commit_hash = self._git_add_commit_if_dirty(ws_path, "chore: task completed")
        if commit_hash is None:
            _, h, _ = self._run_git("rev-parse", "HEAD", cwd=ws_path)
            commit_hash = h.strip() if h else None
        _SKIP_MERGE = frozenset({
                ".git", ".ai_workspaces", "__pycache__",
                ".pytest_cache", ".mypy_cache", "node_modules",
                ".claude", ".codebuddy", ".workbuddy", ".trae",
                ".coverage", "logs", "data", ".venv", ".env",
                "dist", ".next", ".nuxt", "build", ".cache",
                ".tox", ".eggs", "*.egg-info",
            })
        merged_files: list[str] = []
        for child in ws_path.iterdir():
            if child.name in _SKIP_MERGE:
                continue
            dst = project_root / child.name
            try:
                if child.is_dir():
                    shutil.copytree(
                        str(child), str(dst), dirs_exist_ok=True)
                else:
                    shutil.copy2(str(child), str(dst))
                merged_files.append(child.name)
            except OSError:
                pass
        if merged_files:
            self._ensure_git_user(project_root)
            self._run_git("add", "-A", cwd=project_root)
            self._git_add_commit_if_dirty(project_root, f"merge: workspace {ws_path.name} completed")
        return {"success": True, "action": "copy_merge", "commit_hash": commit_hash,
                "merged_files": merged_files}

    def _merge_branch(self, workspace: str, ws_meta: dict) -> dict:
        """branch 模式合并：验证在主分支上后 git merge，失败降级为文件复制。

        不执行 git checkout，当前必须在目标分支上。
        """
        project_root = Path(ws_meta.get("project_root", workspace))
        branch = ws_meta.get("branch", "")
        self._ensure_git_user(project_root)
        main_branch = self._resolve_main_branch(project_root)
        if not self._assert_on_branch(main_branch, project_root):
            logger.warning(
                "[WorkspaceLifecycle] 不在 %s 上，降级为 copy_merge",
                main_branch)
            return self._copy_merge(workspace, str(project_root))
        rc, _, stderr = self._run_git("merge", branch, cwd=project_root)
        if rc != 0:
            self._run_git("merge", "--abort", cwd=project_root)
            logger.warning(
                "[WorkspaceLifecycle] git merge 失败，降级为 copy_merge: %s",
                stderr[:200])
            return self._copy_merge(workspace, str(project_root))
        self._verify_merge_in_main(branch, cwd=project_root)
        return {"success": True, "action": "merged", "branch": branch}

    def _cleanup_worktree(self, workspace: str, ws_meta: dict):
        """清理 worktree 和对应分支，目录残留时强制 rmtree（仅删除含 __wt_ 的路径）"""
        project_root = Path(ws_meta.get("project_root", ""))
        branch = ws_meta.get("branch", "")
        if project_root.exists():
            self._run_git("worktree", "remove", str(workspace), "--force", cwd=project_root)
            if branch:
                self._run_git("branch", "-D", branch, cwd=project_root)
        ws_path = Path(workspace).resolve()
        if ws_path.exists() and "__wt_" in ws_path.name:
            try:
                _force_rmtree(str(ws_path))
                logger.info("[WorkspaceLifecycle] 强制清理残留 worktree 目录: %s", workspace)
            except OSError as e:
                logger.warning("[WorkspaceLifecycle] 强制清理 worktree 目录失败: %s, %s", workspace, e)

    def _cleanup_project_root(self, workspace: str):
        """合并成功后清理 workspace 目录"""
        ws_path = Path(workspace)
        if ws_path.exists() and ".ai_workspaces" in str(ws_path):
            try:
                _force_rmtree(str(ws_path))
                logger.info("[WorkspaceLifecycle] 已清理 workspace: %s", workspace)
            except OSError as e:
                logger.warning("[WorkspaceLifecycle] 清理 workspace 失败: %s, %s", workspace, e)

    # ── 7. 评估失败 ──────────────────────────────────────────────

    def on_eval_failed(self, task_id: str, workspace: str, ws_meta: dict) -> dict:
        """评估失败：reject_count >= max_retries 时回滚，否则允许重试"""
        reject_count = ws_meta.get("reject_count", 0) + 1
        max_retries = ws_meta.get("max_retries", self._config.get("max_retries", 3))
        ws_meta["reject_count"] = reject_count
        self._ws_meta_store[task_id] = ws_meta
        if reject_count >= max_retries:
            logger.info("[WorkspaceLifecycle] 评估失败超限，回滚: task_id=%s, count=%d", task_id, reject_count)
            return self.on_task_failed(workspace, ws_meta)
        logger.info("[WorkspaceLifecycle] 评估失败，重试: task_id=%s, count=%d/%d", task_id, reject_count, max_retries)
        return {"success": True, "action": "retry", "reject_count": reject_count}

    # ── 8. 任务异常回滚 ──────────────────────────────────────────

    def on_task_failed(self, workspace: str, ws_meta: dict) -> dict:
        """异常回滚：worktree 模式先 checkout+clean 再清理 worktree，其他模式直接回滚"""
        ws_path = Path(workspace)
        if not ws_path.exists():
            return {"success": False, "error": f"工作空间不存在: {workspace}"}
        mode = ws_meta.get("mode", "")
        if mode == "worktree":
            self._run_git("checkout", "--", ".", cwd=ws_path)
            self._run_git("clean", "-fd", cwd=ws_path)
            self._cleanup_worktree(workspace, ws_meta)
            logger.info("[WorkspaceLifecycle] worktree 回滚并清理: %s", workspace)
            return {"success": True, "action": "rollback_worktree"}
        self._run_git("checkout", "--", ".", cwd=ws_path)
        self._run_git("clean", "-fd", cwd=ws_path)
        logger.info("[WorkspaceLifecycle] 已回滚工作空间: %s", workspace)
        return {"success": True, "action": "rollback"}

    # ── 9. 安全合并 ──────────────────────────────────────────────

    def _safe_merge(self, workspace: str, ws_meta: dict) -> dict:
        """安全合并：验证在主分支上后尝试 git merge，失败降级为文件复制。

        设计原则：不执行 git checkout 切换分支。
        合并层级逐层向上：子任务 worktree → 容器空间 → 主仓库。
        """
        project_root = ws_meta.get("project_root", "")
        branch = ws_meta.get("branch", "")
        if not project_root:
            return {"success": False, "error": "缺少 project_root 信息"}
        proj_path, ws_path = Path(project_root), Path(workspace)
        # workspace 中提交所有变更
        self._ensure_git_user(ws_path)
        self._git_add_commit_if_dirty(
            ws_path, "chore: auto commit before merge")
        # 目标目录提交暂存变更
        self._ensure_git_user(proj_path)
        main_branch = self._resolve_main_branch(proj_path)
        if not self._assert_on_branch(main_branch, proj_path):
            logger.warning(
                "[WorkspaceLifecycle] 不在 %s 上，降级为 copy_merge",
                main_branch)
            return self._copy_merge(workspace, project_root)
        self._git_add_commit_if_dirty(
            proj_path, "chore: stage untracked files before merge")
        # 尝试 git merge（不切换分支，在当前分支上合并）
        if branch:
            rc, _, stderr = self._run_git(
                "merge", branch, cwd=proj_path)
            if rc == 0:
                return {"success": True, "action": "merged",
                        "method": "git_merge"}
            logger.warning(
                "[WorkspaceLifecycle] git merge 失败，"
                "降级为 copy_merge: branch=%s, error=%s",
                branch, stderr[:200] if stderr else "(empty)")
            self._run_git("merge", "--abort", cwd=proj_path)
            return self._copy_merge(workspace, project_root)
        return self._copy_merge(workspace, project_root)

    def _copy_merge(self, workspace: str, target_dir: str) -> dict:
        """通过文件复制方式合并变更（冲突降级策略），跳过排除目录"""
        src, dst = Path(workspace), Path(target_dir)
        merged: list[str] = []
        for item in src.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(src)
            if any(p in _SKIP_DIRS for p in rel.parts):
                continue
            if item.suffix in _SKIP_EXTENSIONS:
                continue
            target_file = dst / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target_file))
            merged.append(str(rel))
        if merged:
            self._ensure_git_user(dst)
            self._run_git("add", "-A", cwd=dst)
            self._git_add_commit_if_dirty(dst, f"merge: copy_merge ({len(merged)} files)")
        return {"success": True, "action": "merged", "method": "copy", "merged_files": merged}

    # ── 10. 合并验证 ─────────────────────────────────────────────

    def _verify_merge_in_main(self, branch_name: str, cwd: Path | None = None) -> bool:
        """验证分支已合并到主分支：git log main..{branch} 应为空，不为空则阻止后续清理"""
        work_dir = cwd or self._base_path
        main_branch = self._resolve_main_branch(work_dir)
        rc, log_output, _ = self._run_git("log", f"{main_branch}..{branch_name}", cwd=work_dir)
        if rc != 0:
            logger.warning("[WorkspaceLifecycle] 验证合并状态失败: branch=%s", branch_name)
            return False
        if log_output.strip():
            logger.warning("[WorkspaceLifecycle] 分支未完全合并: branch=%s, 未合并=%d",
                           branch_name, len(log_output.splitlines()))
            return False
        return True

    # ── 11. ws_meta 持久化与恢复 ────────────────────────────────────

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
            logger.warning("[WorkspaceLifecycle] _persist_ws_meta 失败: task_id=%s, error=%s", task_id, e)

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

    # ── 12. 工作空间清理 ──────────────────────────────────────────

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
        elif mode == "project_root":
            ws_path = Path(workspace).resolve()
            if ws_path.exists() and ".ai_workspaces" in str(ws_path):
                try:
                    _force_rmtree(str(ws_path))
                    result["dir_removed"] = True
                except OSError as e:
                    logger.warning("[WorkspaceLifecycle] cleanup_workspace rmtree 失败: %s, %s", workspace, e)

        self._ws_meta_store.pop(task_id, None)
        logger.info("[WorkspaceLifecycle] cleanup_workspace: task_id=%s, mode=%s, result=%s", task_id, mode, result)
        return result
