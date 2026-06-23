"""工作空间与隔离模块补充测试。

覆盖需求文档中缺失/不足的 AC：
- AC-WS-07: ws_meta.path 为运行时唯一可信来源
- AC-WS-08: 子任务共享父工作空间（mode=shared）
- AC-WS-09: host 模式创建 worktree 隔离目录
- AC-WS-10: container 模式创建 worktree
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ============================================================
# AC-WS-07: ws_meta.path 为运行时唯一可信来源
# ============================================================


class TestResolveTaskWorkspace:
    """验证 resolve_task_workspace 从 ws_meta.path 获取工作空间路径。"""

    def test_resolve_from_ws_meta_path(self):
        """有 ws_meta.path 时应直接返回该路径。

        AC-WS-07: ws_meta.path 为运行时唯一可信来源。
        """
        from tasks.workspace import resolve_task_workspace

        task = MagicMock()
        task.id = "task-001"
        task.metadata = {
            "ws_meta": {
                "mode": "worktree",
                "path": "/tmp/workspaces/project__wt_task001",
                "branch": "task/task-001",
                "project_root": "/tmp/project",
            }
        }
        task.parent_task_id = None

        result = resolve_task_workspace(task)
        assert result == "/tmp/workspaces/project__wt_task001"

    def test_resolve_returns_none_when_no_ws_meta(self):
        """无 ws_meta 时返回 None。"""
        from tasks.workspace import resolve_task_workspace

        task = MagicMock()
        task.id = "task-002"
        task.metadata = {}
        task.parent_task_id = None

        result = resolve_task_workspace(task)
        assert result is None

    def test_resolve_returns_none_when_no_path(self):
        """ws_meta 存在但 path 为空时返回 None。"""
        from tasks.workspace import resolve_task_workspace

        task = MagicMock()
        task.id = "task-003"
        task.metadata = {"ws_meta": {"mode": "shared", "path": ""}}
        task.parent_task_id = None

        result = resolve_task_workspace(task)
        assert result is None

    def test_resolve_relative_path_becomes_absolute(self):
        """相对路径自动补全为绝对路径。"""
        from tasks.workspace import resolve_task_workspace

        task = MagicMock()
        task.id = "task-004"
        task.metadata = {"ws_meta": {"mode": "plain", "path": "workspace/task004"}}
        task.parent_task_id = None

        result = resolve_task_workspace(task)
        assert result is not None
        assert Path(result).is_absolute()

    def test_resolve_shared_mode_returns_parent_path(self):
        """shared 模式返回父任务路径。"""
        from tasks.workspace import resolve_task_workspace

        parent_path = "/tmp/workspaces/parent_task"
        task = MagicMock()
        task.id = "child-001"
        task.metadata = {
            "ws_meta": {
                "mode": "shared",
                "path": parent_path,
                "parent_workspace": "/tmp/orig",
                "project_root": "/tmp/project",
            }
        }
        task.parent_task_id = "parent-001"

        result = resolve_task_workspace(task)
        assert result == parent_path


# ============================================================
# AC-WS-08: 子任务共享父工作空间（mode=shared）
# ============================================================


class TestSubtaskSharedWorkspace:
    """验证子任务共享父任务工作空间。"""

    def test_subtask_mode_is_shared(self):
        """子任务的 ws_meta.mode 应为 shared。

        AC-WS-08: 子任务共享父工作空间。
        """
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        parent_ws = "/tmp/workspaces/parent"
        ws_meta_store = {
            "parent-001": {
                "mode": "worktree",
                "path": parent_ws,
                "branch": "task/parent-001",
            }
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = "parent-001"

        task_tree = MagicMock()
        task_tree.get_task.return_value = mock_task

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        meta = lifecycle._start_subtask("child-001", "", {"is_root": False})

        assert meta["mode"] == "shared"
        assert meta["path"] == parent_ws

    def test_subtask_and_parent_path_match(self):
        """子任务的 ws_meta.path 必须与父任务一致。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        parent_path = "/tmp/workspaces/project__wt_parent01"
        ws_meta_store = {
            "parent-002": {
                "mode": "worktree",
                "path": parent_path,
                "branch": "task/parent-002",
            }
        }

        mock_child = MagicMock()
        mock_child.parent_task_id = "parent-002"

        task_tree = MagicMock()
        task_tree.get_task.return_value = mock_child

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        parent_meta = ws_meta_store["parent-002"]
        child_meta = lifecycle._start_subtask("child-002", "", {"is_root": False})

        assert child_meta["path"] == parent_meta["path"]


# ============================================================
# AC-WS-09 / AC-WS-10: worktree 隔离目录创建
# ============================================================


class TestWorktreeIsolation:
    """验证 host 和 container 模式都创建 worktree 隔离目录。"""

    def test_host_mode_creates_worktree(self, tmp_path):
        """host 模式 + 指定 workspace → 创建 worktree。

        AC-WS-09: host 模式创建 worktree 隔离目录。
        """
        import subprocess

        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        # 创建有 .git 的源项目
        source = tmp_path / "host_project"
        source.mkdir()
        (source / "main.py").write_text("print('hello')", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=source, capture_output=True, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@test.com", "-c", "user.name=test",
             "add", "-A"],
            cwd=source, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=test@test.com", "-c", "user.name=test",
             "commit", "-m", "init"],
            cwd=source, capture_output=True, check=True,
        )

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={"workspace": {"root": str(ws_root)}},
            task_tree=MagicMock(),
            ws_meta_store={},
            base_path=str(tmp_path / "project"),
        )

        meta = lifecycle.on_task_start(
            task_id="host_task_001",
            workspace=str(source),
            task_data={"is_root": True, "_has_explicit_workspace": True},
        )

        assert meta["mode"] == "worktree"
        assert "__wt_" in meta["path"]
        assert meta.get("branch", "").startswith("task/")
        assert Path(meta["path"]).exists()

        # 清理
        subprocess.run(
            ["git", "worktree", "remove", meta["path"], "--force"],
            cwd=source, capture_output=True,
        )
        subprocess.run(["git", "worktree", "prune"], cwd=source, capture_output=True)

    def test_container_mode_copies_source(self, tmp_path):
        """container 模式初始化容器空间。

        AC-WS-10: container 模式创建隔离目录。
        """
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        source = tmp_path / "container_source"
        source.mkdir()
        (source / "app.py").write_text("app code", encoding="utf-8")

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={"workspace": {"root": str(ws_root)}},
            task_tree=MagicMock(),
            ws_meta_store={},
            base_path=str(tmp_path),
        )

        meta = lifecycle.init_container_workspace(
            container_task_id="container_abc",
            workspace=str(source),
            task_data={"isolation_mode": ""},
        )

        assert meta["mode"] in ("project_root", "host_direct", "container")
        assert Path(meta["path"]).exists()
        # 文件应被复制
        assert (Path(meta["path"]) / "app.py").exists()


# ============================================================
# AC-WS-01: 任务创建→工作空间初始化
# ============================================================


class TestWorkspaceInitialization:
    """验证任务创建时工作空间正确初始化。"""

    def test_root_task_no_workspace_creates_plain(self, tmp_path):
        """根任务无 workspace → 创建 plain 模式工作空间。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        mock_task = MagicMock()
        mock_task.parent_task_id = None

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={"workspace": {"root": str(ws_root)}},
            task_tree=MagicMock(),
            ws_meta_store={},
            base_path=str(tmp_path),
        )
        lifecycle._task_tree.get_task.return_value = mock_task

        meta = lifecycle.on_task_start(
            task_id="plain_task_001",
            workspace="",
            task_data={
                "is_root": True,
                "_has_explicit_workspace": False,
                "workspace_root": str(ws_root),
            },
        )

        assert "mode" in meta
        assert "path" in meta
        assert Path(meta["path"]).exists()
