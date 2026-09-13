# @feature: FP-0.2.二 tasks http api 缺口补测 | @ci: python-coverage
"""tasks http_api 剩余缺口分支补充覆盖（行号锚定 2026-09-13 插桩车道）。

主用例见 test_tasks_http_api.py；本文件补：

1. ``_capability``：实例缺席（__main__ 无 plugin）→ KeyError 契约；
2. ``_read_task_status``：投影行缺失 / 读面调用失败 → None（仲裁降级放行）；
3. ``list_tasks``：priority 筛选参数通路；
4. ``_list_tasks_from_state``：响应非数组 → None、非 dict 行两趟皆跳过、
   无 task.* 键行跳过、owned 键缺第二段跳过；
5. ``_invoke_task_submit_tool``：工具调用 RuntimeError → 500 TOOL_INVOKE_FAILED、
   返回非 dict → 400 TASK_SUBMIT_REJECTED 异常形态；
6. ``create_root_task``：inherit 字段透传工具入参；
7. ``_project_child_pids``：响应非数组 → []；
8. ``list_projects``：session_id 筛选；
9. ``create_project``：文件夹创建失败（父路径为文件）→ 400 PROJECT_FOLDER_FAILED；
10. ``get_project``：子任务摘要非 dict 行跳过 / 读面失败降级空摘要；
11. 路由未命中：projects 未知 action / tasks 域非"/"前缀 sub / 单段 task_id
    未匹配 method → 整体 404。

mock 仅限外部依赖（内核能力句柄故障注入）；登记仓走真实 ProjectRegistry。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "tasks"


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出同名裸模块并注入本插件目录（与 test_tasks_http_api.py 同款隔离）。"""
    d = str(_PLUGIN_DIR)
    shared_root = str(_PLUGIN_DIR.parents[1])
    _was_present = d in sys.path
    _added_shared_root = False
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    if shared_root not in sys.path:
        sys.path.insert(1, shared_root)
        _added_shared_root = True
    _evict_names = (
        "task_types",
        "state_machine",
        "storage",
        "service",
        "timer_manager",
        "agents_types",
        "enum_utils",
        "workspace",
        "service_access",
        "project_registry",
        "_task_cleanup",
        "_task_crud",
        "_task_state",
        "http_api",
        "server",
        "state_fields",
    )
    _evicted: dict[str, object] = {}
    for m in _evict_names:
        if m in sys.modules:
            _evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if _was_present:
        sys.path.insert(0, d)
    if _added_shared_root:
        sys.path.remove(shared_root)
    for m in _evict_names:
        if m in _evicted:
            sys.modules[m] = _evicted[m]
        else:
            sys.modules.pop(m, None)


# ═══════════════════════════════════════════════════════════
# 脚手架
# ═══════════════════════════════════════════════════════════


class _FakeCapability:
    """fake 内核能力句柄：按 method 返回预置响应；exc 注入时抛出该异常。"""

    def __init__(self, responses: dict[str, Any], exc: Exception | None = None) -> None:
        self._responses = responses
        self._exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        if self._exc is not None:
            raise self._exc
        if method not in self._responses:
            raise KeyError(f"unexpected capability method: {method}")
        return self._responses[method]


class _FakeCapabilityHub:
    """fake http_api._capability 入口：按能力名返回 fake 句柄。"""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self._responses = responses or {}
        self.handles: dict[str, _FakeCapability] = {}
        self.excs: dict[str, Exception] = {}

    def set_exc(self, name: str, exc: Exception) -> None:
        self.excs[name] = exc

    def get(self, name: str) -> _FakeCapability:
        if name not in self.handles:
            self.handles[name] = _FakeCapability(self._responses.get(name, {}), self.excs.get(name))
        return self.handles[name]


def _seed_state(hub: _FakeCapabilityHub, rows: Any) -> None:
    hub._responses["pipeline-state"] = {"list": rows}


@pytest.fixture
def hub() -> _FakeCapabilityHub:
    return _FakeCapabilityHub()


def _install(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
    import http_api

    monkeypatch.setattr(http_api, "_capability", hub.get)


def _make_token(user_id: str, username: str = "tester") -> str:
    raw = f"access:{user_id}:{username}:9999999999"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


async def _http(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
                path: str, method: str = "GET", body: dict[str, Any] | None = None,
                query: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    import http_api

    _install(monkeypatch, hub)
    raw_body = json.dumps(body) if body is not None else ""
    result = await http_api.handle_http(path, method, raw_body, query, headers)
    assert result.get("success") is True, f"handle_http 失败: {result}"
    data = result["data"]
    payload = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return {"status": data["status"], "payload": payload}


def _tool_ok(task_id: str, **extra: Any) -> dict[str, Any]:
    return {"success": True, "output": {"task_id": task_id, "pipeline_id": task_id, **extra}}


# ═══════════════════════════════════════════════════════════
# _capability 契约
# ═══════════════════════════════════════════════════════════


class TestCapabilityContract:
    def test_missing_instance_raises_keyerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """实例缺席（全局 None 且 __main__ 无 plugin）→ KeyError（不静默）。"""
        import http_api

        monkeypatch.setattr(http_api, "_plugin_instance", None)
        with pytest.raises(KeyError, match="capability not injected"):
            http_api._capability("pipeline-state")

    def test_set_plugin_instance_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """set_plugin_instance 注入后 _capability 走实例面；get_capability
        故障仍翻译为 KeyError 契约；传 None 清除回独占探测路径。"""
        import http_api

        original = http_api._plugin_instance
        sentinel = object()
        ok_instance = type("_Inst", (), {"get_capability": lambda self, name: sentinel})()
        try:
            http_api.set_plugin_instance(ok_instance)
            assert http_api._capability("pipeline-state") is sentinel

            bad_instance = type(
                "_Bad", (), {"get_capability": lambda self, name: 1 / 0}
            )()
            http_api.set_plugin_instance(bad_instance)
            with pytest.raises(KeyError, match="capability not injected"):
                http_api._capability("pipeline-state")

            http_api.set_plugin_instance(None)
            assert http_api._plugin_instance is None
        finally:
            http_api.set_plugin_instance(original)


# ═══════════════════════════════════════════════════════════
# _read_task_status / _list_tasks_from_state 读面
# ═══════════════════════════════════════════════════════════


class TestStateReadFaces:
    async def test_read_status_missing_row_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, [{"pipeline_id": "other", "task.status": "running"}])
        assert await http_api._read_task_status("t-miss") is None

    async def test_read_status_non_list_rows_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, {"unexpected": "shape"})
        assert await http_api._read_task_status("t1") is None

    async def test_read_status_call_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """投影读面调用失败 → None（恢复仲裁降级放行）。"""
        import http_api

        hub.set_exc("pipeline-state", RuntimeError("state bridge down"))
        _install(monkeypatch, hub)
        assert await http_api._read_task_status("t1") is None

    async def test_state_rows_non_list_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, {"unexpected": "shape"})
        assert await http_api._list_tasks_from_state() is None

    async def test_non_dict_rows_skipped_in_both_passes(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """非 dict 行在任务行趟与 owned 趟均跳过，不崩不误读。"""
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, [
            "garbage-string",
            {"pipeline_id": "p1", "task.goal": "g", "task.status": "running"},
        ])
        out = await http_api._list_tasks_from_state()
        assert [t["id"] for t in out] == ["p1"]

    async def test_row_without_task_keys_skipped(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, [{"pipeline_id": "p-no-task"}])
        assert await http_api._list_tasks_from_state() == []

    async def test_row_with_task_keys_but_empty_pid_skipped(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, [{"pipeline_id": "", "task.goal": "g"}])
        assert await http_api._list_tasks_from_state() == []

    async def test_owned_pid_dedup_against_state_rows(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """owned 声明与 state 行同 id：以 state 行为准，owned 趟去重跳过。"""
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, [
            {"pipeline_id": "p1", "task.goal": "state 行", "task.status": "running"},
            {"pipeline_id": "host", "task.owned.p1.title": "重复声明"},
        ])
        out = await http_api._list_tasks_from_state()
        assert [t["id"] for t in out] == ["p1"]
        assert out[0]["title"] == "state 行"

    async def test_owned_key_with_empty_id_skipped(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """``task.owned..title``（id 段为空）不成任务。"""
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, [{"pipeline_id": "host", "task.owned..title": "空 id"}])
        assert await http_api._list_tasks_from_state() == []

    async def test_owned_key_without_field_segment_skipped(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """``task.owned.<id>``（缺 .field 段）不成任务，完整 owned 键照常。"""
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, [{
            "pipeline_id": "host",
            "task.owned.bad": "no-field-segment",
            "task.owned.c1.title": "容器任务",
            "task.owned.c1.status": "active",
        }])
        out = await http_api._list_tasks_from_state()
        assert [t["id"] for t in out] == ["c1"]

    async def test_list_tasks_priority_filter(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """priority 查询参数通路：state 行无该字段时全部滤除（空页不误报）。"""
        _install(monkeypatch, hub)
        _seed_state(hub, [{"pipeline_id": "p1", "task.goal": "g", "task.status": "running"}])
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks", query={"priority": "3"})
        assert resp["status"] == 200
        assert resp["payload"]["total"] == 0


# ═══════════════════════════════════════════════════════════
# _invoke_task_submit_tool 故障族
# ═══════════════════════════════════════════════════════════


class TestInvokeTaskSubmitFailures:
    async def test_runtime_error_translates_to_500(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """工具调用 RuntimeError → APIError 500 TOOL_INVOKE_FAILED。"""
        headers = {"Authorization": f"Bearer {_make_token('u-1')}"}
        hub.set_exc("tool-executor", RuntimeError("executor died"))
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "t", "agent_id": "a1"}, headers=headers)
        assert resp["status"] == 500
        assert "task_submit 工具调用失败" in resp["payload"]["detail"]
        assert "executor died" in resp["payload"]["detail"]

    async def test_non_dict_tool_result_rejected(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """工具返回非 dict（异常形态）→ 400 TASK_SUBMIT_REJECTED。"""
        headers = {"Authorization": f"Bearer {_make_token('u-1')}"}
        hub._responses["tool-executor"] = {"invoke": "oops-not-a-dict"}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks", "POST",
                           body={"title": "t", "agent_id": "a1"}, headers=headers)
        assert resp["status"] == 400
        assert "异常形态" in resp["payload"]["detail"]

    async def test_root_task_inherit_passthrough(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """根任务 inherit 字段原样透传 task_submit 工具入参。"""
        headers = {"Authorization": f"Bearer {_make_token('u-1')}"}
        hub._responses["tool-executor"] = {"invoke": _tool_ok("rt1")}
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/root", "POST",
                           body={"title": "根任务", "target_id": "a1",
                                 "thread_id": "th1", "inherit": {"persona": "x"}},
                           headers=headers)
        assert resp["status"] == 200
        invoke_params = hub.handles["tool-executor"].calls[0][1]
        assert invoke_params["args"]["inherit"] == {"persona": "x"}

    async def test_submit_with_chat_capability_missing_is_500(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """chat 能力缺席时提交任务 → TaskBirthError → 500（fail-closed 不静默）。"""
        import http_api

        _seed_state(hub, [{"pipeline_id": "pipe-1", "task.goal": "g",
                           "task.status": "pending"}])

        def _selective(name: str) -> Any:
            if name == "chat":
                raise KeyError("capability not injected: chat")
            return hub.get(name)

        monkeypatch.setattr(http_api, "_capability", _selective)
        result = await http_api.handle_http(
            "/ext/task_service/tasks/pipe-1/submit", "POST", "", None,
            {"Authorization": f"Bearer {_make_token('u-1')}"},
        )
        data = result["data"]
        payload = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
        assert data["status"] == 500
        assert "chat capability 未注入" in payload["detail"]


# ═══════════════════════════════════════════════════════════
# projects 域缺口
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    import project_registry as projects_mod

    reg = projects_mod.ProjectRegistry(data_dir=tmp_path / "tasks")
    monkeypatch.setattr(
        projects_mod,
        "load_project_paths",
        lambda: {p.id: p.path for p in reg.list()},
    )
    import http_api

    monkeypatch.setattr(http_api, "get_project_registry", lambda: reg)
    return reg


class TestProjectsGaps:
    async def test_child_pids_non_list_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        import http_api

        _install(monkeypatch, hub)
        _seed_state(hub, {"bad": "shape"})
        assert await http_api._project_child_pids("p1") == []

    async def test_list_projects_session_filter(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub, registry: Any
    ) -> None:
        import project_registry as projects_mod

        registry.save(projects_mod.ProjectModel(title="会话内", session_id="sess-1"))
        registry.save(projects_mod.ProjectModel(title="别的会话", session_id="sess-2"))
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects",
                           query={"session_id": "sess-1"})
        assert resp["payload"]["total"] == 1
        assert resp["payload"]["items"][0]["goal"] == "会话内"

    async def test_create_project_folder_failure_is_400(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub,
        registry: Any, tmp_path: Path
    ) -> None:
        """显式路径父级是文件（文件夹建不出）→ 400 PROJECT_FOLDER_FAILED。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        resp = await _http(monkeypatch, hub, "/ext/task_service/projects", "POST",
                           body={"goal": "目标", "path": str(blocker / "child")})
        assert resp["status"] == 400
        assert "项目文件夹创建失败" in resp["payload"]["detail"]

    async def test_get_project_non_dict_row_skipped(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub, registry: Any
    ) -> None:
        import project_registry as projects_mod

        p = registry.save(projects_mod.ProjectModel(title="详情项目"))
        _seed_state(hub, [
            "bad-row",
            {"pipeline_id": "c1", "task.parent_project_id": p.id,
             "task.goal": "子任务", "task.status": "running"},
        ])
        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{p.id}")
        assert resp["status"] == 200
        children = resp["payload"]["project"]["tasks"]
        assert [c["id"] for c in children] == ["c1"]

    async def test_get_project_read_failure_degrades_to_empty_summary(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub, registry: Any
    ) -> None:
        import project_registry as projects_mod

        p = registry.save(projects_mod.ProjectModel(title="详情项目"))
        hub.set_exc("pipeline-state", RuntimeError("down"))
        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{p.id}")
        assert resp["status"] == 200
        assert resp["payload"]["project"]["tasks"] == []

    async def test_unknown_project_action_is_404(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub, registry: Any
    ) -> None:
        import project_registry as projects_mod

        p = registry.save(projects_mod.ProjectModel(title="动作项目"))
        resp = await _http(monkeypatch, hub, f"/ext/task_service/projects/{p.id}/bogus", "POST")
        assert resp["status"] == 404


# ═══════════════════════════════════════════════════════════
# 路由未命中
# ═══════════════════════════════════════════════════════════


class TestRouteFallbacks:
    async def test_tasks_domain_non_slash_sub_is_404(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """tasks 域 sub 非"/"前缀（如 tasksfoo）→ 未命中 404。"""
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasksfoo")
        assert resp["status"] == 404

    async def test_single_segment_task_id_unmatched_method_is_404(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        resp = await _http(monkeypatch, hub, "/ext/task_service/tasks/t1", "PUT")
        assert resp["status"] == 404
