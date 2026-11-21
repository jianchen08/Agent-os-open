# @feature: FP-0.2.侧边栏UI对象化 | @vision: V2 全能闭环 | @ci: python-coverage
"""ui_bridge P1 补充缺口测试（实现方补充件，签发面之外）：生产外发面
（_EventBusCommandNotifier 全分支 + on_load）、回执路由核校验分支、坏帧容忍、
http.handle command_result 全分支——改动行 diff coverage 100% 的前提。

需求真源（只读）：docs/project/侧边栏UI对象化/需求冻结_2026-11-18.md §2.4。
"""
from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import sys
import threading
import time as real_time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_LOAD_SEQ = itertools.count()


def _load_server() -> Any:
    mod_name = f"ui_bridge_gaps_server_{next(_LOAD_SEQ)}"
    spec = importlib.util.spec_from_file_location(mod_name, _PKG / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ui_bridge() -> Any:
    return _load_server()


@pytest.fixture
def client(ui_bridge: Any) -> TestClient:
    return TestClient(ui_bridge.create_app())


_RESULT_URL = "/ext/ui_bridge/command_result"


# ── 回执路由核校验分支（fail-closed 400）──────────────────────────────


class TestHandleCommandResultValidation:
    def test_non_dict_body_rejected_400(self, ui_bridge: Any) -> None:
        payload, status = ui_bridge.handle_command_result(["not", "a", "dict"])
        assert status == 400
        assert "error" in payload

    def test_missing_or_malformed_command_id_rejected_400(self, ui_bridge: Any) -> None:
        for body in ({}, {"status": "applied"}, {"command_id": 123, "status": "applied"}, {"command_id": "", "status": "applied"}):
            payload, status = ui_bridge.handle_command_result(body)
            assert status == 400, f"{body} 应 400，实得 {status}"
            assert "error" in payload

    def test_unknown_status_rejected_400(self, ui_bridge: Any) -> None:
        payload, status = ui_bridge.handle_command_result({"command_id": "x", "status": "accepted"})
        assert status == 400
        assert "error" in payload

    def test_known_rejected_receipt_accepted_without_resolving(self, ui_bridge: Any) -> None:
        pending = ui_bridge.PendingCommand()
        with ui_bridge._PENDING_LOCK:
            ui_bridge._PENDING["c-rej"] = pending
        payload, status = ui_bridge.handle_command_result({"command_id": "c-rej", "status": "rejected"})
        assert status == 200
        assert payload == {"ok": True}
        with ui_bridge._PENDING_LOCK:
            assert "c-rej" in ui_bridge._PENDING, "rejected 回执不得摘除登记（等待不终结）"

        payload, status = ui_bridge.handle_command_result({"command_id": "c-rej", "status": "applied"})
        assert status == 200
        with ui_bridge._PENDING_LOCK:
            assert "c-rej" not in ui_bridge._PENDING
        assert pending.applied is True

    def test_resolve_applied_unknown_id_returns_false(self, ui_bridge: Any) -> None:
        assert ui_bridge._resolve_command_applied("ghost-cmd") is False


# ── ui_act 入参容忍面 ─────────────────────────────────────────────────


class TestUiActParamTolerance:
    def test_non_dict_params_degrades_to_unknown_action(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ui_bridge, "_time", type("Clock", (), {"monotonic": staticmethod(lambda: 1.0), "sleep": staticmethod(lambda _s: None)})())
        out = ui_bridge.ui_act(None)  # type: ignore[arg-type]
        assert out == {"status": "rejected", "reason": "unknown_action"}

    def test_region_non_string_defaults_sidebar_and_echoes_payload(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict[str, Any]] = []
        monkeypatch.setattr(ui_bridge, "_time", _AdvancingClock())
        ui_bridge.set_ui_command_notifier(captured.append)
        out = ui_bridge.ui_act({"region": 42, "action": "set_collapsed", "params": {"collapsed": True}})
        assert out["status"] == "timeout"
        assert captured[0]["region"] == "sidebar"
        ui_bridge.set_ui_command_notifier(None)


# ── FastAPI command_result 端点全分支 ─────────────────────────────────


class TestFastApiCommandResult:
    def test_non_json_body_rejected_400(self, client: TestClient) -> None:
        resp = client.post(_RESULT_URL, content=b"not-json", headers={"content-type": "application/json"})
        assert resp.status_code == 400

    def test_scalar_json_body_rejected_400(self, client: TestClient) -> None:
        resp = client.post(_RESULT_URL, json=[1, 2, 3])
        assert resp.status_code == 400

    def test_known_pending_applied_resolves_200(self, ui_bridge: Any, client: TestClient) -> None:
        pending = ui_bridge.PendingCommand()
        with ui_bridge._PENDING_LOCK:
            ui_bridge._PENDING["c-api"] = pending
        resp = client.post(_RESULT_URL, json={"command_id": "c-api", "status": "applied"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert pending.applied is True

    def test_unknown_command_id_202_idempotent(self, client: TestClient) -> None:
        for _ in range(2):
            resp = client.post(_RESULT_URL, json={"command_id": "nope", "status": "applied"})
            assert resp.status_code == 202
            assert resp.json() == {"ignored": True}


# ── http.handle 路由核 command_result 全分支 ──────────────────────────


class TestHttpHandleCommandResult:
    def test_invalid_json_body_400(self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        monkeypatch.setattr(ui_bridge, "_time", type("Clock", (), {"monotonic": staticmethod(lambda: 1.0), "sleep": staticmethod(lambda _s: None)})())
        envelope = asyncio.run(
            ui_bridge.http_handle(path="/ext/ui_bridge/command_result", method="POST", raw_body="not-json{")
        )
        assert envelope["success"] is True
        assert int(envelope["data"]["status"]) == 400

    def test_unknown_id_202_via_route_core(self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        monkeypatch.setattr(ui_bridge, "_time", type("Clock", (), {"monotonic": staticmethod(lambda: 1.0), "sleep": staticmethod(lambda _s: None)})())
        envelope = asyncio.run(
            ui_bridge.http_handle(
                path="/ext/ui_bridge/command_result",
                method="POST",
                raw_body=json.dumps({"command_id": "ghost", "status": "rejected"}),
            )
        )
        assert envelope["success"] is True
        assert int(envelope["data"]["status"]) == 202

    def test_known_applied_wakes_waiter_via_route_core(self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        clock = type("IdleClock", (), {"monotonic": staticmethod(lambda: 1.0), "sleep": staticmethod(lambda _s: __import__("time").sleep(0))})()
        monkeypatch.setattr(ui_bridge, "_time", clock)
        pending = ui_bridge.PendingCommand()
        with ui_bridge._PENDING_LOCK:
            ui_bridge._PENDING["c-core"] = pending
        envelope = asyncio.run(
            ui_bridge.http_handle(
                path="/ext/ui_bridge/command_result",
                method="POST",
                raw_body=json.dumps({"command_id": "c-core", "status": "applied"}),
            )
        )
        assert envelope["success"] is True
        assert int(envelope["data"]["status"]) == 200
        assert pending.applied is True

    def test_non_post_method_falls_through_to_unknown_route(self, ui_bridge: Any) -> None:
        import asyncio

        envelope = asyncio.run(ui_bridge.http_handle(path="/ext/ui_bridge/command_result", method="GET"))
        assert envelope["success"] is True
        assert int(envelope["data"]["status"]) == 404


# ── SDK 分发入口与生命周期 ────────────────────────────────────────────


class _AdvancingClock:
    """sleep 即推进假钟（同步跑完等待窗，与签发件 AdvancingClock 同构）。"""

    def __init__(self) -> None:
        self.current = 100_000.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


class TestSdkEntryAndLifecycle:
    def test_ui_act_tool_entry_kwargs_assembly(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _AdvancingClock()
        monkeypatch.setattr(ui_bridge, "_time", clock)
        out = asyncio.run(ui_bridge._ui_act_tool(action="set_collapsed", params={"collapsed": True}))
        assert out["status"] == "timeout"
        assert clock.current - 100_000.0 >= 2.0

    def test_on_load_captures_loop_and_registers_notifier(self, ui_bridge: Any) -> None:
        asyncio.run(ui_bridge._on_load({}))
        assert ui_bridge._ui_command_notifier is ui_bridge._ui_bus_notifier
        ui_bridge.set_ui_command_notifier(None)


# ── 生产外发面 _EventBusCommandNotifier 全分支 ────────────────────────


class _FakeBus:
    def __init__(self, fail: bool = False) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.ev = threading.Event()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("bus down")
        self.emitted.append((method, params))
        self.ev.set()


class _PluginRef:
    def __init__(self, cap: Any) -> None:
        self._cap = cap

    def get_capability(self, name: str) -> Any:
        if self._cap is None:
            raise KeyError(name)
        return self._cap


def _run_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5.0)
    loop.close()


class TestEventBusCommandNotifier:
    def test_bus_missing_degrades_without_raising(self, ui_bridge: Any) -> None:
        notifier = ui_bridge._EventBusCommandNotifier(_PluginRef(None))
        notifier({"command_id": "c1"})  # 不抛即降级成功

    def test_loop_not_captured_degrades_without_raising(self, ui_bridge: Any) -> None:
        notifier = ui_bridge._EventBusCommandNotifier(_PluginRef(_FakeBus()))
        notifier({"command_id": "c2"})

    def test_closed_loop_degrades_without_raising(self, ui_bridge: Any) -> None:
        loop, thread = _run_loop_in_thread()
        notifier = ui_bridge._EventBusCommandNotifier(_PluginRef(_FakeBus()))

        async def _capture() -> None:
            notifier.capture_loop()

        asyncio.run_coroutine_threadsafe(_capture(), loop).result(5.0)
        _stop_loop(loop, thread)
        notifier({"command_id": "c3"})  # loop.is_closed() → 降级

    def test_success_path_emits_via_event_bus(self, ui_bridge: Any) -> None:
        bus = _FakeBus()
        notifier = ui_bridge._EventBusCommandNotifier(_PluginRef(bus))
        loop, thread = _run_loop_in_thread()

        async def _capture() -> None:
            notifier.capture_loop()

        asyncio.run_coroutine_threadsafe(_capture(), loop).result(5.0)
        try:
            payload = {"command_id": "c4", "region": "sidebar", "action": "set_collapsed", "params": {}}
            notifier(payload)
            assert bus.ev.wait(5.0), "emit 未在 5s 内到达 fake bus"
            assert bus.emitted == [("emit", {"event": "ui_command", "payload": payload})]
        finally:
            _stop_loop(loop, thread)

    def test_emit_failure_logged_not_raised(self, ui_bridge: Any) -> None:
        bus = _FakeBus(fail=True)
        notifier = ui_bridge._EventBusCommandNotifier(_PluginRef(bus))
        loop, thread = _run_loop_in_thread()

        async def _capture() -> None:
            notifier.capture_loop()

        asyncio.run_coroutine_threadsafe(_capture(), loop).result(5.0)
        try:
            notifier({"command_id": "c5"})
            # 留 done-callback 执行槽：bus.notify 抛错 → future 异常 → 只记日志不外抛
            real_time.sleep(0.3)
        finally:
            _stop_loop(loop, thread)

    def test_schedule_failure_runtimeerror_logged_not_raised(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = _FakeBus()
        notifier = ui_bridge._EventBusCommandNotifier(_PluginRef(bus))
        loop, thread = _run_loop_in_thread()

        async def _capture() -> None:
            notifier.capture_loop()

        asyncio.run_coroutine_threadsafe(_capture(), loop).result(5.0)
        try:
            monkeypatch.setattr(
                ui_bridge.asyncio,
                "run_coroutine_threadsafe",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("loop shutting down")),
            )
            notifier({"command_id": "c6"})  # RuntimeError 分支：只记日志不外抛
        finally:
            _stop_loop(loop, thread)
