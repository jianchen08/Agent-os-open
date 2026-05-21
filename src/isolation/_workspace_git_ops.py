"""工作空间 Git 操作 Mixin。

提供 WorkspaceLifecycleManager 的 Git 命令封装和分支管理方法。
从 workspace_lifecycle.py 拆分而来。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 排除的目录（不参与场景检测和大小计算）
_SKIP_DIRS = frozenset({".git", ".ai_workspaces", "__pycache__", ".pytest_cache"})
_SKIP_EXTENSIONS = frozenset({".bak", ".pyc", ".pyo"})
_SPARSE_THRESHOLD_BYTES = 50 * 1024 * 1024  # sparse checkout 大小阈值（50MB）
_GIT_TIMEOUT = 30  # git 命令执行超时（秒）
_GIT_INIT_TIMEOUT = 120  # git init/add/commit 超时（秒），初始化操作耗时更长


def _safe_ws_name(project_name: str, task_id: str, name_limit: int = 15) -> str:
    """生成安全的 worktree 目录名，项目名截断到 name_limit 字符避免 Windows 路径超限。"""
    import re
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', project_name)
    safe = safe.replace(" ", "_")
    safe = re.sub(r'_+', '_', safe).strip('._')
    if not safe:
        safe = "ws"
    if len(safe) > name_limit:
        safe = safe[:name_limit].rstrip('._')
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


class _GitOpsMixin:
    """Git 操作 Mixin，提供 Git 命令执行和分支管理方法。

    要求宿主类提供以下属性：
    - self._config: dict[str, Any]
    - self._base_path: Path
    - self._main_branch: str
    - self._merge_locks: dict[str, threading.Lock]
    - self._global_lock: threading.Lock
    - self._size_cache: dict[str, tuple[float, int]]
    - self._ws_meta_store: Any
    - self._task_tree: Any
    - self._resource_merge: Any
    """

    def _get_workspace_root(self) -> Path:
        """从配置中读取工作空间基目录，解析为绝对路径。

        workspace.root 支持绝对路径和相对路径（相对于 CWD）。
        返回的是所有工作空间（worktree/container）的父目录。
        例如配置 root: "D:/myproject" 则返回 Path("D:/myproject")。
        """
        from isolation.workspace import _DEFAULT_WORKSPACE_ROOT
        raw = self._config.get("workspace", {}).get("root", _DEFAULT_WORKSPACE_ROOT)
        p = Path(raw)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p.resolve()

    def _run_git(self, *args: str, cwd: Path, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
        """执行 git 命令（同步，使用 subprocess）"""
        cmd = ["git"] + list(args)
        try:
            r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
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

    def _record_main_branch(self):
        """记录项目根目录的主分支，用于检测外部分支切换。

        BUG-FIX-fix_20260512_worktree_base:
        原逻辑只记录当前 HEAD 分支名，不验证是否为真正的主分支。
        当用户在 feature 分支上启动任务时，worktree 会基于 feature 分支创建，
        合并时 _resolve_main_branch 却硬找 main → 分支不匹配 → 降级为 copy_merge
        → 旧文件覆盖新文件。现增加非主分支警告。
        """
        try:
            rc, out, _ = self._run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=self._base_path)
            if rc == 0 and out.strip():
                branch = out.strip()
                self._main_branch = branch
                if branch in ("main", "master"):
                    logger.debug("[WorkspaceLifecycle] 记录主分支: %s", branch)
                else:
                    logger.warning(
                        "[WorkspaceLifecycle] 当前分支 '%s' 不是主分支(main/master)，"
                        "worktree 将基于此分支创建。建议在主分支上启动任务。", branch)
        except Exception:
            logger.warning("[WorkspaceLifecycle] _record_main_branch 失败", exc_info=True)

    def _guard_root_branch(self, cwd: Path) -> bool:
        """守卫：如果 cwd 是项目根目录，验证分支未被外部切换。

        workspace_lifecycle 只允许对项目根目录做 commit 和 merge，
        不允许 checkout 切换分支。如果检测到分支变更则拒绝操作。
        """
        try:
            if not self._main_branch:
                return True
            if cwd.resolve() != self._base_path.resolve():
                return True
            rc, current, _ = self._run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
            if rc == 0 and current.strip() == self._main_branch:
                return True
            logger.warning(
                "[WorkspaceLifecycle] BRANCH GUARD: 项目根目录分支已变更! "
                "expected=%s, actual=%s — 跳过操作避免写入错误分支",
                self._main_branch, current.strip() if rc == 0 else "(unknown)")
            return False
        except Exception:
            logger.warning("[WorkspaceLifecycle] _guard_root_branch 检查异常，默认放行", exc_info=True)
            return True

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
            rc, _, _ = self._run_git("rev-parse", "HEAD", cwd=cwd)
            if rc == 0:
                needs_init = False
            else:
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
                ws_root = self._get_workspace_root()
                ws_root_name = ws_root.name + "/"
                gitignore.write_text("\n".join([
                    ws_root_name,
                    "__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/",
                    "*.bak", "*.egg-info/", ".mypy_cache/",
                    "node_modules/", ".env", "*.log", ".tox/",
                ]) + "\n", encoding="utf-8")
            except OSError:
                pass

        self._remove_index_lock(cwd)

        rc, _, stderr = self._run_git("add", "-A", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
        if rc != 0:
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
        self._remove_index_lock(cwd)

        rc, _, _ = self._run_git("add", "-A", cwd=cwd)
        if rc != 0:
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

    def _git_add_tracked_and_commit(self, cwd: Path, message: str) -> str | None:
        """只提交已跟踪文件的修改，不添加未跟踪文件。返回 commit hash 或 None。"""
        self._remove_index_lock(cwd)
        rc, _, _ = self._run_git("add", "-u", cwd=cwd)
        if rc != 0:
            self._remove_index_lock(cwd)
            rc, _, _ = self._run_git("add", "-u", cwd=cwd)
            if rc != 0:
                return None
        rc, status, _ = self._run_git("status", "--porcelain", "-uno", cwd=cwd)
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

    def _effective_skip_dirs(self) -> frozenset[str]:
        """合并硬编码排除目录和配置文件中的 worktree_exclude_patterns。"""
        ws_cfg = self._config.get("workspace", {})
        extra = frozenset(ws_cfg.get("worktree_exclude_patterns", []))
        return _SKIP_DIRS | extra

    def _copy_project_to_container(self, container_path: Path, src: Path | None = None) -> int:
        """从指定源目录复制文件到容器空间，跳过排除目录和扩展名。返回复制的文件数。"""
        _src = src if src is not None else self._base_path
        if not _src.exists():
            return 0
        skip = self._effective_skip_dirs()
        count = 0
        for item in _src.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(src)
            if any(p in skip for p in rel.parts):
                continue
            if item.suffix in _SKIP_EXTENSIONS:
                continue
            target = container_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target))
            count += 1
        return count

    def _calc_project_size(self, project_root: str, task_id: str) -> int:
        """计算项目工作文件总大小（不含 .git），两轮扫描策略 + 增量缓存"""
        root = Path(project_root)
        skip = self._effective_skip_dirs()
        if project_root in self._size_cache:
            cached_mtime, cached_size = self._size_cache[project_root]
            git_dir = root / ".git"
            cur = git_dir.stat().st_mtime if git_dir.exists() else root.stat().st_mtime
            if cur == cached_mtime:
                return cached_size
        total = 0
        for item in root.iterdir():
            if item.name in skip:
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
                    if any(p in skip for p in f.relative_to(root).parts):
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

    def _worktree_add_with_repair(
        self, repo_path: Path, branch: str, ws_dir: Path, task_id: str,
    ) -> None:
        """创建 worktree，失败时自动 prune 并重试一次。

        常见失败原因：之前 worktree 清理不彻底，.git/worktrees 下残留引用，
        导致 git 认为路径状态不一致。prune 可清除这些失效引用。
        """
        rc, _, stderr = self._run_git(
            "worktree", "add", "-b", branch, str(ws_dir), cwd=repo_path)
        if rc == 0:
            return

        logger.warning(
            "[WorkspaceLifecycle] worktree add 失败，尝试 prune 修复: "
            "task_id=%s, path=%s, error=%s",
            task_id, repo_path, stderr,
        )
        self._run_git("worktree", "prune", cwd=repo_path)
        if ws_dir.exists():
            def _remove_readonly(func, path, exc_info):
                import stat
                os.chmod(path, stat.S_IWRITE)
                func(path)
            shutil.rmtree(str(ws_dir), onexc=_remove_readonly)
        self._run_git("branch", "-D", branch, cwd=repo_path)

        rc, _, stderr = self._run_git(
            "worktree", "add", "-b", branch, str(ws_dir), cwd=repo_path)
        if rc != 0:
            raise RuntimeError(
                f"git worktree add 失败（prune 后重试仍失败）: "
                f"task_id={task_id}, error={stderr}")

    def _setup_sparse_worktree(self, ws_dir: Path, project_root: Path, branch: str):
        """为大项目设置 sparse-checkout worktree，排除目录通过符号链接关联（Windows 用 junction point 降级）"""
        self._run_git("worktree", "add", "--no-checkout", "-b", branch, str(ws_dir), cwd=project_root)
        self._run_git("sparse-checkout", "init", "--cone", cwd=ws_dir)
        whitelist = self._config.get("workspace", {}).get("worktree_include_patterns", ["src", "config"])
        if whitelist:
            self._run_git("sparse-checkout", "set", *whitelist, cwd=ws_dir)
        self._run_git("checkout", "HEAD", cwd=ws_dir)
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

    def _detect_scenario(self, workspace: str, task_data: dict) -> tuple[str, str]:
        """检测工作空间场景

        workspace 为空 -> new_project，path = {ws_root}/{task_id}
        路径存在且有文件（排除 .git/.ai_workspaces/__pycache__/.pytest_cache）-> existing_project
        路径不存在或无文件 -> new_project

        Returns:
            (scenario: "existing_project"|"new_project", project_root: str)
        """
        task_id = task_data.get("task_id", "")
        ws_root = self._get_workspace_root()
        if not workspace:
            return "new_project", str(ws_root / task_id)
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
