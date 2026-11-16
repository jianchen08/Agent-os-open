# @feature: FP-0.2.〇 终态随写 | @ci: python-coverage
"""cancel 终态随写（谁终态谁随写——cancel 侧）。

词汇收敛（ADR 2026-10-30-task-status-authority-vocabulary-convergence，
cancelled 任务域退役）后取消终态写 ``stopped``。父管道 owned 镜像已退役
（ADR 2026-11-02-state-key-governance）：``task.owned.<id>.status`` 双写
漂移源全链路退役，任务状态单一真源 = 子管道行 ``task.status``——父会话
面板 join 子行取真值，cancel 随写只落自身投影，父侧零写面。

行为契约（断输入→输出/副作用，不钉实现）：

- API 取消路径：自身投影随写 stopped；有父锚点也不触碰父管道行；
- 事件路径（run.cancelled，内核 run 域事件名保留）：同样只随写自身投影
  （投影未达终态才写，幂等省写）；
- 根任务（无父锚点）取消：不报错、不凭空造 owned 键；
- 父管道写面故障：不影响取消（父侧零写面——若镜像写面回归，故障注入即红）。

外部依赖（内核 capability 句柄）走伪对象；任务域逻辑走真实实现。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tasks"


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出同名裸模块，强制按本插件目录解析（与 test_tasks_http_api.py 同范式）。"""
    d = str(_PLUGIN_DIR)
    shared_root = str(_PLUGIN_DIR.parents[0])
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
        "http_api",
        "events",
        "orchestration",
        "id_utils",
        "workspace",
        "task_birth",
        "state_fields",
        "time_iso",
    )
    evicted: dict[str, Any] = {}
    for name in _evict_names:
        if name in sys.modules:
            evicted[name] = sys.modules.pop(name)
    try:
        yield
    finally:
        for name in _evict_names:
            sys.modules.pop(name, None)
        sys.modules.update(evicted)
        if not _was_present and d in sys.path:
            sys.path.remove(d)
        if _added_shared_root and shared_root in sys.path:
            sys.path.remove(shared_root)


class _FakeCapability:
    """fake 内核能力句柄：按 method 返回预置响应（未预置 → KeyError 语义）。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        if method not in self._responses:
            raise KeyError(f"unexpected capability method: {method}")
        value = self._responses[method]
        if isinstance(value, Exception):
            raise value
        return value


class _FakeCapabilityHub:
    """fake http_api._capability 入口：按能力名返回 fake 句柄。"""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self._responses: dict[str, dict[str, Any]] = responses or {}
        self.handles: dict[str, _FakeCapability] = {}

    def get(self, name: str) -> _FakeCapability:
        if name not in self.handles:
            self.handles[name] = _FakeCapability(self._responses.get(name, {}))
        return self.handles[name]


@pytest.fixture
def hub() -> _FakeCapabilityHub:
    return _FakeCapabilityHub()


def _state_row(pid: str, status: str, **extra: Any) -> dict[str, Any]:
    row = {
        "pipeline_id": pid,
        # task.id 出生协议必写（ADR 2026-11-06 D1：取消面身份门按
        # task.id ∨ lineage.* 判任务管道，根任务靠 task.id 臂命中）
        "task.id": pid,
        "task.goal": f"任务 {pid}",
        "task.status": status,
        "task.submitted_by": "u-1",
        "thread_id": pid,
    }
    row.update(extra)
    return row


def _install(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub) -> None:
    import http_api

    monkeypatch.setattr(http_api, "_capability", hub.get)


def _seed_state(hub: _FakeCapabilityHub, rows: list[dict[str, Any]]) -> None:
    hub._responses["pipeline-state"] = {"list": rows, "update": {"status": "updated"}}


def _update_writes(hub: _FakeCapabilityHub) -> list[tuple[str, dict[str, Any]]]:
    handle = hub.handles.get("pipeline-state")
    return [(p["pipeline_id"], p["fields"]) for m, p in (handle.calls if handle else []) if m == "update"]


async def _cancel(monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub, task_id: str) -> int:
    import http_api

    _install(monkeypatch, hub)
    result = await http_api.handle_http(
        f"/ext/task_service/tasks/{task_id}/cancel", "POST", "{}", None, None,
    )
    assert result.get("success") is True, f"handle_http 失败: {result}"
    data = result["data"]
    json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return int(data["status"])


class TestCancelOwnTerminal:
    """API 取消路径：自身投影随写 stopped；父侧零写面（镜像退役）。"""

    async def test_cancel_writes_own_terminal_without_parent_mirror(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """有父锚点：取消后自身 task.status=stopped；父管道行零触碰
        （owned 镜像写面已退役，ADR 2026-11-02-state-key-governance）。"""
        _seed_state(
            hub,
            [_state_row("t-child-1", "running", **{"lineage.parent_pipeline_id": "p-parent-1"})],
        )
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"status": "suspended", "run_id": "run-1"},
        }
        status = await _cancel(monkeypatch, hub, "t-child-1")
        assert status == 200

        writes = _update_writes(hub)
        own = dict(writes).get("t-child-1", {})
        assert own.get("task.status") == "stopped"
        assert "p-parent-1" not in dict(writes), (
            f"父管道镜像写面已退役，零父侧写：{writes!r}"
        )

    async def test_root_task_cancel_writes_no_owned_key(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """根任务（无父锚点）：取消不报错、不凭空造 owned 键。"""
        _seed_state(hub, [_state_row("t-root-1", "running")])
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"status": "suspended", "run_id": "run-2"},
        }
        status = await _cancel(monkeypatch, hub, "t-root-1")
        assert status == 200
        writes = _update_writes(hub)
        assert dict(writes).get("t-root-1", {}).get("task.status") == "stopped"
        assert all(
            not any(key.startswith("task.owned.") for key in fields)
            for _, fields in writes
        ), f"根任务不该造 owned 键：{writes!r}"

    async def test_parent_write_face_failure_does_not_break_cancel(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub, caplog
    ) -> None:
        """父管道写面故障不影响取消（镜像退役：父侧零写面，取消本体照常
        200、自身终态已落；若镜像写面回归，此故障注入即红）。"""
        _seed_state(
            hub,
            [_state_row("t-child-2", "running", **{"lineage.parent_pipeline_id": "p-parent-2"})],
        )
        hub._responses["pipeline-executor"] = {
            "suspend_pipeline": {"status": "suspended", "run_id": "run-3"},
        }
        # 父管道写面故障注入：任何父侧行写都会炸
        rows = [
            _state_row("t-child-2", "running", **{"lineage.parent_pipeline_id": "p-parent-2"})
        ]
        original_call = _FakeCapability.call

        async def _call(self, method: str, params: dict[str, Any], timeout: float | None = None):
            if method == "update" and params.get("pipeline_id") == "p-parent-2":
                raise RuntimeError("镜像写面故障")
            return await original_call(self, method, params, timeout)

        handle = _FakeCapability({"list": rows, "update": {"status": "updated"}})
        handle.call = _call.__get__(handle)  # type: ignore[method-assign]
        hub.handles["pipeline-state"] = handle  # type: ignore[assignment]

        status = await _cancel(monkeypatch, hub, "t-child-2")
        assert status == 200
        writes = _update_writes(hub)
        assert dict(writes).get("t-child-2", {}).get("task.status") == "stopped"
        assert "p-parent-2" not in dict(writes), f"父侧零写面：{writes!r}"


class TestCancelEventPath:
    """事件路径：run.cancelled → 只随写自身投影（父镜像退役）。"""

    async def test_event_path_writes_own_terminal_only(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        import http_api

        _install(monkeypatch, hub)
        _seed_state(
            hub,
            [_state_row("t-child-3", "running", **{"lineage.parent_pipeline_id": "p-parent-3"})],
        )
        await http_api.follow_write_cancel_terminal("t-child-3")
        writes = _update_writes(hub)
        by_target = dict(writes)
        assert by_target.get("t-child-3", {}).get("task.status") == "stopped"
        assert "p-parent-3" not in by_target, (
            f"父镜像写面已退役，零父侧写：{writes!r}"
        )

    async def test_event_path_is_idempotent_on_own_projection(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """自身投影已是 stopped 终态 → 不重复写（幂等省写）；父镜像退役
        后整面零写。"""
        import http_api

        _install(monkeypatch, hub)
        _seed_state(
            hub,
            [_state_row("t-child-4", "stopped", **{"lineage.parent_pipeline_id": "p-parent-4"})],
        )
        await http_api.follow_write_cancel_terminal("t-child-4")
        writes = _update_writes(hub)
        assert writes == [], f"已是终态省写且父镜像退役：零写面：{writes!r}"

    async def test_server_subscribes_run_cancelled(
        self, monkeypatch: pytest.MonkeyPatch, hub: _FakeCapabilityHub
    ) -> None:
        """任务域订阅 run.cancelled：事件到达即走取消终态随写（自身投影）。

        事件路径与 API 路径两处口径必须一致——此前任务域只订阅
        run.completed / run.failed，取消事件无人接。
        """
        import importlib.util

        import http_api

        _install(monkeypatch, hub)
        _seed_state(
            hub,
            [_state_row("t-child-5", "running", **{"lineage.parent_pipeline_id": "p-parent-5"})],
        )

        mod_name = "tasks_server_cancel_mirror_test"
        spec = importlib.util.spec_from_file_location(
            mod_name, str(_PLUGIN_DIR / "server.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(mod_name, None)

        # server.py 的域事件入口走插件实例的能力面；此处把 http_api 层的
        # 能力句柄替换为伪句柄（同一 pipeline-state 通道）。
        monkeypatch.setattr(http_api, "_capability", hub.get)
        monkeypatch.setattr(
            mod.plugin, "get_capability", lambda name: hub.get("pipeline-state")
        )
        await mod._on_domain_event({"event": "run.cancelled", "pipeline_id": "t-child-5"})

        writes = _update_writes(hub)
        by_target = dict(writes)
        assert by_target.get("t-child-5", {}).get("task.status") == "stopped", (
            f"run.cancelled 未落自身终态：{writes!r}"
        )
        assert "p-parent-5" not in by_target, (
            f"run.cancelled 父镜像写面已退役（零父侧写）：{writes!r}"
        )
