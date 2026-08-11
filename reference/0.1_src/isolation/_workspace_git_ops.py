"""工作空间 Git 操作 Mixin。

提供 WorkspaceLifecycleManager 的 Git 命令封装和分支管理方法。
从 workspace_lifecycle.py 拆分而来。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 排除的目录（不参与场景检测、复制和大小计算）
_SKIP_DIRS = frozenset({".git", ".ai_workspaces", "__pycache__", ".pytest_cache", "data"})
_SKIP_EXTENSIONS = frozenset({".bak", ".pyc", ".pyo"})
_WIN_RESERVED_NAMES = frozenset(
    {
        "nul",
        "NUL",
        "CON",
        "PRN",
        "AUX",
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
_SPARSE_THRESHOLD_BYTES = 50 * 1024 * 1024  # sparse checkout 大小阈值（50MB）
_GIT_TIMEOUT = 30  # git 命令执行超时（秒）
_GIT_INIT_TIMEOUT = 120  # git init/add/commit 超时（秒），初始化操作耗时更长


def _safe_ws_name(project_name: str, task_id: str, name_limit: int = 15) -> str:
    """生成安全的 worktree 目录名，项目名截断到 name_limit 字符避免 Windows 路径超限。"""
    import re  # noqa: PLC0415

    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", project_name)
    safe = safe.replace(" ", "_")
    safe = re.sub(r"_+", "_", safe).strip("._")
    if not safe:
        safe = "ws"
    if len(safe) > name_limit:
        safe = safe[:name_limit].rstrip("._")
    return f"{safe}__wt_{task_id[:8]}"


def _force_rmtree(path: str) -> None:
    """强制删除目录树，兼容 Windows 下 .git 只读文件。

    Windows 上 git objects 文件为只读属性，shutil.rmtree 默认无法删除。
    通过 onerror 回调去除只读属性后重试。
    """

    def _on_error(func, filepath, exc_info):
        if os.name == "nt":
            os.chmod(filepath, stat.S_IWRITE)  # noqa: PTH101
            func(filepath)
        else:
            raise  # noqa: PLE0704

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

    _WIN_ABS_PATH = __import__("re").compile(r"^[a-zA-Z]:[/\\]")

    def _get_workspace_root(self) -> Path:
        """从配置中读取工作空间基目录，解析为绝对路径。

        workspace.root 支持绝对路径和相对路径（相对于 CWD）。
        返回的是所有工作空间（worktree/container）的父目录。
        例如配置 root: "D:/myproject" 则返回 Path("D:/myproject")。
        """
        from isolation.workspace import _DEFAULT_WORKSPACE_ROOT  # noqa: PLC0415

        raw = self._config.get("workspace", {}).get("root", _DEFAULT_WORKSPACE_ROOT)
        if self._WIN_ABS_PATH.match(raw):
            return Path(raw)
        p = Path(raw)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p.resolve()

    def _run_git(self, *args: str, cwd: Path, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
        """执行 git 命令（同步，使用 subprocess）"""
        cmd = ["git"] + list(args)
        try:
            r = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
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
        except OSError as e:
            # Windows 上 cwd 不存在时 subprocess.run 抛 NotADirectoryError [WinError 267]。
            # 这里返回错误码而非抛异常，让上层按 rc!=0 走合并失败分支
            # （complete_evaluation(passed=False)），避免合并门控
            # （_safe_merge → on_eval_passed → merge_worktree_before_complete）
            # 一路崩溃成"管道退出后评估执行失败: [WinError 267] 目录名称无效"。
            return -1, "", f"git 工作目录无效或不存在: {cwd} ({e})"

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

        不能硬编码 'main'：git init 在不同平台/版本下默认分支名不同（main 或 master），
        硬编码会导致 checkout/merge 失败。
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
        rc, current, _ = self._run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
        if rc == 0 and current.strip() == expected:
            return True
        logger.warning(
            "[WorkspaceLifecycle] EXPECT %s but on %s — 不允许 checkout 切换分支: cwd=%s",
            expected,
            current.strip() if rc == 0 else "(unknown)",
            cwd,
        )
        return False

    def _record_main_branch(self):
        """记录项目根目录的主分支，用于检测外部分支切换。

        记录当前 HEAD 分支名并验证是否为真正的主分支：若用户在 feature 分支上启动
        任务，worktree 会基于 feature 分支创建，合并时分支不匹配会降级为 copy_merge
        导致旧文件覆盖新文件，因此对非主分支情况发出警告。
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
                        "worktree 将基于此分支创建。建议在主分支上启动任务。",
                        branch,
                    )
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
                self._main_branch,
                current.strip() if rc == 0 else "(unknown)",
            )
            return False
        except Exception:
            logger.warning("[WorkspaceLifecycle] _guard_root_branch 检查异常，默认放行", exc_info=True)
            return True

    def _git_init_and_initial_commit(self, cwd: Path, message: str) -> bool:  # noqa: PLR0912
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
                logger.warning(
                    "[WorkspaceLifecycle] git init --initial-branch=main failed: %s, retry without flag", stderr
                )
                rc, _, stderr = self._run_git("init", cwd=cwd)
                if rc != 0:
                    logger.warning("[WorkspaceLifecycle] git init failed: %s", stderr)
                    return False
                self._run_git("checkout", "-b", "main", cwd=cwd)

        self._ensure_git_user(cwd)

        gitignore = cwd / ".gitignore"
        if not gitignore.exists():
            logger.warning("[WorkspaceLifecycle] .gitignore 不存在，生成最小保护版本: %s", gitignore)
            with contextlib.suppress(OSError):
                gitignore.write_text(
                    "data/\n__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\nnode_modules/\n.env\n*.log\n*.bak\n",
                    encoding="utf-8",
                )

        self._remove_index_lock(cwd)

        rc, _, stderr = self._run_git("add", "-A", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
        if rc != 0:
            if "index.lock" in (stderr or "") and self._remove_index_lock(cwd):
                rc, _, stderr = self._run_git("add", "-A", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
            if rc != 0:
                logger.warning("[WorkspaceLifecycle] git add -A failed (非致命，继续提交): %s", stderr)

        rc, out, stderr = self._run_git("commit", "-m", message, "--allow-empty", cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
        if rc != 0:
            if "index.lock" in (stderr or ""):  # noqa: SIM102
                if self._remove_index_lock(cwd):
                    rc, out, stderr = self._run_git(
                        "commit", "-m", message, "--allow-empty", cwd=cwd, timeout=_GIT_INIT_TIMEOUT
                    )
            if rc != 0:
                logger.warning("[WorkspaceLifecycle] git commit failed after retry: %s | stdout: %s", stderr, out)
                return False

        return True

    def _git_add_commit_if_dirty(self, cwd: Path, message: str) -> str | None:
        """暂存并提交变更（如果有），返回 commit hash 或 None。

        先用 git status --porcelain 检查是否有变更，无变更直接返回，避免无条件
        执行 git add -A（遍历整个项目添加所有文件到 index，大项目耗时 5-15s）。
        有变更时才执行 git add -A + commit。
        """
        self._remove_index_lock(cwd)

        rc, status, _ = self._run_git("status", "--porcelain", cwd=cwd)
        if rc != 0 or not status.strip():
            return None

        gitignore = cwd / ".gitignore"
        if not gitignore.exists():
            logger.warning("[WorkspaceLifecycle] .gitignore 不存在，生成最小保护版本: %s", gitignore)
            with contextlib.suppress(OSError):
                gitignore.write_text(
                    "data/\n__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\nnode_modules/\n.env\n*.log\n*.bak\n",
                    encoding="utf-8",
                )

        rc, _, _ = self._run_git("add", "-A", cwd=cwd)
        if rc != 0:
            self._remove_index_lock(cwd)
            rc, _, _ = self._run_git("add", "-A", cwd=cwd)
            if rc != 0:
                return None

        commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
        if commit_rc != 0:
            self._remove_index_lock(cwd)
            commit_rc, _, _ = self._run_git("commit", "-m", message, cwd=cwd)
            if commit_rc != 0:
                return None
        _, h, _ = self._run_git("rev-parse", "HEAD", cwd=cwd)
        return h.strip() if h else None

    def _autosave_before_worktree(self, cwd: Path, message: str, task_id: str) -> None:
        """worktree 创建前的 auto-save，提交失败时中断以保护数据。

        auto-save 后强制校验工作区已干净：`_git_add_commit_if_dirty` 在 git add/commit
        失败时返回 None，与"无变更"无法区分；若脏改动未能提交，worktree 会基于旧 HEAD
        创建（不含这些改动），合并回 project_root 后这些改动就永久丢失。因此残留已跟踪
        脏改动即视为致命错误，中断 worktree 创建——宁可任务失败，也不丢数据。
        """
        self._git_add_commit_if_dirty(cwd, message)
        # 只校验已跟踪文件（-uno 忽略 untracked）：已跟踪文件修改丢失才是
        # 不可逆真损失；untracked 独立于 git，不受 worktree/merge 流程影响，
        # 且运行时生成的 .gitignore 文件（日志等）会让全量 status 误报。
        rc, status, _ = self._run_git("status", "--porcelain", "-uno", cwd=cwd)
        if rc == 0 and status.strip():
            dirty = [line.strip() for line in status.splitlines() if line.strip()]
            raise RuntimeError(
                f"auto-save 失败：工作区仍存在未提交的已跟踪变更，"
                f"为避免数据丢失中止 worktree 创建。task_id={task_id}, "
                f"path={cwd}, 文件={dirty[:10]}"
            )
        if rc != 0:
            # 校验命令本身失败不应阻塞任务启动（避免 git 偶发故障放大成任务失败），
            # 但必须留下告警便于排查。
            logger.warning(
                "[WorkspaceLifecycle] auto-save 后状态校验命令失败，无法确认工作区干净（放行）: task_id=%s, path=%s",
                task_id,
                cwd,
            )

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
        try:
            for item in _src.rglob("*"):
                try:
                    if not item.is_file():
                        continue
                    if item.name in _WIN_RESERVED_NAMES:
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
                except (OSError, PermissionError, ValueError):
                    # WinError 1920 / 权限 / 路径过长：跳过单个文件，不中断整个复制
                    logger.debug("[WorkspaceLifecycle] 复制跳过: %s", item)
                    continue
        except (OSError, PermissionError) as exc:
            # 遍历本身失败（如根目录权限）：记录并返回已复制的数量
            logger.warning("[WorkspaceLifecycle] 项目遍历中断: %s", exc)
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
        try:
            for item in root.iterdir():
                if item.name in skip:
                    continue
                if item.is_file():
                    with contextlib.suppress(Exception):
                        total += item.stat().st_size
                elif item.is_dir():
                    try:
                        for f in item.rglob("*"):
                            try:
                                if not f.is_file():
                                    continue
                                if any(p in skip for p in f.relative_to(root).parts):
                                    continue
                                total += f.stat().st_size
                            except Exception:
                                continue
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError) as exc:
            logger.warning("[WorkspaceLifecycle] 项目大小计算中断: %s", exc)
        git_dir = root / ".git"
        mtime = git_dir.stat().st_mtime if git_dir.exists() else root.stat().st_mtime
        self._size_cache[project_root] = (mtime, total)
        logger.debug("[WorkspaceLifecycle] 项目大小: root=%s, size=%d, task=%s", project_root, total, task_id)
        return total

    def _worktree_add_with_repair(
        self,
        repo_path: Path,
        branch: str,
        ws_dir: Path,
        task_id: str,
    ) -> None:
        """创建 worktree，失败时自动 prune 并重试一次。

        常见失败原因：之前 worktree 清理不彻底，.git/worktrees 下残留引用，
        导致 git 认为路径状态不一致。prune 可清除这些失效引用。
        """
        rc, _, stderr = self._run_git("worktree", "add", "-b", branch, str(ws_dir), cwd=repo_path)
        if rc == 0:
            self._link_worktree_dependencies(ws_dir, repo_path)
            return

        logger.warning(
            "[WorkspaceLifecycle] worktree add 失败，尝试 prune 修复: task_id=%s, path=%s, error=%s",
            task_id,
            repo_path,
            stderr,
        )
        self._run_git("worktree", "prune", cwd=repo_path)
        if ws_dir.exists():

            def _remove_readonly(func, path, exc_info):
                import stat  # noqa: PLC0415

                os.chmod(path, stat.S_IWRITE)  # noqa: PTH101
                func(path)

            shutil.rmtree(str(ws_dir), onerror=_remove_readonly)
        self._run_git("branch", "-D", branch, cwd=repo_path)

        rc, _, stderr = self._run_git("worktree", "add", "-b", branch, str(ws_dir), cwd=repo_path)
        if rc != 0:
            raise RuntimeError(f"git worktree add 失败（prune 后重试仍失败）: task_id={task_id}, error={stderr}")
        self._link_worktree_dependencies(ws_dir, repo_path)

    def _link_worktree_dependencies(self, ws_dir: Path, project_root: Path) -> None:
        """从主空间向 worktree 创建符号链接，继承 .gitignore 排除的运行时依赖。

        读取配置 worktree_link_patterns，对每个路径：
        - 源不存在（主空间无该文件/目录）→ 跳过
        - 目标已存在（worktree 中已有）→ 跳过
        - 目录：Linux symlink / Windows junction (mklink /J)
        - 文件：Linux symlink / Windows mklink（无 /J）
        - 含父目录的路径（如 frontend/node_modules）→ 自动创建父目录
        """
        link_patterns = self._config.get("workspace", {}).get("worktree_link_patterns", [])
        if not link_patterns:
            return

        for link_name in link_patterns:
            src = project_root / link_name
            dst = ws_dir / link_name
            if not src.exists() or dst.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    if os.name == "nt":
                        subprocess.run(  # noqa: PLW1510
                            ["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True, timeout=10
                        )
                    else:
                        dst.symlink_to(src)
                elif os.name == "nt":
                    subprocess.run(  # noqa: PLW1510
                        ["cmd", "/c", "mklink", str(dst), str(src)], capture_output=True, timeout=10
                    )
                else:
                    dst.symlink_to(src)
                logger.info("[WorkspaceLifecycle] 符号链接已创建: %s -> %s", dst, src)
            except Exception as e:
                logger.warning("[WorkspaceLifecycle] 创建符号链接失败: %s -> %s, error=%s", src, dst, e)

    def _setup_sparse_worktree(self, ws_dir: Path, project_root: Path, branch: str):
        """为大项目设置 sparse-checkout worktree，排除目录通过符号链接关联（Windows 用 junction point 降级）

        白名单必须包含 .gitignore 等基础设施文件：如果 sparse checkout 不包含 .gitignore，
        worktree 分支上就没有这个文件，git merge 时会把 project_root 的 .gitignore 当作
        "被删除"处理，导致 .gitignore 丢失，后续 git add -A 会跟踪 data/ 等本应排除的目录。
        """
        self._run_git("worktree", "add", "--no-checkout", "-b", branch, str(ws_dir), cwd=project_root)
        self._run_git("sparse-checkout", "init", "--cone", cwd=ws_dir)
        whitelist = self._config.get("workspace", {}).get("worktree_include_patterns", ["src", "config"])
        mandatory = [".gitignore", ".gitattributes", ".gitmodules"]
        for m in mandatory:
            if m not in whitelist:
                whitelist = whitelist + [m]
        if whitelist:
            self._run_git("sparse-checkout", "set", *whitelist, cwd=ws_dir)
        self._run_git("checkout", "HEAD", cwd=ws_dir)
        self._link_worktree_dependencies(ws_dir, project_root)

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
        """查找父容器任务的工作空间路径。

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
