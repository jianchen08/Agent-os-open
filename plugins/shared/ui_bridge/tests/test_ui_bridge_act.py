# @feature: FP-0.2.侧边栏UI对象化 | @vision: V2 全能闭环 | @ci: python-coverage
"""ui_bridge 插件 P1 命令闭环验收测试（AC-P1-1/2/3，TDD Red——API 契约由本文件定义）。

需求真源（只读，冲突回报调度方，禁自行裁量）：
- docs/project/侧边栏UI对象化/需求冻结_2026-11-18.md §2.4（命令闭环契约逐字）、
  §三 AC-P1-1/2/3、§四 状态机与边界
- docs/decisions/2026-11-18-sidebar-ui-object-model.md（S1「纯函数+薄接线」）

被测入口契约（实现方必须满足，全部新增面）：
1. ``plugins/shared/ui_bridge/server.py`` 新增模块级 ``ui_act(params: dict) -> dict``
   （与 ui_describe 共用同一 ``_SNAPSHOTS`` 缓存与 ``_time`` 时刻源）。
2. 命令外发经**可注入 seam**：模块级 ``set_ui_command_notifier(fn)`` 注册 notifier；
   ``ui_act`` 每发出一条命令，以**命令载荷 dict**（flat，含冻结四字段
   ``{command_id, region, action, params}``）调用 notifier 恰一次。事件名包装
   （仿 human 插件 ``_EventBusNotifier``：``bus.notify("emit", {event, payload})``）
   属生产接线细节，不在 seam 面断言。**先布防后发令**：notifier 被调用时等待者
   必已登记（回执先于等待到达不失效）。默认（未注册 notifier）外发面降级：
   不抛异常，命令按无回执路径走完等待窗口（timeout）。
3. 新增 POST ``/ext/ui_bridge/command_result``（create_app FastAPI app 与
   http.handle 路由核同路由）：body ``{command_id, status: "applied"|"rejected",
   reason?}``；未知 command_id → 202 忽略（幂等）；已知 → 唤醒等待者。
4. 等待窗口 2s 走模块命名空间 ``_time``（``_time.monotonic()`` 判窗 + ``_time.sleep``
   让渡），禁真实 sleep——本文件假钟两档注入：
   - ``AdvancingClock``：``sleep`` 即推进假钟（同步跑完等待窗，测 timeout 边界）；
   - ``IdleClock``：``sleep`` 只让渡不推进（配合回执线程测 applied 唤醒）。

冻结语义澄清（依据 = §2.4 + §四 状态机逐字，非自行解释）：
- 状态机终态只有 ``applied*``/``timeout*``（emitted 之后）；``rejected`` 终态仅
  出自**发令前**校验失败（四拒绝路径）。故回执 ``rejected`` **不终结**等待——
  多端各回执取首个 ``applied``（§2.4 等待窗口行逐字），窗口内无人 applied 即 timeout。
- 每条拒绝路径 fail-closed：不同步发 ``ui_command``（notifier 不被调），且其
  （不存在的）command_id 在回执端点视角为未知 → 202。

预期 Red：``ui_act``/``set_ui_command_notifier``/``/ext/ui_bridge/command_result``
尚不存在（AttributeError / 404 / manifest 缺声明）——TDD Red 属预期，实现方按本
契约交付后转绿。
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import sys
import threading
import time as real_time
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]

_LOAD_SEQ = itertools.count()

_WAIT_WINDOW_S = 2.0


def _load_server() -> Any:
    """以独名模块装载 server.py（模块级缓存/等待登记随装载实例隔离，防跨用例串扰）。"""
    mod_name = f"ui_bridge_act_server_under_test_{next(_LOAD_SEQ)}"
    spec = importlib.util.spec_from_file_location(mod_name, _PKG / "server.py")
    assert spec is not None, "cannot load server.py"
    assert spec.loader is not None, "cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeClockBase:
    """注入钟骨架：``monotonic`` 读假钟，``sleep`` 让渡调度（禁真实睡眠）。"""

    def __init__(self, start: float = 100_000.0) -> None:
        self.current = start

    def monotonic(self) -> float:
        return self.current

    def _yield(self) -> None:
        real_time.sleep(0)  # 让出一个调度槽：线程用例防忙等热旋


class AdvancingClock(_FakeClockBase):
    """sleep 即按入睡量推进假钟：同步无线程跑完 2s 等待窗（timeout 边界用）。"""

    def sleep(self, seconds: float) -> None:
        self.current += seconds
        self._yield()


class IdleClock(_FakeClockBase):
    """sleep 只让渡不推进：假钟冻结 → 等待窗永不超时，直到回执线程送达唤醒。"""

    def sleep(self, _seconds: float) -> None:
        self._yield()


@pytest.fixture
def ui_bridge() -> Any:
    return _load_server()


@pytest.fixture
def client(ui_bridge: Any) -> TestClient:
    return TestClient(ui_bridge.create_app())


def _patch_clock(ui_bridge: Any, clock: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(ui_bridge, "_time", clock)
    return clock


# ── 请求体工厂（与 P0 签发件同构的合法快照）────────────────────────────


def _items(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"item-{i:03d}",
            "kind": "builtin" if i % 2 == 0 else "plugin_page",
            "title": f"侧栏项 {i}",
            "order": float(i),
            "visible": True,
        }
        for i in range(n)
    ]


def _sessions(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"sess-{i:03d}",
            "title": f"会话 {i}",
            "pinned": i % 5 == 0,
            "active": i == 0,
        }
        for i in range(n)
    ]


def _valid_body(client_id: str = "fe-1", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_id": client_id,
        "region": "sidebar",
        "ts": 1_700_000_000_000,
        "state": {
            "collapsed": False,
            "width_ratio": 0.3,
            "active_view": "sessions",
            "unread_count": 2,
        },
        "items": _items(2),
        "sessions": _sessions(2),
        "actions": ["navigate", "set_collapsed", "open_page", "highlight"],
    }
    body.update(overrides)
    return body


_STATE_URL = "/ext/ui_bridge/state"
_RESULT_URL = "/ext/ui_bridge/command_result"

# plugin.json 的 ui_act output_schema（未声明即红——声明面契约）


@pytest.fixture
def ui_act_schemas() -> tuple[dict[str, Any], dict[str, Any]]:
    """（input_schema, output_schema）——plugin.json 必须全量声明 ui_act 双 schema。"""
    manifest = json.loads((_PKG / "plugin.json").read_text(encoding="utf-8"))
    tools = manifest.get("capabilities", {}).get("tools", [])
    matches = [t for t in tools if t.get("name") == "ui_act"]
    assert matches, "plugin.json 未声明 ui_act 工具（§2.4 声明面）"
    input_schema = matches[0].get("input_schema")
    output_schema = matches[0].get("output_schema")
    assert isinstance(input_schema, dict), "ui_act 未声明 input_schema（§2.4）"
    assert isinstance(output_schema, dict), "ui_act 未全量声明 output_schema（tool_core fail-closed 消费）"
    return input_schema, output_schema


def _snapshot_after_nullable_object(prop: dict[str, Any]) -> bool:
    """snapshot_after 声明须容纳 object 与 null（可空对象，任务面口径）。"""
    if "anyOf" in prop:
        types = {sub.get("type") for sub in prop["anyOf"]}
        return {"object", "null"} <= types
    declared = prop.get("type")
    if isinstance(declared, list):
        return {"object", "null"} <= set(declared)
    return declared == "object" or declared is None  # type: ignore[return-value]


# ── 声明面契约：plugin.json ui_act 双 schema（§2.4 逐字）────────────────


class TestUiActManifestSchema:
    """ui_act 声明面：input/output schema 冻结字段逐一核对 + 样例输出过 jsonschema。"""

    def test_input_schema_frozen_shape(self, ui_act_schemas: tuple[dict[str, Any], dict[str, Any]]) -> None:
        input_schema, _ = ui_act_schemas
        assert input_schema.get("type") == "object"
        props = input_schema.get("properties", {})
        action = props.get("action")
        assert isinstance(action, dict), "input_schema.properties 缺 action"
        assert set(action.get("enum", [])) == {"navigate", "set_collapsed", "open_page", "highlight"}, (
            "action 词表 = §2.4 封闭四值"
        )
        params = props.get("params")
        assert isinstance(params, dict), "input_schema.properties 缺 params"
        assert params.get("type") == "object", "params 须声明为 object"
        assert "region" in props, "region 声明位在册（默认 sidebar）"

    def test_output_schema_frozen_shape(self, ui_act_schemas: tuple[dict[str, Any], dict[str, Any]]) -> None:
        _, output_schema = ui_act_schemas
        assert set(output_schema.get("required", [])) == {"status"}, "output required = [status]（§2.4 逐字）"
        props = output_schema.get("properties", {})
        status = props.get("status")
        assert isinstance(status, dict), "output_schema.properties 缺 status"
        assert set(status.get("enum", [])) == {"applied", "rejected", "timeout"}, (
            "status 枚举三值（§2.4 逐字）"
        )
        reason = props.get("reason")
        assert isinstance(reason, dict), "output_schema.properties 缺 reason"
        assert set(reason.get("enum", [])) == {
            "unknown_action",
            "unknown_target",
            "no_fresh_snapshot",
            "invalid_params",
        }, "reason 枚举四值（§2.4 逐字）"
        assert "snapshot_after" in props, "snapshot_after 声明位在册（applied 时附最新缓存快照）"
        assert _snapshot_after_nullable_object(props["snapshot_after"]), "snapshot_after = 可空对象"

    def test_sample_outputs_validate_against_declared_schema(
        self,
        ui_bridge: Any,
        ui_act_schemas: tuple[dict[str, Any], dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """四拒绝路径样例 + 无快照 set_collapsed 样例输出全过声明 schema（fail-closed 面即输出面）。"""
        _, output_schema = ui_act_schemas
        _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)

        samples = [
            ui_bridge.ui_act({"action": "scroll_to_top", "params": {}}),
            ui_bridge.ui_act({"action": "navigate", "params": {}}),
            ui_bridge.ui_act({"action": "navigate", "params": {"target": "sess-999"}}),
            ui_bridge.ui_act({"action": "open_page", "params": {"page_id": "no.page"}}),
        ]
        for out in samples:
            jsonschema.validate(out, output_schema)
            assert out["status"] == "rejected"


# ── AC-P1-1：四拒绝路径各自可达且 fail-closed ─────────────────────────


class TestFailClosedRejections:
    """AC-P1-1：unknown_action / unknown_target / no_fresh_snapshot / invalid_params
    四拒绝路径逐条可达；拒绝同步发生（不外发 ui_command），回执端点视角其
    command_id 未知（202）——脏命令既不广播也不可被伪造回执激活。"""

    def _assert_rejected(
        self,
        ui_bridge: Any,
        client: TestClient,
        params: dict[str, Any],
        reason: str,
        notifier: list[dict[str, Any]],
    ) -> None:
        out = ui_bridge.ui_act(params)
        assert out["status"] == "rejected", f"{reason}: 期望 rejected，实得 {out}"
        assert out.get("reason") == reason, f"{reason}: reason 枚举错位，实得 {out.get('reason')}"
        assert out.get("snapshot_after") is None, f"{reason}: 拒绝路径不附快照"
        assert notifier == [], f"{reason}: fail-closed 不同步发 ui_command"
        if out.get("command_id"):
            resp = client.post(
                _RESULT_URL,
                json={"command_id": out["command_id"], "status": "applied"},
            )
            assert resp.status_code == 202, f"{reason}: 拒绝命令不得留可回执的 pending"

    def test_unknown_action(self, ui_bridge: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)
        notifier: list[dict[str, Any]] = []
        ui_bridge.set_ui_command_notifier(notifier.append)
        self._assert_rejected(
            ui_bridge, client, {"action": "scroll", "params": {}}, "unknown_action", notifier
        )

    @pytest.mark.parametrize(
        ("params", "label"),
        [
            ({"action": "navigate", "params": {}}, "navigate-missing-target"),
            ({"action": "navigate", "params": {"target": 123}}, "navigate-target-not-string"),
            ({"action": "set_collapsed", "params": {}}, "set_collapsed-missing-collapsed"),
            ({"action": "set_collapsed", "params": {"collapsed": "yes"}}, "set_collapsed-collapsed-not-bool"),
            ({"action": "open_page", "params": {}}, "open_page-missing-page_id"),
            ({"action": "highlight", "params": {}}, "highlight-missing-item_id"),
            ({"action": "highlight", "params": {"item_id": "item-000", "duration_ms": 6000}}, "highlight-duration-over-5000"),
        ],
    )
    def test_invalid_params(
        self, ui_bridge: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch, params: dict[str, Any], label: str
    ) -> None:
        _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)
        notifier: list[dict[str, Any]] = []
        ui_bridge.set_ui_command_notifier(notifier.append)
        self._assert_rejected(ui_bridge, client, params, "invalid_params", notifier)

    def test_no_fresh_snapshot_without_any_snapshot(
        self, ui_bridge: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空缓存 + navigate（缺快照即拒）：目标存在性无从判定，no_fresh_snapshot 先行。"""
        _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)
        notifier: list[dict[str, Any]] = []
        ui_bridge.set_ui_command_notifier(notifier.append)
        self._assert_rejected(
            ui_bridge,
            client,
            {"action": "navigate", "params": {"target": "sess-000"}},
            "no_fresh_snapshot",
            notifier,
        )

    def test_no_fresh_snapshot_with_stale_snapshot(
        self, ui_bridge: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """有快照但已过期（注入钟推进 ≥31s）：同样 no_fresh_snapshot（§2.4「快照缺失/过期」）。"""
        clock = _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)
        client.post(_STATE_URL, json=_valid_body())
        clock.current += 31.0  # P0 同款边界：≥31s 即 stale
        notifier: list[dict[str, Any]] = []
        ui_bridge.set_ui_command_notifier(notifier.append)
        self._assert_rejected(
            ui_bridge,
            client,
            {"action": "navigate", "params": {"target": "sess-000"}},
            "no_fresh_snapshot",
            notifier,
        )

    @pytest.mark.parametrize(
        ("params", "label"),
        [
            ({"action": "navigate", "params": {"target": "sess-999"}}, "navigate-session-missing"),
            ({"action": "navigate", "params": {"target": "ghost.item"}}, "navigate-item-missing"),
            ({"action": "open_page", "params": {"page_id": "ghost.page"}}, "open_page-page-missing"),
        ],
    )
    def test_unknown_target(
        self, ui_bridge: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch, params: dict[str, Any], label: str
    ) -> None:
        """fresh 快照在册但目标不在 items/sessions：unknown_target（§2.4 目标校验逐字）。"""
        _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)
        client.post(_STATE_URL, json=_valid_body())
        notifier: list[dict[str, Any]] = []
        ui_bridge.set_ui_command_notifier(notifier.append)
        self._assert_rejected(ui_bridge, client, params, "unknown_target", notifier)


# ── AC-P1-2/3：合法命令外发 ui_command + 回执唤醒/超时/取首个 applied ──


class _UiActRunner:
    """ui_act 后台线程运行器：IdleClock 冻结假钟 → 等待窗由回执线程驱动唤醒。"""

    def __init__(self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mod = ui_bridge
        self.clock = _patch_clock(ui_bridge, IdleClock(), monkeypatch)
        self.captured: list[dict[str, Any]] = []
        self._emitted = threading.Event()
        ui_bridge.set_ui_command_notifier(self._on_emit)

    def _on_emit(self, payload: dict[str, Any]) -> None:
        self.captured.append(payload)
        self._emitted.set()

    def run(self, params: dict[str, Any]) -> threading.Thread:
        holder = self

        def target() -> None:
            holder.output = self.mod.ui_act(params)  # type: ignore[attr-defined]

        thread = threading.Thread(target=target, name="ui_act-under-test", daemon=True)
        thread.start()
        assert self._emitted.wait(10.0), "ui_act 未在 10s（真实墙钟握手上限）内发出 ui_command"
        return thread


class TestEmitAndApplied:
    """AC-P1-2：外发 ui_command 载荷四字段齐全；AC-P1-3：applied 回执唤醒 →
    applied + snapshot_after=最新缓存快照。≥3 组区分度输入（navigate 带 fresh
    快照 / highlight 带 fresh 快照 / set_collapsed 零快照）。"""

    @pytest.mark.parametrize(
        ("params", "seed_snapshot", "label"),
        [
            ({"action": "navigate", "params": {"target": "sess-000"}}, True, "navigate-with-fresh-snapshot"),
            ({"action": "highlight", "params": {"item_id": "item-000"}}, True, "highlight-with-fresh-snapshot"),
            ({"action": "set_collapsed", "params": {"collapsed": True}}, False, "set_collapsed-without-snapshot"),
        ],
    )
    def test_emit_payload_then_applied_receipt(
        self,
        ui_bridge: Any,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        params: dict[str, Any],
        seed_snapshot: bool,
        label: str,
    ) -> None:
        snapshot_body: dict[str, Any] | None = None
        if seed_snapshot:
            snapshot_body = _valid_body()
            resp = client.post(_STATE_URL, json=snapshot_body)
            assert resp.status_code == 200

        runner = _UiActRunner(ui_bridge, monkeypatch)
        thread = runner.run(params)

        assert len(runner.captured) == 1, f"{label}: 一条命令恰一次外发"
        payload = runner.captured[0]
        assert set(payload) >= {"command_id", "region", "action", "params"}, (
            f"{label}: ui_command 载荷四字段齐全（§2.4 data = {payload}）"
        )
        assert isinstance(payload["command_id"], str), "command_id 须为字符串"
        assert payload["command_id"], "command_id 非空"
        assert payload["region"] == "sidebar"
        assert payload["action"] == params["action"], "action 回显入参"
        assert payload["params"] == params["params"], "params 回显入参"

        resp = client.post(_RESULT_URL, json={"command_id": payload["command_id"], "status": "applied"})
        assert 200 <= resp.status_code < 300, f"{label}: 已知 pending 回执被受理（冻结件未钉死成功码）"

        thread.join(10.0)
        assert not thread.is_alive(), f"{label}: applied 回执后 ui_act 未返回"
        out = runner.output
        assert out["status"] == "applied", f"{label}: 期望 applied，实得 {out}"
        if snapshot_body is not None:
            assert out["snapshot_after"] is not None, f"{label}: applied 附最新缓存快照"
            assert out["snapshot_after"]["state"] == snapshot_body["state"], f"{label}: snapshot_after=缓存快照体"
        else:
            assert out.get("snapshot_after") is None, f"{label}: 无缓存快照时 snapshot_after 为空"


class TestTimeoutAndDegrade:
    """AC-P1-3 timeout 臂 + 外发面降级：AdvancingClock 同步跑完等待窗，禁真实 sleep。"""

    def test_no_receipt_times_out_at_two_second_window(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)
        notifier: list[dict[str, Any]] = []
        ui_bridge.set_ui_command_notifier(notifier.append)

        out = ui_bridge.ui_act({"action": "set_collapsed", "params": {"collapsed": False}})

        assert notifier, "合法命令已外发（等待的是回执，不是没发）"
        assert out["status"] == "timeout"
        assert out.get("snapshot_after") is None, "timeout 不附快照"
        elapsed = clock.current - 100_000.0
        assert elapsed >= _WAIT_WINDOW_S, "等待窗不满 2s 不得判 timeout"
        assert elapsed < _WAIT_WINDOW_S + 1.0, "等待窗显著超 2s（>3s）即违约"

    def test_missing_notifier_degrades_without_raising(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认无 notifier：外发面降级——不抛异常，按无回执路径走完窗口落 timeout。"""
        _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)

        out = ui_bridge.ui_act({"action": "set_collapsed", "params": {"collapsed": True}})

        assert out["status"] == "timeout", f"降级面 = timeout（fail-closed 四拒绝枚举不含降级），实得 {out}"


class TestFirstAppliedWins:
    """AC-P1-3 多端回执取首个 applied：rejected 回执不终结等待（状态机 emitted 后
    终态仅 applied/timeout），首个 applied 回执到达即 applied 终局。"""

    def test_rejected_receipt_does_not_resolve_applied_receipt_wins(
        self, ui_bridge: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client.post(_STATE_URL, json=_valid_body(client_id="fe-a"))
        runner = _UiActRunner(ui_bridge, monkeypatch)
        thread = runner.run({"action": "navigate", "params": {"target": "sess-000"}})
        command_id = runner.captured[0]["command_id"]

        client.post(_RESULT_URL, json={"command_id": command_id, "status": "rejected", "reason": "unknown_target"})
        thread.join(0.5)
        assert thread.is_alive(), "单端 rejected 回执不得终结等待（取首个 applied 的前提）"

        client.post(_RESULT_URL, json={"command_id": command_id, "status": "applied"})
        thread.join(10.0)
        assert not thread.is_alive(), "applied 回执到达后应终结"
        out = runner.output
        assert out["status"] == "applied", f"首个 applied 回执胜出，实得 {out}"
        assert out["snapshot_after"] is not None


class TestCommandResultEndpoint:
    """AC-P1-3 未知 command_id → 202 忽略（幂等），零状态污染。"""

    def test_unknown_command_id_is_ignored_idempotently(self, client: TestClient) -> None:
        for _ in range(2):
            resp = client.post(_RESULT_URL, json={"command_id": "no-such-command", "status": "applied"})
            assert resp.status_code == 202, "未知 command_id → 202（幂等忽略，冻结件逐字）"

        # 零污染：忽略后真实命令闭环仍可用（发令 → applied 唤醒）
        body = _valid_body()
        client.post(_STATE_URL, json=body)

    def test_command_result_routed_via_http_handle_core(
        self, ui_bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """路由核（http.handle）同持 command_result 面：未知 id 202（sidecar 运行面与
        FastAPI 测试面共用路由语义，P0 双面同构先例）。"""
        import asyncio

        _patch_clock(ui_bridge, AdvancingClock(), monkeypatch)
        envelope = asyncio.run(
            ui_bridge.http_handle(
                path="/ext/ui_bridge/command_result",
                method="POST",
                raw_body=json.dumps({"command_id": "ghost-cmd", "status": "applied"}),
            )
        )
        assert envelope["success"] is True
        assert int(envelope["data"]["status"]) == 202
