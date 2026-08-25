# @feature: FP-0.2.二 workspace 插件 http 面 | @vision: V3 可嵌入 | @ci: python-coverage
"""workspace 插件 workspaces 域 11 端点测试（channel_api 侧车化承接）。

覆盖（对齐原 tests/channels/test_routes_workspaces.py 语义 + 新 http.handle 分发层）：
1. GET /{id} /{id}/artifacts /{id}/file-tree —— 工作空间详情/制品聚合/文件树
2. GET/PUT /{id}/file-content —— 文件读写 + 路径穿越防护 + 10MB 限制
3. POST /{id}/create-entry / DELETE /{id}/entries / POST rename-entry / move-entry
4. POST /open-file 与 /{id}/open —— IDE 连接器链路（无连接器/成功/失败/异常）
5. 404 未知路由 / 解码异常 500 / auth 由声明承接（handler 不读 _user）
6. _on_load 的 pipeline-state 读面注入（能力就绪 → set_state_reader；未授予 →
   降级回退，workspace_service 读面 None）

外部依赖（tasks.service_access / connectors）经 sys.modules 注入与进程内
ConnectorRegistry 直连控制，不接真实内核。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "workspace"


def _load_server() -> Any:
    """动态加载 workspace/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "workspace_server_http_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["workspace_server_http_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


@pytest.fixture
def ws_dir(tmp_path: Path) -> str:
    """真实工作空间目录（create-entry 等 FS 操作目标）。"""
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


def _inject_workspace_path(server: Any, ws_path: str | None) -> None:
    """控制 _resolve_workspace_path：None = 未找到工作空间路径。"""

    async def fake(container_task_id: str) -> str | None:
        if container_task_id == "_missing":
            return None
        return ws_path

    server._resolve_workspace_path = fake  # type: ignore[method-assign]


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(server: Any, **kwargs: Any) -> dict[str, Any]:
    """同步调用 http.handle（测试侧统一 asyncio 跑）。"""
    return _run(server.http_handle(**kwargs))


def _decode_http(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


class _FakeTask:
    def __init__(self, task_id: str, parent_task_id: str | None = None, metadata: dict | None = None) -> None:
        self.id = task_id
        self.parent_task_id = parent_task_id
        self.metadata = metadata or {}


class _FakeTaskService:
    def __init__(self) -> None:
        self.tasks: dict[str, _FakeTask] = {}
        self.subtasks: dict[str, list[_FakeTask]] = {}

    def get_task(self, task_id: str) -> _FakeTask | None:
        return self.tasks.get(task_id)

    def list_subtasks(self, parent_id: str) -> list[_FakeTask]:
        return self.subtasks.get(parent_id, [])


def _inject_task_service(fake: _FakeTaskService) -> None:
    """注入伪 tasks.service_access（懒加载路径）。"""
    mod = types.ModuleType("tasks.service_access")
    mod.get_task_service = lambda: fake  # type: ignore[attr-defined]
    sys.modules["tasks.service_access"] = mod


class _FakeResult:
    def __init__(self, success: bool, error: str = "") -> None:
        self.success = success
        self.error = error


class _FakeConnector:
    """满足 ConnectorRegistry.get_best_connector_for 的最小连接器。"""

    def __init__(
        self,
        capabilities: list[str],
        result: _FakeResult | None = None,
        raise_on_execute: Exception | None = None,
        connector_type: str = "fake_vscode",
        connected: bool = True,
    ) -> None:
        self._capabilities = capabilities
        self._result = result or _FakeResult(True)
        self._raise = raise_on_execute
        self.connector_type = connector_type
        self.is_connected = connected
        self.action_calls: list[str] = []

    def get_info(self) -> Any:
        return types.SimpleNamespace(capabilities=self._capabilities, priority=10)

    async def execute_action(self, action: Any) -> _FakeResult:
        self.action_calls.append(action.action_type)
        if self._raise is not None:
            raise self._raise
        return self._result


class _FakeRegistry:
    """最小注册表：只暴露 handler 用到的 get_best_connector_for。"""

    def __init__(self, connector: Any | None = None) -> None:
        self.connector = connector

    def get_best_connector_for(self, action_type: str) -> Any | None:  # noqa: ARG002
        return self.connector


# ═══════════════════════════════════════════════════════════
# 1. 工作空间详情 / 制品聚合 / 文件树
# ═══════════════════════════════════════════════════════════


class TestWorkspaceDetail:
    def test_get_workspace_creates_on_miss(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/task-001",
                method="GET",
            )
        )
        assert status == 200
        assert body["container_task_id"] == "task-001"

    def test_get_workspace_second_call_returns_same_id(self, server: Any) -> None:
        _, first = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/task-001",
                method="GET",
            )
        )
        _, second = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/task-001",
                method="GET",
            )
        )
        assert first["id"] == second["id"]

    def test_get_workspace_artifacts_empty(self, server: Any) -> None:
        """无工作空间 → {items:[], total:0}。"""
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/ghost/artifacts",
                method="GET",
            )
        )
        assert status == 200
        assert body == {"items": [], "total": 0}

    def test_get_workspace_artifacts_aggregates(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """有工作空间：聚合容器任务 + 子任务制品（state 读面未注入 → legacy 路径）。"""
        _decode_http(_call(server, path="/ext/workspace_service/workspaces/root", method="GET"))
        task_svc = _FakeTaskService()
        task_svc.tasks["root"] = _FakeTask("root")
        task_svc.subtasks["root"] = [_FakeTask("child-1")]
        _inject_task_service(task_svc)
        fake_art = types.ModuleType("artifacts.artifact_service")

        async def list_artifacts(task_id: str, limit: int = 100) -> dict:  # noqa: ARG001
            return {"items": [{"task_id": task_id, "name": f"art-{task_id}"}], "total": 1}

        fake_art.get_artifact_service = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
            list_artifacts_by_task=list_artifacts,
        )
        monkeypatch.setitem(sys.modules, "artifacts.artifact_service", fake_art)

        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/root/artifacts",
                method="GET",
            )
        )
        assert status == 200
        assert body["total"] == 2
        assert {i["task_id"] for i in body["items"]} == {"root", "child-1"}

    def test_get_file_tree_scans_workspace(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "a.txt").write_text("a", encoding="utf-8")
        (Path(ws_dir) / "sub").mkdir()
        (Path(ws_dir) / ".hidden").write_text("h", encoding="utf-8")

        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-tree",
                method="GET",
            )
        )
        assert status == 200
        names = {n["name"] for n in body["tree"]}
        assert names == {"a.txt", "sub"}

    def test_get_file_tree_no_workspace_path(self, server: Any) -> None:
        _inject_workspace_path(server, None)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-tree",
                method="GET",
            )
        )
        assert status == 200
        assert body["tree"] == []


# ═══════════════════════════════════════════════════════════
# 2. 文件读写（GET/PUT file-content）
# ═══════════════════════════════════════════════════════════


class TestFileContent:
    def test_read_file_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "note.md").write_text("hello", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="GET",
                query={"path": "note.md"},
            )
        )
        assert status == 200
        assert body["success"] is True
        assert body["content"] == "hello"
        assert body["size"] == 5

    def test_read_file_absolute_path(self, server: Any, ws_dir: str) -> None:
        (Path(ws_dir) / "abs.txt").write_text("abs", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/_local/file-content",
                method="GET",
                query={"path": str(Path(ws_dir) / "abs.txt")},
            )
        )
        assert status == 200
        assert body["success"] is True

    def test_read_file_escape_workspace(self, server: Any, ws_dir: str, tmp_path: Path) -> None:
        _inject_workspace_path(server, ws_dir)
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="GET",
                query={"path": f"../{outside.name}"},
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "超出工作空间范围" in body["message"]

    def test_read_file_missing(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="GET",
                query={"path": "nope.md"},
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "文件不存在" in body["message"]

    def test_read_file_io_error(self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_workspace_path(server, ws_dir)
        target = Path(ws_dir) / "locked.txt"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="GET",
                query={"path": "locked.txt"},
            )
        )
        assert status == 200
        assert body["success"] is False

    def test_save_file_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="PUT",
                query={"path": "out.md"},
                raw_body=base64.b64encode(b'{"content": "saved"}').decode(),
            )
        )
        assert status == 200
        assert body["success"] is True
        assert (Path(ws_dir) / "out.md").read_text(encoding="utf-8") == "saved"

    def test_save_file_no_workspace(self, server: Any) -> None:
        _inject_workspace_path(server, None)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="PUT",
                query={"path": "out.md"},
                raw_body='{"content": "x"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "未找到工作空间路径" in body["message"]

    def test_save_file_escape(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="PUT",
                query={"path": "../evil.md"},
                raw_body='{"content": "x"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "超出工作空间范围" in body["message"]

    def test_save_file_parent_missing(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="PUT",
                query={"path": "no/such/dir/out.md"},
                raw_body='{"content": "x"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "目标目录不存在" in body["message"]


# ═══════════════════════════════════════════════════════════
# 3. 条目操作（create-entry / entries / rename-entry / move-entry）
# ═══════════════════════════════════════════════════════════


class TestEntryOps:
    def test_create_file_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "new.py", "type": "file"}',
            )
        )
        assert status == 200
        assert body["success"] is True
        assert (Path(ws_dir) / "new.py").exists()

    def test_create_directory_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "d1", "type": "directory"}',
            )
        )
        assert status == 200
        assert body["success"] is True
        assert (Path(ws_dir) / "d1").is_dir()

    def test_create_entry_missing_path(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "", "type": "file"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "path 参数不能为空" in body["message"]

    def test_create_entry_bad_type(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "x", "type": "symlink"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "type 参数必须为 file 或 directory" in body["message"]

    def test_create_entry_escape(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "../evil", "type": "file"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "路径超出工作空间范围" in body["message"]

    def test_create_entry_existing(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "dup.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "dup.py", "type": "file"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "路径已存在" in body["message"]

    def test_create_entry_no_workspace(self, server: Any) -> None:
        _inject_workspace_path(server, None)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "x.py", "type": "file"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "未找到工作空间路径" in body["message"]

    def test_create_entry_io_error(self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_workspace_path(server, ws_dir)

        def boom(*a: Any, **k: Any) -> Any:
            raise OSError("denied")

        monkeypatch.setattr(Path, "write_text", boom)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "x.py", "type": "file"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "创建失败" in body["message"]

    def test_delete_file_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "rm.txt").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/entries",
                method="DELETE",
                query={"path": "rm.txt"},
            )
        )
        assert status == 200
        assert body["success"] is True
        assert not (Path(ws_dir) / "rm.txt").exists()

    def test_delete_directory_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "rmdir").mkdir()
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/entries",
                method="DELETE",
                query={"path": "rmdir"},
            )
        )
        assert body["success"] is True

    def test_delete_empty_path(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/entries",
                method="DELETE",
                query={"path": ""},
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "path 参数不能为空" in body["message"]

    def test_delete_root_forbidden(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/entries",
                method="DELETE",
                query={"path": "."},
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "禁止删除工作空间根目录" in body["message"]

    def test_delete_escape(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/entries",
                method="DELETE",
                query={"path": "../evil"},
            )
        )
        assert status == 200
        assert body["success"] is False

    def test_delete_missing_entry(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/entries",
                method="DELETE",
                query={"path": "ghost.txt"},
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "路径不存在" in body["message"]

    def test_delete_io_error(self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "locked.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "unlink", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/entries",
                method="DELETE",
                query={"path": "locked.txt"},
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "删除失败" in body["message"]

    def test_rename_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "old.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "old.py", "new_name": "new.py"}',
            )
        )
        assert status == 200
        assert body["success"] is True
        assert body["new_path"] == "new.py"
        assert (Path(ws_dir) / "new.py").exists()

    def test_rename_relative_nested(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        sub = Path(ws_dir) / "sub"
        sub.mkdir()
        (sub / "a.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "sub/a.py", "new_name": "b.py"}',
            )
        )
        assert status == 200
        assert body["new_path"] == str(Path("sub") / "b.py")

    def test_rename_missing_old_path(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "", "new_name": "b.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "old_path 参数不能为空" in body["message"]

    def test_rename_missing_new_name(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "a.py", "new_name": ""}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "new_name 参数不能为空" in body["message"]

    def test_rename_new_name_with_separator(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "a.py", "new_name": "evil/../b.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "new_name 不能包含路径分隔符" in body["message"]

    def test_rename_missing_old(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "ghost.py", "new_name": "b.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "路径不存在" in body["message"]

    def test_rename_target_exists(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "a.py").write_text("x", encoding="utf-8")
        (Path(ws_dir) / "b.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "a.py", "new_name": "b.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "目标名称已存在" in body["message"]

    def test_rename_io_error(self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "a.py").write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "rename", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/rename-entry",
                method="POST",
                raw_body='{"old_path": "a.py", "new_name": "b.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "重命名失败" in body["message"]

    def test_move_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        (Path(ws_dir) / "dest").mkdir()
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "m.py", "destination_dir": "dest"}',
            )
        )
        assert status == 200
        assert body["success"] is True
        assert body["destination_path"] == str(Path("dest") / "m.py")
        assert (Path(ws_dir) / "dest" / "m.py").exists()

    def test_move_missing_source_path(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "", "destination_dir": "dest"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "source_path 参数不能为空" in body["message"]

    def test_move_missing_dest(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "m.py", "destination_dir": ""}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "destination_dir 参数不能为空" in body["message"]

    def test_move_missing_source(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "dest").mkdir()
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "ghost.py", "destination_dir": "dest"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "源路径不存在" in body["message"]

    def test_move_dest_not_dir(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "m.py", "destination_dir": "notadir"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "目标目录不存在或不是目录" in body["message"]

    def test_move_into_own_subdir(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "dir").mkdir()
        (Path(ws_dir) / "dir" / "inner").mkdir()
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "dir", "destination_dir": "dir/inner"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "自身子目录" in body["message"]

    def test_move_target_exists(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        (Path(ws_dir) / "dest").mkdir()
        (Path(ws_dir) / "dest" / "m.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "m.py", "destination_dir": "dest"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "已存在同名文件" in body["message"]

    def test_move_io_error(self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        (Path(ws_dir) / "dest").mkdir()
        import shutil

        monkeypatch.setattr(shutil, "move", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/move-entry",
                method="POST",
                raw_body='{"source_path": "m.py", "destination_dir": "dest"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "移动失败" in body["message"]


# ═══════════════════════════════════════════════════════════
# 4. IDE 连接器链路（open-file / open）
# ═══════════════════════════════════════════════════════════


class TestIdeOpenFile:
    def test_open_file_missing_path(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body='{"file_path": ""}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "file_path 参数不能为空" in body["message"]

    def test_open_file_no_connector_none_registry(self, server: Any) -> None:
        """_connector_registry 单例尚未创建 → 真实 ConnectorRegistry 实例化 + 无连接器。"""
        server._connector_registry = None  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body='{"file_path": "a.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "没有可用的 IDE 连接器" in body["message"]
        assert server._connector_registry is not None

    def test_open_file_no_connector_injected_registry(self, server: Any) -> None:
        server._connector_registry = _FakeRegistry(None)  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body='{"file_path": "a.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False

    def test_open_file_connector_success(self, server: Any) -> None:
        conn = _FakeConnector(["open_file"], _FakeResult(True))
        server._connector_registry = _FakeRegistry(conn)  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body='{"file_path": "a.py", "line": 3, "column": 5}',
            )
        )
        assert status == 200
        assert body["success"] is True
        assert conn.action_calls == ["open_file"]
        assert "已在" in body["message"]

    def test_open_file_connector_failure(self, server: Any) -> None:
        conn = _FakeConnector(["open_file"], _FakeResult(False, "connector boom"))
        server._connector_registry = _FakeRegistry(conn)  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body='{"file_path": "a.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "连接器执行失败" in body["message"]

    def test_open_file_connector_exception(self, server: Any) -> None:
        conn = _FakeConnector(["open_file"], raise_on_execute=RuntimeError("kaput"))
        server._connector_registry = _FakeRegistry(conn)  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body='{"file_path": "a.py"}',
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "打开文件失败" in body["message"]


class TestIdeOpenWorkspace:
    def test_open_workspace_no_path_found(self, server: Any) -> None:
        _inject_workspace_path(server, None)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "未找到任务" in body["message"]

    def test_open_workspace_no_connector_file_manager_ok(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inject_workspace_path(server, ws_dir)
        server._connector_registry = _FakeRegistry(None)  # type: ignore[attr-defined]
        monkeypatch.setattr(server, "_open_in_system_file_manager", lambda _p: True)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is True
        assert "系统文件管理器" in body["message"]

    def test_open_workspace_no_connector_no_file_manager(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inject_workspace_path(server, ws_dir)
        server._connector_registry = _FakeRegistry(None)  # type: ignore[attr-defined]
        monkeypatch.setattr(server, "_open_in_system_file_manager", lambda _p: False)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "无法启动系统文件管理器" in body["message"]

    def test_open_workspace_connector_success(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        conn = _FakeConnector(["open_folder"], _FakeResult(True))
        server._connector_registry = _FakeRegistry(conn)  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is True
        assert conn.action_calls == ["open_folder"]

    def test_open_workspace_connector_failure(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        conn = _FakeConnector(["open_folder"], _FakeResult(False, "boom"))
        server._connector_registry = _FakeRegistry(conn)  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "连接器执行失败" in body["message"]

    def test_open_workspace_connector_exception(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        conn = _FakeConnector(["open_folder"], raise_on_execute=RuntimeError("kaput"))
        server._connector_registry = _FakeRegistry(conn)  # type: ignore[attr-defined]
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "打开工作空间失败" in body["message"]

    def test_open_in_system_file_manager_missing_dir(self) -> None:
        server = _load_server()
        assert server._open_in_system_file_manager("/no/such/dir/anywhere") is False


# ═══════════════════════════════════════════════════════════
# 5. 分发层：404 / 非法 body / _resolve_workspace_path 任务解析
# ═══════════════════════════════════════════════════════════


class TestDispatch:
    def test_unknown_path_404(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/unknown",
                method="GET",
            )
        )
        assert status == 404
        assert "not found" in body["error"]

    def test_wrong_method_404(self, server: Any) -> None:
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-tree",
                method="POST",
            )
        )
        assert status == 404

    def test_invalid_json_body_500(self, server: Any) -> None:
        result = _call(
            server,
            path="/ext/workspace_service/workspaces/t1/create-entry",
            method="POST",
            raw_body="{not-json",
        )
        assert result["success"] is False
        assert result["data"]["status"] == 500

    def test_plain_json_body_decoded(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/create-entry",
                method="POST",
                raw_body='{"path": "plain.py", "type": "file"}',
            )
        )
        assert status == 200
        assert body["success"] is True

    def test_resolve_workspace_path_from_task_metadata(self, server: Any, ws_dir: str) -> None:
        """_resolve_workspace_path 经 tasks.service_access 读 ws_meta.path。"""
        task_svc = _FakeTaskService()
        task_svc.tasks["c1"] = _FakeTask("c1", metadata={"ws_meta": {"path": ws_dir}})
        _inject_task_service(task_svc)
        assert _run(server._resolve_workspace_path("c1")) == ws_dir

    def test_resolve_workspace_path_task_missing(self, server: Any) -> None:
        task_svc = _FakeTaskService()
        _inject_task_service(task_svc)
        assert _run(server._resolve_workspace_path("ghost")) is None

    def test_resolve_workspace_path_service_none(self, server: Any) -> None:
        _inject_task_service(None)  # type: ignore[arg-type]
        assert _run(server._resolve_workspace_path("c1")) is None

    def test_resolve_workspace_path_local(self, server: Any) -> None:
        root = _run(server._resolve_workspace_path("_local"))
        assert root == str(Path(__file__).resolve().parents[4])

    def test_resolve_workspace_path_exception_degrades(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("tasks.service_access")

        def gts() -> Any:
            raise RuntimeError("task service broke")

        mod.get_task_service = gts  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tasks.service_access", mod)
        assert _run(server._resolve_workspace_path("c1")) is None


# ═══════════════════════════════════════════════════════════
# 6. pipeline-state 读面注入（_on_load）
# ═══════════════════════════════════════════════════════════


class TestStateReaderInjection:
    def test_on_load_injects_state_reader(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """能力就绪 → set_state_reader 被调用，读面返回内核行。"""
        calls: list[Any] = []

        class _FakeHandle:
            async def call(self, method: str, params: dict) -> list[dict]:  # noqa: ARG002
                return [{"pipeline_id": "p1"}]

        monkeypatch.setitem(server.plugin._capabilities, "pipeline-state", _FakeHandle())
        monkeypatch.setattr(server, "set_state_reader", calls.append)
        _run(server._on_load({}))
        assert len(calls) == 1
        assert _run(calls[0]()) == [{"pipeline_id": "p1"}]

    def test_on_load_injection_failure_degrades(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """能力未授予（get_capability KeyError）→ 警告降级，不抛。"""
        monkeypatch.setattr(server, "set_state_reader", lambda _reader: pytest.fail("不应注入"))
        _run(server._on_load({}))  # 不抛即过

    def test_on_load_resets_service_singleton(self, server: Any) -> None:
        _run(server._on_load({}))
        assert server._service is server.get_workspace_service()
        assert server._service is not None
        _run(server._on_unload({}))
        assert server._service is None


class TestToolsStillRegistered:
    def test_workspace_tool_functions_kept(self, server: Any) -> None:
        assert "workspace.get_or_create" in server.plugin._tools
        assert "workspace.get" in server.plugin._tools
        assert "workspace.get_file_tree" in server.plugin._tools
        assert "http.handle" in server.plugin._tools

    def test_workspace_get_tool_uninitialized(self, server: Any) -> None:
        server._service = None  # type: ignore[attr-defined]
        assert _run(server.workspace_get("t1"))["success"] is False


# ═══════════════════════════════════════════════════════════
# 7. 补充覆盖：引导分支 / 空 body / 大文件 / 防御分支（diff coverage 100%）
# ═══════════════════════════════════════════════════════════


class TestBootstrapBranches:
    def test_load_adds_system_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """system 目录不在 sys.path 时，server.py 自举补入（对齐 sidecar 生产行为）。"""
        sys_dir = str(_PLUGIN_DIR.parent)
        removed: list[str] = []
        for p in list(sys.path):
            if p == sys_dir:
                sys.path.remove(p)
                removed.append(p)
        assert removed, "测试前置：system 目录应本就在 sys.path 上"
        try:
            loaded = _load_server()
            assert "http.handle" in loaded.plugin._tools
            assert sys_dir in sys.path
        finally:
            for p in removed:
                if p not in sys.path:
                    sys.path.insert(0, p)

    def test_on_load_no_capability_keeps_legacy_reader(self, server: Any) -> None:
        """on_load 后未注入读面：_read_state_rows 走 None 回退（legacy 镜像）。"""
        _run(server._on_load({}))
        svc = server.get_workspace_service()
        assert _run(svc._read_state_rows()) is None


class TestEmptyAndOversizedPayloads:
    def test_create_entry_empty_raw_body(self, server: Any, ws_dir: str) -> None:
        """raw_body 空串 → _decode_body 返回 {} → create_entry 参数校验兜住。"""
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/create-entry",
            method="POST",
        ))
        assert status == 200
        assert body["success"] is False
        assert "path 参数不能为空" in body["message"]

    def test_save_file_body_none_direct_call(self, server: Any, ws_dir: str) -> None:
        """save_file_content body=None → {}（handler 直调路径，dispatch 恒传 dict）。"""
        _inject_workspace_path(server, ws_dir)
        result = _run(server.save_file_content("t1", "x.md", None))
        assert result["success"] is True
        assert (Path(ws_dir) / "x.md").exists()

    def test_read_file_too_large(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        huge = Path(ws_dir) / "huge.txt"
        huge.write_text("x" * (10 * 1024 * 1024 + 1), encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": "huge.txt"},
        ))
        assert status == 200
        assert body["success"] is False
        assert "文件过大" in body["message"]

    def test_save_file_too_large(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="PUT",
            query={"path": "huge.md"},
            raw_body=json.dumps({"content": "x" * (10 * 1024 * 1024 + 1)}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "内容过大" in body["message"]

    def test_save_file_write_io_error(self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_workspace_path(server, ws_dir)
        monkeypatch.setattr(Path, "write_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")))
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="PUT",
            query={"path": "out.md"},
            raw_body=json.dumps({"content": "x"}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "保存文件失败" in body["message"]

    def test_read_relative_path_no_workspace_in_range(self, server: Any) -> None:
        """无工作空间 + 相对路径在项目根内 → 按项目根解析读取。"""
        _inject_workspace_path(server, None)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/_missing/file-content",
            method="GET",
            query={"path": "AGENTS.md"},
        ))
        assert status == 200
        assert body["success"] is True

    def test_read_relative_path_no_workspace_escape(self, server: Any) -> None:
        """无工作空间 + 相对路径越出项目根 → 拒绝。"""
        _inject_workspace_path(server, None)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/_missing/file-content",
            method="GET",
            query={"path": "../escape.txt"},
        ))
        assert status == 200
        assert body["success"] is False
        assert "超出工作空间范围" in body["message"]


class TestEntryDefensiveBranches:
    def test_delete_entry_no_workspace(self, server: Any) -> None:
        _inject_workspace_path(server, None)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/entries",
            method="DELETE",
            query={"path": "x.txt"},
        ))
        assert status == 200
        assert body["success"] is False
        assert "未找到工作空间路径" in body["message"]

    def test_rename_no_workspace(self, server: Any) -> None:
        _inject_workspace_path(server, None)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/rename-entry",
            method="POST",
            raw_body=json.dumps({"old_path": "a.py", "new_name": "b.py"}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "未找到工作空间路径" in body["message"]

    def test_rename_source_escape(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/rename-entry",
            method="POST",
            raw_body=json.dumps({"old_path": "../evil.py", "new_name": "b.py"}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "路径超出工作空间范围" in body["message"]

    def test_rename_new_path_out_of_workspace(self, server: Any, ws_dir: str) -> None:
        """new_name=.. 无路径分隔符，但解析后越出工作空间 → 拒绝。"""
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "a.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/rename-entry",
            method="POST",
            raw_body=json.dumps({"old_path": "a.py", "new_name": ".."}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "目标路径超出工作空间范围" in body["message"]

    def test_move_no_workspace(self, server: Any) -> None:
        _inject_workspace_path(server, None)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/move-entry",
            method="POST",
            raw_body=json.dumps({"source_path": "a.py", "destination_dir": "dest"}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "未找到工作空间路径" in body["message"]

    def test_move_source_escape(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/move-entry",
            method="POST",
            raw_body=json.dumps({"source_path": "../evil.py", "destination_dir": "dest"}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "源路径超出工作空间范围" in body["message"]

    def test_move_dest_escape(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/move-entry",
            method="POST",
            raw_body=json.dumps({"source_path": "m.py", "destination_dir": "../evil"}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "目标路径超出工作空间范围" in body["message"]

    def test_move_dest_outside_workspace(self, server: Any, ws_dir: str, tmp_path: Path) -> None:
        """防御分支：目标解析路径不落在工作空间内（经校验函数注入外部目录触发）。"""
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        outside = tmp_path / "outside_dest"
        outside.mkdir()
        orig = server._validate_path_in_workspace

        def fake(workspace_path: Path, rel_path: str) -> Path | None:
            if rel_path == "dest":
                return outside
            return orig(workspace_path, rel_path)

        server._validate_path_in_workspace = fake  # type: ignore[method-assign]
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/move-entry",
            method="POST",
            raw_body=json.dumps({"source_path": "m.py", "destination_dir": "dest"}),
        ))
        assert status == 200
        assert body["success"] is False
        assert "目标路径超出工作空间范围" in body["message"]


class TestSystemFileManager:
    def test_open_in_system_file_manager_win32(
        self, tmp_path: Path, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "openme"
        d.mkdir()
        calls: list[list[str]] = []
        monkeypatch.setattr(server.subprocess, "Popen", calls.append)
        monkeypatch.setattr(server.sys, "platform", "win32")
        assert server._open_in_system_file_manager(str(d)) is True
        assert calls
        assert calls[0][0] == "explorer"

    def test_open_in_system_file_manager_darwin(
        self, tmp_path: Path, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "openme"
        d.mkdir()
        calls: list[list[str]] = []
        monkeypatch.setattr(server.subprocess, "Popen", calls.append)
        monkeypatch.setattr(server.sys, "platform", "darwin")
        assert server._open_in_system_file_manager(str(d)) is True
        assert calls[0][0] == "open"

    def test_open_in_system_file_manager_linux(
        self, tmp_path: Path, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "openme"
        d.mkdir()
        calls: list[list[str]] = []
        monkeypatch.setattr(server.subprocess, "Popen", calls.append)
        monkeypatch.setattr(server.sys, "platform", "linux")
        assert server._open_in_system_file_manager(str(d)) is True
        assert calls[0][0] == "xdg-open"

    def test_open_in_system_file_manager_popen_error(
        self, tmp_path: Path, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "openme"
        d.mkdir()

        def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("no explorer")

        monkeypatch.setattr(server.subprocess, "Popen", boom)
        assert server._open_in_system_file_manager(str(d)) is False


class TestWorkspaceToolsSuccess:
    def test_get_or_create_tool_success(self, server: Any) -> None:
        _run(server._on_load({}))
        result = _run(server.workspace_get_or_create("t1", session_id="s1", title="标题"))
        assert result["success"] is True
        assert result["workspace"]["container_task_id"] == "t1"

    def test_get_or_create_tool_uninitialized(self, server: Any) -> None:
        server._service = None  # type: ignore[attr-defined]
        result = _run(server.workspace_get_or_create("t1"))
        assert result["success"] is False

    def test_get_tool_success_and_missing(self, server: Any) -> None:
        _run(server._on_load({}))
        ws = _run(server.workspace_get_or_create("t1"))
        found = _run(server.workspace_get("t1"))
        assert found["success"] is True
        assert found["workspace"]["id"] == ws["workspace"]["id"]
        missing = _run(server.workspace_get("ghost"))
        assert missing["success"] is False
        assert "不存在" in missing["error"]

    def test_get_file_tree_tool_success(self, server: Any, tmp_path: Path) -> None:
        _run(server._on_load({}))
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        result = _run(server.workspace_get_file_tree("t1", base_path=str(tmp_path)))
        assert result["success"] is True
        assert {n["name"] for n in result["tree"]} == {"f.txt"}

    def test_get_file_tree_tool_uninitialized(self, server: Any) -> None:
        server._service = None  # type: ignore[attr-defined]
        result = _run(server.workspace_get_file_tree("t1"))
        assert result["success"] is False
