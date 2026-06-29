"""验证合并修复：git_merge 验证（含 diff 基准）+ 重试 + fail_task"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def git(*args, cwd=None):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, timeout=10)
    return r.returncode, r.stdout, r.stderr


def _make_lifecycle(project_root: str):
    from isolation.workspace_lifecycle import WorkspaceLifecycleManager
    task_svc = MagicMock()
    task_svc.get_task.return_value = None
    ws_meta_store = {}
    resource_merge = MagicMock()
    return WorkspaceLifecycleManager(
        resource_merge=resource_merge,
        config={},
        task_tree=task_svc,
        ws_meta_store=ws_meta_store,
        base_path=project_root,
    )


def _setup_project(tmp: Path):
    """创建带 git 的项目 + worktree，在 worktree 中做修改"""
    proj = tmp / "project"
    proj.mkdir()
    git("init", cwd=proj)
    git("config", "user.email", "test@test.com", cwd=proj)
    git("config", "user.name", "Test", cwd=proj)
    (proj / "hello.txt").write_text("hello")
    git("add", "-A", cwd=proj)
    git("commit", "-m", "init", cwd=proj)
    wt_dir = tmp / "wt_test"
    git("worktree", "add", "-b", "task/test1", str(wt_dir), cwd=proj)
    (wt_dir / "hello.txt").write_text("hello modified")
    (wt_dir / "new_file.txt").write_text("new content")
    git("add", "-A", cwd=wt_dir)
    git("commit", "-m", "changes", cwd=wt_dir)
    return proj, wt_dir


class TestMergeVerifyFix:

    def test_git_merge_success_with_verify(self, tmp_path):
        """正常 git_merge 合并 + 验证通过 → 清理 worktree"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        ws_meta = {
            "mode": "worktree",
            "path": str(wt_dir),
            "branch": "task/test1",
            "project_root": str(proj),
        }
        result = lifecycle.on_eval_passed("test1", str(wt_dir), ws_meta)

        assert result["success"] is True
        assert result["method"] == "git_merge"
        assert (proj / "hello.txt").read_text() == "hello modified"
        assert (proj / "new_file.txt").read_text() == "new content"
        assert not wt_dir.exists(), "worktree 应该被清理"

    def test_verify_merge_result_copy(self, tmp_path):
        """copy_merge 验证：文件到达目标 → 通过"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        ws_meta = {"branch": "task/test1", "project_root": str(proj)}
        merge_result = {
            "method": "copy",
            "merged_files": ["hello.txt", "new_file.txt"],
        }
        (proj / "hello.txt").write_text("content")
        (proj / "new_file.txt").write_text("content")

        verified, detail = lifecycle._verify_merge_result(
            str(wt_dir), str(proj), ws_meta, merge_result)
        assert verified is True
        assert detail == "验证通过"

    def test_verify_merge_result_copy_missing_files(self, tmp_path):
        """copy_merge 验证：文件未到达 → 失败"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        ws_meta = {"branch": "task/test1", "project_root": str(proj)}
        merge_result = {
            "method": "copy",
            "merged_files": ["hello.txt", "missing_file.txt"],
        }
        (proj / "hello.txt").write_text("content")
        if (proj / "missing_file.txt").exists():
            (proj / "missing_file.txt").unlink()

        verified, detail = lifecycle._verify_merge_result(
            str(wt_dir), str(proj), ws_meta, merge_result)
        assert verified is False
        assert "missing_file.txt" in detail

    def test_verify_merge_result_project_root_not_exist(self, tmp_path):
        """project_root 不存在 → 验证失败"""
        lifecycle = _make_lifecycle(str(tmp_path))
        ws_meta = {"branch": "task/test1", "project_root": "/nonexistent"}
        verified, detail = lifecycle._verify_merge_result(
            "/tmp/ws", "/nonexistent", ws_meta, {"method": "copy", "merged_files": []})
        assert verified is False
        assert "不存在" in detail

    def test_git_merge_with_deleted_files_not_misjudged(self, tmp_path):
        """git_merge 合并含删除文件的任务不应被验证逻辑误判为失败

        回归 BUG-fix_20260628_merge_verify_deleted_files:
        原验证用 `git diff --name-only` 取改动文件后逐个 exists() 校验，
        不区分增/改/删 → 任务正确删除的废弃文件合并后本就不存在，
        被误判为「文件未到达目标」，重组/清理类任务必然合并失败（重试也无解）。
        """
        proj, wt_dir = _setup_project(tmp_path)
        # worktree 删除一个已跟踪文件（模拟任务清理废弃模块）
        (wt_dir / "new_file.txt").unlink()
        git("add", "-A", cwd=wt_dir)
        git("commit", "-m", "remove deprecated file", cwd=wt_dir)

        lifecycle = _make_lifecycle(str(proj))
        ws_meta = {
            "mode": "worktree",
            "path": str(wt_dir),
            "branch": "task/test1",
            "project_root": str(proj),
        }
        result = lifecycle.on_eval_passed("test_del", str(wt_dir), ws_meta)

        assert result["success"] is True, result
        assert result["method"] == "git_merge"
        # 删除已生效
        assert not (proj / "new_file.txt").exists()
        # 修改已生效
        assert (proj / "hello.txt").read_text() == "hello modified"
        assert not wt_dir.exists(), "worktree 应被清理"

    def test_git_merge_multiple_commits_not_misjudged(self, tmp_path):
        """worktree 含多个 commit 时，文件级验证不应被旧 diff 基准误判

        回归 BUG-fix_20260629_verify_diff_base:
        旧逻辑用 `branch~1..branch` 作 diff 基准，只比分支【最后一次 commit】的
        增量。worktree 上有多个 commit 时：第一个 commit 新建的文件不在最后一次
        commit 的 diff 里，旧逻辑虽取到该文件（因 diff 基准仍能列出本次 commit
        外的文件）但实际场景中多次出现「N 文件未到目标」的误判（实测线上 case
        6 文件）。新基准 `pre_merge_head..HEAD` 精确反映合并真实增量。
        本用例构造 worktree 两次 commit：A 文件在首次 commit、B 文件在第二次，
        合并后两个文件都应到达 project_root 且验证通过。
        """
        proj, wt_dir = _setup_project(tmp_path)
        # _setup_project 已在 worktree 做了 1 次 commit；再追加第 2 个 commit
        (wt_dir / "second_commit_file.txt").write_text("v2 content")
        git("add", "-A", cwd=wt_dir)
        git("commit", "-m", "second commit on task branch", cwd=wt_dir)

        lifecycle = _make_lifecycle(str(proj))
        ws_meta = {
            "mode": "worktree",
            "path": str(wt_dir),
            "branch": "task/test1",
            "project_root": str(proj),
        }
        result = lifecycle.on_eval_passed("test_multi", str(wt_dir), ws_meta)

        assert result["success"] is True, result
        assert result["method"] == "git_merge"
        # 两个 commit 的产出都应到达合并目标
        assert (proj / "new_file.txt").read_text() == "new content"
        assert (proj / "second_commit_file.txt").read_text() == "v2 content"
        assert (proj / "hello.txt").read_text() == "hello modified"
        assert not wt_dir.exists(), "worktree 应被清理"

    def test_verify_uses_pre_merge_head_base(self, tmp_path):
        """_verify_merge_result 用 pre_merge_head..HEAD 而非 branch~1..branch

        直接验证 diff 基准：构造一次干净合并，确认校验基于 merge_result 里的
        pre_merge_head。若误用 branch~1..branch，跨 commit 场景会列出错误文件。
        """
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        # 取合并前 HEAD 作为 pre_merge_head 基准
        rc, head, _ = git("rev-parse", "HEAD", cwd=proj)
        assert rc == 0
        pre_merge_head = head.strip()
        # 真正把 task/test1 合并进来，使 commit graph 校验（HEAD..branch 为空）成立
        rc_m, _, err = git("merge", "task/test1", "--no-edit", cwd=proj)
        assert rc_m == 0, f"merge 失败: {err}"
        # 合并后这两个文件应已在 project_root
        assert (proj / "new_file.txt").exists()

        ws_meta = {"branch": "task/test1", "project_root": str(proj)}
        merge_result = {"method": "git_merge", "pre_merge_head": pre_merge_head}
        verified, detail = lifecycle._verify_merge_result(
            str(wt_dir), str(proj), ws_meta, merge_result)
        assert verified is True, detail

    def test_verify_skips_file_check_when_no_pre_merge_head(self, tmp_path):
        """pre_merge_head 缺失时跳过文件级校验（仅 commit graph 校验）"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        ws_meta = {"branch": "task/test1", "project_root": str(proj)}
        # 不带 pre_merge_head，强制走跳过分支
        merge_result = {"method": "git_merge"}
        verified, detail = lifecycle._verify_merge_result(
            str(wt_dir), str(proj), ws_meta, merge_result)
        # 分支已合并到当前 HEAD（_setup_project 后 task/test1 在初始 commit 上分叉，
        # 这里分支未真正合并，commit graph 校验可能失败）—— 关键断言是「不因缺
        # pre_merge_head 而返回 git_merge 文件验证失败」
        assert "git_merge 文件验证失败" not in detail

    def test_on_eval_passed_retries_on_verify_fail(self, tmp_path):
        """验证失败时重试，最终 worktree 保留不清理"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        ws_meta = {
            "mode": "worktree",
            "path": str(wt_dir),
            "branch": "task/test1",
            "project_root": str(proj),
        }
        call_count = 0

        def mock_verify(workspace, project_root, ws_meta_arg, merge_result):
            nonlocal call_count
            call_count += 1
            return False, f"mock验证失败 call={call_count}"

        lifecycle._verify_merge_result = mock_verify
        result = lifecycle.on_eval_passed("test1", str(wt_dir), ws_meta)

        assert result["success"] is False
        assert result["success"] is False
        assert "verify_error" in result
        assert call_count == 2, f"应重试2次, 实际调用{call_count}次"
        assert wt_dir.exists(), "worktree 应该保留不清理"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
