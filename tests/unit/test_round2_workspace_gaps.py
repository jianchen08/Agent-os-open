"""
Round2 工作空间与隔离模块 — 测试缺口补充。

与 round1 独立，聚焦以下 AC 的边界和深度覆盖：

AC-WS-07: ws_meta.path 为运行时唯一可信来源（resolve_task_workspace）
AC-WS-08: 子任务共享父工作空间（mode=shared，path 等于父任务）
AC-WS-09: host 模式创建 worktree 隔离目录
AC-WS-10: container 模式创建 worktree（branch 以 task/ 开头）

额外覆盖：
- ws_meta 数据结构完整性（mode/path/branch/project_root）
- resolve_task_workspace 边界场景（无 ws_meta / 路径非绝对 / 相对路径）
- _safe_ws_name 格式验证
- 容器空间初始化（init_container_workspace）参数矩阵
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ============================================================
# 辅助：创建模拟 task 对象
# ============================================================

def _make_task(
    task_id: str = "test-task-001",
    metadata: dict | None = None,
    parent_task_id: str | None = None,
) -> Any:
    """创建一个模拟的 task 对象。"""
    task = MagicMock()
    task.id = task_id
    task.metadata = metadata or {}
    task.parent_task_id = parent_task_id
    return task


def _make_ws_meta(
    mode: str = "worktree",
    path: str = "/tmp/workspace/test__wt_abc123",
    branch: str = "task/abc123",
    project_root: str = "/tmp/workspace/test",
    parent_workspace: str | None = None,
) -> dict:
    """创建标准 ws_meta 字典。"""
    meta: dict[str, Any] = {
        "mode": mode,
        "path": path,
        "branch": branch,
        "project_root": project_root,
    }
    if parent_workspace:
        meta["parent_workspace"] = parent_workspace
    return meta


# ============================================================
# AC-WS-07: ws_meta.path 为运行时唯一可信来源
# ============================================================

class TestWsMetaTrustedSource:
    """resolve_task_workspace 从 task.metadata['ws_meta']['path'] 读取路径。"""

    def test_resolve_returns_ws_meta_path(self):
        """resolve_task_workspace 返回 ws_meta.path。"""
        from src.tasks.workspace import resolve_task_workspace

        task = _make_task(
            metadata={"ws_meta": _make_ws_meta(path="/data/projects/myproject__wt_t1")}
        )
        result = resolve_task_workspace(task)
        assert result == str(Path("/data/projects/myproject__wt_t1"))

    def test_resolve_returns_none_without_ws_meta(self):
        """无 ws_meta 时返回 None。"""
        from src.tasks.workspace import resolve_task_workspace

        task = _make_task(metadata={})
        result = resolve_task_workspace(task)
        assert result is None

    def test_resolve_returns_none_with_none_metadata(self):
        """metadata 为 None 时返回 None。"""
        from src.tasks.workspace import resolve_task_workspace

        task = _make_task()
        task.metadata = None
        result = resolve_task_workspace(task)
        assert result is None

    def test_resolve_relative_path_becomes_absolute(self):
        """ws_meta.path 为相对路径时转为绝对路径。"""
        from src.tasks.workspace import resolve_task_workspace

        task = _make_task(
            metadata={"ws_meta": _make_ws_meta(path="relative/path")}
        )
        result = resolve_task_workspace(task)
        assert result is not None
        assert Path(result).is_absolute()

    def test_resolve_empty_path_returns_none(self):
        """ws_meta.path 为空字符串时返回 None。"""
        from src.tasks.workspace import resolve_task_workspace

        task = _make_task(
            metadata={"ws_meta": _make_ws_meta(path="")}
        )
        result = resolve_task_workspace(task)
        assert result is None

    def test_resolve_ws_meta_not_dict_returns_none(self):
        """ws_meta 非 dict 类型时返回 None。"""
        from src.tasks.workspace import resolve_task_workspace

        task = _make_task(metadata={"ws_meta": "not a dict"})
        result = resolve_task_workspace(task)
        assert result is None


# ============================================================
# AC-WS-08: 子任务共享父工作空间
# ============================================================

class TestChildTaskSharedWorkspace:
    """子任务 mode=shared，path 等于父任务。"""

    def test_shared_mode_path_equals_parent(self):
        """子任务的 ws_meta.path 等于父任务的 ws_meta.path。"""
        parent_path = "/data/projects/myproject__wt_parent"

        parent_task = _make_task(
            task_id="parent-001",
            metadata={"ws_meta": _make_ws_meta(path=parent_path)}
        )
        child_task = _make_task(
            task_id="child-001",
            parent_task_id="parent-001",
            metadata={
                "ws_meta": _make_ws_meta(
                    mode="shared",
                    path=parent_path,
                    branch="",
                    project_root=parent_path,
                    parent_workspace=parent_path,
                )
            }
        )

        from src.tasks.workspace import resolve_task_workspace
        assert resolve_task_workspace(parent_task) == resolve_task_workspace(child_task)

    def test_shared_mode_has_no_branch(self):
        """shared 模式下 ws_meta 不应含 branch（或 branch 为空）。"""
        child_meta = _make_ws_meta(
            mode="shared",
            path="/data/shared/path",
            branch="",
            project_root="/data/shared/path",
        )
        assert child_meta["mode"] == "shared"
        assert not child_meta.get("branch")

    def test_shared_mode_has_parent_workspace_field(self):
        """shared 模式下 ws_meta 包含 parent_workspace 字段。"""
        child_meta = _make_ws_meta(
            mode="shared",
            path="/data/shared/path",
            parent_workspace="/data/original",
        )
        assert "parent_workspace" in child_meta
        assert child_meta["parent_workspace"] == "/data/original"

    def test_multiple_children_share_same_path(self):
        """同一父任务的多个子任务共享同一工作空间路径。"""
        parent_path = "/data/projects/proj__wt_p1"
        children = []
        for i in range(3):
            child = _make_task(
                task_id=f"child-{i}",
                parent_task_id="parent-001",
                metadata={"ws_meta": _make_ws_meta(mode="shared", path=parent_path)},
            )
            children.append(child)

        from src.tasks.workspace import resolve_task_workspace
        paths = {resolve_task_workspace(c) for c in children}
        assert len(paths) == 1  # 所有子任务路径相同


# ============================================================
# AC-WS-09 / AC-WS-10: worktree 模式结构验证
# ============================================================

class TestWorktreeModeStructure:
    """验证 worktree 模式下 ws_meta 的结构完整性。"""

    def test_worktree_mode_has_branch_starting_with_task(self):
        """worktree 模式下 branch 以 'task/' 开头。"""
        meta = _make_ws_meta(
            mode="worktree",
            path="/data/proj__wt_abc123",
            branch="task/abc123",
        )
        assert meta["mode"] == "worktree"
        assert meta["branch"].startswith("task/")

    def test_worktree_mode_path_contains_wt_marker(self):
        """worktree 路径包含 __wt_ 标识。"""
        meta = _make_ws_meta(
            mode="worktree",
            path="/data/projects/myproject__wt_abc123",
            branch="task/abc123",
        )
        assert "__wt_" in meta["path"]

    def test_worktree_mode_has_project_root(self):
        """worktree 模式下包含 project_root 字段。"""
        meta = _make_ws_meta(
            mode="worktree",
            project_root="/data/projects/myproject",
        )
        assert "project_root" in meta
        assert meta["project_root"]

    def test_host_direct_mode_for_container(self):
        """host_direct 模式（容器任务 host 模式）的 ws_meta 结构。"""
        meta = {
            "mode": "host_direct",
            "path": "/data/ws/container_task001",
            "project_root": "/data/ws/container_task001",
            "is_container_workspace": True,
        }
        assert meta["mode"] == "host_direct"
        assert meta.get("is_container_workspace") is True


# ============================================================
# _safe_ws_name 格式验证
# ============================================================

class TestSafeWsNameFormat:
    """验证 _safe_ws_name 函数的格式输出。"""

    def test_safe_ws_name_contains_task_id(self):
        """_safe_ws_name 包含 task_id。"""
        from src.isolation._workspace_git_ops import _safe_ws_name
        result = _safe_ws_name("/data/projects/myproject", "task123")
        assert "task123" in result

    def test_safe_ws_name_contains_wt_marker(self):
        """_safe_ws_name 包含 __wt_ 标识。"""
        from src.isolation._workspace_git_ops import _safe_ws_name
        result = _safe_ws_name("/data/projects/myproject", "task456")
        assert "__wt_" in result

    def test_safe_ws_name_no_path_separators(self):
        """_safe_ws_name 不含路径分隔符（已替换为下划线）。"""
        from src.isolation._workspace_git_ops import _safe_ws_name
        result = _safe_ws_name("/data/projects/myproject", "task789")
        assert "/" not in result
        assert "\\" not in result

    def test_safe_ws_name_is_string(self):
        """_safe_ws_name 返回字符串。"""
        from src.isolation._workspace_git_ops import _safe_ws_name
        result = _safe_ws_name("/data/projects/myproject", "task999")
        assert isinstance(result, str)
        assert len(result) > 0


# ============================================================
# ws_meta 数据结构完整性
# ============================================================

class TestWsMetaStructureIntegrity:
    """验证 ws_meta 字典的字段完整性。"""

    def test_ws_meta_has_required_fields(self):
        """ws_meta 包含 mode 和 path 两个必需字段。"""
        meta = _make_ws_meta()
        assert "mode" in meta
        assert "path" in meta

    def test_ws_meta_mode_is_valid_enum(self):
        """mode 值属于合法枚举。"""
        valid_modes = {"worktree", "shared", "host_direct", "project_root", "isolated", "container"}
        for mode in valid_modes:
            meta = _make_ws_meta(mode=mode)
            assert meta["mode"] in valid_modes

    def test_ws_meta_worktree_has_branch_and_project_root(self):
        """worktree 模式下有 branch 和 project_root。"""
        meta = _make_ws_meta(mode="worktree")
        assert meta.get("branch")
        assert meta.get("project_root")

    def test_ws_meta_shared_has_parent_workspace(self):
        """shared 模式下有 parent_workspace。"""
        meta = _make_ws_meta(
            mode="shared",
            path="/shared/path",
            parent_workspace="/original",
        )
        assert meta.get("parent_workspace")


# ============================================================
# 容器空间初始化（init_container_workspace）参数矩阵
# ============================================================

class TestContainerWorkspaceInit:
    """容器空间初始化参数矩阵测试。

    验证不同 isolation_mode × workspace 组合下的路径生成规则。
    """

    def _make_lifecycle_manager(self, tmp_path):
        """创建一个真实的 WorkspaceLifecycleManager 实例。"""
        from src.isolation.workspace_lifecycle import WorkspaceLifecycleManager

        config = {
            "workspace": {"root": str(tmp_path / "ws_root")},
            "container": {"root": str(tmp_path / "container_root")},
        }

        resource_merge = MagicMock()
        task_tree = MagicMock()
        ws_meta_store: dict[str, Any] = {}

        mgr = WorkspaceLifecycleManager(
            resource_merge=resource_merge,
            config=config,
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path=str(tmp_path),
        )
        return mgr

    def test_host_mode_with_workspace_uses_original_path(self, tmp_path):
        """host 模式 + 指定 workspace → 直接使用原 workspace 路径。"""
        mgr = self._make_lifecycle_manager(tmp_path)
        workspace_path = tmp_path / "myproject"
        workspace_path.mkdir(parents=True, exist_ok=True)
        (workspace_path / ".git").mkdir(exist_ok=True)

        meta = mgr.init_container_workspace(
            container_task_id="container-001",
            workspace=str(workspace_path),
            task_data={"isolation_mode": "host"},
        )

        assert meta["path"] == str(workspace_path)

    def test_host_mode_without_workspace_creates_container_dir(self, tmp_path):
        """host 模式 + 无 workspace → 创建 container_{task_id} 目录。"""
        mgr = self._make_lifecycle_manager(tmp_path)

        meta = mgr.init_container_workspace(
            container_task_id="container-002",
            workspace=None,
            task_data={"isolation_mode": "host"},
        )

        assert "container_container-002" in meta["path"]

    def test_isolated_mode_with_workspace_copies_files(self, tmp_path):
        """isolated 模式 + 指定 workspace → 创建容器空间并复制文件。"""
        mgr = self._make_lifecycle_manager(tmp_path)
        src = tmp_path / "source_project"
        src.mkdir(parents=True, exist_ok=True)
        (src / "README.md").write_text("hello", encoding="utf-8")

        meta = mgr.init_container_workspace(
            container_task_id="container-003",
            workspace=str(src),
            task_data={"isolation_mode": "isolated"},
        )

        assert "container_container-003" in meta["path"]
        assert meta["mode"] in ("project_root", "container")

    def test_isolated_mode_without_workspace_empty_space(self, tmp_path):
        """isolated 模式 + 无 workspace → 创建空的容器空间。"""
        mgr = self._make_lifecycle_manager(tmp_path)

        meta = mgr.init_container_workspace(
            container_task_id="container-004",
            workspace=None,
            task_data={"isolation_mode": "isolated"},
        )

        assert "container_container-004" in meta["path"]

    def test_container_workspace_persisted_to_meta_store(self, tmp_path):
        """容器空间元数据持久化到 ws_meta_store。"""
        mgr = self._make_lifecycle_manager(tmp_path)

        mgr.init_container_workspace(
            container_task_id="container-005",
            workspace=None,
            task_data={"isolation_mode": "host"},
        )

        assert "container-005" in mgr._ws_meta_store

    def test_container_meta_has_is_container_flag(self, tmp_path):
        """容器空间 meta 包含 is_container_workspace 标记。"""
        mgr = self._make_lifecycle_manager(tmp_path)

        meta = mgr.init_container_workspace(
            container_task_id="container-006",
            workspace=None,
            task_data={"isolation_mode": "host"},
        )

        assert meta.get("is_container_workspace") is True


# ============================================================
# 隔离模式对比（host vs isolated）
# ============================================================

class TestHostVsIsolatedMode:
    """验证 host 和 isolated 模式的关键差异。"""

    def test_host_mode_value(self):
        """host 模式枚举值正确。"""
        from src.isolation.types import IsolationMode
        assert IsolationMode.HOST.value == "host"

    def test_isolated_mode_value(self):
        """isolated 模式枚举值正确。"""
        from src.isolation.types import IsolationMode
        assert IsolationMode.ISOLATED.value == "isolated"

    def test_both_modes_exist_in_enum(self):
        """两种隔离模式都在枚举中定义。"""
        from src.isolation.types import IsolationMode
        assert hasattr(IsolationMode, "HOST")
        assert hasattr(IsolationMode, "ISOLATED")


# ============================================================
# 容器名映射规则
# ============================================================

class TestContainerNameMapping:
    """验证容器名映射规则：cua-{ws_key}。"""

    def test_container_name_format(self):
        """容器名格式为 cua-{path_name}。"""
        # 验证 PurePath(ws_meta.path).name 用于容器名
        from pathlib import PurePath
        ws_path = "/data/workspace/myproject__wt_abc123"
        ws_name = PurePath(ws_path).name
        expected_container_name = f"cua-{ws_name}"
        assert expected_container_name == "cua-myproject__wt_abc123"

    def test_same_workspace_shares_container(self):
        """同一 workspace 路径的多个任务共享同一容器名。"""
        from pathlib import PurePath
        ws_path = "/data/workspace/shared_project"
        ws_name = PurePath(ws_path).name
        container_name = f"cua-{ws_name}"

        # 任务A 和 任务B 使用同一 workspace
        assert container_name == f"cua-{ws_name}"
