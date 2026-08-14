# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""workspace 插件（工作空间服务）单元测试。

覆盖（对齐 plugins/shared/system/workspace/）：
1. WorkspaceService —— 创建/查询/制品聚合/文件树/容器任务解析
2. 全局单例 get_workspace_service / reset_workspace_service
3. models —— FileTreeNode / Workspace 序列化往返

外部依赖（tasks.service_access / artifacts.artifact_service）全部用
sys.modules 注入伪模块替代，不依赖真实内核。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/workspace/
_SYSTEM_DIR = _PLUGIN_DIR.parent
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))


def _load_workspace_modules() -> dict[str, Any]:
    """以真实包名加载 workspace.{models,workspace_service} 并注册 sys.modules。"""
    out: dict[str, Any] = {}
    for name in ("models", "workspace_service"):
        mod_name = f"workspace.{name}"
        if mod_name in sys.modules:
            out[name] = sys.modules[mod_name]
            continue
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        out[name] = module
    return out


_MODS = _load_workspace_modules()
WorkspaceService = _MODS["workspace_service"].WorkspaceService
FileTreeNode = _MODS["models"].FileTreeNode
Workspace = _MODS["models"].Workspace
get_workspace_service = _MODS["workspace_service"].get_workspace_service
reset_workspace_service = _MODS["workspace_service"].reset_workspace_service


class _FakeTask:
    def __init__(self, task_id: str, parent_task_id: str | None = None, metadata: dict | None = None) -> None:
        self.id = task_id
        self.parent_task_id = parent_task_id
        self.metadata = metadata or {}


class _FakeTaskService:
    """伪 TaskService：get_task / list_subtasks。"""

    def __init__(self) -> None:
        self.tasks: dict[str, _FakeTask] = {}
        self.subtasks: dict[str, list[_FakeTask]] = {}

    def get_task(self, task_id: str) -> _FakeTask | None:
        return self.tasks.get(task_id)

    def list_subtasks(self, parent_id: str) -> list[_FakeTask]:
        return self.subtasks.get(parent_id, [])


class _FakeArtifactService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_artifacts_by_task(self, task_id: str, limit: int = 100) -> dict:
        self.calls.append(task_id)
        return {"items": [{"task_id": task_id, "name": f"art-{task_id}"}], "total": 1}


def _inject_task_service(fake: _FakeTaskService) -> None:
    """注入伪 tasks.service_access（懒加载路径）。"""
    mod = types.ModuleType("tasks.service_access")
    mod.get_task_service = lambda: fake
    sys.modules["tasks.service_access"] = mod


def _inject_artifact_service(fake: _FakeArtifactService) -> None:
    mod = types.ModuleType("artifacts.artifact_service")
    mod.get_artifact_service = lambda: fake
    sys.modules["artifacts.artifact_service"] = mod


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# WorkspaceService：创建与查询
# ═══════════════════════════════════════════════════════════


class TestWorkspaceServiceCreate:
    def test_get_or_create_workspace(self) -> None:
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("task-001", session_id="s1", title="标题"))
        assert ws.container_task_id == "task-001"
        assert ws.session_id == "s1"
        assert ws.title == "标题"

    def test_get_or_create_workspace_idempotent(self) -> None:
        """同 container_task_id 二次获取返回同一实例。"""
        svc = WorkspaceService()
        ws1 = _run(svc.get_or_create_workspace("task-001"))
        ws2 = _run(svc.get_or_create_workspace("task-001", title="另一个标题"))
        assert ws1.id == ws2.id
        assert ws2.title == ws1.title  # 不覆盖已有

    def test_default_title_uses_task_id(self) -> None:
        """未传 title → 默认 f"工作空间-{id[:8]}"。"""
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("abcdefgh1234"))
        assert ws.title == "工作空间-abcdefgh"

    def test_get_workspace(self) -> None:
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("t1"))
        assert _run(svc.get_workspace("t1")) is ws
        assert _run(svc.get_workspace("missing")) is None


class TestWorkspaceServiceSingleton:
    def test_singleton_get_and_reset(self) -> None:
        reset_workspace_service()
        svc1 = get_workspace_service()
        svc2 = get_workspace_service()
        assert svc1 is svc2
        reset_workspace_service()
        assert get_workspace_service() is not svc1
        reset_workspace_service()


# ═══════════════════════════════════════════════════════════
# 制品聚合 / 文件树 / 容器任务解析
# ═══════════════════════════════════════════════════════════


class TestWorkspaceArtifacts:
    def test_list_artifacts_no_workspace(self) -> None:
        svc = WorkspaceService()
        result = _run(svc.list_artifacts_by_workspace("missing"))
        assert result == {"items": [], "total": 0}

    def test_list_artifacts_aggregates_tasks(self) -> None:
        """聚合容器任务自身 + 子任务的制品。"""
        svc = WorkspaceService()
        _run(svc.get_or_create_workspace("root-task"))
        task_svc = _FakeTaskService()
        task_svc.subtasks["root-task"] = [_FakeTask("child-1"), _FakeTask("child-2")]
        _inject_task_service(task_svc)
        art = _FakeArtifactService()
        _inject_artifact_service(art)

        result = _run(svc.list_artifacts_by_workspace("root-task"))

        assert result["total"] == 3
        assert set(art.calls) == {"root-task", "child-1", "child-2"}

    def test_list_artifacts_service_unavailable(self) -> None:
        """task_service 为 None → 仅容器任务自身。"""
        svc = WorkspaceService()
        _run(svc.get_or_create_workspace("root-task"))
        _inject_task_service(None)  # type: ignore[arg-type]
        _inject_artifact_service(_FakeArtifactService())
        result = _run(svc.list_artifacts_by_workspace("root-task"))
        assert result["total"] == 1


class TestWorkspaceFileTree:
    def test_get_file_tree_scans_directory(self, tmp_path: Path) -> None:
        svc = WorkspaceService()
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "sub" / "b.md").write_text("b", encoding="utf-8")
        # 隐藏目录/文件被跳过
        (tmp_path / ".git").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / ".hidden").write_text("h", encoding="utf-8")

        result = _run(svc.get_file_tree("t1", base_path=str(tmp_path)))

        names = {n["name"] for n in result["tree"]}
        assert names == {"a.txt", "sub"}
        sub = next(n for n in result["tree"] if n["name"] == "sub")
        assert sub["children"][0]["name"] == "b.md"
        assert sub["children"][0]["type"] == "file"

    def test_get_file_tree_invalid_base_path(self) -> None:
        """base_path 不存在 → 空树。"""
        svc = WorkspaceService()
        result = _run(svc.get_file_tree("t1", base_path="/no/such/dir"))
        assert result["tree"] == []

    def test_get_file_tree_updates_workspace_cache(self, tmp_path: Path) -> None:
        svc = WorkspaceService()
        ws = _run(svc.get_or_create_workspace("t1"))
        (tmp_path / "x.txt").write_text("x", encoding="utf-8")
        _run(svc.get_file_tree("t1", base_path=str(tmp_path)))
        assert ws.file_tree, "file_tree 缓存应被更新"

    def test_scan_directory_skips_windows_reserved(self, tmp_path: Path) -> None:
        """Windows 保留名（CON 等）跳过；max_depth 生效。"""
        (tmp_path / "CON").write_text("x", encoding="utf-8")
        svc = WorkspaceService()
        nodes = svc._scan_directory(str(tmp_path), str(tmp_path), max_depth=1)
        assert nodes == []

    def test_scan_directory_permission_error_degrades(self, tmp_path: Path, monkeypatch) -> None:
        """os.listdir 抛 OSError → 返回空列表。"""
        import builtins

        real_listdir = os_listdir = None  # noqa: F841
        import os

        real_listdir = os.listdir

        def boom(path):
            raise PermissionError("denied")

        monkeypatch.setattr(os, "listdir", boom)
        svc = WorkspaceService()
        assert svc._scan_directory(str(tmp_path), str(tmp_path)) == []
        monkeypatch.setattr(os, "listdir", real_listdir)


class TestWorkspaceResolveContainerTask:
    def test_resolve_no_service(self) -> None:
        """task_service 不可用 → 原样返回 task_id。"""
        svc = WorkspaceService()
        _inject_task_service(None)  # type: ignore[arg-type]
        assert _run(svc.resolve_container_task("t1")) == "t1"

    def test_resolve_no_task(self) -> None:
        svc = WorkspaceService()
        task_svc = _FakeTaskService()
        _inject_task_service(task_svc)
        assert _run(svc.resolve_container_task("ghost")) == "ghost"

    def test_resolve_root_task(self) -> None:
        svc = WorkspaceService()
        task_svc = _FakeTaskService()
        task_svc.tasks["root"] = _FakeTask("root")
        _inject_task_service(task_svc)
        assert _run(svc.resolve_container_task("root")) == "root"

    def test_resolve_container_marked_task(self) -> None:
        svc = WorkspaceService()
        task_svc = _FakeTaskService()
        task_svc.tasks["sub"] = _FakeTask("sub", parent_task_id="root", metadata={"is_container": True})
        _inject_task_service(task_svc)
        assert _run(svc.resolve_container_task("sub")) == "sub"

    def test_resolve_walks_up_parent_chain(self) -> None:
        """沿 parent_task_id 链向上找到根任务。"""
        svc = WorkspaceService()
        task_svc = _FakeTaskService()
        task_svc.tasks["root"] = _FakeTask("root")
        task_svc.tasks["mid"] = _FakeTask("mid", parent_task_id="root")
        task_svc.tasks["leaf"] = _FakeTask("leaf", parent_task_id="mid")
        _inject_task_service(task_svc)
        assert _run(svc.resolve_container_task("leaf")) == "root"

    def test_resolve_broken_chain(self) -> None:
        """父任务缺失 → 返回链上最后一个已知任务。"""
        svc = WorkspaceService()
        task_svc = _FakeTaskService()
        task_svc.tasks["mid"] = _FakeTask("mid", parent_task_id="ghost-parent")
        task_svc.tasks["leaf"] = _FakeTask("leaf", parent_task_id="mid")
        _inject_task_service(task_svc)
        assert _run(svc.resolve_container_task("leaf")) == "mid"


# ═══════════════════════════════════════════════════════════
# models：序列化往返
# ═══════════════════════════════════════════════════════════


class TestModels:
    def test_file_tree_node_roundtrip(self) -> None:
        node = FileTreeNode(
            name="a.py",
            type="file",
            path="src/a.py",
            artifact_id="art-1",
            children=[FileTreeNode(name="child", path="c")],
            metadata={"k": "v"},
        )
        restored = FileTreeNode.from_dict(node.to_dict())
        assert restored.name == "a.py"
        assert restored.artifact_id == "art-1"
        assert restored.children[0].name == "child"
        assert restored.metadata == {"k": "v"}

    def test_file_tree_node_minimal(self) -> None:
        """无 artifact_id/children/metadata → 序列化省略可选字段。"""
        node = FileTreeNode(name="x")
        data = node.to_dict()
        assert "artifact_id" not in data
        assert "children" not in data
        assert "metadata" not in data

    def test_workspace_roundtrip(self) -> None:
        ws = Workspace(
            container_task_id="t1",
            session_id="s1",
            title="标题",
            file_tree=[FileTreeNode(name="f.txt", path="f.txt")],
        )
        restored = Workspace.from_dict(ws.to_dict())
        assert restored.container_task_id == "t1"
        assert restored.title == "标题"
        assert restored.file_tree[0].name == "f.txt"

    def test_workspace_from_dict_defaults(self) -> None:
        ws = Workspace.from_dict({})
        assert ws.id and ws.container_task_id == ""
