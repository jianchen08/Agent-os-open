# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""tasks/server.py 适配层分支补测（域事件闸门 / 启动调和 / 清理执行链 / HTTP 面）。

行为契约（断输入→输出/副作用，不钉实现）：
- _on_domain_event：非 run 终态事件在能力闸门前忽略；能力句柄缺席 →
  warning 留痕跳过；run 终态事件走真实派生链（events.handle_run_terminal_event：
  载荷 state 派生 task_completed / 未决投影 + run.failed 派生 task_failed 并
  对账补落 task.status，经伪 event-bus 观察发射）
- _on_load：能力句柄在位 → 启动调和扫描真实发生（pipeline-state.list ×
  pipeline-runs.list failed/suspended 两查询）
- task.delete：清理链把 pipeline-executor 句柄接到 suspend_pipeline（伪句柄
  记录调用）
- task.transition：转换成功后任务不可读 → {"ok": False, "error": "Task not
  found"}（伪服务在依赖边界桩定竞态形态）
- http.handle：委托 http_api.handle_http（未路由 path → 404 响应体）

走真实 TaskService（tmp data_dir，YAML 落盘真依赖）；伪对象只桩内核
capability 句柄面（外部依赖）。模块隔离与 test_server_adapter.py 同范式
（裸名逐出 + 代际还原）。

结构性不可达防御分支（如实记录，勿硬凑）：_on_load 的 ``except KeyError``
（set_cleanup_capabilities 降级）——FrontendEmitter.from_plugin 吞掉全部
异常（返回 None），set_cleanup_capabilities 是纯全局赋值，KeyError 无触发路径。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_EVICT_NAMES = (
    "task_types",
    "state_machine",
    "storage",
    "service",
    "timer_manager",
    "agents_types",
    "enum_utils",
    "workspace",
    "service_access",
    "_task_cleanup",
    "_task_crud",
    "_task_state",
    "server",
    "http_api",
    "events",
    "reconcile",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """裸名逐出 + 代际还原（同 test_server_adapter.py，串扰防线）。"""
    d = str(_PLUGIN_DIR)
    was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if was_present:
        sys.path.insert(0, d)
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


def _load_server() -> Any:
    """按显式路径加载 server 模块（唯一模块名隔离同名 server.py）。"""
    mod_name = "tasks_server_gaps_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeCapHandle:
    """伪 capability handle：记录 call 并按表回值（内核能力面的外部依赖桩）。"""

    def __init__(self, name: str, results: dict[str, Any] | None = None) -> None:
        self._name = name
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        if method in self.results:
            value = self.results[method]
            if isinstance(value, Exception):
                raise value
            return value
        return None


def _fake_capability_lookup(handles: dict[str, _FakeCapHandle]):
    def _get_capability(name: str) -> _FakeCapHandle:
        return handles[name]  # 缺名 KeyError = SDK「能力未注入」同语义

    return _get_capability


@pytest.fixture()
def gaps_srv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """加载 server.py，注入伪能力句柄并完成 on_load（启动调和真实执行）。"""
    mod = _load_server()
    handles = {
        "pipeline-executor": _FakeCapHandle("pipeline-executor"),
        "pipeline-state": _FakeCapHandle("pipeline-state", results={"list": []}),
        "service-registry": _FakeCapHandle("service-registry", results={"pipeline-runs.list": []}),
        "event-bus": _FakeCapHandle("event-bus"),
    }
    monkeypatch.setattr(mod.plugin, "get_config", lambda: {"data_dir": str(tmp_path / "tasks")})
    monkeypatch.setattr(mod.plugin, "get_capability", _fake_capability_lookup(handles))
    asyncio.run(mod._on_load({}))
    yield mod, handles
    asyncio.run(mod._on_unload({}))


# ═══════════════════════════════════════════════════════════
# _on_domain_event：域事件派生闸门
# ═══════════════════════════════════════════════════════════


class TestOnDomainEvent:
    @pytest.mark.parametrize(
        "params",
        [
            {"event": "session.created"},
            {"event": "run.started"},
            {},
        ],
    )
    def test_non_run_terminal_events_ignored(self, params: dict) -> None:
        """非 run 终态事件在能力闸门前返回（冷插件无能力，闸门失效即 KeyError）。"""
        mod = _load_server()
        assert asyncio.run(mod._on_domain_event(params)) is None

    def test_missing_capabilities_warns_and_skips(self, caplog) -> None:
        mod = _load_server()
        with caplog.at_level(logging.WARNING):
            assert asyncio.run(mod._on_domain_event({"event": "run.completed"})) is None
        assert any("能力句柄缺席" in r.getMessage() for r in caplog.records)

    def test_run_completed_payload_derives_task_completed(self, monkeypatch) -> None:
        """completed 投影权威：派生 task_completed 发射，且无 task.status 对账写。"""
        mod = _load_server()
        state = _FakeCapHandle("pipeline-state")
        bus = _FakeCapHandle("event-bus")
        monkeypatch.setattr(
            mod.plugin, "get_capability", _fake_capability_lookup({"pipeline-state": state, "event-bus": bus})
        )
        params = {
            "event": "run.completed",
            "pipeline_id": "p-run-1",
            "state": {
                "pipeline_id": "p-run-1",
                "task.id": "t-1",
                "task.status": "completed",
                "task.goal": "写周报",
            },
        }
        asyncio.run(mod._on_domain_event(params))
        assert len(bus.calls) == 1
        method, payload = bus.calls[0]
        assert method == "emit_domain"
        assert payload["event"] == "task_completed"
        assert payload["tags"]["task_id"] == "t-1"
        assert payload["tags"]["pipeline_id"] == "p-run-1"
        assert state.calls == [], "completed 投影权威，不需要对账写回"

    def test_run_failed_undecided_project_derives_and_reconciles(self, monkeypatch) -> None:
        """未决投影 + run.failed → task_failed 派生 + task.status 对账补落。"""
        mod = _load_server()
        state = _FakeCapHandle("pipeline-state")
        bus = _FakeCapHandle("event-bus")
        monkeypatch.setattr(
            mod.plugin, "get_capability", _fake_capability_lookup({"pipeline-state": state, "event-bus": bus})
        )
        params = {
            "event": "run.failed",
            "pipeline_id": "p-run-2",
            "state": {
                "pipeline_id": "p-run-2",
                "task.id": "t-2",
                "task.status": "running",
            },
        }
        asyncio.run(mod._on_domain_event(params))
        assert [p["event"] for _, p in bus.calls] == ["task_failed"]
        assert ("update", {"pipeline_id": "p-run-2", "fields": {"task.status": "failed"}}) in state.calls


# ═══════════════════════════════════════════════════════════
# _on_load：启动调和真实执行（能力句柄在位）
# ═══════════════════════════════════════════════════════════


class TestOnLoadReconcile:
    def test_on_load_scans_state_and_runs_for_reconcile(self, gaps_srv, caplog) -> None:
        mod, handles = gaps_srv
        assert mod._get_service() is not None
        assert ("list", {}) in handles["pipeline-state"].calls
        runs_calls = handles["service-registry"].calls
        assert ("pipeline-runs.list", {"status": "failed", "limit": 500}) in runs_calls
        assert ("pipeline-runs.list", {"status": "suspended", "limit": 500}) in runs_calls
        assert not any("调和能力缺席" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# 清理链：task.delete → pipeline-executor.suspend_pipeline
# ═══════════════════════════════════════════════════════════


class TestDeleteRoutesToPipelineExecutor:
    def test_delete_suspends_pipeline_via_cleanup_chain(self, gaps_srv) -> None:
        mod, handles = gaps_srv
        created = asyncio.run(mod.task_create(title="待删"))
        result = asyncio.run(mod.task_delete(created["id"]))
        assert result["deleted"] is True
        assert handles["pipeline-executor"].calls == [
            ("suspend_pipeline", {"pipeline_id": created["id"]})
        ]


# ═══════════════════════════════════════════════════════════
# task.transition：转换成功后任务不可读（竞态形态的适配层契约）
# ═══════════════════════════════════════════════════════════


class _VanishingTaskService:
    """转换成功但任务随即不可读的服务形态（依赖边界桩，非内部 mock）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def start_task(self, task_id: str) -> None:
        self.calls.append(("start", task_id))

    async def pause_task(self, task_id: str) -> None:
        self.calls.append(("pause", task_id))

    def get_task(self, task_id: str) -> None:
        return None


class TestTransitionAfterVanishedTask:
    @pytest.mark.parametrize("action", ["start", "pause"])
    def test_reports_not_found_after_transition(self, gaps_srv, monkeypatch, action: str) -> None:
        mod, _ = gaps_srv
        fake = _VanishingTaskService()
        monkeypatch.setattr(mod, "_service", fake)
        result = asyncio.run(mod.task_transition("ghost", action))
        assert result == {"ok": False, "error": "Task not found"}
        assert fake.calls == [(action, "ghost")], "转换副作用已发生，回读才失联"


# ═══════════════════════════════════════════════════════════
# http.handle：委托 http_api.handle_http
# ═══════════════════════════════════════════════════════════


def _decode_body(result: dict) -> dict:
    assert result["success"] is True
    data = result["data"]
    assert data["status"] == 404
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


class TestHttpHandleDelegation:
    def test_unknown_path_outside_domains(self, gaps_srv) -> None:
        mod, _ = gaps_srv
        result = asyncio.run(mod.http_handle(path="/no/such/prefix", method="GET"))
        body = _decode_body(result)
        assert body["error"] == "not found"
        assert body["path"] == "/no/such/prefix"

    def test_tasks_domain_unrouted_subpath(self, gaps_srv) -> None:
        mod, _ = gaps_srv
        result = asyncio.run(
            mod.http_handle(
                path="/ext/task_service/definitely/not-a-route",
                method="GET",
                query={"limit": "5"},
                headers={"x-request-id": "r-1"},
            )
        )
        body = _decode_body(result)
        assert body["error"] == "not found"
        assert body["path"] == "/ext/task_service/definitely/not-a-route"
