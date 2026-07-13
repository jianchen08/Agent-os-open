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
from pathlib import Path
from unittest.mock import MagicMock


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

        mgr.cleanup_workspace("abc12345")

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


# ─── Test 7: on_eval_passed 合并验证后才清理 ───────────────────

class TestEvalPassedMergeGuard:
    """验证 on_eval_passed 在合并失败/验证失败时跳过清理"""

    def test_merge_fail_keeps_worktree(self, tmp_path):
        """_safe_merge 返回失败时 worktree 不被删除"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        ws = tmp_path / "ws_fail"
        ws.mkdir()
        (ws / "important.txt").write_text("must not lose")

        mgr = _make_manager(tmp_path)
        ws_meta = {"mode": "worktree", "project_root": "", "branch": ""}

        result = mgr.on_eval_passed("fail_task", str(ws), ws_meta)

        assert result["success"] is False
        assert (ws / "important.txt").exists()

    def test_merge_success_with_git_merge_cleans_up(self, tmp_path):
        """git merge 成功且验证通过时正常清理"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 创建 worktree 分支并提交变更
        _run_cmd("git", "checkout", "-b", "task/t1", cwd=repo)
        (repo / "feature.py").write_text("feature code")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "feature", cwd=repo)
        _run_cmd("git", "checkout", "main", cwd=repo)

        # 创建真实 worktree
        wt = tmp_path / "wt_repo__wt_t1abcd12"
        _run_cmd("git", "worktree", "add", str(wt), "task/t1", cwd=repo)
        (wt / "extra.txt").write_text("extra work")
        _run_cmd("git", "add", "-A", cwd=wt)
        _run_cmd("git", "config", "user.email", "test@test.local", cwd=wt)
        _run_cmd("git", "config", "user.name", "Test", cwd=wt)
        _run_cmd("git", "commit", "-m", "extra", cwd=wt)

        mgr = _make_manager(tmp_path)
        ws_meta = {"mode": "worktree", "project_root": str(repo), "branch": "task/t1"}

        result = mgr.on_eval_passed("t1abcd1234", str(wt), ws_meta)

        assert result["success"] is True
        assert not wt.exists()

    def test_copy_merge_success_cleans_up(self, tmp_path):
        """copy_merge 成功时正常清理（无 branch 验证）"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        ws = tmp_path / "wt_repo__wt_cabcd12"
        ws.mkdir()
        (ws / "output.txt").write_text("result")
        _init_git_repo(ws)

        mgr = _make_manager(tmp_path)
        ws_meta = {"mode": "worktree", "project_root": str(repo), "branch": ""}

        result = mgr.on_eval_passed("cabcd1234", str(ws), ws_meta)

        assert result["success"] is True
        assert not ws.exists()

    def test_verify_merge_fail_keeps_worktree(self, tmp_path):
        """git merge 成功但验证失败时保留 worktree（不调用 cleanup）"""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        # 创建分支，不 merge 到 main
        _run_cmd("git", "checkout", "-b", "task/v1", cwd=repo)
        (repo / "new.py").write_text("new code")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "task work", cwd=repo)
        _run_cmd("git", "checkout", "main", cwd=repo)

        wt = tmp_path / "wt_repo__wt_v1abcd12"
        _run_cmd("git", "worktree", "add", str(wt), "task/v1", cwd=repo)

        mgr = _make_manager(tmp_path)
        ws_meta = {"mode": "worktree", "project_root": str(repo), "branch": "task/v1"}

        # Mock _safe_merge to return success via git_merge
        call_count = {"n": 0}

        def _mock_safe_merge(workspace, meta):
            call_count["n"] += 1
            return {"success": True, "action": "merged", "method": "git_merge"}

        mgr._safe_merge = _mock_safe_merge

        result = mgr.on_eval_passed("v1abcd1234", str(wt), ws_meta)

        assert result["success"] is True
        assert call_count["n"] == 2
        assert "verify_error" in result
        # _verify_merge_in_main 应返回 False（分支未合入 main），所以 worktree 不应被删
        assert wt.exists()


# ─── Test 8: resolve_path 绝对路径重定向 ───────────────────────


# ─── Test 9: ws_dir 绝对路径解析 ───────────────────────────────

class TestWsDirAbsolutePath:
    """验证 ws_dir 在 worktree 创建时始终为绝对路径。

    BUG-FIX: 当 workspace 指向子目录时，ws_dir 曾被构建为相对路径
    .ai_workspaces/ws_name，导致后续 _ensure_git_user(ws_dir) 的 subprocess
    cwd 解析相对于进程 cwd 而非子目录，触发 WinError 267。
    修复: ws_dir = root_path / ws_root / _safe_ws_name(...)
    """

    def test_ws_dir_is_absolute_when_workspace_is_subdirectory(self, tmp_path):
        """workspace 指向子目录时 ws_dir 应为绝对路径"""
        project = tmp_path / "my_project"
        project.mkdir()
        (project / "code.py").write_text("print('hello')")
        # 子目录作为 workspace
        sub = project / "docs"
        sub.mkdir()
        (sub / "readme.md").write_text("# docs")

        mgr = _make_manager(tmp_path)
        task_data = {
            "task_id": "sub12345",
            "_has_explicit_workspace": True,
            "workspace_root": ".ai_workspaces",
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("sub12345", str(sub), task_data)

        ws_dir = Path(meta["path"])
        assert ws_dir.is_absolute(), (
            f"ws_dir should be absolute, got: {meta['path']}"
        )

        # 清理 worktree
        if meta.get("project_root"):
            _run_cmd("git", "worktree", "prune", cwd=Path(meta["project_root"]))

    def test_ws_dir_is_absolute_when_workspace_is_project_root(self, tmp_path):
        """workspace 指向项目根时 ws_dir 也应为绝对路径"""
        project = tmp_path / "root_project"
        _init_git_repo(project)

        mgr = _make_manager(tmp_path)
        task_data = {
            "task_id": "root12345",
            "_has_explicit_workspace": True,
            "workspace_root": ".ai_workspaces",
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("root12345", str(project), task_data)

        ws_dir = Path(meta["path"])
        assert ws_dir.is_absolute(), (
            f"ws_dir should be absolute, got: {meta['path']}"
        )

        # 清理 worktree
        if meta.get("project_root"):
            _run_cmd("git", "worktree", "prune", cwd=Path(meta["project_root"]))

    def test_worktree_created_at_correct_location(self, tmp_path):
        """worktree 目录应存在于 root_path/.ai_workspaces/ws_name/"""
        project = tmp_path / "located_project"
        project.mkdir()
        (project / "main.py").write_text("pass")

        mgr = _make_manager(tmp_path)
        task_data = {
            "task_id": "loc12345",
            "_has_explicit_workspace": True,
            "workspace_root": ".ai_workspaces",
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("loc12345", str(project), task_data)

        ws_dir = Path(meta["path"])
        assert ws_dir.exists(), f"worktree directory should exist: {ws_dir}"
        # 目录里应有 .git（worktree 的 git link）
        assert (ws_dir / ".git").exists(), (
            f"worktree should have .git link: {ws_dir}"
        )
        # 应包含项目文件（通过 worktree checkout）
        assert (ws_dir / "main.py").exists(), (
            f"worktree should contain project files: {ws_dir}"
        )

        # 清理
        proj_root = Path(meta["project_root"])
        _run_cmd("git", "worktree", "remove", str(ws_dir), "--force", cwd=proj_root)
        _run_cmd("git", "worktree", "prune", cwd=proj_root)

    def test_ensure_git_user_succeeds_in_worktree(self, tmp_path):
        """_ensure_git_user(ws_dir) 在 worktree 中不应报错（WinError 267 回归）"""
        project = tmp_path / "gituser_project"
        project.mkdir()
        (project / "app.py").write_text("import os")

        mgr = _make_manager(tmp_path)
        task_data = {
            "task_id": "git12345",
            "_has_explicit_workspace": True,
            "workspace_root": ".ai_workspaces",
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("git12345", str(project), task_data)

        ws_dir = Path(meta["path"])
        # 关键操作：_ensure_git_user 用 ws_dir 作为 subprocess cwd
        # 如果 ws_dir 是相对路径且解析错误，这里会抛出异常
        mgr._ensure_git_user(ws_dir)

        # 验证 git config 设置成功
        _, email, _ = _run_cmd("git", "config", "user.email", cwd=ws_dir)
        assert email == "agent@agent-os.local"
        _, name, _ = _run_cmd("git", "config", "user.name", cwd=ws_dir)
        assert name == "Agent OS"

        # 清理
        proj_root = Path(meta["project_root"])
        _run_cmd("git", "worktree", "remove", str(ws_dir), "--force", cwd=proj_root)
        _run_cmd("git", "worktree", "prune", cwd=proj_root)


# ─── Test 10: Branch Guard ─────────────────────────────────────

class TestBranchGuard:
    """验证 _record_main_branch / _guard_root_branch 分支守卫。

    防止 auto-save 在用户手动切换分支后写入错误分支。
    - _record_main_branch: 记录 _base_path 当前分支
    - _guard_root_branch(cwd): 当 cwd == _base_path 且分支变更时返回 False
    """

    def test_record_main_branch_on_init(self, tmp_path):
        """初始化时 _main_branch 应被设为当前分支"""
        repo = tmp_path / "guard_repo"
        _init_git_repo(repo, branch_name="main")

        mgr = _make_manager(repo)

        assert mgr._main_branch == "main", (
            f"Expected _main_branch='main', got '{mgr._main_branch}'"
        )

    def test_record_main_branch_on_init_master(self, tmp_path):
        """初始化时如果默认分支是 master，也应正确记录"""
        repo = tmp_path / "master_repo"
        _init_git_repo(repo, branch_name="master")

        mgr = _make_manager(repo)

        assert mgr._main_branch == "master", (
            f"Expected _main_branch='master', got '{mgr._main_branch}'"
        )

    def test_guard_allows_same_branch(self, tmp_path):
        """cwd 是 base_path 且分支未变时，_guard_root_branch 返回 True"""
        repo = tmp_path / "same_branch"
        _init_git_repo(repo)

        mgr = _make_manager(repo)
        # _main_branch 已在 __init__ 中设为 "main"

        result = mgr._guard_root_branch(repo)
        assert result is True, "Guard should allow when branch unchanged"

    def test_guard_rejects_changed_branch(self, tmp_path):
        """cwd 是 base_path 但分支已切换时，_guard_root_branch 返回 False"""
        repo = tmp_path / "changed_branch"
        _init_git_repo(repo)

        mgr = _make_manager(repo)
        assert mgr._main_branch == "main"

        # 模拟外部切换分支
        _run_cmd("git", "checkout", "-b", "develop", cwd=repo)

        result = mgr._guard_root_branch(repo)
        assert result is False, (
            "Guard should reject when branch has changed from main to develop"
        )

    def test_guard_allows_non_root_paths(self, tmp_path):
        """cwd 不是 base_path 时，_guard_root_branch 始终返回 True"""
        repo = tmp_path / "root_repo"
        _init_git_repo(repo)

        mgr = _make_manager(repo)
        assert mgr._main_branch == "main"

        # 一个完全不同的路径（模拟 worktree 路径）
        other_path = tmp_path / "some_worktree"
        other_path.mkdir()

        result = mgr._guard_root_branch(other_path)
        assert result is True, (
            "Guard should allow non-root paths regardless of branch state"
        )

    def test_guard_allows_when_main_branch_empty(self, tmp_path):
        """_main_branch 为空字符串时，_guard_root_branch 始终返回 True"""
        repo = tmp_path / "empty_branch"
        _init_git_repo(repo)

        mgr = _make_manager(repo)
        mgr._main_branch = ""  # 强制清空

        result = mgr._guard_root_branch(repo)
        assert result is True, "Guard should allow when _main_branch is empty"

    def test_auto_save_skipped_when_branch_changed(self, tmp_path):
        """分支变更后调用 _start_root_task 应跳过 auto-save"""
        repo = tmp_path / "autosave_repo"
        _init_git_repo(repo)
        (repo / "initial.txt").write_text("initial")
        _run_cmd("git", "add", "-A", cwd=repo)
        _run_cmd("git", "commit", "-m", "initial commit", cwd=repo)

        # 记录 auto-save 前的 commit 数
        _, log_before, _ = _run_cmd("git", "log", "--oneline", cwd=repo)
        commits_before = len(log_before.strip().splitlines())

        # 模拟外部切换分支
        _run_cmd("git", "checkout", "-b", "feature-x", cwd=repo)

        mgr = _make_manager(repo)
        # _main_branch 在 __init__ 时被记录，但分支已经变了
        # 需要让 _main_branch 仍然是 "main" 来触发 guard
        mgr._main_branch = "main"

        task_data = {
            "task_id": "guard1234",
            "_has_explicit_workspace": True,
            "workspace_root": ".ai_workspaces",
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        meta = mgr._start_root_task("guard1234", str(repo), task_data)

        # auto-save 应被跳过（没有新的 commit）
        _, log_after, _ = _run_cmd("git", "log", "--oneline", cwd=repo)
        commits_after = len(log_after.strip().splitlines())
        assert commits_after == commits_before, (
            f"Auto-save should be skipped when branch changed. "
            f"Before: {commits_before}, After: {commits_after}"
        )

        # worktree 仍然应该被创建
        assert meta["mode"] == "worktree"
        assert Path(meta["path"]).exists()

        # 清理
        ws_dir = Path(meta["path"])
        _run_cmd("git", "worktree", "remove", str(ws_dir), "--force", cwd=repo)
        _run_cmd("git", "worktree", "prune", cwd=repo)

    def test_record_main_branch_refreshed_on_task_start(self, tmp_path):
        """on_task_start 应刷新 _main_branch"""
        repo = tmp_path / "refresh_repo"
        _init_git_repo(repo)

        mgr = _make_manager(repo)
        assert mgr._main_branch == "main"

        # 切换到新分支
        _run_cmd("git", "checkout", "-b", "updated-branch", cwd=repo)

        # on_task_start 应该调用 _record_main_branch 刷新
        mock_task = MagicMock()
        mock_task.parent_task_id = None
        mgr._task_tree.get_task.return_value = mock_task

        task_data = {
            "task_id": "refresh1",
            "_has_explicit_workspace": False,
            "workspace_root": str(tmp_path / "ws"),
        }

        mgr.on_task_start("refresh1", "", task_data)

        # _main_branch 应被刷新为 "updated-branch"
        assert mgr._main_branch == "updated-branch", (
            f"Expected _main_branch='updated-branch', got '{mgr._main_branch}'"
        )
