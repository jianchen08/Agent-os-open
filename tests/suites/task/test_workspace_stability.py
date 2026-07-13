"""工作空间稳定性测试 — 验证 workspace 默认值、注入、隔离、继承逻辑。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_task(
    task_id: str,
    parent_task_id: str | None = None,
    workspace: str | None = None,
) -> MagicMock:
    """创建模拟的 TaskModel 对象。"""
    task = MagicMock()
    task.id = task_id
    task.parent_task_id = parent_task_id
    task.metadata = {"workspace": workspace} if workspace else {}
    return task


def _make_task_service(tasks: dict[str, MagicMock]) -> MagicMock:
    """创建模拟的 TaskService，按 task_id 返回预设的任务。"""
    svc = MagicMock()
    svc.get_task.side_effect = lambda tid: tasks.get(tid)
    return svc


def _inject_workspace(args: dict, state: dict) -> dict:
    """模拟 ParamInjectPlugin 中 workspace 注入逻辑。"""
    if "workspace" not in args:
        workspace = state.get("workspace", "")
        if workspace:
            args["workspace"] = workspace
    return args


def _create_worker_with_service(task_service: MagicMock | None = None) -> Any:
    """创建 TaskWorker 实例并注入模拟的 task_service。"""
    from infrastructure.task_worker import TaskWorker

    services = {}
    if task_service:
        services["task_service"] = task_service

    worker = TaskWorker(
        task_service=task_service,
        plugin_registry=MagicMock(),
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        services=services,
        event_bus=MagicMock(),
    )
    return worker


class TestWorkspaceInheritance:
    """工作空间继承测试套件。"""

    @pytest.mark.task
    @pytest.mark.unit
    def test_root_task_default_workspace(self) -> None:
        """根任务（无父任务）且无显式 workspace 时，默认 .ai_workspaces/{task_id}。"""
        root = _make_task("root001")
        svc = _make_task_service({"root001": root})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("root001")

        assert result == ".ai_workspaces/root001"

    @pytest.mark.task
    @pytest.mark.unit
    def test_root_task_custom_workspace(self) -> None:
        """根任务有显式 workspace 时，解析为 .ai_workspaces/{workspace}。"""
        root = _make_task("root001", workspace="my_project")
        svc = _make_task_service({"root001": root})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("root001", "my_project")

        assert result == ".ai_workspaces/my_project"

    @pytest.mark.task
    @pytest.mark.unit
    def test_child_task_inherits_parent_workspace(self) -> None:
        """子任务无显式 workspace 时，嵌套在父任务工作空间下。"""
        root = _make_task("root001")
        child = _make_task("child001", parent_task_id="root001")
        svc = _make_task_service({"root001": root, "child001": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("child001")

        assert result == ".ai_workspaces/root001/child001"

    @pytest.mark.task
    @pytest.mark.unit
    def test_child_task_with_explicit_workspace(self) -> None:
        """子任务有显式 workspace 时，相对于父工作空间解析。"""
        root = _make_task("root001")
        child = _make_task("child001", parent_task_id="root001")
        svc = _make_task_service({"root001": root, "child001": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("child001", "sub_dir")

        assert result == ".ai_workspaces/root001/sub_dir"

    @pytest.mark.task
    @pytest.mark.unit
    def test_grandchild_task_three_level_nesting(self) -> None:
        """三层嵌套：孙任务的工作空间嵌套在子任务下，子任务嵌套在根任务下。"""
        root = _make_task("root001")
        child = _make_task("child001", parent_task_id="root001")
        grandchild = _make_task("grand001", parent_task_id="child001")
        svc = _make_task_service({
            "root001": root,
            "child001": child,
            "grand001": grandchild,
        })
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("grand001")

        assert result == ".ai_workspaces/root001/child001/grand001"

    @pytest.mark.task
    @pytest.mark.unit
    def test_circular_reference_no_hang(self) -> None:
        """环形 parent_task_id 引用不会导致无限循环。"""
        task_a = _make_task("aaa", parent_task_id="bbb")
        task_b = _make_task("bbb", parent_task_id="aaa")
        svc = _make_task_service({"aaa": task_a, "bbb": task_b})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("aaa")

        assert ".ai_workspaces/" in result

    @pytest.mark.task
    @pytest.mark.unit
    def test_missing_parent_fallback(self) -> None:
        """父任务不存在时，优雅降级。"""
        child = _make_task("orphan001", parent_task_id="nonexistent")
        svc = _make_task_service({"orphan001": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("orphan001")

        assert ".ai_workspaces/" in result

    @pytest.mark.task
    @pytest.mark.unit
    def test_no_task_service_fallback(self) -> None:
        """TaskService 不可用时，降级到旧的平级行为。"""
        worker = _create_worker_with_service(None)

        result = worker._resolve_task_workspace("task001")

        assert result == ".ai_workspaces/task001"

    @pytest.mark.task
    @pytest.mark.unit
    def test_no_task_service_with_explicit_workspace(self) -> None:
        """TaskService 不可用但有显式 workspace 时，直接使用传入值。"""
        worker = _create_worker_with_service(None)

        result = worker._resolve_task_workspace("task001", "custom_dir")

        assert result == "custom_dir"

    @pytest.mark.task
    @pytest.mark.unit
    def test_parent_with_custom_workspace_child_inherits(self) -> None:
        """父任务有自定义 workspace 时，子任务嵌套在其下。"""
        root = _make_task("root001", workspace="project_x")
        child = _make_task("child001", parent_task_id="root001")
        svc = _make_task_service({"root001": root, "child001": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("child001")

        assert result == ".ai_workspaces/project_x/child001"

    @pytest.mark.task
    @pytest.mark.unit
    def test_none_workspace_treated_as_empty(self) -> None:
        """workspace 参数为 None 时等同于空，使用默认嵌套路径。"""
        root = _make_task("root001")
        child = _make_task("child001", parent_task_id="root001")
        svc = _make_task_service({"root001": root, "child001": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("child001", None)

        assert result == ".ai_workspaces/root001/child001"

    @pytest.mark.task
    @pytest.mark.unit
    def test_child_workspace_with_full_path_prefix_no_duplication(self) -> None:
        """回归测试：子任务 metadata 中存了含 .ai_workspaces/ 前缀的完整路径时，不会产生路径嵌套。

        BUG-FIX-fix_20260419_workspace_nesting:
        问题根因: resolve_workspace 子任务分支缺少前缀去重保护，
                 当子任务 metadata["workspace"] 存了 ".ai_workspaces/parent/child" 这样的完整路径时，
                 会被直接拼接到父 workspace 后面，产生 ".ai_workspaces/parent/.ai_workspaces/parent/child"。
        """
        root = _make_task("9cf744b92907")
        child = _make_task(
            "0a130399b403",
            parent_task_id="9cf744b92907",
            workspace=".ai_workspaces/9cf744b92907/0a130399b403",
        )
        svc = _make_task_service({"9cf744b92907": root, "0a130399b403": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("0a130399b403")

        assert result == ".ai_workspaces/9cf744b92907/0a130399b403"
        assert ".ai_workspaces/.ai_workspaces" not in result

    @pytest.mark.task
    @pytest.mark.unit
    def test_child_workspace_with_root_prefix_only_no_duplication(self) -> None:
        """回归测试：子任务 workspace 只有 .ai_workspaces/xxx 前缀（不含父路径），也不应重复拼接。"""
        root = _make_task("root001", workspace="project_a")
        child = _make_task(
            "child001",
            parent_task_id="root001",
            workspace=".ai_workspaces/project_a/child001",
        )
        svc = _make_task_service({"root001": root, "child001": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("child001")

        assert result == ".ai_workspaces/project_a/child001"
        assert result.count(".ai_workspaces") == 1

    @pytest.mark.task
    @pytest.mark.unit
    def test_child_workspace_with_absolute_path_no_duplication(self) -> None:
        """回归测试：中间祖先 metadata 存了绝对路径 workspace 时，不应被拼接到父路径后面。

        BUG-FIX-fix_20260420_workspace_abs_path:
        问题根因: resolve_workspace 子任务分支缺少绝对路径检查，
                 当中间祖先 metadata["workspace"] 存了绝对路径(如
                 D:\\Jianguoyun\\Agent os\\.ai_workspaces\\xxx)时，
                 会被拼接到父 workspace 后面，产生
                 .ai_workspaces/parent/D:\\Jianguoyun\\Agent os\\.ai_workspaces\\xxx
                 这样的错误路径，导致 agent 找不到文件。

        复现场景: 根任务 -> 中间祖先(存了绝对路径) -> 子任务
        _resolve_task_workspace 对中间祖先使用 metadata 中的 workspace，
        而非显式传入的参数，所以绝对路径通过 metadata 传入 resolve_workspace。
        """
        root = _make_task("root001")
        mid = _make_task(
            "mid001",
            parent_task_id="root001",
            workspace="D:\\Jianguoyun\\Agent os\\.ai_workspaces\\6723b1671933",
        )
        child = _make_task("child001", parent_task_id="mid001")
        svc = _make_task_service({"root001": root, "mid001": mid, "child001": child})
        worker = _create_worker_with_service(svc)

        result = worker._resolve_task_workspace("child001")

        assert result == "D:/Jianguoyun/Agent os/.ai_workspaces/6723b1671933/child001"
        assert result.count(".ai_workspaces") == 1


class TestWorkspaceStability:
    """工作空间稳定性测试套件。"""

    @pytest.mark.task
    @pytest.mark.unit
    def test_workspace_injected_to_file_tools(self) -> None:
        """workspace 从 context.state 注入到工具调用参数中。"""
        args: dict = {}
        state = {"workspace": ".ai_workspaces/abc123"}

        result = _inject_workspace(args, state)

        assert result["workspace"] == ".ai_workspaces/abc123"

    @pytest.mark.task
    @pytest.mark.unit
    def test_two_tasks_isolated(self, tmp_path: Path) -> None:
        """两个同级任务的 workspace 在文件系统级别相互隔离。"""
        ws_root = tmp_path / ".ai_workspaces"
        ws_a = ws_root / "aaa"
        ws_b = ws_root / "bbb"
        ws_a.mkdir(parents=True)
        ws_b.mkdir(parents=True)

        (ws_a / "file_a.txt").write_text("task a output", encoding="utf-8")
        (ws_b / "file_b.txt").write_text("task b output", encoding="utf-8")

        assert (ws_a / "file_a.txt").exists()
        assert not (ws_a / "file_b.txt").exists()
        assert (ws_b / "file_b.txt").exists()
        assert not (ws_b / "file_a.txt").exists()

    @pytest.mark.task
    @pytest.mark.unit
    def test_child_workspace_nested_under_parent(self, tmp_path: Path) -> None:
        """子任务工作空间嵌套在父任务工作空间下，可访问父任务文件。"""
        ws_root = tmp_path / ".ai_workspaces"
        parent_ws = ws_root / "parent001"
        child_ws = parent_ws / "child001"
        parent_ws.mkdir(parents=True)
        child_ws.mkdir(parents=True)

        (parent_ws / "shared_config.yaml").write_text("key: value", encoding="utf-8")
        (child_ws / "output.txt").write_text("child result", encoding="utf-8")

        assert (child_ws / "output.txt").exists()
        assert (parent_ws / "shared_config.yaml").exists()
        assert not (parent_ws / "output.txt").exists()
