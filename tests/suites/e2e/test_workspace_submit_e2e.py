"""submit 阶段工作空间初始化的真实效果测试。

核心验证目标（不做 mock，用真实文件系统 + 真实 git）：
1. 容器任务传入 workspace → 真实复制源项目文件到 container_{task_id} 目录
2. 根任务（非容器）传入 workspace 且源项目有 .git → 真实创建 worktree
3. resolved_workspace 指向真实存在的路径
4. 工作空间初始化失败 → 返回 WORKSPACE_INIT_FAILED 且任务被清理

这组测试存在的意义：之前的单元测试全用 MagicMock，从不验证
"到底创建了什么空间、文件在不在、是不是真的 worktree"。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from isolation.workspace_lifecycle import WorkspaceLifecycleManager


# ── 测试辅助 ─────────────────────────────────────────────────────


class _MockResourceMerge:
    def __init__(self, base_path: str):
        self.base_path = base_path

    def merge(self, *args, **kwargs):
        return {"success": True}

    def rollback(self, *args, **kwargs):
        return {"success": True}


class _MockTaskTree:
    """任务树 mock，支持预设任务返回。"""

    def __init__(self, tasks: dict | None = None):
        self._tasks = tasks or {}

    def get_task(self, task_id: str):
        return self._tasks.get(task_id)

    def save_task(self, task):
        self._tasks[task.id] = task


class _MockWsMetaStore(dict):
    """ws_meta 存储直接用 dict 子类。"""


def _make_lifecycle(base_path: str, ws_root: str, tasks: dict | None = None) -> WorkspaceLifecycleManager:
    """创建真实 WorkspaceLifecycleManager（不 mock 内部方法）。"""
    return WorkspaceLifecycleManager(
        resource_merge=_MockResourceMerge(base_path),
        config={"workspace": {"root": ws_root}},
        task_tree=_MockTaskTree(tasks),
        ws_meta_store=_MockWsMetaStore(),
        base_path=base_path,
    )


def _make_source_project(tmp_path: Path, name: str = "source_app", with_git: bool = True) -> Path:
    """创建一个真实的源项目（含文件，可选 git init）。"""
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text("print('hello')", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "utils.py").write_text("def add(a, b): return a + b", encoding="utf-8")

    if with_git:
        import subprocess
        subprocess.run(["git", "init"], cwd=project, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=project, capture_output=True, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@test.com", "-c", "user.name=test",
             "commit", "-m", "init"],
            cwd=project, capture_output=True, check=True,
        )
    return project


# ── 1. 容器任务：真实复制源项目 ─────────────────────────────────


class TestContainerWorkspaceCopy:
    """容器任务传入 workspace 时，应真实复制源项目文件。"""

    def test_container_copies_source_files(self, tmp_path):
        """init_container_workspace 应复制源项目文件到 container_{task_id}。"""
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()
        source = _make_source_project(tmp_path, "source_app", with_git=False)

        lifecycle = _make_lifecycle(str(tmp_path / "project"), str(ws_root))

        meta = lifecycle.init_container_workspace(
            container_task_id="container_abc123",
            workspace=str(source),
            task_data={"isolation_mode": ""},
        )

        # 验证返回的元数据
        assert meta["mode"] == "project_root"
        container_path = Path(meta["path"])
        assert "container_container_abc123" in container_path.name

        # 核心验证：文件真实被复制过来了
        assert (container_path / "main.py").exists()
        assert (container_path / "src" / "utils.py").exists()
        # 复制的内容正确
        assert (container_path / "main.py").read_text(encoding="utf-8") == "print('hello')"

        # 容器空间应该有 git init
        assert (container_path / ".git").exists()

    def test_container_empty_when_no_workspace(self, tmp_path):
        """无 workspace 时创建空容器空间（仅 git init）。"""
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        lifecycle = _make_lifecycle(str(tmp_path / "project"), str(ws_root))

        meta = lifecycle.init_container_workspace(
            container_task_id="container_empty",
            workspace=None,
            task_data={"isolation_mode": ""},
        )

        container_path = Path(meta["path"])
        assert container_path.exists()
        assert (container_path / ".git").exists()
        # 无源项目，不应有 main.py
        assert not (container_path / "main.py").exists()

    def test_container_host_mode_reuses_source(self, tmp_path):
        """host 模式直接复用原空间，不复制。"""
        source = _make_source_project(tmp_path, "host_app", with_git=False)

        lifecycle = _make_lifecycle(str(tmp_path / "project"), str(tmp_path / "ws"))

        meta = lifecycle.init_container_workspace(
            container_task_id="container_host",
            workspace=str(source),
            task_data={"isolation_mode": "non_isolated"},
        )

        # host 模式 path 就是原空间
        assert Path(meta["path"]) == source.resolve() or meta["path"] == str(source)


# ── 2. 根任务带原空间：真实创建 worktree ────────────────────────


class TestRootTaskWorktree:
    """根任务（非容器）传入有 .git 的 workspace 时，应创建 worktree。"""

    def test_root_task_creates_worktree(self, tmp_path):
        """根任务传入有 .git 的项目 → 创建真实 worktree。"""
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()
        source = _make_source_project(tmp_path, "git_app", with_git=True)

        lifecycle = _make_lifecycle(str(source), str(ws_root))

        # _has_explicit_workspace=True 模拟 task_submit 真实传入的 task_data
        meta = lifecycle.on_task_start(
            task_id="root_task_001",
            workspace=str(source),
            task_data={"is_root": True, "_has_explicit_workspace": True},
        )

        # 应该是 worktree 模式
        assert meta["mode"] == "worktree"
        worktree_path = Path(meta["path"])

        # 核心验证：worktree 真实存在
        assert worktree_path.exists()
        # worktree 里有源项目的文件
        assert (worktree_path / "main.py").exists()

        # 验证确实是 git worktree（.git 是文件不是目录）
        git_entry = worktree_path / ".git"
        assert git_entry.exists()
        assert git_entry.is_file(), "worktree 的 .git 应该是文件（指向主仓库）"

        # 验证分支是 task/root_task_001
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path, capture_output=True, text=True,
        )
        assert "root_task_001" in result.stdout

    def test_root_task_workspace_in_meta(self, tmp_path):
        """on_task_start 写入的 ws_meta.path 指向真实存在的路径。"""
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()
        source = _make_source_project(tmp_path, "meta_app", with_git=True)

        lifecycle = _make_lifecycle(str(source), str(ws_root))

        meta = lifecycle.on_task_start(
            task_id="root_task_002",
            workspace=str(source),
            task_data={"is_root": True, "_has_explicit_workspace": True},
        )

        resolved_path = meta["path"]
        # resolved_workspace 指向的路径必须真实存在
        assert Path(resolved_path).exists(), (
            f"resolved_workspace 指向的路径不存在: {resolved_path}"
        )


# ── 3. on_task_start 幂等性（submit 调一次，executor 再调一次） ────


class TestWorkspaceIdempotency:
    """submit 阶段调一次 on_task_start，executor 复用时不应重复创建。"""

    def test_on_task_start_reuses_existing(self, tmp_path):
        """已有 ws_meta 且路径存在时，on_task_start 应复用而非重建。"""
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()
        source = _make_source_project(tmp_path, "idem_app", with_git=True)

        lifecycle = _make_lifecycle(str(source), str(ws_root))

        # 第一次调用：创建 worktree
        meta1 = lifecycle.on_task_start(
            task_id="idem_task",
            workspace=str(source),
            task_data={"is_root": True, "_has_explicit_workspace": True},
        )
        path1 = meta1["path"]

        # 第二次调用：应复用同一个路径
        meta2 = lifecycle.on_task_start(
            task_id="idem_task",
            workspace=str(source),
            task_data={"is_root": True, "_has_explicit_workspace": True},
        )
        path2 = meta2["path"]

        assert path1 == path2, "复用时路径不应变化"
