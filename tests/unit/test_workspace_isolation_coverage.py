"""工作空间与隔离模块补充测试。

覆盖需求文档中 7 个关键测试缺口（全部用 Mock/单元测试，不依赖 Docker/Git）：
1. ws_meta 数据结构字段完整性
2. resolve_task_workspace 从 ws_meta.path 读取路径的一致性
3. 子任务共享父工作空间（mode=shared, path==父任务path）
4. worktree 路径格式包含 __wt_ 标识
5. 容器任务空间 mode/container/host_direct 的区别
6. Workspace 模型 FileTreeNode 递归结构与时间戳格式
7. _safe_ws_name() 格式

[来源: docs/requirements/各模块需求文档/10_工作空间与隔离模块需求文档.md]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ============================================================
# 辅助：轻量 FakeTask
# ============================================================

@dataclass
class _FakeTask:
    """轻量任务模型，模拟 TaskModel 的关键属性。"""

    id: str = "task001"
    parent_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ============================================================
# 缺口 1: ws_meta 数据结构 — mode/path/branch/project_root 字段完整性
# ============================================================


class TestWsMetaStructure:
    """验证 ws_meta 数据结构的字段完整性。

    需求: ws_meta 必须包含 mode、path、branch（仅 worktree）、
    project_root（仅 worktree）等字段。
    [来源: 需求文档 5.7 ws_meta 数据结构]
    """

    @pytest.mark.parametrize("mode", ["worktree", "shared", "plain", "project_root", "host_direct", "container"])
    def test_ws_meta_always_has_mode_and_path(self, mode: str):
        """每种 mode 的 ws_meta 都必须包含 mode 和 path 字段。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_meta_store: dict[str, dict] = {
            "test-task": {"mode": mode, "path": f"/tmp/ws/{mode}_test"}
        }

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=MagicMock(),
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        # on_task_start 中 existing 分支会检查 mode
        meta = ws_meta_store["test-task"]
        assert "mode" in meta, f"ws_meta 缺少 mode 字段 (mode={mode})"
        assert "path" in meta, f"ws_meta 缺少 path 字段 (mode={mode})"
        assert meta["mode"] == mode

    def test_worktree_mode_has_branch_and_project_root(self):
        """worktree 模式的 ws_meta 应包含 branch 和 project_root。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_meta_store: dict[str, dict] = {
            "wt-task": {
                "mode": "worktree",
                "path": "/tmp/ws/myproject__wt_abc12345",
                "branch": "task/abc12345",
                "project_root": "/tmp/myproject",
            }
        }

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=MagicMock(),
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        meta = ws_meta_store["wt-task"]
        assert meta.get("branch", "").startswith("task/"), (
            "worktree 模式 branch 必须以 'task/' 开头"
        )
        assert "project_root" in meta, "worktree 模式必须包含 project_root"

    def test_shared_mode_has_parent_workspace(self):
        """shared 模式应包含 parent_workspace 字段。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        parent_ws = "/tmp/ws/parent"
        parent_meta = {"mode": "worktree", "path": parent_ws, "branch": "task/parent"}
        ws_meta_store: dict[str, dict] = {"parent-1": parent_meta}

        mock_task = MagicMock()
        mock_task.parent_task_id = "parent-1"
        task_tree = MagicMock()
        task_tree.get_task.return_value = mock_task

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        meta = lifecycle._start_subtask("child-1", "/original/workspace", {"is_root": False})

        assert meta["mode"] == "shared"
        assert "parent_workspace" in meta, "shared 模式必须包含 parent_workspace 字段"
        assert meta["path"] == parent_ws


# ============================================================
# 缺口 2: resolve_task_workspace 从 ws_meta.path 读取路径的一致性
# ============================================================


class TestResolveTaskWorkspaceConsistency:
    """验证 resolve_task_workspace 始终从 task.metadata['ws_meta']['path'] 读取。

    需求: ws_meta.path 为运行时唯一可信来源。
    [来源: 需求文档 5.1 参数传递全链路, AC-WS-07]
    """

    def test_path_directly_returned_from_ws_meta(self):
        """resolve_task_workspace 应直接返回 ws_meta.path 的值。"""
        from tasks.workspace import resolve_task_workspace

        ws_path = "/tmp/workspaces/project__wt_abc12345"
        task = _FakeTask(
            id="task-consist-1",
            metadata={"ws_meta": {"mode": "worktree", "path": ws_path}},
        )

        result = resolve_task_workspace(task)

        assert result is not None
        assert result == ws_path

    def test_path_from_ws_meta_matches_stored_value(self):
        """多次调用 resolve_task_workspace 应返回一致的值。"""
        from tasks.workspace import resolve_task_workspace

        ws_path = "/tmp/workspaces/consistent_check"
        task = _FakeTask(
            id="task-consist-2",
            metadata={"ws_meta": {"mode": "plain", "path": ws_path}},
        )

        result1 = resolve_task_workspace(task)
        result2 = resolve_task_workspace(task)

        assert result1 == result2 == ws_path

    def test_ignores_other_metadata_keys(self):
        """resolve_task_workspace 只看 ws_meta.path，不受其他 metadata 键影响。"""
        from tasks.workspace import resolve_task_workspace

        ws_path = "/tmp/workspaces/only_ws_meta_matters"
        task = _FakeTask(
            id="task-consist-3",
            metadata={
                "ws_meta": {"mode": "plain", "path": ws_path},
                "workspace": "/tmp/some_other_path",  # 应被忽略
                "isolation_level": "host",
            },
        )

        result = resolve_task_workspace(task)

        assert result is not None
        assert result == ws_path, "应返回 ws_meta.path 而非 metadata 中其他 workspace 字段"

    def test_none_metadata_returns_none(self):
        """metadata 为 None 时返回 None。"""
        from tasks.workspace import resolve_task_workspace

        task = MagicMock()
        task.id = "task-consist-4"
        task.metadata = None
        task.parent_task_id = None

        result = resolve_task_workspace(task)
        assert result is None

    def test_non_dict_ws_meta_returns_none(self):
        """ws_meta 不是 dict 时返回 None。"""
        from tasks.workspace import resolve_task_workspace

        task = _FakeTask(
            id="task-consist-5",
            metadata={"ws_meta": "not_a_dict"},
        )

        result = resolve_task_workspace(task)
        assert result is None


# ============================================================
# 缺口 3: 子任务共享父工作空间 — mode=shared, path==父任务path
# ============================================================


class TestSubtaskSharedParentWorkspace:
    """验证子任务统一共享父任务的工作空间。

    需求: 子任务不创建独立目录，ws_meta.mode='shared',
    ws_meta.path == 父任务 ws_meta.path。
    [来源: 需求文档 5.4 子任务 workspace 继承规则, AC-WS-08]
    """

    def test_subtask_mode_is_shared(self):
        """子任务的 ws_meta.mode 必须为 'shared'。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        parent_ws = "/tmp/workspaces/parent_a"
        ws_meta_store = {
            "parent-a": {"mode": "worktree", "path": parent_ws, "branch": "task/pa"}
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = "parent-a"
        task_tree = MagicMock()
        task_tree.get_task.return_value = mock_task

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        meta = lifecycle._start_subtask("child-a", "", {"is_root": False})

        assert meta["mode"] == "shared", f"子任务 mode 应为 shared，实际: {meta['mode']}"

    def test_subtask_path_equals_parent_path(self):
        """子任务 path 必须等于父任务 path。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        parent_ws = "/tmp/workspaces/project__wt_parent01"
        ws_meta_store = {
            "parent-b": {
                "mode": "worktree",
                "path": parent_ws,
                "branch": "task/parent01",
            }
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = "parent-b"
        task_tree = MagicMock()
        task_tree.get_task.return_value = mock_task

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        parent_meta = ws_meta_store["parent-b"]
        child_meta = lifecycle._start_subtask("child-b", "", {"is_root": False})

        assert child_meta["path"] == parent_meta["path"], (
            f"子任务 path 应等于父任务 path。子: {child_meta['path']}, 父: {parent_meta['path']}"
        )

    def test_subtask_inherits_parent_project_root(self):
        """子任务应继承父任务的 project_root。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        parent_ws = "/tmp/workspaces/project__wt_parent02"
        parent_root = "/tmp/project_root_x"
        ws_meta_store = {
            "parent-c": {
                "mode": "worktree",
                "path": parent_ws,
                "project_root": parent_root,
            }
        }

        mock_task = MagicMock()
        mock_task.parent_task_id = "parent-c"
        task_tree = MagicMock()
        task_tree.get_task.return_value = mock_task

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        child_meta = lifecycle._start_subtask("child-c", "", {"is_root": False})

        assert child_meta.get("project_root") == parent_root, (
            "子任务应继承父任务的 project_root"
        )

    def test_subtask_fallback_when_no_parent_meta(self):
        """父任务无 ws_meta 时，子任务使用传入的 workspace 作为 path。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_meta_store: dict[str, dict] = {}

        # 子任务有 parent_task_id，但父任务无 ws_meta
        mock_child = MagicMock()
        mock_child.parent_task_id = "parent-d"
        mock_parent = MagicMock()
        mock_parent.metadata = {}  # 无 ws_meta

        task_tree = MagicMock()
        # 第一次 get_task 返回子任务，第二次返回父任务
        task_tree.get_task.side_effect = [mock_child, mock_parent]

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={},
            task_tree=task_tree,
            ws_meta_store=ws_meta_store,
            base_path="/tmp",
        )

        fallback_ws = "/tmp/fallback_workspace"
        meta = lifecycle._start_subtask("child-d", fallback_ws, {"is_root": False})

        assert meta["mode"] == "shared"
        assert meta["path"] == fallback_ws, (
            "父任务无 ws_meta 时，应使用传入的 workspace 作为 fallback path"
        )


# ============================================================
# 缺口 4: worktree 路径格式包含 __wt_ 标识
# ============================================================


class TestWorktreePathFormat:
    """验证 worktree 路径格式包含 __wt_ 标识。

    需求: worktree 路径格式为 {ws_root}/{project_name}__wt_{task_id}，
    必须包含 __wt_ 标识用于区分 worktree 和普通目录。
    [来源: 需求文档 5.2 根任务 workspace 参数组合矩阵]
    """

    def test_worktree_path_contains_wt_marker(self):
        """worktree 模式的 path 必须包含 '__wt_'。"""
        ws_meta = {
            "mode": "worktree",
            "path": "/tmp/ws/myproject__wt_abc12345",
            "branch": "task/abc12345",
        }
        assert "__wt_" in ws_meta["path"], "worktree 路径必须包含 '__wt_' 标识"

    def test_shared_path_may_not_contain_wt(self):
        """shared 模式的 path 是父路径，不强制包含 __wt_。"""
        ws_meta = {
            "mode": "shared",
            "path": "/tmp/ws/container_abc",
        }
        assert "__wt_" not in ws_meta["path"], (
            "shared 路径是父路径，不应在自身路径中包含 __wt_"
        )

    def test_branch_format_starts_with_task(self):
        """worktree 模式的 branch 必须以 'task/' 开头。"""
        ws_meta = {
            "mode": "worktree",
            "path": "/tmp/ws/myproject__wt_abc12345",
            "branch": "task/abc12345",
        }
        assert ws_meta["branch"].startswith("task/"), (
            "worktree 分支必须以 'task/' 开头"
        )

    def test_safe_ws_name_in_path(self):
        """_safe_ws_name 生成的路径应包含 __wt_ 标识和 task_id 前 8 位。"""
        from isolation._workspace_git_ops import _safe_ws_name

        task_id = "abcdef1234567890"
        ws_name = _safe_ws_name("my_project", task_id)
        assert "__wt_" in ws_name
        assert task_id[:8] in ws_name


# ============================================================
# 缺口 5: 容器任务空间 — mode/container/host_direct 的区别
# ============================================================


class TestContainerWorkspaceModes:
    """验证容器任务的不同模式：host_direct vs container vs project_root。

    需求: 容器任务根据隔离模式产生不同的 ws_meta.mode。
    [来源: 需求文档 5.3 容器任务的 workspace → 容器空间路径]
    """

    def test_init_container_workspace_non_host_mode(self, tmp_path):
        """非 host 模式 + 指定 workspace → 创建 ws_root/container_{task_id}。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        source = tmp_path / "src_project"
        source.mkdir()
        (source / "file.py").write_text("# test", encoding="utf-8")

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={"workspace": {"root": str(ws_root)}},
            task_tree=MagicMock(),
            ws_meta_store={},
            base_path=str(tmp_path),
        )

        meta = lifecycle.init_container_workspace(
            container_task_id="container_001",
            workspace=str(source),
            task_data={"isolation_mode": ""},
        )

        assert meta["mode"] == "project_root"
        assert meta.get("is_container_workspace") is True
        assert Path(meta["path"]).exists()

    def test_init_container_workspace_host_mode(self, tmp_path):
        """host 模式 + 指定 workspace → 直接使用原空间路径。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        source = tmp_path / "host_project"
        source.mkdir()
        (source / "app.py").write_text("# app", encoding="utf-8")

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={"workspace": {"root": str(ws_root)}},
            task_tree=MagicMock(),
            ws_meta_store={},
            base_path=str(tmp_path),
        )

        meta = lifecycle.init_container_workspace(
            container_task_id="container_002",
            workspace=str(source),
            task_data={"isolation_mode": "host"},
        )

        assert meta["mode"] == "project_root"
        assert meta["path"] == str(source), "host 模式应直接复用原空间"

    def test_init_container_workspace_no_workspace(self, tmp_path):
        """无 workspace → 创建空容器空间。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={"workspace": {"root": str(ws_root)}},
            task_tree=MagicMock(),
            ws_meta_store={},
            base_path=str(tmp_path),
        )

        meta = lifecycle.init_container_workspace(
            container_task_id="container_003",
            workspace=None,
            task_data={"isolation_mode": ""},
        )

        assert meta["mode"] == "project_root"
        assert Path(meta["path"]).exists()
        # 路径应包含 container_ 前缀
        assert "container_container_003" in meta["path"] or "container_003" in meta["path"]

    def test_init_container_workspace_project_root_field(self, tmp_path):
        """容器任务的 ws_meta 应包含 project_root 等于其 path。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir()

        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MagicMock(),
            config={"workspace": {"root": str(ws_root)}},
            task_tree=MagicMock(),
            ws_meta_store={},
            base_path=str(tmp_path),
        )

        meta = lifecycle.init_container_workspace(
            container_task_id="container_004",
            workspace=None,
            task_data={"isolation_mode": ""},
        )

        assert meta.get("project_root") == meta["path"], (
            "容器空间的 project_root 应等于其 path"
        )
        assert meta.get("branch") == "main", "容器空间的 branch 应为 main"

    def test_resolve_container_workspace_path_isolated(self):
        """resolve_container_workspace_path 非 host 模式返回 ws_root/container_{task_id}。"""
        from isolation.workspace import resolve_container_workspace_path

        result = resolve_container_workspace_path(
            workspace="/some/path",
            task_id="task_abc",
            isolation_mode="isolated",
        )
        assert "container_task_abc" in result, (
            "非 host 模式应返回 ws_root/container_{task_id}"
        )

    def test_resolve_container_workspace_path_host_with_workspace(self):
        """resolve_container_workspace_path host 模式 + workspace 返回原值。"""
        from isolation.workspace import resolve_container_workspace_path

        original = "/tmp/my_project"
        result = resolve_container_workspace_path(
            workspace=original,
            task_id="task_def",
            isolation_mode="host",
        )
        assert result == original, "host 模式 + workspace 应返回原 workspace 值"

    def test_resolve_container_workspace_path_host_no_workspace(self):
        """resolve_container_workspace_path host 模式 + 无 workspace 返回 ws_root/container_{task_id}。"""
        from isolation.workspace import resolve_container_workspace_path

        result = resolve_container_workspace_path(
            workspace=None,
            task_id="task_ghi",
            isolation_mode="host",
        )
        assert "container_task_ghi" in result


# ============================================================
# 缺口 6: Workspace 模型 — FileTreeNode 递归结构与时间戳格式
# ============================================================


class TestWorkspaceModel:
    """验证 Workspace/FileTreeNode 数据模型。

    需求: FileTreeNode 支持递归子节点，Workspace 时间戳为 ISO 8601 格式。
    [来源: 需求文档 2.1 Workspace 模型, 2.2 FileTreeNode 模型]
    """

    def test_file_tree_node_file_type(self):
        """FileTreeNode 类型为 file 时不包含 children。"""
        from workspace.models import FileTreeNode

        node = FileTreeNode(name="main.py", type="file", path="/project/main.py")
        d = node.to_dict()

        assert d["name"] == "main.py"
        assert d["type"] == "file"
        assert d["path"] == "/project/main.py"
        assert "children" not in d, "file 类型不应有序列化 children"

    def test_file_tree_node_directory_with_children(self):
        """FileTreeNode directory 类型可包含子节点。"""
        from workspace.models import FileTreeNode

        child1 = FileTreeNode(name="a.py", type="file", path="/project/src/a.py")
        child2 = FileTreeNode(name="b.py", type="file", path="/project/src/b.py")
        parent = FileTreeNode(
            name="src", type="directory", path="/project/src", children=[child1, child2]
        )
        d = parent.to_dict()

        assert d["type"] == "directory"
        assert len(d["children"]) == 2
        assert d["children"][0]["name"] == "a.py"
        assert d["children"][1]["name"] == "b.py"

    def test_file_tree_node_deep_recursion(self):
        """FileTreeNode 支持深层递归嵌套（≥3 层）。"""
        from workspace.models import FileTreeNode

        leaf = FileTreeNode(name="leaf.txt", type="file", path="/a/b/c/leaf.txt")
        level2 = FileTreeNode(name="c", type="directory", path="/a/b/c", children=[leaf])
        level1 = FileTreeNode(name="b", type="directory", path="/a/b", children=[level2])
        root = FileTreeNode(name="a", type="directory", path="/a", children=[level1])

        d = root.to_dict()
        assert d["children"][0]["children"][0]["children"][0]["name"] == "leaf.txt"

    def test_file_tree_node_round_trip(self):
        """FileTreeNode to_dict / from_dict 往返一致性。"""
        from workspace.models import FileTreeNode

        original = FileTreeNode(
            name="root",
            type="directory",
            path="/root",
            artifact_id="art-001",
            children=[
                FileTreeNode(name="file1.py", type="file", path="/root/file1.py"),
            ],
            metadata={"key": "value"},
        )

        d = original.to_dict()
        restored = FileTreeNode.from_dict(d)

        assert restored.name == original.name
        assert restored.type == original.type
        assert restored.path == original.path
        assert restored.artifact_id == original.artifact_id
        assert len(restored.children) == 1
        assert restored.children[0].name == "file1.py"
        assert restored.metadata == {"key": "value"}

    def test_file_tree_node_with_artifact_id(self):
        """FileTreeNode 支持 artifact_id 字段。"""
        from workspace.models import FileTreeNode

        node = FileTreeNode(
            name="output.pdf",
            type="file",
            path="/project/output.pdf",
            artifact_id="artifact-uuid-001",
        )
        d = node.to_dict()

        assert d["artifact_id"] == "artifact-uuid-001"

    def test_workspace_has_all_fields(self):
        """Workspace 模型必须包含所有必需字段。"""
        from workspace.models import Workspace

        ws = Workspace(
            container_task_id="container-001",
            session_id="session-001",
            title="Test Workspace",
            description="Test Description",
        )

        assert ws.id  # 自动生成
        assert ws.container_task_id == "container-001"
        assert ws.session_id == "session-001"
        assert ws.title == "Test Workspace"
        assert ws.description == "Test Description"
        assert ws.file_tree == []  # 默认空列表
        assert ws.created_at  # 自动生成
        assert ws.updated_at  # 自动生成

    def test_workspace_timestamps_iso8601_format(self):
        """Workspace 的 created_at / updated_at 必须为 ISO 8601 格式。"""
        from workspace.models import Workspace

        ws = Workspace()
        # ISO 8601 正则：日期 + 时间 + 时区信息
        iso_pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$"
        )

        assert iso_pattern.match(ws.created_at), (
            f"created_at 不是 ISO 8601 格式: {ws.created_at}"
        )
        assert iso_pattern.match(ws.updated_at), (
            f"updated_at 不是 ISO 8601 格式: {ws.updated_at}"
        )

    def test_workspace_timestamps_utc(self):
        """Workspace 时间戳应为 UTC 时间。"""
        from workspace.models import _now_iso

        ts = _now_iso()
        # datetime.now(UTC).isoformat() 在 Python 3.11+ 输出 +00:00
        assert "+00:00" in ts or ts.endswith("Z"), (
            f"时间戳应包含 UTC 标识 (+00:00 或 Z): {ts}"
        )

    def test_workspace_round_trip_with_file_tree(self):
        """Workspace to_dict / from_dict 往返一致性（含 FileTreeNode）。"""
        from workspace.models import FileTreeNode, Workspace

        ws = Workspace(
            container_task_id="container-rt",
            session_id="session-rt",
            title="Round Trip",
            file_tree=[
                FileTreeNode(
                    name="src",
                    type="directory",
                    path="/project/src",
                    children=[
                        FileTreeNode(name="main.py", type="file", path="/project/src/main.py"),
                    ],
                )
            ],
        )

        d = ws.to_dict()
        restored = Workspace.from_dict(d)

        assert restored.title == "Round Trip"
        assert restored.container_task_id == "container-rt"
        assert len(restored.file_tree) == 1
        assert restored.file_tree[0].name == "src"
        assert len(restored.file_tree[0].children) == 1
        assert restored.file_tree[0].children[0].name == "main.py"

    def test_workspace_id_format(self):
        """Workspace id 应为 12 位 hex 字符串（UUID hex 前 12 位）。"""
        from workspace.models import Workspace

        ws1 = Workspace()
        ws2 = Workspace()

        assert len(ws1.id) == 12, f"id 长度应为 12，实际: {len(ws1.id)}"
        assert re.match(r"^[0-9a-f]{12}$", ws1.id), f"id 应为 12 位 hex: {ws1.id}"
        assert ws1.id != ws2.id, "每个 Workspace 的 id 应唯一"


# ============================================================
# 缺口 7: _safe_ws_name() 格式
# ============================================================


class TestSafeWsName:
    """验证 _safe_ws_name() 生成格式。

    需求: 格式为 {project_name}__wt_{task_id[:8]}，项目名截断到 name_limit 字符。
    [来源: 需求文档 5.2, src/isolation/_workspace_git_ops.py L33-43]
    """

    def test_basic_format(self):
        """基本格式: {project_name}__wt_{task_id[:8]}。"""
        from isolation._workspace_git_ops import _safe_ws_name

        name = _safe_ws_name("my_project", "abcdef1234567890")
        assert "__wt_" in name
        assert name.startswith("my_project__wt_")
        # task_id 前 8 位
        assert "abcdef12" in name

    def test_task_id_truncated_to_8(self):
        """task_id 被截断到前 8 位。"""
        from isolation._workspace_git_ops import _safe_ws_name

        task_id = "0123456789abcdef"
        name = _safe_ws_name("proj", task_id)
        # __wt_ 后面应正好是 8 个字符
        suffix = name.split("__wt_")[1]
        assert suffix == task_id[:8], f"task_id 应截断到前 8 位，实际后缀: {suffix}"

    def test_project_name_truncation(self):
        """长项目名被截断到 name_limit（默认 15）字符。"""
        from isolation._workspace_git_ops import _safe_ws_name

        long_name = "a" * 50
        name = _safe_ws_name(long_name, "task1234")
        prefix = name.split("__wt_")[0]
        assert len(prefix) <= 15, f"项目名应截断到 ≤15 字符，实际: {len(prefix)}"

    def test_custom_name_limit(self):
        """自定义 name_limit 截断。"""
        from isolation._workspace_git_ops import _safe_ws_name

        name = _safe_ws_name("abcdefghijklmnop", "task1234", name_limit=5)
        prefix = name.split("__wt_")[0]
        assert len(prefix) <= 5, f"自定义截断到 5 字符，实际 prefix: {prefix}"

    def test_special_chars_replaced(self):
        """特殊字符被替换为下划线。"""
        from isolation._workspace_git_ops import _safe_ws_name

        name = _safe_ws_name('my<project>test', "task1234")
        assert "<" not in name
        assert ">" not in name
        assert ":" not in name
        assert '"' not in name
        assert "/" not in name
        assert "\\" not in name
        assert "|" not in name
        assert "?" not in name
        assert "*" not in name

    def test_spaces_replaced(self):
        """空格被替换为下划线。"""
        from isolation._workspace_git_ops import _safe_ws_name

        name = _safe_ws_name("my project name", "task1234")
        assert " " not in name, "空格应被替换为下划线"

    def test_consecutive_underscores_collapsed(self):
        """连续下划线被合并为一个。"""
        from isolation._workspace_git_ops import _safe_ws_name

        name = _safe_ws_name("a___b", "task1234")
        prefix = name.split("__wt_")[0]
        assert "___" not in prefix, "连续下划线应被合并"

    def test_empty_project_name_defaults_to_ws(self):
        """空项目名或纯特殊字符时使用默认值 'ws'。"""
        from isolation._workspace_git_ops import _safe_ws_name

        name = _safe_ws_name("", "task1234")
        assert name.startswith("ws__wt_"), "空项目名应默认为 'ws'"

        name2 = _safe_ws_name("///", "task1234")
        assert name2.startswith("ws__wt_"), "纯特殊字符应默认为 'ws'"

    def test_stripped_of_leading_trailing_dots(self):
        """项目名首尾的点号被剥离。"""
        from isolation._workspace_git_ops import _safe_ws_name

        name = _safe_ws_name("..project..", "task1234")
        prefix = name.split("__wt_")[0]
        assert not prefix.startswith("."), "项目名不应以点号开头"
        assert not prefix.endswith("."), "项目名不应以点号结尾"

    def test_result_always_contains_wt_separator(self):
        """_safe_ws_name 的结果始终包含 '__wt_' 分隔符。"""
        from isolation._workspace_git_ops import _safe_ws_name

        test_cases = [
            ("normal", "task0001"),
            ("", "task0002"),
            ("a" * 100, "task0003"),
            ("<>|?*", "task0004"),
        ]

        for project_name, task_id in test_cases:
            name = _safe_ws_name(project_name, task_id)
            assert "__wt_" in name, (
                f"_safe_ws_name 始终应包含 '__wt_' 分隔符。输入: ({project_name!r}, {task_id!r})"
            )
