"""工作空间 Git 操作 Mixin。

提供 WorkspaceLifecycleManager 的 Git 命令封装和分支管理方法。
从 workspace_lifecycle.py 拆分而来。
"""

# config-write-surface-exempt: 写动作针对任务工作区运行时面;命中串为gitignore内容
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from agentos_plugin_sdk.fs_utils import force_rmtree

logger = logging.getLogger(__name__)

# git worktree add 的跨文件系统 fatal：`path is on mount 'D:', start on mount 'C:'`
# （mount = 文件系统挂载，跨盘是其最常见成因）。git 无 "跨盘" 字样，判定靠该串。
_CROSS_MOUNT_ERROR = re.compile(r"is on mount '(?P<dst>[^']+)', start on mount '(?P<src>[^']+)'", re.IGNORECASE)

# 任务运行时目录（wsl_native 执行壳注入的 XDG_CACHE_HOME/npm_config_cache/
# PYTHONUSERBASE 落点）：纯执行副作用（缓存/临时/用户包装），非任务产物，
# 必须排除在 git 产物对比面之外。
_TASK_RUNTIME_DIR = ".task_runtime"

# 最小保护 .gitignore（workspace 缺 .gitignore 时播种）
_GITIGNORE_MINIMAL = (
    f"data/\n__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\nnode_modules/\n.env\n*.log\n*.bak\n{_TASK_RUNTIME_DIR}/\n"
)

# 排除的目录（不参与场景检测、复制和大小计算）
_SKIP_DIRS = frozenset({".git", ".ai_workspaces", "__pycache__", ".pytest_cache", "data", _TASK_RUNTIME_DIR})
_GIT_TIMEOUT = 30  # git 命令执行超时（秒）
_GIT_INIT_TIMEOUT = 120  # git init/add/commit 超时（秒），初始化操作耗时更长
# worktree add 在大型仓库/并发 I/O 下可能超过 30s：默认放宽，可经 workspace 配置覆盖。
_DEFAULT_WORKTREE_ADD_TIMEOUT = 120
# index.lock 视为陈旧的年龄阈值（秒）：活锁（并发 git 正在持有）不得删除。
_DEFAULT_INDEX_LOCK_STALE_SECS = 120
# 所有 git 调用（含其子进程）统一关闭自动 gc/维护：commit 后不得在后台
# repack 大型对象库（与并发 checkout/status 抢 I/O、放大 30s 级超时）。
# 走 GIT_CONFIG_* 环境变量而非 argv，保持 _run_git 的 argv 形状不变
# （调用方/测试按 argv[:2] 识别子命令）。
_GIT_NO_MAINTENANCE = {"gc.auto": "0", "maintenance.auto": "false"}


def _git_env() -> dict[str, str]:
    """当前环境副本 + 关闭自动 gc/维护的 GIT_CONFIG_* 注入（子进程继承）。"""
    env = os.environ.copy()
    try:
        base = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        base = 0
    for offset, (key, value) in enumerate(_GIT_NO_MAINTENANCE.items(), start=base):
        env[f"GIT_CONFIG_KEY_{offset}"] = key
        env[f"GIT_CONFIG_VALUE_{offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(base + len(_GIT_NO_MAINTENANCE))
    return env


def _cross_mount_mounts(stderr: str) -> str | None:
    """worktree add 跨文件系统错误的盘位描述；非该错误返回 None。

    形态 `path is on mount 'D:', start on mount 'C:'`——dst = worktree 目标
    所在挂载，src = 源仓库所在挂载，返回 "D: ≠ C:" 供错误面直读。
    """
    m = _CROSS_MOUNT_ERROR.search(stderr or "")
    if not m:
        return None
    return f"{m.group('dst')} != {m.group('src')}"


def _safe_ws_name(project_name: str, task_id: str, name_limit: int = 100) -> str:
    """生成安全的 worktree 目录名，项目名原样保留（上限 name_limit 仅防
    Windows 单组件 255 上限的病态长名）。截断过短会让目录名与项目失去
    可读对应（如「Worktree_与_Plai__wt_xx」），故上限取宽松值。"""
    import re  # noqa: PLC0415

    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", project_name)
    safe = safe.replace(" ", "_")
    safe = re.sub(r"_+", "_", safe).strip("._")
    if not safe:
        safe = "ws"
    if len(safe) > name_limit:
        safe = safe[:name_limit].rstrip("._")
    return f"{safe}__wt_{task_id[:8]}"


class _GitOpsMixin:
    """Git 操作 Mixin，提供 Git 命令执行和分支管理方法。

    要求宿主类提供以下属性：
    - self._config: dict[str, Any]
    - self._base_path: Path
    - self._main_branch: str
    - self._merge_locks: dict[str, threading.Lock]
    - self._global_lock: threading.Lock
    - self._ws_meta_store: Any
    - self._task_tree: Any
    - self._resource_merge: Any
    """

    _WIN_ABS_PATH = __import__("re").compile(r"^[a-zA-Z]:[/\\]")

    def _get_workspace_root(self) -> Path:
        """从配置中读取工作空间基目录，解析为绝对路径。

        workspace.root 支持绝对路径和相对路径（**相对 base_path=项目根**，
        不是 cwd——sidecar 的 cwd 是插件目录，拼 cwd 会把工作空间建错位置）。
        返回的是所有工作空间（worktree/container）的父目录。
        例如配置 root: "D:/myproject" 则返回 Path("D:/myproject")。

        统一走 isolation.workspace.get_workspace_base_dir()（配置驱动：先
        config_center 后文件回退；根目录经 find_project_root 祖先查找，不
        硬编码）。唯一例外：调用方显式注入的 self._config.workspace.root
        优先（内核 plugin.get_config() 注入链，2026-08-24 收口后仅测试
        直构 manager 传 config 走此分支）。
        """
        injected = self._config.get("workspace", {}).get("root")
        if injected:
            if self._WIN_ABS_PATH.match(str(injected)):
                return Path(str(injected))
            p = Path(str(injected))
            if not p.is_absolute():
                # 相对路径基于 base_path（项目根），不能拼 cwd——sidecar cwd 是插件目录
                base = getattr(self, "_base_path", None) or Path.cwd()
                p = Path(base) / p
            return p.resolve()

        from isolation.workspace import get_workspace_base_dir  # noqa: PLC0415

        return Path(get_workspace_base_dir())

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
                env=_git_env(),
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
        except FileNotFoundError as e:
            # POSIX 上 cwd 不存在同样抛 FileNotFoundError（子进程 chdir ENOENT），
            # 须与"git 可执行缺失"区分：cwd 确实不在即归因工作目录，与 Windows
            # NotADirectoryError 走同一语义分支。
            if cwd is not None and not os.path.isdir(cwd):
                return -1, "", f"git 工作目录无效或不存在: {cwd} ({e})"
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

    def _ws_config(self) -> dict[str, Any]:
        """workspace 配置子树（缺省空字典，宿主/测试未注入时安全）。"""
        cfg = getattr(self, "_config", None)
        if not isinstance(cfg, dict):
            return {}
        ws = cfg.get("workspace")
        return ws if isinstance(ws, dict) else {}

    def _worktree_add_timeout(self) -> int:
        """worktree add 单次超时（秒，可经 workspace.worktree_add_timeout_seconds 覆盖）。"""
        raw = self._ws_config().get("worktree_add_timeout_seconds", _DEFAULT_WORKTREE_ADD_TIMEOUT)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = _DEFAULT_WORKTREE_ADD_TIMEOUT
        return max(_GIT_TIMEOUT, min(value, 3600))

    def _ensure_git_user(self, cwd: Path):
        """配置 workspace 的 git 用户信息"""
        self._run_git("config", "user.email", "agent@agent-os.local", cwd=cwd)
        self._run_git("config", "user.name", "Agent OS", cwd=cwd)

    def _remove_index_lock(self, cwd: Path) -> bool:
        """移除**陈旧** index.lock（活锁不删——并发 git 正在持有时删除会破坏锁协议）。

        陈旧判定 = mtime 距现在超过 workspace.index_lock_stale_seconds
        （默认 120s）。并发 task_submit 场景下，兄弟任务刚创建/正在持有的锁
        必须保留，否则会出现「删活锁 → 对方 git 失败 → 无限重试」。
        """
        lock_path = cwd / ".git" / "index.lock"
        try:
            st = lock_path.stat()
        except FileNotFoundError:
            return False
        except OSError as e:
            logger.warning("[WorkspaceLifecycle] stat index.lock 失败 %s: %s", lock_path, e)
            return False
        try:
            stale_secs = int(
                self._ws_config().get("index_lock_stale_seconds", _DEFAULT_INDEX_LOCK_STALE_SECS)
            )
        except (TypeError, ValueError):
            stale_secs = _DEFAULT_INDEX_LOCK_STALE_SECS
        age = time.time() - st.st_mtime
        if age < stale_secs:
            logger.warning(
                "[WorkspaceLifecycle] index.lock 在位且未超陈旧阈值（%.0fs < %ss），按活锁保留: %s",
                age,
                stale_secs,
                lock_path,
            )
            return False
        try:
            lock_path.unlink()
            logger.info("[WorkspaceLifecycle] Removed stale index.lock: %s", lock_path)
            return True
        except OSError as e:
            logger.warning("[WorkspaceLifecycle] Failed to remove index.lock %s: %s", lock_path, e)
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

    def _ensure_gitignore(self, cwd: Path) -> None:
        """确保 .gitignore 在位且排除任务运行时目录。

        缺失 → 写最小保护版本（含 .task_runtime/）；存在但缺该行 → 幂等追加
        系统自产行（存量 workspace 不重生成，也不改用户既有内容）。
        """
        gitignore = cwd / ".gitignore"
        try:
            if not gitignore.exists():
                logger.warning("[WorkspaceLifecycle] .gitignore 不存在，生成最小保护版本: %s", gitignore)
                gitignore.write_text(_GITIGNORE_MINIMAL, encoding="utf-8")
                return
            text = gitignore.read_text(encoding="utf-8", errors="replace")
            if _TASK_RUNTIME_DIR not in text:
                with gitignore.open("a", encoding="utf-8") as f:
                    f.write(f"{_TASK_RUNTIME_DIR}/\n")
        except OSError as e:
            logger.warning("[WorkspaceLifecycle] .gitignore 维护失败（非致命）: %s | %s", gitignore, e)

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
            rc, _, stderr = self._run_git("rev-parse", "HEAD", cwd=cwd)
            if rc == 0:
                needs_init = False
            elif rc < 0:
                # 瞬态失败（超时 / 找不到 git / OSError）——绝不能据此删除 .git。
                # _run_git 把这三类异常统一映射成 rc=-1（git 自身退出码恒为正），
                # 必须与 git 自身非零退出（仓库空/损坏）区分：瞬态失败走失败闭合，
                # 保留 .git，返回 False 让上层（workspace_lifecycle.py 的调用方）
                # 抛 RuntimeError 介入。
                logger.error(
                    "[WorkspaceLifecycle] rev-parse HEAD 失败但属瞬态（rc=%d，疑似超时/IO），"
                    "拒绝删除 .git 以防数据丢失: %s | %s",
                    rc,
                    git_dir,
                    stderr,
                )
                return False
            else:
                logger.info("[WorkspaceLifecycle] Existing .git is empty/corrupt, removing: %s", git_dir)
                try:
                    force_rmtree(str(git_dir))
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

        self._ensure_gitignore(cwd)

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
        """只暂存并提交**已跟踪文件**的 diff（tracked-only），返回 commit hash 或 None。

        不看 untracked：构建产物/日志/临时目录不得随 auto-save 入库（历史教训：
        `git add -A` 曾把 llvm-cov 的 exe/pdb 等 1,960 个文件提交进仓库，撑大
        对象库并触发全库 repack）。untracked 独立于 git，不受 worktree/merge
        流程影响，无需暂存。
        """
        self._remove_index_lock(cwd)

        rc, status, _ = self._run_git("status", "--porcelain", "-uno", cwd=cwd)
        if rc != 0 or not status.strip():
            return None

        self._ensure_gitignore(cwd)

        rc, _, _ = self._run_git("add", "-u", cwd=cwd)
        if rc != 0:
            self._remove_index_lock(cwd)
            rc, _, _ = self._run_git("add", "-u", cwd=cwd)
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
            # 校验命令失败（瞬态超时/IO 或真实 git 错误）= 无法确认工作区是否干净。
            # 放行会让可能存在的脏已跟踪改动随 worktree 建在旧 HEAD 上，合并回来时
            # 永久丢失——这正是本函数 docstring 所立"宁可任务失败，也不丢数据"契约
            # 要防止的。校验失败时失败闭合，让上层重新派发，而非赌"应该是干净的"。
            raise RuntimeError(
                f"auto-save 后状态校验失败(rc={rc})，无法确认工作区干净，"
                f"为避免数据丢失中止 worktree 创建: task_id={task_id}, path={cwd}"
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

        超时/IO（rc<0）先认领「内容已完整落地、只差收尾解锁」的 worktree
        （`git worktree add` 在 30s/120s 边界被杀的唯一安全恢复面），认领
        失败才按原契约 fail-closed，保留分支与现场。
        """
        add_timeout = self._worktree_add_timeout()
        rc, _, stderr = self._run_git(
            "worktree", "add", "-b", branch, str(ws_dir), cwd=repo_path, timeout=add_timeout
        )
        if rc == 0:
            self._link_worktree_dependencies(ws_dir, repo_path)
            self._normalize_worktree_gitdir_links(ws_dir, repo_path)
            return

        if rc < 0:
            # 瞬态失败（超时/找不到 git/OSError）——worktree 与分支的真实状态未知
            # （可能已创建、可能没有）。绝不能盲目走 prune + branch -D + rmtree：
            # 若本任务为重派发、branch 已含上一轮提交，这里会摧毁可恢复的工作
            # （与 _git_init_and_initial_commit 的 .git 擦除同构：rc<0 误判为可清理）。
            # 先尝试认领「已完整落地」的现场；认领不到才失败闭合。
            if self._reclaim_completed_worktree(repo_path, branch, ws_dir, task_id):
                return
            raise RuntimeError(
                f"git worktree add 瞬态失败（rc={rc}，疑似超时/IO），拒绝 branch -D 以防数据丢失: "
                f"task_id={task_id}, branch={branch}, error={stderr}"
            )

        mounts = _cross_mount_mounts(stderr)
        if mounts is not None:
            # 跨文件系统是确定性错误：同一对路径重试结果恒定，prune 无从修复；
            # 而中间的 `branch -D` 对重派发的同一 task_id 可能真删分支——与本
            # 模块"宁可失败也不丢数据"的契约直接冲突，故不进修复分支。
            # 正常路径已由 isolation.workspace.resolve_workspace_base 在建树
            # 前退化到同盘；走到这里说明基目录是用户显式配置的跨盘绝对路径。
            raise RuntimeError(
                f"git worktree add 跨盘失败（目标挂载 {mounts}），不重试以保护分支: "
                f"task_id={task_id}, branch={branch}, worktree={ws_dir}, repo={repo_path}, "
                f"error={stderr}。worktree 必须与源仓库同一文件系统：请把工作空间根"
                f"（workspace.root）配到项目所在盘。"
            )

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

        rc, _, stderr = self._run_git(
            "worktree", "add", "-b", branch, str(ws_dir), cwd=repo_path, timeout=add_timeout
        )
        if rc != 0:
            raise RuntimeError(f"git worktree add 失败（prune 后重试仍失败）: task_id={task_id}, error={stderr}")
        self._link_worktree_dependencies(ws_dir, repo_path)
        self._normalize_worktree_gitdir_links(ws_dir, repo_path)

    def _worktree_entries(self, repo_path: Path) -> list[dict[str, str]]:
        """解析 `git worktree list --porcelain` 为条目字典列表（失败返回空）。"""
        rc, out, _ = self._run_git("worktree", "list", "--porcelain", cwd=repo_path)
        if rc != 0:
            return []
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value.strip()
        if current:
            entries.append(current)
        return entries

    def _find_worktree_entry(self, repo_path: Path, ws_dir: Path) -> dict[str, str] | None:
        """按路径（Windows 大小写归一）定位 worktree 注册条目。"""
        try:
            target = os.path.normcase(str(Path(ws_dir).resolve()))
        except OSError:
            return None
        for entry in self._worktree_entries(repo_path):
            raw = entry.get("worktree")
            if not raw:
                continue
            try:
                current = os.path.normcase(str(Path(raw).resolve()))
            except OSError:
                continue
            if current == target:
                return entry
        return None

    def _unlock_worktree(self, repo_path: Path, ws_dir: Path) -> None:
        """解除 worktree 的 locked 标记（git 收尾前被杀的残留）。"""
        rc, _, err = self._run_git("worktree", "unlock", str(ws_dir), cwd=repo_path)
        if rc == 0:
            logger.info("[WorkspaceLifecycle] worktree 已解锁: %s", ws_dir)
            return
        # 兜底：git 版本/路径形态差异时直接移除 admin 侧 locked 标记。
        admin_locked = repo_path / ".git" / "worktrees" / ws_dir.name / "locked"
        try:
            admin_locked.unlink()
        except OSError:
            logger.warning(
                "[WorkspaceLifecycle] worktree 解锁失败（admin locked 标记未移除）: ws=%s error=%s",
                ws_dir,
                err,
            )
        else:
            logger.info("[WorkspaceLifecycle] worktree 已解锁（admin locked 标记移除）: %s", ws_dir)

    def _reclaim_completed_worktree(
        self, repo_path: Path, branch: str, ws_dir: Path, task_id: str
    ) -> bool:
        """认领「内容已完整落地、只差收尾解锁」的 worktree（超时/IO 失败面）。

        判定三件套：注册条目存在且 branch 匹配 / worktree 内 HEAD 可解析 /
        tracked 工作面干净（`status --porcelain -uno` 为空 = checkout 已完整，
        不是半截）。三件全中才认领；任一不中返回 False，由调用方 fail-closed。
        """
        try:
            if not ws_dir.is_dir() or not (ws_dir / ".git").exists():
                return False
            entry = self._find_worktree_entry(repo_path, ws_dir)
            if entry is None or entry.get("branch") != f"refs/heads/{branch}":
                return False
            rc, _, _ = self._run_git("rev-parse", "--verify", "HEAD", cwd=ws_dir)
            if rc != 0:
                return False
            rc, status, _ = self._run_git("status", "--porcelain", "-uno", cwd=ws_dir)
            if rc != 0 or status.strip():
                return False
            logger.warning(
                "[WorkspaceLifecycle] worktree add 超时但工作树已完整落地，认领并解锁"
                "（不判失败、不删分支）: task_id=%s, branch=%s, path=%s",
                task_id,
                branch,
                ws_dir,
            )
            self._unlock_worktree(repo_path, ws_dir)
            self._link_worktree_dependencies(ws_dir, repo_path)
            self._normalize_worktree_gitdir_links(ws_dir, repo_path)
            return True
        except Exception:  # noqa: BLE001 — 认领是恢复路径，任何异常都按未认领处理
            logger.warning(
                "[WorkspaceLifecycle] 认领超时 worktree 失败（按未认领处理）: task_id=%s",
                task_id,
                exc_info=True,
            )
            return False

    def _normalize_worktree_gitdir_links(self, ws_dir: Path, repo_path: Path) -> None:
        """归一 worktree 双向 gitdir 链接：ws 侧相对（跨 OS 侧可读），admin 侧绝对。

        `git worktree add` 在哪侧执行就把链接写成哪侧绝对路径（宿主 `D:/`
        与 WSL `/mnt/d` 互不解析）——另一侧读 worktree 会报
        "fatal: not a git repository"（2026-09-17 SWE 种子实跑实证）。ws 侧
        `.git` 文件的 gitdir 由 git 以该文件所在目录为基准解析，写成相对形态
        后两侧均可用。

        admin 侧（`.git/worktrees/<name>/gitdir`）**必须保持绝对**：git 以
        进程 cwd 相对解析该文件内容，相对形态会被任何一次 `git worktree
        prune` 判死并删除 admin（BUG-53 真机实证：并发任务的清理 prune 误删
        在途 worktree 的 admin → worktree 悬空、合并门控 not a git
        repository 误判失败）。指向 `<ws>/.git` 的绝对形式与 git 原生写法
        一致。幂等；定位失败仅告警不阻断建树。
        """
        git_file = ws_dir / ".git"
        if not git_file.is_file():
            return
        admin_root = repo_path / ".git" / "worktrees"
        if not admin_root.is_dir():
            return
        admin_dir = self._resolve_worktree_admin_dir(git_file, admin_root)
        if admin_dir is None or not admin_dir.is_dir():
            logger.warning(
                "[WorkspaceGitOps] worktree admin 目录未定位到，gitdir 保持原样: ws=%s",
                ws_dir,
            )
            return
        rel_git = os.path.relpath(admin_dir, ws_dir).replace("\\", "/")
        abs_gitdir = str(ws_dir / ".git")
        # Windows git 给 worktree .git 文件加隐藏/只读属性——CPython 无法以
        # 写模式打开隐藏文件，用临时文件 + os.replace 覆盖（replace 可替换
        # 隐藏目标；只读目标先 chmod 放开）
        for target in (git_file, admin_dir / "gitdir"):
            with contextlib.suppress(OSError):
                os.chmod(target, stat.S_IWRITE)
        self._replace_text_forced(git_file, f"gitdir: {rel_git}\n")
        self._replace_text_forced(admin_dir / "gitdir", abs_gitdir + "\n")
        rc, _, err = self._run_git("rev-parse", "--git-dir", cwd=ws_dir)
        if rc != 0:
            logger.warning(
                "[WorkspaceGitOps] gitdir 归一后本侧 rev-parse 失败（保留归一形态）: %s",
                err[:200],
            )
        else:
            logger.info(
                "[WorkspaceGitOps] worktree gitdir 已归一（ws 侧相对跨侧可用，admin 侧绝对防 prune 误删）: ws=%s",
                ws_dir,
            )

    @staticmethod
    def _replace_text_forced(path: Path, text: str) -> None:
        """覆盖写（含 Windows 隐藏文件）：临时文件写好后 os.replace 顶替。"""
        tmp = path.with_name(path.name + ".tmp_norm")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _resolve_worktree_admin_dir(git_file: Path, admin_root: Path) -> Path | None:
        """从 .git 文件的 gitdir 指向定位 admin 目录。

        兼容宿主 `D:/` 与 WSL `/mnt/d` 两种来源格式：不解析外路径本身，
        只取 `.git/worktrees/` 之后的目录段映射到本侧 admin_root 下。"""
        import re  # noqa: PLC0415

        try:
            text = git_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        m = re.match(r"^gitdir:\s*(.+)$", text)
        if not m:
            return None
        parts = [p for p in m.group(1).strip().replace("\\", "/").split("/") if p]
        if "worktrees" in parts:
            i = parts.index("worktrees")
            if i + 1 < len(parts):
                return admin_root / parts[i + 1]
        return None

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
