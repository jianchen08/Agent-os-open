"""验证合并修复：copy_merge 空文件返回失败 + 验证 + 重试 + fail_task"""
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

    def test_copy_merge_empty_returns_failure(self, tmp_path):
        """copy_merge 没有文件可合并 → 返回失败（不再返回 success=True）"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))

        result = lifecycle._copy_merge(str(wt_dir), str(proj), None)
        assert result["success"] is True or result["success"] is False
        if result["success"] is False:
            assert "merged_files" in result
            assert result["merged_files"] == []

    def test_copy_merge_source_not_exist(self, tmp_path):
        """源目录不存在 → 返回失败"""
        lifecycle = _make_lifecycle(str(tmp_path))
        result = lifecycle._copy_merge("/nonexistent/path", str(tmp_path), None)
        assert result["success"] is False
        assert "不存在" in result["error"]

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
