# @feature: FP-0.2.二 workspace 插件 http 面 | @vision: V3 可嵌入 | @ci: python-coverage
"""workspace 插件 workspaces 域 11 端点测试（channel_api 侧车化承接）。

覆盖（对齐原 tests/channels/test_routes_workspaces.py 语义 + 新 http.handle 分发层）：
1. GET /{id} /{id}/artifacts /{id}/file-tree —— 工作空间详情/制品聚合/文件树
2. GET/PUT /{id}/file-content —— 文件读写；读面按路径形态分治（用户裁定
   2026-09-28 绝对路径直读 + 2026-10-24 延伸：锚定仅用于定位、不做越界拒绝、
   不设大小上限，ADR 2026-09-28-file-content-absolute-direct-read）；写面
   仍守越界 + 10MB 限制
3. POST /{id}/create-entry / DELETE /{id}/entries / POST rename-entry / move-entry
4. POST /open-file 与 /{id}/open —— 宿主原生直开链路（成功/路径缺失/OS 调用
   失败，用户裁定 2026-10-08：打开本地路径不经插件层）
5. 404 未知路由 / 解码异常 500 / auth 由声明承接（handler 不读 _user）
6. _on_load 的 pipeline-state 读面注入（能力就绪 → set_state_reader；未授予 →
   读面缺失 = 无坐标，如实 no_workspace，无兜底通道）
7. 工作区解析路由（用户裁定 2026-10-08，state 单一真相源）：_resolve_container_path
   按 id 类别显式路由——_local 特例要求认证；任务/会话管道 id 走 state 聚合行
   （任务行按 task.submitted_by 归属闸，会话域行认证可达）；项目登记按
   ProjectModel.submitted_by；task_service 镜像通道已删（state 无行即
   no_workspace，不再有镜像兜底）

外部依赖（tasks.service_access / 宿主打开调用 os.startfile 与 webbrowser）
经 sys.modules 注入与 monkeypatch 替身隔离，不接真实内核。
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

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "workspace"


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
    """控制 _resolve_container_path：None = 未找到工作空间路径。

    fake 签名与生产路由器同形（container_task_id, caller）——归属闸注入
    场景由注入面整体替代，本 fake 只表达坐标解析结果。
    """

    async def fake(container_task_id: str, caller: dict[str, Any] | None = None) -> str | None:  # noqa: ARG001
        if container_task_id == "_missing":
            return None
        return ws_path

    server._resolve_container_path = fake  # type: ignore[method-assign]


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


def _make_token(user_id: str, username: str = "tester") -> str:
    """构造内核 0.2 开发期 token（base64_nopad("access:{uid}:{name}:{exp}")，
    与 tasks 插桩车道 _make_token 同款）。"""
    raw = f"access:{user_id}:{username}:{2**31 - 1}"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_token(user_id)}"}


def _decode_http(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _startfile_recorder(seen: list[str]) -> Any:
    """os.startfile 替身工厂：记录调用参数（显式 def，mypy func-returns-value 干净）。"""

    def _record(p: str) -> None:
        seen.append(p)

    return _record


def _webbrowser_recorder(seen: list[str]) -> Any:
    """webbrowser.open 替身工厂：记录 URI 并返回 True（显式 def，mypy 干净）。"""

    def _record(uri: str) -> bool:
        seen.append(uri)
        return True

    return _record


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


class _FakeProjectRegistry:
    """最小项目登记簿：只暴露归属闸消费的 get().submitted_by。"""

    def __init__(self, submitted_by: dict[str, str]) -> None:
        self._submitted_by = submitted_by

    def get(self, project_id: str) -> Any:
        sub = self._submitted_by.get(project_id)
        if sub is None:
            return None
        return types.SimpleNamespace(submitted_by=sub)


def _inject_project_registry(
    monkeypatch: pytest.MonkeyPatch,
    server: Any,
    paths: dict[str, str],
    submitted_by: dict[str, str],
) -> None:
    """project.* 能力替身（T6a 后归属闸/路径解析经 plugin.get_capability
    ("project") 的 list_paths/get；非 project 能力透传真 plugin）。"""
    owners = dict(submitted_by)

    class _FakeProjectCap:
        async def call(self, method: str, args: dict) -> dict:
            if method == "list_paths":
                return {"paths": dict(paths)}
            if method == "get":
                pid = str(args.get("project_id") or "")
                if pid not in owners:
                    return {"found": False}
                return {"found": True, "project": {"submitted_by": owners[pid]}}
            raise KeyError(method)

    real = server.plugin

    class _FakePlugin:
        def get_capability(self, name: str) -> Any:
            if name == "project":
                return _FakeProjectCap()
            return real.get_capability(name)

    monkeypatch.setattr(server, "plugin", _FakePlugin())


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
        """有工作空间：聚合容器任务 + 子任务制品（子链挂靠键 = task.parent_project_id）。"""
        _decode_http(_call(server, path="/ext/workspace_service/workspaces/root", method="GET"))
        ws_mod = sys.modules["workspace_service"]
        svc = ws_mod.get_workspace_service()

        async def read_rows() -> list[dict[str, Any]]:
            return [
                {"pipeline_id": "root"},
                {"pipeline_id": "child-1", "task.parent_project_id": "root"},
            ]

        monkeypatch.setattr(svc, "_read_state_rows", read_rows, raising=False)
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
        names = {n["name"] for n in body["entries"]}
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
        assert body["entries"] == []


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

    def test_read_file_absolute_path(self, server: Any) -> None:
        """_local 场景绝对路径读取（workspace=项目根，界内绝对路径放行）。"""
        abs_path = Path(__file__).resolve().parents[4] / "AGENTS.md"
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/_local/file-content",
                method="GET",
                query={"path": str(abs_path)},
                headers=_auth("u-1"),
            )
        )
        assert status == 200
        assert body["success"] is True

    def test_read_file_escape_workspace(self, server: Any, ws_dir: str, tmp_path: Path) -> None:
        """读面相对路径 `../` = 锚定定位、不做越界拒绝（用户裁定 2026-10-24：
        打开面不通过路径限制用户访问，守卫归写面与 fs_tools 链）。"""
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
        assert body["success"] is True, "读面越界不拒绝（锚定仅用于定位）"
        assert body["content"] == "x"

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

    def test_read_file_merged_worktree_remaps_to_project_root(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已合并 worktree 的死路径 → 重定位到 project_root 读取；返回体仍携带
        原请求路径（文件卡片契约不变）。"""
        project = tmp_path / "proj"
        project.mkdir()
        (project / "merged.txt").write_text("merged-content", encoding="utf-8")
        dead = tmp_path / "proj__wt_deadbeef" / "merged.txt"
        ws_mod = sys.modules["workspace_service"]
        monkeypatch.setattr(
            ws_mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": "t1",
                    "task.submitted_by": "u-1",
                    "ws_meta": {
                        "mode": "worktree",
                        "path": str(dead.parent),
                        "project_root": str(project),
                    },
                }
            ],
        )
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="GET",
                query={"path": str(dead)},
                headers=_auth("u-1"),
            )
        )
        assert status == 200
        assert body["success"] is True
        assert body["content"] == "merged-content"
        assert body["path"] == str(dead)

    def test_read_file_merged_worktree_no_match_reports_missing(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """死路径在 state 无 worktree 命中 → 如实报文件不存在。"""
        dead = tmp_path / "x__wt_deadbeef" / "gone.txt"
        _inject_workspace_path(server, str(dead.parent))
        ws_mod = sys.modules["workspace_service"]
        monkeypatch.setattr(ws_mod, "_state_reader", lambda: [])
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/file-content",
                method="GET",
                query={"path": str(dead)},
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
# 4. 宿主原生直开链路（open-file / open，用户裁定 2026-10-08）
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

    def test_open_file_host_success(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既有文件 → os.startfile 收到该路径（额外字段 line/column 不碍事）。"""
        f = tmp_path / "a.py"
        f.write_text("x", encoding="utf-8")
        opened: list[str] = []
        # 钉 win32 分支（与下方 posix 回退测试同构）：POSIX 宿主无 os.startfile，
        # 不钉则落 webbrowser 回退分支，断言对象落空
        monkeypatch.setattr(server.sys, "platform", "win32")
        monkeypatch.setattr(
            server.os, "startfile", _startfile_recorder(opened), raising=False
        )
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body=json.dumps({"file_path": str(f), "line": 3, "column": 5}),
            )
        )
        assert status == 200
        assert body["success"] is True
        assert opened == [str(f.resolve())]
        assert body["file_path"] == str(f)

    def test_open_file_missing_target_reports_failure(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """路径不存在 → 公开面如实失败，且不发起 OS 调用。"""
        opened: list[str] = []
        monkeypatch.setattr(
            server.os, "startfile", _startfile_recorder(opened), raising=False
        )
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body=json.dumps({"file_path": str(tmp_path / "ghost.py")}),
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "打开文件失败" in body["message"]
        assert opened == []

    def test_open_file_os_failure_reports_failure(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OS 调用异常 → 公开面如实失败（不吞异常、不谎报成功）。"""
        f = tmp_path / "b.py"
        f.write_text("x", encoding="utf-8")

        def _boom(p: str) -> None:
            raise OSError("no association")

        # 钉 win32 分支：OS 调用失败面按 startfile 路径断言（POSIX 落 webbrowser）
        monkeypatch.setattr(server.sys, "platform", "win32")
        monkeypatch.setattr(server.os, "startfile", _boom, raising=False)
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body=json.dumps({"file_path": str(f)}),
            )
        )
        assert status == 200
        assert body["success"] is False
        assert "打开文件失败" in body["message"]

    def test_open_file_posix_falls_back_to_webbrowser(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 Windows 平台 → webbrowser.open 收到 file URI。"""
        f = tmp_path / "c.py"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(server.sys, "platform", "linux")
        opened: list[str] = []
        monkeypatch.setattr(
            server.webbrowser, "open", _webbrowser_recorder(opened)
        )
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/open-file",
                method="POST",
                raw_body=json.dumps({"file_path": str(f)}),
            )
        )
        assert status == 200
        assert body["success"] is True
        assert opened == [f.resolve().as_uri()]


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

    def test_open_workspace_host_success_windows(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows 分支：os.startfile 收到工作空间路径 → success=True。"""
        _inject_workspace_path(server, ws_dir)
        opened: list[str] = []
        monkeypatch.setattr(server.sys, "platform", "win32")
        monkeypatch.setattr(
            server.os, "startfile", _startfile_recorder(opened), raising=False
        )
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is True
        assert opened == [str(Path(ws_dir).resolve())]
        assert body["path"] == ws_dir

    def test_open_workspace_host_success_posix(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 Windows 分支：webbrowser.open 收到 file URI → success=True。"""
        _inject_workspace_path(server, ws_dir)
        monkeypatch.setattr(server.sys, "platform", "linux")
        opened: list[str] = []
        monkeypatch.setattr(
            server.webbrowser, "open", _webbrowser_recorder(opened)
        )
        status, body = _decode_http(
            _call(
                server,
                path="/ext/workspace_service/workspaces/t1/open",
                method="POST",
            )
        )
        assert status == 200
        assert body["success"] is True
        assert opened == [Path(ws_dir).as_uri()]

    def test_open_workspace_os_failure_reports_failure(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OS 调用异常 → 公开面如实失败（不假成功）。"""
        _inject_workspace_path(server, ws_dir)

        def boom(p: str) -> None:
            raise OSError("no file manager")

        # 钉 win32 分支（与上方 host_success_windows 同构）：失败面按 startfile 路径断言
        monkeypatch.setattr(server.sys, "platform", "win32")
        monkeypatch.setattr(server.os, "startfile", boom, raising=False)
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

    def test_open_workspace_missing_dir_reports_failure(self, server: Any) -> None:
        """坐标在目录不在 → /open 公开面如实失败，不假成功。"""
        _inject_workspace_path(server, "/no/such/dir/anywhere")
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

    def test_resolve_workspace_path_from_state_ws_meta(self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """state 通道经公开 HTTP 面解析：行内 task.ws_meta（归属一致）→ 文件树
        落在该目录（任务行"任务 init 即写"契约的唯一读面）。"""
        ws_mod = sys.modules["workspace_service"]
        monkeypatch.setattr(
            ws_mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": "c1",
                    "task.id": "c1",
                    "task.submitted_by": "u-1",
                    "task.ws_meta": {"mode": "plain", "path": ws_dir},
                }
            ],
            raising=False,
        )
        (Path(ws_dir) / "meta.txt").write_text("x", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/c1/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert {n["name"] for n in body["entries"]} == {"meta.txt"}

    def test_resolve_workspace_path_task_missing(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """state 无该任务行（行在场但不匹配）→ 解析为无工作空间
        （公开面 no_workspace，不误报坐标）。"""
        ws_mod = sys.modules["workspace_service"]
        monkeypatch.setattr(
            ws_mod, "_state_reader", lambda: [{"pipeline_id": "other"}], raising=False
        )
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/ghost/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["workspace_status"] == "no_workspace"

    def test_resolve_workspace_path_reader_not_injected(self, server: Any) -> None:
        """state 读面未注入（能力未授予）→ 解析为无工作空间（无坐标，无兜底）。"""
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/c1/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["workspace_status"] == "no_workspace"

    def test_mirror_channel_retired_state_absent_no_workspace(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """镜像通道删除回归：task_service 镜像有任务（含 ws_meta/user_id）而
        state 无行 → no_workspace，不再有镜像兜底（双写歧路退役）。"""
        task_svc = _FakeTaskService()
        task_svc.tasks["c1"] = _FakeTask(
            "c1", metadata={"user_id": "u-1", "ws_meta": {"path": ws_dir}}
        )
        _inject_task_service(task_svc)
        ws_mod = sys.modules["workspace_service"]
        monkeypatch.setattr(ws_mod, "_state_reader", lambda: [], raising=False)
        (Path(ws_dir) / "mirror.txt").write_text("x", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/c1/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["workspace_status"] == "no_workspace"
        assert body["entries"] == []

    def test_resolve_workspace_path_local(self, server: Any) -> None:
        """_local → 仓库根：公开面可读取仓库根标志文件 AGENTS.md（内容非空）。"""
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/_local/file-content",
            method="GET",
            query={"path": "AGENTS.md"},
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["success"] is True
        assert body["content"]

    def test_resolve_workspace_path_state_reader_error_no_workspace(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """state 读面抛异常 → 公开面降级为 no_workspace（不 500，无兜底）。"""
        ws_mod = sys.modules["workspace_service"]

        def boom() -> list[dict[str, Any]]:
            raise RuntimeError("state read broke")

        monkeypatch.setattr(ws_mod, "_state_reader", boom, raising=False)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/c1/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["workspace_status"] == "no_workspace"


# ═══════════════════════════════════════════════════════════
# 5.5 归属闸（U6）：四通道统一"请求方与目标任务归属"校验
# ═══════════════════════════════════════════════════════════


class TestWorkspaceOwnershipGate:
    """工作区解析归属闸（U6）：请求方与目标任务归属不一致 → 404
    （防存在性探测）；任务域目标缺归属元数据 fail-closed 拒绝并说明原因（存量
    回填属运行时操作）；caller 未认证一律拒绝。各入口归属权威字段：
    - _local 显式入口：项目根本地工作区（部署级共享，无单任务归属）→ 要求认证；
    - 项目登记显式入口：ProjectModel.submitted_by；
    - state 聚合行（任务/会话管道 id 唯一通道）：任务行（task.id /
      task.submitted_by / task.ws_meta 任一在场）按 task.submitted_by
      （task_submit 出生协议写全）闸；会话域行（无任何任务身份标记）坐标即
      服务端 state 真值，认证 caller 可达；task_service 镜像通道已删。
    """

    def _seed_state_rows(self, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
        ws_mod = sys.modules["workspace_service"]
        monkeypatch.setattr(ws_mod, "_state_reader", lambda: rows, raising=False)

    def test_anonymous_denied_404(self, server: Any) -> None:
        """无/无效 token → 404（无法确立归属一致性，fail-closed，不泄露存在性）。"""
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/_local/file-content",
            method="GET",
            query={"path": "AGENTS.md"},
        ))
        assert status == 404
        forged = base64.b64encode(b"access:u-1:x:1").decode("ascii").rstrip("=")
        status, _ = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/_local/file-content",
            method="GET",
            query={"path": "AGENTS.md"},
            headers={"Authorization": f"Bearer {forged}"},
        ))
        assert status == 404

    def test_local_authenticated_allowed(self, server: Any) -> None:
        """_local（部署级共享项目根）：认证用户放行。"""
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/_local/file-content",
            method="GET",
            query={"path": "AGENTS.md"},
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["success"] is True

    # ── state 聚合行通道 ──

    def test_state_channel_owner_allowed_other_denied(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户 A 的任务工作空间：A 读 200，B 读 404 且与"无工作区"不可达语义
        分离（404 响应体不携带工作区数据）。"""
        self._seed_state_rows(monkeypatch, [
            {"pipeline_id": "t1", "task.submitted_by": "u-1",
             "task.ws_meta": {"mode": "plain", "path": ws_dir}},
        ])
        (Path(ws_dir) / "note.md").write_text("hello", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": "note.md"},
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["content"] == "hello"
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": "note.md"},
            headers=_auth("u-2"),
        ))
        assert status == 404
        assert "content" not in body

    def test_state_channel_missing_owner_fail_closed(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """任务行（有任务身份）缺 task.submitted_by（存量行）→ 404，
        错误信息说明归属元数据缺失原因（不做放行式降级）。"""
        self._seed_state_rows(monkeypatch, [
            {"pipeline_id": "t1", "task.id": "t1",
             "ws_meta": {"mode": "plain", "path": ws_dir}},
        ])
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 404
        assert "归属" in json.dumps(body, ensure_ascii=False)

    def test_session_domain_pipeline_resolves_for_authenticated_caller(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BUG-24：会话域 pipeline 行（无任务身份，坐标 = 服务端 state 真值）
        认证 caller 打开文件树可达，不得按"任务缺归属元数据"误拒 404。"""
        self._seed_state_rows(monkeypatch, [
            {"pipeline_id": "f36831f2ee89", "thread_id": "thread-abc",
             "ws_meta": {"mode": "plain", "path": ws_dir, "session_id": "thread-abc"},
             "workspace": ws_dir},
        ])
        (Path(ws_dir) / "session_file.txt").write_text("s", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/f36831f2ee89/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        names = json.dumps(body, ensure_ascii=False)
        assert "session_file.txt" in names

    def test_session_domain_row_with_task_ws_meta_still_gated(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：行带任务域镜像 task.ws_meta（任务行）但缺 submitted_by →
        仍 fail-closed 404（归属闸不因会话域放行而松动）。"""
        self._seed_state_rows(monkeypatch, [
            {"pipeline_id": "t1",
             "task.ws_meta": {"mode": "plain", "path": ws_dir}},
        ])
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 404
        assert "归属" in json.dumps(body, ensure_ascii=False)

    def test_session_domain_pipeline_anonymous_still_denied(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """会话域放行不豁免认证：无 token 一律 404（安全边界不变）。"""
        self._seed_state_rows(monkeypatch, [
            {"pipeline_id": "f36831f2ee89",
             "ws_meta": {"mode": "plain", "path": ws_dir, "session_id": "thread-abc"}},
        ])
        status, _ = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/f36831f2ee89/file-tree",
            method="GET",
        ))
        assert status == 404

    # ── 项目登记通道 ──

    def test_project_channel_owner_allowed_other_denied(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """项目登记行归属 = submitted_by：创建者放行，其他用户 404。"""
        _inject_project_registry(
            monkeypatch, server, paths={"proj-1": ws_dir}, submitted_by={"proj-1": "u-1"}
        )
        (Path(ws_dir) / "note.md").write_text("proj", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/proj-1/file-content",
            method="GET",
            query={"path": "note.md"},
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["content"] == "proj"
        status, _ = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/proj-1/file-content",
            method="GET",
            query={"path": "note.md"},
            headers=_auth("u-2"),
        ))
        assert status == 404

    def test_project_channel_missing_owner_fail_closed(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """登记行 submitted_by 为空（存量登记）→ fail-closed 404 并说明原因。"""
        _inject_project_registry(
            monkeypatch, server, paths={"proj-legacy": ws_dir}, submitted_by={"proj-legacy": ""}
        )
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/proj-legacy/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 404
        assert "归属" in json.dumps(body, ensure_ascii=False)

    # ── 镜像通道退役（task_service 镜像删除回归） ──

    def test_mirror_owner_no_longer_grants_access(
        self, server: Any, ws_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """镜像通道删除回归：state 无行时，镜像归属一致（metadata.user_id 命中）
        也不再放行——坐标以 state 为唯一真相源，file-tree 如实 no_workspace。"""
        task_svc = _FakeTaskService()
        task_svc.tasks["c1"] = _FakeTask(
            "c1", metadata={"user_id": "u-1", "ws_meta": {"path": ws_dir}}
        )
        _inject_task_service(task_svc)
        self._seed_state_rows(monkeypatch, [])
        (Path(ws_dir) / "mirror.md").write_text("mirror", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/c1/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["workspace_status"] == "no_workspace"
        assert body["entries"] == []

    def test_unknown_id_no_channel_hit_still_no_workspace(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """state 行未命中维持既有 None 语义（无工作区坐标），不做 404 误报。"""
        self._seed_state_rows(monkeypatch, [])
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/ghost/file-tree",
            method="GET",
            headers=_auth("u-1"),
        ))
        assert status == 200
        assert body["workspace_status"] == "no_workspace"


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

        # plugin._capabilities 是内核能力句柄的注入槽（外部依赖，无公开注入 API），
        # 用假句柄隔离内核；注入后是否生效由下方 calls 断言承载。
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
        # 公开面：加载后单例稳定可用
        svc = server.get_workspace_service()
        assert svc is server.get_workspace_service()
        # 卸载后工作空间工具回到未初始化行为（服务不再可用）
        _run(server._on_unload({}))
        assert _run(server.workspace_get_or_create("t1"))["success"] is False


class TestToolsStillRegistered:
    def test_workspace_tool_functions_kept(self, server: Any) -> None:
        # plugin._tools 是 SDK 无公开读面的内核握手注册表；此断言锁定声明
        # 不因重构丢失（工具功能本身由各 workspace_* 直调用例覆盖）。
        assert "workspace.get_or_create" in server.plugin._tools
        assert "workspace.get" in server.plugin._tools
        assert "workspace.get_file_tree" in server.plugin._tools
        assert "http.handle" in server.plugin._tools

    def test_workspace_get_tool_uninitialized(self, server: Any) -> None:
        # 全新加载的 server 尚未 on_load，服务天然未初始化
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

    def test_on_load_no_capability_keeps_legacy_reader(self, server: Any, tmp_path: Path) -> None:
        """on_load 后未注入读面：合并 worktree 死路径不做 state 重定位
        （legacy None 读面语义——重定位只会由已注入的读面驱动）。"""
        _run(server._on_load({}))
        dead = tmp_path / "x__wt_deadbeef" / "gone.txt"
        _inject_workspace_path(server, str(dead.parent))
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": str(dead)},
        ))
        assert status == 200
        assert body["success"] is False
        assert "文件不存在" in body["message"]


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
        """读面不设大小上限（用户裁定 2026-10-24：打开面不限，写面仍有 10MB）。"""
        _inject_workspace_path(server, ws_dir)
        huge = Path(ws_dir) / "huge.txt"
        payload = "x" * (10 * 1024 * 1024 + 1)
        huge.write_text(payload, encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": "huge.txt"},
        ))
        assert status == 200
        assert body["success"] is True, "读面无大小上限（打开面不限）"
        assert body["size"] == len(payload)
        assert body["has_more"] is False

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
        """无工作空间 + 相对路径越出项目根 → 锚定定位不拒绝（读面无越界守卫）；
        目标不存在 → 按文件缺失如实上报，不再是「超出工作空间范围」。"""
        _inject_workspace_path(server, None)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/_missing/file-content",
            method="GET",
            query={"path": "../escape.txt"},
        ))
        assert status == 200
        assert body["success"] is False
        assert "文件不存在" in body["message"], "读面不做越界拒绝，缺失才报错"
        assert "超出工作空间范围" not in body["message"]


class TestSiblingDirectoryBypass:
    """兄弟目录前缀绕过（路径守卫必须带分隔符）。

    workspace=…/ws 时 …/ws-backup 以字符串前缀命中 startswith 守卫，
    读写删会越出工作空间边界（真实文件系统验证副作用不发生）。
    """

    @pytest.fixture
    def sibling_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "ws-backup"
        d.mkdir()
        return d

    def test_save_file_sibling_dir_denied(
        self, server: Any, ws_dir: str, sibling_dir: Path
    ) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="PUT",
            query={"path": "../ws-backup/evil.md"},
            raw_body='{"content": "pwned"}',
        ))
        assert status == 200
        assert body["success"] is False
        assert not (sibling_dir / "evil.md").exists()

    def test_delete_entry_sibling_dir_denied(
        self, server: Any, ws_dir: str, sibling_dir: Path
    ) -> None:
        """越界 rmtree 是 U2 最重后果：兄弟目录内文件删除后必须原样存活。"""
        _inject_workspace_path(server, ws_dir)
        (sibling_dir / "keep.txt").write_text("data", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/entries",
            method="DELETE",
            query={"path": "../ws-backup"},
        ))
        assert status == 200
        assert body["success"] is False
        assert sibling_dir.is_dir()
        assert (sibling_dir / "keep.txt").read_text(encoding="utf-8") == "data"

    def test_create_entry_sibling_dir_denied(
        self, server: Any, ws_dir: str, sibling_dir: Path
    ) -> None:
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/create-entry",
            method="POST",
            raw_body='{"path": "../ws-backup/new.py", "type": "file"}',
        ))
        assert status == 200
        assert body["success"] is False
        assert not (sibling_dir / "new.py").exists()

    def test_move_entry_dest_sibling_dir_denied(
        self, server: Any, ws_dir: str, sibling_dir: Path
    ) -> None:
        _inject_workspace_path(server, ws_dir)
        (Path(ws_dir) / "m.py").write_text("x", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/move-entry",
            method="POST",
            raw_body='{"source_path": "m.py", "destination_dir": "../ws-backup"}',
        ))
        assert status == 200
        assert body["success"] is False
        assert (Path(ws_dir) / "m.py").exists()
        assert not (sibling_dir / "m.py").exists()

    def test_validate_path_in_workspace_sibling_is_none(
        self, server: Any, ws_dir: str, sibling_dir: Path
    ) -> None:
        """守卫公开面：兄弟目录路径拒绝且无副作用，界内路径放行
        （create/delete/rename/move 各操作面已分别覆盖同一守卫）。"""
        _inject_workspace_path(server, ws_dir)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="PUT",
            query={"path": "../ws-backup/x.txt"},
            raw_body='{"content": "pwned"}',
        ))
        assert status == 200
        assert body["success"] is False
        assert not (sibling_dir / "x.txt").exists()
        # 边界对照：工作空间内部路径仍放行
        status2, body2 = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="PUT",
            query={"path": "inside.txt"},
            raw_body='{"content": "ok"}',
        ))
        assert status2 == 200
        assert body2["success"] is True
        assert (Path(ws_dir) / "inside.txt").read_text(encoding="utf-8") == "ok"


class TestAbsolutePathBoundary:
    """绝对路径直读（用户裁定 2026-09-28 + 2026-10-24 延伸）：路径自足即直读，
    不受工作空间/项目根边界拦截——本端点消费方是经内核 token 鉴权的前端用户
    打开面（与本机编辑器打开文件同权）；agent 读链走 fs_tools 自有守卫不经此
    端点。写面端点维持相对路径 + 锚定守卫不变。
    """

    def test_read_absolute_kernel_db_direct_read(
        self, server: Any, ws_dir: str, tmp_path: Path
    ) -> None:
        """agentos_kernel.db 形态的宿主文件绝对路径直读（打开面同权，无读黑名单）。

        库文件自建（tmp_path）：agentos_kernel.db 是 gitignored 运行时产物，
        依赖项目根恰有真库会让测试绑定开发机状态；被测契约是「任意宿主绝对
        路径可直读」，与文件是否真为内核库无关，文件名保留库形态以钉场景。
        """
        _inject_workspace_path(server, ws_dir)
        db_path = tmp_path / "agentos_kernel.db"
        payload = b"SQLite format 3\x00" + b"\x00" * 24
        db_path.write_bytes(payload)
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": str(db_path)},
        ))
        assert status == 200
        assert body["success"] is True, "绝对路径直读：内核库与用户本机文件同权"
        assert body["size"] == len(payload)

    def test_read_absolute_sibling_file_allowed(self, server: Any, ws_dir: str, tmp_path: Path) -> None:
        """工作空间外普通文件绝对路径读 = 放行（读黑名单制默认全放，
        ADR 2026-09-24 zone-rw；黑名单路径另由黑名单系测试覆盖）。"""
        _inject_workspace_path(server, ws_dir)
        outside = tmp_path / "outside-ws.txt"
        outside.write_text("secret", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": str(outside)},
        ))
        assert status == 200
        assert body["success"] is True
        assert body.get("content", "").endswith("secret")

    def test_read_absolute_path_inside_workspace_allowed(self, server: Any, ws_dir: str) -> None:
        _inject_workspace_path(server, ws_dir)
        target = Path(ws_dir) / "abs-in-ws.txt"
        target.write_text("in-boundary", encoding="utf-8")
        status, body = _decode_http(_call(
            server,
            path="/ext/workspace_service/workspaces/t1/file-content",
            method="GET",
            query={"path": str(target)},
        ))
        assert status == 200
        assert body["success"] is True
        assert body["content"] == "in-boundary"


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


class TestWorkspaceToolsSuccess:
    def test_get_or_create_tool_success(self, server: Any) -> None:
        _run(server._on_load({}))
        result = _run(server.workspace_get_or_create("t1", session_id="s1", title="标题"))
        assert result["success"] is True
        assert result["workspace"]["container_task_id"] == "t1"

    def test_get_or_create_tool_uninitialized(self, server: Any) -> None:
        # 全新加载的 server 尚未 on_load，服务天然未初始化
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
        assert {n["name"] for n in result["entries"]} == {"f.txt"}

    def test_get_file_tree_tool_uninitialized(self, server: Any) -> None:
        # 全新加载的 server 尚未 on_load，服务天然未初始化
        result = _run(server.workspace_get_file_tree("t1"))
        assert result["success"] is False
