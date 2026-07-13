"""合并门控修复测试。

覆盖 BUG-FIX-fix_20260618_lifecycle_not_in_provider 的核心改动：
- WorkspaceLifecycleManager.merge_worktree_before_complete 四个分支
- task_evaluate._try_merge_before_complete 委托 + lifecycle 未注册兜底

回归保障：worktree 任务标记 completed 前必须真正合并，否则标记 failed。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


def _git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10,
    )
    return r.returncode, r.stdout, r.stderr


def _make_lifecycle(project_root: str) -> Any:
    """构造真实的 WorkspaceLifecycleManager（git 操作可用）。"""
    from isolation.workspace_lifecycle import WorkspaceLifecycleManager

    task_svc = MagicMock()
    task_svc.get_task.return_value = None
    return WorkspaceLifecycleManager(
        resource_merge=MagicMock(),
        config={},
        task_tree=task_svc,
        ws_meta_store={},
        base_path=project_root,
    )


def _setup_project(tmp: Path) -> tuple[Path, Path]:
    """创建带 git 的项目 + worktree 分支，并在 worktree 中提交改动。"""
    proj = tmp / "project"
    proj.mkdir()
    _git("init", cwd=proj)
    _git("config", "user.email", "test@test.com", cwd=proj)
    _git("config", "user.name", "Test", cwd=proj)
    (proj / "hello.txt").write_text("hello", encoding="utf-8")
    _git("add", "-A", cwd=proj)
    _git("commit", "-m", "init", cwd=proj)
    wt_dir = tmp / "wt_test"
    _git("worktree", "add", "-b", "task/test1", str(wt_dir), cwd=proj)
    (wt_dir / "new_file.txt").write_text("new content", encoding="utf-8")
    _git("add", "-A", cwd=wt_dir)
    _git("commit", "-m", "changes", cwd=wt_dir)
    return proj, wt_dir


# ── merge_worktree_before_complete ───────────────────────────


class TestMergeWorktreeBeforeComplete:
    """合并门控公共方法的四个分支。"""

    def test_worktree_merge_success(self, tmp_path: Path) -> None:
        """分支①：worktree 模式合并成功 → 返回 None，文件到达 project_root。"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        task = MagicMock()
        task.metadata = {
            "ws_meta": {
                "mode": "worktree",
                "path": str(wt_dir),
                "branch": "task/test1",
                "project_root": str(proj),
            }
        }
        lifecycle._task_tree.get_task.return_value = task

        err = lifecycle.merge_worktree_before_complete("test1")

        assert err is None
        assert (proj / "new_file.txt").read_text(encoding="utf-8") == "new content"

    def test_worktree_merge_failure_returns_error(self, tmp_path: Path) -> None:
        """分支②：worktree 模式合并失败 → 返回错误字符串（而非 None）。"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        # 注入会失败的合并
        lifecycle.on_eval_passed = MagicMock(
            return_value={"success": False, "error": "模拟合并冲突"},
        )
        task = MagicMock()
        task.metadata = {
            "ws_meta": {
                "mode": "worktree",
                "path": str(wt_dir),
                "branch": "task/test1",
                "project_root": str(proj),
            }
        }
        lifecycle._task_tree.get_task.return_value = task

        err = lifecycle.merge_worktree_before_complete("test1")

        assert err is not None
        assert "模拟合并冲突" in err

    def test_plain_mode_skips_merge(self, tmp_path: Path) -> None:
        """分支③：plain/shared 模式无需合并 → 返回 None，不调 on_eval_passed。"""
        lifecycle = _make_lifecycle(str(tmp_path))
        lifecycle.on_eval_passed = MagicMock()
        task = MagicMock()
        task.metadata = {"ws_meta": {"mode": "plain", "path": str(tmp_path)}}
        lifecycle._task_tree.get_task.return_value = task

        err = lifecycle.merge_worktree_before_complete("test1")

        assert err is None
        lifecycle.on_eval_passed.assert_not_called()

    def test_missing_ws_meta_returns_error(self, tmp_path: Path) -> None:
        """分支④：worktree 任务读不到 ws_meta → 返回错误（不再静默跳过）。

        这是本次修复的核心：旧逻辑返回 None 假装成功，导致产出永久丢失。
        """
        lifecycle = _make_lifecycle(str(tmp_path))
        task = MagicMock()
        task.metadata = {}  # 无 ws_meta
        lifecycle._task_tree.get_task.return_value = task
        # _ws_meta_store 也为空
        lifecycle._ws_meta_store = {}

        err = lifecycle.merge_worktree_before_complete("test1")

        assert err is not None
        assert "ws_meta" in err

    def test_ws_meta_fallback_from_store(self, tmp_path: Path) -> None:
        """兜底：task.metadata 缺失但 _ws_meta_store 有 → 从 store 恢复并合并。"""
        proj, wt_dir = _setup_project(tmp_path)
        lifecycle = _make_lifecycle(str(proj))
        # task.metadata 无 ws_meta（模拟异步持久化延迟）
        task = MagicMock()
        task.metadata = {}
        lifecycle._task_tree.get_task.return_value = task
        # 但 store 里有
        lifecycle._ws_meta_store = {
            "test1": {
                "mode": "worktree",
                "path": str(wt_dir),
                "branch": "task/test1",
                "project_root": str(proj),
            }
        }

        err = lifecycle.merge_worktree_before_complete("test1")

        assert err is None
        assert (proj / "new_file.txt").read_text(encoding="utf-8") == "new content"


# ── _try_merge_before_complete 委托 ───────────────────────────


class TestTryMergeBeforeCompleteDelegation:
    """task_evaluate 工具的合并门控委托逻辑。"""

    def test_delegates_to_lifecycle_when_registered(self) -> None:
        """lifecycle 已注册到 ServiceProvider → 调用 merge_worktree_before_complete。"""
        from infrastructure.service_provider import ServiceProvider
        from tools.builtin.task_evaluate.tool import TaskEvaluateTool

        ServiceProvider.reset()
        provider = ServiceProvider()
        mock_lifecycle = MagicMock()
        mock_lifecycle.merge_worktree_before_complete.return_value = None
        provider.register("workspace_lifecycle_manager", mock_lifecycle)

        tool = TaskEvaluateTool()
        task = MagicMock()
        task.id = "task_x"

        err = tool._try_merge_before_complete(task)

        assert err is None
        mock_lifecycle.merge_worktree_before_complete.assert_called_once_with("task_x")

    def test_returns_none_when_lifecycle_not_registered(self) -> None:
        """lifecycle 未注册 → 记录 warning 并返回 None（不阻塞非 worktree 任务）。"""
        from infrastructure.service_provider import ServiceProvider
        from tools.builtin.task_evaluate.tool import TaskEvaluateTool

        ServiceProvider.reset()
        ServiceProvider()  # 空的 provider，无 lifecycle

        tool = TaskEvaluateTool()
        task = MagicMock()
        task.id = "task_y"

        err = tool._try_merge_before_complete(task)

        assert err is None  # lifecycle 不可用时不能误判失败

    def test_propagates_merge_failure(self) -> None:
        """合并失败 → 错误字符串透传，调用方据此标记 failed。"""
        from infrastructure.service_provider import ServiceProvider
        from tools.builtin.task_evaluate.tool import TaskEvaluateTool

        ServiceProvider.reset()
        provider = ServiceProvider()
        mock_lifecycle = MagicMock()
        mock_lifecycle.merge_worktree_before_complete.return_value = "git merge 冲突"
        provider.register("workspace_lifecycle_manager", mock_lifecycle)

        tool = TaskEvaluateTool()
        task = MagicMock()
        task.id = "task_z"

        err = tool._try_merge_before_complete(task)

        assert err == "git merge 冲突"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
