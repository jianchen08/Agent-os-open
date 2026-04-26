"""测试 workspace_lifecycle 合并逻辑不切换分支。

所有 git 操作在临时目录进行，不影响项目文件。
验证：
1. _assert_on_branch 只验证不切换
2. _safe_merge 不 checkout，在主分支上 merge
3. 不在主分支时降级为 copy_merge
4. plain 模式无 git 操作
5. worktree 创建前自动 git init
6. cleanup 打 tag 保留回退记录
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_PATH = PROJECT_ROOT / "src"

import sys

sys.path.insert(0, str(SRC_PATH))

from isolation.workspace_lifecycle import WorkspaceLifecycleManager


def _run_cmd(*args, cwd):
    r = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=15,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _make_manager(tmp_path):
    """创建一个最简 manager，base_path 指向 tmp_path"""
    mgr = WorkspaceLifecycleManager(
        resource_merge=MagicMock(),
        config={},
        task_tree=MagicMock(),
        ws_meta_store={},
        base_path=str(tmp_path),
    )
    return mgr


def _init_git_repo(path: Path, branch_name: str = "main") -> None:
    """在 path 初始化一个 git 仓库并做首次提交"""
    os.makedirs(str(path), exist_ok=True)
    _run_cmd("git", "init", "-b", branch_name, cwd=path)
    _run_cmd("git", "config", "user.email", "test@test.local", cwd=path)
    _run_cmd("git", "config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("init")
    _run_cmd("git", "add", "-A", cwd=path)
    _run_cmd("git", "commit", "-m", "init", cwd=path)


# ─── Test 1: _assert_on_branch ─────────────────────────────────

class TestAssertOnBranch:
    """验证 _assert_on_branch 只读不切换"""

    def test_on_correct_branch(self, tmp_path):
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        mgr = _make_manager(tmp_path)

        assert mgr._assert_on_branch("main", repo) is True

    def test_on_wrong_branch_no_switch(self, tmp_path):
        """在 main 上断言 master 应该返回 False，但不切换"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        mgr = _make_manager(tmp_path)

        result = mgr._assert_on_branch("master", repo)

        assert result is False
        _, cur, _ = _run_cmd("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "main"

    def test_on_detached_head(self, tmp_path):
        """detached HEAD 时返回 False 但不崩溃"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        _run_cmd("git", "checkout", "HEAD~0", cwd=repo)

        mgr = _make_manager(tmp_path)
        result = mgr._assert_on_branch("main", repo)
        assert result is False

        _, cur, _ = _run_cmd("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "HEAD"


# ─── Test 2: _safe_merge 不 checkout ───────────────────────────

class TestSafeMerge:
    """验证 _safe_merge 不做 git checkout，且不在 project root 上 git add/commit"""

    def test_safe_merge_on_main_uses_git_merge(self, tmp_path):
        """在 main 上且有 branch，应该走 git merge"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 创建 worktree 分支
        _run_cmd("git", "checkout", "-b", "task/abc", cwd=repo)
        (repo / "task_file.txt").write_text("task data")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "task work", cwd=repo)

        # workspace 目录（模拟 worktree）
        ws = tmp_path / "ws_abc"
        os.makedirs(str(ws))
        shutil.copy2(str(repo / "task_file.txt"), str(ws / "task_file.txt"))
        _init_git_repo(ws)

        # 切回 main
        _run_cmd("git", "checkout", "main", cwd=repo)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "task/abc",
        }

        result = mgr._safe_merge(str(ws), ws_meta)

        assert result["success"] is True
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "main"

    def test_safe_merge_not_on_main_falls_back(self, tmp_path):
        """不在 main 上时降级为 copy_merge，不切换"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        _run_cmd("git", "checkout", "-b", "other-branch", cwd=repo)

        ws = tmp_path / "ws_other"
        os.makedirs(str(ws))
        (ws / "ws_file.txt").write_text("content")
        _init_git_repo(ws)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "",
        }

        result = mgr._safe_merge(str(ws), ws_meta)

        assert result["success"] is True
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "other-branch"

    def test_safe_merge_no_branch_uses_copy(self, tmp_path):
        """无 branch 信息时走 copy_merge"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        ws = tmp_path / "ws_copy"
        os.makedirs(str(ws))
        (ws / "copied.txt").write_text("copied content")
        _init_git_repo(ws)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "",
        }

        result = mgr._safe_merge(str(ws), ws_meta)

        assert result["success"] is True
        assert result.get("method") == "copy"
        assert (repo / "copied.txt").exists()
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "main"

    def test_safe_merge_no_add_commit_on_project_root(self, tmp_path):
        """_safe_merge 不在 project root 上执行 git add/commit"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 在 main 上创建分支（模拟 worktree 分支）
        _run_cmd("git", "checkout", "-b", "task/test123", cwd=repo)
        (repo / "new_file.txt").write_text("new content")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "task changes", cwd=repo)

        # 切回 main，制造一个未暂存的文件
        _run_cmd("git", "checkout", "main", cwd=repo)
        (repo / "untracked.txt").write_text("should stay untracked")

        # 记录 merge 前的 commit 数
        _, log_before, _ = _run_cmd("git", "log", "--oneline", cwd=repo)
        commits_before = len(log_before.strip().splitlines())

        # workspace
        ws = tmp_path / "ws_test"
        os.makedirs(str(ws))
        _init_git_repo(ws)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "task/test123",
        }

        result = mgr._safe_merge(str(ws), ws_meta)

        assert result["success"] is True
        # untracked.txt 应该还在，没被 add/commit
        assert (repo / "untracked.txt").exists()
        _, status, _ = _run_cmd("git", "status", "--porcelain", cwd=repo)
        assert "untracked.txt" in status  # 仍然是 untracked

        # merge commit 只增加了 1 个（不是 2 个 — 没有 "chore: stage untracked"）
        _, log_after, _ = _run_cmd("git", "log", "--oneline", cwd=repo)
        commits_after = len(log_after.strip().splitlines())
        assert commits_after == commits_before + 1  # 只有 merge commit


# ─── Test 3: plain 模式 ────────────────────────────────────────

class TestPlainMode:
    """验证 plain 模式：只 mkdir，不做 git 操作"""

    def test_start_root_task_plain_no_git(self, tmp_path):
        """无显式 workspace 时创建 plain 模式，无 .git"""
        mgr = _make_manager(tmp_path)
        task_data = {
            "task_id": "abc12345",
            "_has_explicit_workspace": False,
            "workspace_root": str(tmp_path / "ws"),
        }

        # 模拟 task_tree 返回非容器父任务
        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("abc12345", "", task_data)

        assert meta["mode"] == "plain"
        assert Path(meta["path"]).exists()
        assert not (Path(meta["path"]) / ".git").exists()

    def test_on_eval_passed_plain_skips_merge(self, tmp_path):
        """plain 模式评估通过时跳过合并"""
        mgr = _make_manager(tmp_path)
        ws_meta = {"mode": "plain", "path": str(tmp_path / "ws")}

        result = mgr.on_eval_passed("abc12345", str(tmp_path / "ws"), ws_meta)

        assert result["success"] is True
        assert result["action"] == "none"

    def test_on_eval_failed_plain_no_rollback(self, tmp_path):
        """plain 模式评估失败时不回滚"""
        mgr = _make_manager(tmp_path)
        ws_meta = {"mode": "plain", "path": str(tmp_path / "ws")}

        result = mgr.on_eval_failed("abc12345", str(tmp_path / "ws"), ws_meta)

        assert result["success"] is True
        assert result["action"] == "none"

    def test_on_task_failed_plain_no_rollback(self, tmp_path):
        """plain 模式任务失败时不回滚"""
        mgr = _make_manager(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.txt").write_text("data")

        result = mgr.on_task_failed(str(ws), {"mode": "plain"})

        assert result["success"] is True
        assert (ws / "output.txt").exists()  # 文件没被删

    def test_cleanup_workspace_plain_keeps_dir(self, tmp_path):
        """plain 模式清理时不删除目录"""
        mgr = _make_manager(tmp_path)
        ws = tmp_path / "ws" / "abc12345"
        ws.mkdir(parents=True)
        (ws / "result.txt").write_text("output")

        mgr._ws_meta_store["abc12345"] = {"mode": "plain", "path": str(ws)}

        result = mgr.cleanup_workspace("abc12345")

        assert ws.exists()  # 目录没被删
        assert (ws / "result.txt").exists()

    def test_on_before_evaluate_plain_no_git(self, tmp_path):
        """plain 模式评估前不做 git 操作"""
        mgr = _make_manager(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()

        result = mgr.on_before_evaluate(str(ws), {"mode": "plain"})

        assert result["success"] is True
        assert result["commit_hash"] is None


# ─── Test 4: worktree 统一流程 ─────────────────────────────────

class TestWorktreeUnified:
    """验证显式 workspace 统一走 worktree 流程"""

    def test_explicit_workspace_without_git_gets_init_and_worktree(self, tmp_path):
        """显式 workspace 指向无 .git 的目录 → git init + worktree"""
        project = tmp_path / "my_project"
        project.mkdir()
        (project / "code.py").write_text("print('hello')")

        mgr = _make_manager(tmp_path)
        task_data = {
            "task_id": "task12345",
            "_has_explicit_workspace": True,
            "workspace_root": str(tmp_path / "ws"),
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("task12345", str(project), task_data)

        assert meta["mode"] == "worktree"
        assert meta["branch"] == "task/task12345"
        # project root 应该被 git init
        assert (project / ".git").exists()
        # worktree 目录应该存在
        assert Path(meta["path"]).exists()

    def test_explicit_workspace_with_git_creates_worktree(self, tmp_path):
        """显式 workspace 指向已有 .git 的目录 → 直接 worktree"""
        project = tmp_path / "existing_project"
        _init_git_repo(project)

        mgr = _make_manager(tmp_path)
        task_data = {
            "task_id": "task99999",
            "_has_explicit_workspace": True,
            "workspace_root": str(tmp_path / "ws"),
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("task99999", str(project), task_data)

        assert meta["mode"] == "worktree"
        assert meta["branch"] == "task/task99999"
        assert Path(meta["path"]).exists()


# ─── Test 5: cleanup 打 tag ────────────────────────────────────

class TestCleanupTag:
    """验证 worktree 清理时打 tag 保留回退记录"""

    def test_cleanup_with_tag_task_id_creates_tag(self, tmp_path):
        """合并成功后清理时打 tag"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 创建分支
        _run_cmd("git", "checkout", "-b", "task/abc12345", cwd=repo)
        (repo / "work.txt").write_text("done")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "task done", cwd=repo)
        _run_cmd("git", "checkout", "main", cwd=repo)
        _run_cmd("git", "merge", "task/abc12345", cwd=repo)

        # 创建 worktree 目录（模拟）
        wt = tmp_path / "ws" / "repo__wt_abc12345"
        wt.mkdir(parents=True)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "task/abc12345",
        }

        mgr._cleanup_worktree(str(wt), ws_meta, tag_task_id="abc12345")

        # 验证 tag 存在
        _, tags, _ = _run_cmd("git", "tag", "-l", "task-merge/abc12345*", cwd=repo)
        assert "task-merge/abc12345" in tags

        # 分支已删
        _, branches, _ = _run_cmd("git", "branch", "--list", "task/abc12345", cwd=repo)
        assert branches.strip() == ""

    def test_cleanup_without_tag_task_id_no_tag(self, tmp_path):
        """回滚场景（无 tag_task_id）不打 tag"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        _run_cmd("git", "checkout", "-b", "task/xyz99999", cwd=repo)
        (repo / "work.txt").write_text("done")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "task done", cwd=repo)
        _run_cmd("git", "checkout", "main", cwd=repo)

        wt = tmp_path / "ws" / "repo__wt_xyz99999"
        wt.mkdir(parents=True)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "task/xyz99999",
        }

        mgr._cleanup_worktree(str(wt), ws_meta)

        # 不应打 tag
        _, tags, _ = _run_cmd("git", "tag", "-l", "task-merge/*", cwd=repo)
        assert tags.strip() == ""

        # 分支已删
        _, branches, _ = _run_cmd("git", "branch", "--list", "task/xyz99999", cwd=repo)
        assert branches.strip() == ""


# ─── Test 6: 容器子任务层级合并流向 ────────────────────────────

class TestContainerHierarchy:
    """验证子任务 worktree → 容器空间 → 主仓库的合并流向"""

    def test_subtask_merge_to_container_not_main_repo(self, tmp_path):
        """子任务 worktree 合并到容器空间，不碰主仓库分支"""
        main_repo = tmp_path / "main_repo"
        _init_git_repo(main_repo)
        (main_repo / "main_file.py").write_text("main code")
        _run_cmd("git", "add", "-A", cwd=main_repo)
        _run_cmd("git", "commit", "-m", "main init", cwd=main_repo)

        container = tmp_path / "ai_workspaces" / "task_root"
        os.makedirs(str(container))
        _init_git_repo(container)
        (container / "container_file.py").write_text("container code")
        _run_cmd("git", "add", "-A", cwd=container)
        _run_cmd("git", "commit", "-m", "container init", cwd=container)

        subtask_ws = tmp_path / "ai_workspaces" / "subtask_123"
        os.makedirs(str(subtask_ws))
        _init_git_repo(subtask_ws)
        (subtask_ws / "subtask_result.py").write_text("subtask result")
        _run_cmd("git", "add", "-A", cwd=subtask_ws)
        _run_cmd("git", "commit", "-m", "subtask done", cwd=subtask_ws)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(container),
            "branch": "",
        }

        result = mgr._safe_merge(str(subtask_ws), ws_meta)

        assert result["success"] is True
        assert (container / "subtask_result.py").exists()
        _, main_cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=main_repo)
        assert main_cur == "main"
        _, container_cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=container)
        assert container_cur == "main"


# ─── Test 7: resolve_path 绝对路径重定向 ───────────────────────

class TestResolvePathRedirect:
    """验证绝对路径指向 project_root 时重定向到 workspace"""

    def test_absolute_path_redirected_to_workspace(self, tmp_path):
        """绝对路径指向 project_root 下的文件 → 重定向到 workspace"""
        from tools.builtin.workspace_aware import WorkspaceAwareMixin

        project_root = tmp_path / "project"
        project_root.mkdir()
        workspace = tmp_path / "ws" / "project__wt_abc"
        workspace.mkdir(parents=True)

        # 在两边各放一个文件
        (project_root / "src").mkdir()
        (project_root / "src" / "main.py").write_text("original")
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("worktree version")

        mixin = WorkspaceAwareMixin()
        mixin._workspace = workspace
        mixin._project_root = project_root

        result = mixin.resolve_path(str(project_root / "src" / "main.py"))

        assert str(result).replace("\\", "/").startswith(
            str(workspace).replace("\\", "/"))
        assert "worktree version" in result.read_text()

    def test_absolute_path_not_in_project_root_passes_through(self, tmp_path):
        """绝对路径不在 project_root 下 → 不重定向"""
        from tools.builtin.workspace_aware import WorkspaceAwareMixin

        project_root = tmp_path / "project"
        project_root.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        (other / "file.txt").write_text("outside")

        mixin = WorkspaceAwareMixin()
        mixin._workspace = workspace
        mixin._project_root = project_root

        result = mixin.resolve_path(str(other / "file.txt"))

        assert result == (other / "file.txt").resolve()

    def test_relative_path_unaffected(self, tmp_path):
        """相对路径不受重定向影响，照常跟 workspace 拼接"""
        from tools.builtin.workspace_aware import WorkspaceAwareMixin

        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "file.txt").write_text("ws file")

        mixin = WorkspaceAwareMixin()
        mixin._workspace = workspace
        mixin._project_root = tmp_path / "project"

        result = mixin.resolve_path("file.txt")

        assert result == (workspace / "file.txt").resolve()

    def test_no_project_root_absolute_passes_through(self, tmp_path):
        """没有 _project_root 时绝对路径直接返回"""
        from tools.builtin.workspace_aware import WorkspaceAwareMixin

        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "f.txt").write_text("x")

        mixin = WorkspaceAwareMixin()
        mixin._workspace = workspace
        # 不设 _project_root

        result = mixin.resolve_path(str(outside / "f.txt"))
        assert result == (outside / "f.txt").resolve()
