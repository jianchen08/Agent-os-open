"""工作空间统一生命周期管理

封装完整生命周期：初始化 -> 开发 -> 评估前保存 -> 评估通过合并/不通过重试 -> 清理。

暴露接口：
- WorkspaceLifecycleManager：工作空间生命周期管理器
"""
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 排除的目录（不参与场景检测和大小计算）
_SKIP_DIRS = frozenset({".git", ".ai_workspaces", "__pycache__", ".pytest_cache"})
_SPARSE_THRESHOLD_BYTES = 50 * 1024 * 1024  # sparse checkout 大小阈值（50MB）
_GIT_TIMEOUT = 30  # git 命令执行超时（秒）
_GIT_INIT_TIMEOUT = 120  # git init/add/commit 超时（秒），初始化操作耗时更长


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
            if r.returncode != 0 and r.stderr:
                logger.warning("[WorkspaceLifecycle] git %s failed (rc=%d): %s", " ".join(args), r.returncode, r.stderr[:200])
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
                    shutil.rmtree(str(git_dir))
                except OSError as e:
                    logger.warning("[WorkspaceLifecycle] Failed to remove corrupt .git: %s", e)
                    return False

        if needs_init:
            rc, _, stderr = self._run_git("init", cwd=cwd)
            if rc != 0:
                logger.warning("[WorkspaceLifecycle] git init failed: %s", stderr)
                return False

        self._ensure_git_user(cwd)

        # Remove any stale index.lock before add/commit operations
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

        rc, _, stderr = self._run_git("commit", "-m", message, cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
        if rc != 0:
            if "index.lock" in (stderr or ""):
                if self._remove_index_lock(cwd):
                    rc, _, stderr = self._run_git("commit", "-m", message, cwd=cwd, timeout=_GIT_INIT_TIMEOUT)
            if rc != 0:
                logger.warning("[WorkspaceLifecycle] git commit failed after retry: %s", stderr)
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
        if not task_data.get("is_root", True):
            return self._start_subtask(task_id, workspace, task_data)
        return self._start_root_task(task_id, workspace, task_data)

    def _start_subtask(self, task_id: str, workspace: str, task_data: dict) -> dict:
        """子任务启动：直接共享父工作空间"""
        parent_info = self._task_tree.get_parent_info(task_id)
        parent_ws = parent_info.get("workspace", "")
        parent_meta = parent_info.get("ws_meta", {})
        parent_path = parent_meta.get("path", parent_ws)

        meta = {"mode": "shared", "path": parent_path,
                "parent_workspace": parent_ws,
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
        scenario, project_root = self._detect_scenario(str(self._base_path), task_data)
        root_path = Path(project_root)
        logger.info("[WorkspaceLifecycle] _start_root_task: task_id=%s, scenario=%s, base_path=%s, root_path=%s", task_id, scenario, self._base_path, root_path)

        if scenario == "new_project":
            # 场景A：创建新目录并初始化 git
            root_path.mkdir(parents=True, exist_ok=True)
            self._git_init_and_initial_commit(root_path, "chore: initial project")
            meta = {"mode": "project_root", "path": str(root_path),
                    "branch": "main", "project_root": str(root_path)}
        elif not (root_path / ".git").exists():
            # 场景B：已有项目但无 .git -> 初始化并提交现有文件
            self._git_init_and_initial_commit(root_path, "chore: initial commit for workspace isolation")
            meta = {"mode": "project_root", "path": str(root_path),
                    "branch": "main", "project_root": str(root_path)}
        else:
            # 场景C：已有 .git -> 复制项目文件到 workspace 目录
            self._ensure_git_user(root_path)
            self._git_add_commit_if_dirty(
                root_path,
                f"chore: auto-save before worktree for task {task_id}")
            branch = f"task/{task_id}"
            ws_dir = Path(
                task_data.get("workspace_root", ".ai_workspaces")
            ) / task_id
            ws_dir.mkdir(parents=True, exist_ok=True)
            # Names to skip at ANY depth during copytree
            _SKIP_NAMES = frozenset({
                ".git", ".ai_workspaces", "__pycache__",
                ".pytest_cache", ".mypy_cache", "node_modules",
                ".claude", ".codebuddy", ".workbuddy", ".trae",
                ".coverage", "logs", "data", ".venv", ".env",
                "dist", ".next", ".nuxt", "build", ".cache",
                ".tox", ".eggs", "*.egg-info", ".mypy_cache",
            })

            def _copy_ignore(_dir: str, contents: list[str]) -> list[str]:
                """shutil.copytree ignore callback."""
                return [c for c in contents
                        if c in _SKIP_NAMES or c.startswith(".")]

            _SKIP_TOP = _SKIP_NAMES | {".coverage"}
            for child in root_path.iterdir():
                if child.name in _SKIP_TOP or child.name.startswith("."):
                    continue
                dst = ws_dir / child.name
                try:
                    if child.is_dir():
                        shutil.copytree(
                            str(child), str(dst),
                            dirs_exist_ok=True, ignore=_copy_ignore)
                    else:
                        shutil.copy2(str(child), str(dst))
                except OSError:
                    pass
            # Initialize ws_dir as a git repo with all copied files
            if not self._git_init_and_initial_commit(ws_dir, f"workspace snapshot for task {task_id}"):
                logger.warning("[WorkspaceLifecycle] Failed to initialize workspace git repo: %s", ws_dir)
            meta = {"mode": "project_root", "path": str(ws_dir),
                    "branch": "main", "project_root": str(root_path)}

        self._ws_meta_store[task_id] = meta
        return meta

    # ── 3. Sparse Worktree 设置 ──────────────────────────────────

    def _setup_sparse_worktree(self, ws_dir: Path, project_root: Path, branch: str):
        """为大项目设置 sparse-checkout worktree，排除目录通过符号链接关联（Windows 用 junction point 降级）"""
        self._run_git("worktree", "add", "--no-checkout", "-b", branch, str(ws_dir), cwd=project_root)
        self._run_git("sparse-checkout", "init", "--cone", cwd=ws_dir)
        whitelist = self._config.get("sparse_whitelist", ["src", "config"])
        if whitelist:
            self._run_git("sparse-checkout", "set", *whitelist, cwd=ws_dir)
        # 排除目录通过符号链接关联
        for skip_dir in _SKIP_DIRS:
            src, dst = project_root / skip_dir, ws_dir / skip_dir
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
        _SKIP_MERGE = {".git", ".ai_workspaces", "__pycache__",
                       ".pytest_cache", "node_modules", ".claude",
                       ".codebuddy", ".workbuddy", ".trae",
                       ".mypy_cache", "dist", ".next", "build"}
        merged_files: list[str] = []
        for child in ws_path.iterdir():
            if child.name in _SKIP_MERGE or child.name.startswith("."):
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
        """branch 模式合并：将 feature 分支合并到项目 main 分支"""
        project_root = Path(ws_meta.get("project_root", workspace))
        branch = ws_meta.get("branch", "")
        self._ensure_git_user(project_root)
        self._run_git("checkout", "main", cwd=project_root)
        rc, _, stderr = self._run_git("merge", branch, cwd=project_root)
        if rc != 0:
            self._run_git("merge", "--abort", cwd=project_root)
            logger.warning("[WorkspaceLifecycle] branch 合并失败: %s", stderr)
            return {"success": False, "error": f"合并失败: {stderr}"}
        self._verify_merge_in_main(branch, cwd=project_root)
        return {"success": True, "action": "merged", "branch": branch}

    def _cleanup_worktree(self, workspace: str, ws_meta: dict):
        """清理 worktree 和对应分支"""
        project_root = Path(ws_meta.get("project_root", ""))
        branch = ws_meta.get("branch", "")
        if project_root.exists():
            self._run_git("worktree", "remove", str(workspace), "--force", cwd=project_root)
            if branch:
                self._run_git("branch", "-D", branch, cwd=project_root)

    def _cleanup_project_root(self, workspace: str):
        """合并成功后清理 workspace 目录"""
        ws_path = Path(workspace)
        if ws_path.exists() and ".ai_workspaces" in str(ws_path):
            try:
                shutil.rmtree(str(ws_path))
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
        """异常回滚：git checkout -- . + git clean -fd"""
        ws_path = Path(workspace)
        if not ws_path.exists():
            return {"success": False, "error": f"工作空间不存在: {workspace}"}
        self._run_git("checkout", "--", ".", cwd=ws_path)
        self._run_git("clean", "-fd", cwd=ws_path)
        logger.info("[WorkspaceLifecycle] 已回滚工作空间: %s", workspace)
        return {"success": True, "action": "rollback"}

    # ── 9. 安全合并 ──────────────────────────────────────────────

    def _safe_merge(self, workspace: str, ws_meta: dict) -> dict:
        """安全合并：先尝试 git merge，冲突时降级为文件复制"""
        project_root = ws_meta.get("project_root", "")
        branch = ws_meta.get("branch", "")
        if not project_root:
            return {"success": False, "error": "缺少 project_root 信息"}
        proj_path, ws_path = Path(project_root), Path(workspace)
        # 在 workspace 中提交所有变更
        self._ensure_git_user(ws_path)
        self._git_add_commit_if_dirty(ws_path, "chore: auto commit before merge")
        # 在主仓库中尝试 git merge
        self._ensure_git_user(proj_path)
        if branch:
            rc, _, stderr = self._run_git("merge", branch, cwd=proj_path)
            if rc == 0:
                return {"success": True, "action": "merged", "method": "git_merge"}
            # 检测冲突文件
            _, diff, _ = self._run_git("diff", "--name-only", "--diff-filter=U", cwd=proj_path)
            conflicts = [l.strip() for l in diff.splitlines() if l.strip()] if diff else []
            self._run_git("merge", "--abort", cwd=proj_path)
            if conflicts:
                logger.info("[WorkspaceLifecycle] 合并冲突，降级为 copy: conflicts=%d", len(conflicts))
                return self._copy_merge(workspace, project_root)
            return {"success": False, "error": f"git merge 失败: {stderr}"}
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
            target_file = dst / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target_file))
            merged.append(str(rel))
        return {"success": True, "action": "merged", "method": "copy", "merged_files": merged}

    # ── 10. 合并验证 ─────────────────────────────────────────────

    def _verify_merge_in_main(self, branch_name: str, cwd: Path | None = None) -> bool:
        """验证分支已合并到 main：git log main..{branch} 应为空，不为空则阻止后续清理"""
        work_dir = cwd or self._base_path
        rc, log_output, _ = self._run_git("log", f"main..{branch_name}", cwd=work_dir)
        if rc != 0:
            logger.warning("[WorkspaceLifecycle] 验证合并状态失败: branch=%s", branch_name)
            return False
        if log_output.strip():
            logger.warning("[WorkspaceLifecycle] 分支未完全合并: branch=%s, 未合并=%d",
                           branch_name, len(log_output.splitlines()))
            return False
        return True
