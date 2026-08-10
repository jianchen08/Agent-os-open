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


def _setup_conflicting_project(tmp: Path) -> tuple[Path, Path, str]:
    """构造 add/add 冲突：主分支与 worktree 分支各自独立新增同名文件，内容不同。

    复现 a3e6a2f3c6f8 任务的真实故障场景——多个子任务并行生成同名报告文件
    (docs/working/function_verify_report.md)，第一个子任务合并后 main 上已有同名文件，
    后续子任务的 worktree 分支也独立写了同名文件，merge 时触发 add/add 冲突。
    必须让两边在分叉后各自独立 add（而非一边基于另一边修改），才能产生真冲突。
    """
    proj = tmp / "project"
    proj.mkdir()
    _git("init", cwd=proj)
    _git("config", "user.email", "test@test.com", cwd=proj)
    _git("config", "user.name", "Test", cwd=proj)
    (proj / "README.md").write_text("init", encoding="utf-8")
    _git("add", "-A", cwd=proj)
    _git("commit", "-m", "init", cwd=proj)
    # 先创建 worktree（此时 main 上还没有同名报告文件）
    wt_dir = tmp / "wt_conflict"
    _git("worktree", "add", "-b", "task/conflict1", str(wt_dir), cwd=proj)
    # worktree 分支独立新增同名文件
    wt_conflict = wt_dir / "docs" / "working" / "function_verify_report.md"
    wt_conflict.parent.mkdir(parents=True, exist_ok=True)
    wt_conflict.write_text("worktree branch version", encoding="utf-8")
    _git("add", "-A", cwd=wt_dir)
    _git("commit", "-m", "wt adds report", cwd=wt_dir)
    # main 分支也独立新增同名文件（模拟第一个子任务已合并，main 上已有同名产物）
    main_conflict = proj / "docs" / "working" / "function_verify_report.md"
    main_conflict.parent.mkdir(parents=True, exist_ok=True)
    main_conflict.write_text("main branch version", encoding="utf-8")
    _git("add", "-A", cwd=proj)
    _git("commit", "-m", "main adds same report", cwd=proj)
    return proj, wt_dir, "task/conflict1"


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


# ── _safe_merge 失败诊断（BUG: unknown 信息丢失） ─────────────


class TestSafeMergeFailureDiagnostics:
    """回归：_safe_merge 失败时错误信息必须包含 stdout 和冲突文件清单。

    历史 BUG：git merge 把 CONFLICT 行写到 stdout（不是 stderr），
    旧实现 `rc, _, stderr = ...` 丢弃 stdout，stderr 为空时 fallback 成
    'unknown'，导致所有冲突失败原因不可见（任务 a3e6a2f3c6f8）。
    """

    def test_conflict_error_includes_stdout_and_conflict_files(self, tmp_path: Path) -> None:
        """add/add 冲突时 error 必须包含 CONFLICT 行(stdout)和冲突文件名。"""
        proj, wt_dir, branch = _setup_conflicting_project(tmp_path)
        mgr = _make_lifecycle(str(proj))

        ws_meta = {
            "mode": "worktree",
            "path": str(wt_dir),
            "branch": branch,
            "project_root": str(proj),
        }

        result = mgr._safe_merge(str(wt_dir), ws_meta)

        assert result["success"] is False
        error = result["error"]
        # 核心断言：绝不能出现无信息的 'unknown'
        assert "unknown" not in error, f"错误信息退化为 unknown: {error}"
        # 必须包含 stdout 中的 CONFLICT 标识（git 把冲突信息写到 stdout）
        assert "CONFLICT" in error, f"缺少 CONFLICT 标识: {error}"
        # 必须包含冲突文件清单（来自 git diff --diff-filter=U）
        assert "冲突文件" in error, f"缺少冲突文件清单: {error}"
        assert "function_verify_report.md" in error, f"冲突文件名缺失: {error}"

    def test_conflict_error_includes_branch_name(self, tmp_path: Path) -> None:
        """错误信息必须带 branch 名，方便定位是哪个子任务的合并失败。"""
        proj, wt_dir, branch = _setup_conflicting_project(tmp_path)
        mgr = _make_lifecycle(str(proj))

        ws_meta = {
            "mode": "worktree",
            "path": str(wt_dir),
            "branch": branch,
            "project_root": str(proj),
        }

        result = mgr._safe_merge(str(wt_dir), ws_meta)

        assert result["success"] is False
        assert branch in result["error"], f"错误信息缺少 branch 名: {result['error']}"

    def test_merge_aborted_after_conflict(self, tmp_path: Path) -> None:
        """冲突后必须 git merge --abort，不能让 project_root 停在冲突状态。"""
        proj, wt_dir, branch = _setup_conflicting_project(tmp_path)
        mgr = _make_lifecycle(str(proj))
        ws_meta = {
            "mode": "worktree",
            "path": str(wt_dir),
            "branch": branch,
            "project_root": str(proj),
        }

        mgr._safe_merge(str(wt_dir), ws_meta)

        # abort 后 main 上不应该有未解决的冲突
        rc, status, _ = _git("status", "--porcelain", cwd=str(proj))
        assert rc == 0
        # 不应出现 UU/AA/DD 等冲突状态标记
        for line in status.splitlines():
            assert not line.startswith(("UU", "AA", "DD", "AU", "UA", "DU", "UD")), (
                f"残留冲突状态: {line}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
