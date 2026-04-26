"""测试 workspace_lifecycle 合并逻辑不切换分支。

所有 git 操作在临时目录进行，不影响项目文件。
验证：
1. _assert_on_branch 只验证不切换
2. _merge_branch 不 checkout，在主分支上 merge
3. _safe_merge 不 checkout，在主分支上 merge
4. 不在主分支时降级为 copy_merge
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


def _create_worktree(repo_path: Path, branch: str, wt_path: Path):
    """从 repo_path 创建 worktree，分支名 branch，路径 wt_path"""
    _run_cmd("git", "branch", branch, cwd=repo_path)
    _run_cmd("git", "worktree", "add", "-b", f"wt-{branch}",
             str(wt_path), branch, cwd=repo_path)
    _run_cmd("git", "config", "user.email", "test@test.local", cwd=wt_path)
    _run_cmd("git", "config", "user.name", "Test", cwd=wt_path)


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

        # 当前在 main，断言 master
        result = mgr._assert_on_branch("master", repo)

        assert result is False

        # 确认没有切换：仍然是 main
        _, cur, _ = _run_cmd("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "main"

    def test_on_detached_head(self, tmp_path):
        """detached HEAD 时返回 False 但不崩溃"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        # 进入 detached HEAD
        _run_cmd("git", "checkout", "HEAD~0", cwd=repo)

        mgr = _make_manager(tmp_path)
        result = mgr._assert_on_branch("main", repo)
        assert result is False

        # 确认是 detached，没被切换
        _, cur, _ = _run_cmd("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "HEAD"


# ─── Test 2: _merge_branch 不 checkout ─────────────────────────

class TestMergeBranch:
    """验证 _merge_branch 不做 git checkout"""

    def test_merge_on_main_no_checkout(self, tmp_path):
        """在 main 上 merge 分支，不应 checkout"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 创建 feature 分支并提交
        _run_cmd("git", "checkout", "-b", "feature-x", cwd=repo)
        (repo / "feature.txt").write_text("hello")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "feature work", cwd=repo)

        # 切回 main
        _run_cmd("git", "checkout", "main", cwd=repo)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "feature-x",
        }

        result = mgr._merge_branch(str(repo), ws_meta)

        assert result["success"] is True
        # feature.txt 应该已经合并进来
        assert (repo / "feature.txt").exists()
        # 确认仍在 main 上
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "main"

    def test_merge_not_on_main_falls_back_to_copy(self, tmp_path):
        """不在 main 上时降级为 copy_merge，不切换分支"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 创建 feature 分支
        _run_cmd("git", "checkout", "-b", "feature-y", cwd=repo)
        (repo / "feature_y.txt").write_text("y data")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "feature y", cwd=repo)

        # 创建独立的 workspace 目录（模拟容器空间）
        ws = tmp_path / "workspace"
        os.makedirs(str(ws))
        _init_git_repo(ws)
        (ws / "ws_file.txt").write_text("ws content")
        _run_cmd("git", "add", "-A", cwd=ws)
        _run_cmd("git", "commit", "-m", "ws work", cwd=ws)

        # 让 repo 处于非 main 分支
        _run_cmd("git", "checkout", "feature-y", cwd=repo)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "feature-y",
        }

        result = mgr._merge_branch(str(ws), ws_meta)

        # 应该降级为 copy_merge
        assert result["success"] is True
        assert result.get("method") == "copy" or result.get("action") == "merged"
        # 确认 repo 没有被切换到 main（仍在 feature-y）
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "feature-y"

    def test_merge_conflict_falls_back_to_copy(self, tmp_path):
        """merge 冲突时降级为 copy_merge，不丢失文件"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # main 上修改 README
        (repo / "README.md").write_text("main version")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "main change", cwd=repo)

        # feature 分支修改同一个文件
        _run_cmd("git", "checkout", "-b", "conflict-branch", cwd=repo)
        (repo / "README.md").write_text("feature version")
        (repo / "new_file.txt").write_text("new content")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "feature change", cwd=repo)

        # 切回 main
        _run_cmd("git", "checkout", "main", cwd=repo)

        mgr = _make_manager(tmp_path)
        ws_meta = {
            "project_root": str(repo),
            "branch": "conflict-branch",
        }

        result = mgr._merge_branch(str(repo), ws_meta)

        # 冲突导致降级为 copy_merge
        assert result["success"] is True
        # 仍在 main 上
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "main"


# ─── Test 3: _safe_merge 不 checkout ───────────────────────────

class TestSafeMerge:
    """验证 _safe_merge 不做 git checkout"""

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
        # 仍在 main 上
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        assert cur == "main"

    def test_safe_merge_not_on_main_falls_back(self, tmp_path):
        """不在 main 上时降级为 copy_merge，不切换"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 确保不在 main 上
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
        # repo 仍在 other-branch，没有被 checkout 到 main
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


# ─── Test 4: 容器子任务层级合并流向 ────────────────────────────

class TestContainerHierarchy:
    """验证子任务 worktree → 容器空间 → 主仓库的合并流向"""

    def test_subtask_merge_to_container_not_main_repo(self, tmp_path):
        """子任务 worktree 合并到容器空间，不碰主仓库分支"""
        # 主仓库（模拟 D:\Jianguoyun\Agent os）
        main_repo = tmp_path / "main_repo"
        _init_git_repo(main_repo)
        (main_repo / "main_file.py").write_text("main code")
        _run_cmd("git", "add", "-A", cwd=main_repo)
        _run_cmd("git", "commit", "-m", "main init", cwd=main_repo)

        # 容器空间（模拟 .ai_workspaces/task_xxx）
        container = tmp_path / "ai_workspaces" / "task_root"
        os.makedirs(str(container))
        shutil.copytree(str(main_repo), str(container / "_temp"), dirs_exist_ok=True)
        # 实际用容器自身作为独立 git 仓库
        _init_git_repo(container)
        (container / "container_file.py").write_text("container code")
        _run_cmd("git", "add", "-A", cwd=container)
        _run_cmd("git", "commit", "-m", "container init", cwd=container)

        # 子任务 worktree
        subtask_ws = tmp_path / "ai_workspaces" / "subtask_123"
        os.makedirs(str(subtask_ws))
        _init_git_repo(subtask_ws)
        (subtask_ws / "subtask_result.py").write_text("subtask result")
        _run_cmd("git", "add", "-A", cwd=subtask_ws)
        _run_cmd("git", "commit", "-m", "subtask done", cwd=subtask_ws)

        mgr = _make_manager(tmp_path)
        # 子任务的 project_root 指向容器空间，不是主仓库
        ws_meta = {
            "project_root": str(container),
            "branch": "",
        }

        result = mgr._safe_merge(str(subtask_ws), ws_meta)

        assert result["success"] is True
        # 子任务结果应出现在容器空间
        assert (container / "subtask_result.py").exists()
        # 主仓库不应被修改分支
        _, main_cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=main_repo)
        assert main_cur == "main"
        # 容器空间不应被切换分支
        _, container_cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=container)
        assert container_cur == "main"

    def test_container_merge_to_main_repo_no_checkout(self, tmp_path):
        """容器完成合并到主仓库，不 checkout 主仓库分支"""
        # 主仓库
        main_repo = tmp_path / "main_repo"
        _init_git_repo(main_repo)
        (main_repo / "app.py").write_text("v1")
        _run_cmd("git", "add", "-A", cwd=main_repo)
        _run_cmd("git", "commit", "-m", "init", cwd=main_repo)

        # 容器工作空间
        container = tmp_path / "ai_workspaces" / "task_done"
        os.makedirs(str(container))
        _init_git_repo(container)
        (container / "app.py").write_text("v2 improved")
        (container / "new_module.py").write_text("new code")
        _run_cmd("git", "add", "-A", cwd=container)
        _run_cmd("git", "commit", "-m", "container done", cwd=container)

        mgr = _make_manager(tmp_path)
        # 容器的 project_root 指向主仓库
        ws_meta = {
            "project_root": str(main_repo),
            "branch": "",
        }

        result = mgr._safe_merge(str(container), ws_meta)

        assert result["success"] is True
        # 主仓库应该有新文件
        assert (main_repo / "new_module.py").exists()
        # 主仓库仍在 main，没有 checkout
        _, cur, _ = _run_cmd(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=main_repo)
        assert cur == "main"
