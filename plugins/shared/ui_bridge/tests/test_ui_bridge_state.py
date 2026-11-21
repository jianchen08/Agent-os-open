# @feature: FP-0.2.侧边栏UI对象化 | @vision: V2 全能闭环 | @ci: python-coverage
"""ui_bridge 插件 P0 感知闭环验收测试（AC-P0-1/2/3/5/6，TDD Red——API 契约由本文件定义）。

需求真源（只读，冲突回报调度方）：
- docs/project/侧边栏UI对象化/需求冻结_2026-11-18.md §2.1（快照上报）、§2.2（ui_describe）、§三 AC、§四 状态机与边界
- docs/decisions/2026-11-18-sidebar-ui-object-model.md

被测入口契约（实现方必须满足）：
1. ``plugins/shared/ui_bridge/server.py`` 提供 ``create_app() -> FastAPI``；
   POST/GET ``/ext/ui_bridge/state`` 逻辑在此 app 内自含，不依赖内核进程——
   本文件以 Starlette TestClient 直测。
2. ``ui_describe`` 工具入口 = 同模块模块级函数 ``ui_describe(params: dict) -> dict``
   （sidecar 主循环与测试共用同一入口，先例 = memory 插件 ``server.memory(...)``）；
   与端点读写**同一份进程内快照缓存**（respawn 即失，靠 ≤30s 心跳自愈，ADR 负面项）。
3. freshness 时刻源 = 模块命名空间 ``_time``（server.py 以 ``import time as _time``
   持有，收到时刻经 ``_time.monotonic()`` 取，单调、不信任 client ts）；
   本文件按 repo fake clock 惯例 ``setattr(server_module, "_time", fake)`` 注入
   假钟推进，禁 sleep 真实时间。
4. 每个用例独立装载一份 server 模块：模块级缓存随装载实例隔离，防跨用例串扰。
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from pathlib import Path
from typing import Any, Iterator

import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]

_LOAD_SEQ = itertools.count()


def _load_server() -> Any:
    """以独名模块装载 server.py（跨插件同名裸模块互不串扰，先例 = notification_sounds）。"""
    mod_name = f"ui_bridge_state_server_under_test_{next(_LOAD_SEQ)}"
    spec = importlib.util.spec_from_file_location(mod_name, _PKG / "server.py")
    assert spec is not None, "cannot load server.py"
    assert spec.loader is not None, "cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeClock:
    """注入时钟（AC-P0-2 禁真实 sleep）：手动推进的 monotonic 假钟。"""

    def __init__(self, start: float = 100_000.0) -> None:
        self.current = start

    def monotonic(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


@pytest.fixture
def ui_bridge() -> Iterator[Any]:
    yield _load_server()


@pytest.fixture
def clock(ui_bridge: Any, monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(ui_bridge, "_time", fake)
    return fake


@pytest.fixture
def client(ui_bridge: Any) -> TestClient:
    return TestClient(ui_bridge.create_app())


# ── 请求体工厂（§2.1 合法快照）──────────────────────────────────────────


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
_GET_SINGLE = {"region": "sidebar"}


# ── AC-P0-1：POST → 200 {"ok":true}；GET 单端回读同内容 ────────────────


class TestPostGetRoundTrip:
    """AC-P0-1：上报即缓存、回读即同内容——感知闭环的地基行为。

    两组有区分度输入（普通体 / CJK+emoji 标题体）：JSON 透传不转码（冻结件 §四 字符串行）。
    """

    @pytest.mark.parametrize(
        "body",
        [
            _valid_body(),
            _valid_body(
                client_id="fe-中文✨",
                state={
                    "collapsed": True,
                    "width_ratio": 0.85,
                    "active_view": "workspace🚀",
                    "unread_count": 0,
                },
            ),
        ],
        ids=["plain", "cjk-emoji"],
    )
    def test_post_ok_then_get_returns_same_content(self, ui_bridge: Any, client: TestClient, body: dict[str, Any]) -> None:
        assert isinstance(ui_bridge.create_app(), FastAPI), "入口契约：create_app() 返回 FastAPI 实例"

        resp = client.post(_STATE_URL, json=body)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}, "成功面冻结契约 = 200 {\"ok\": true}"

        data = client.get(_STATE_URL, params=_GET_SINGLE).json()
        assert set(data) == {"region", "client_id", "received_at", "fresh", "snapshot"}
        assert data["region"] == "sidebar"
        assert data["client_id"] == body["client_id"]
        assert data["fresh"] is True
        assert isinstance(data["received_at"], float), "received_at 契约 = float（插件收到时刻）"

        snapshot = data["snapshot"]
        # 回显保真：snapshot 四内容字段逐字段等于上报体（client_id/ts 不入快照体）
        assert snapshot["state"] == body["state"]
        assert snapshot["items"] == body["items"]
        assert snapshot["sessions"] == body["sessions"]
        assert snapshot["actions"] == body["actions"]

    def test_get_without_any_snapshot_returns_nulls_not_404(self, client: TestClient) -> None:
        """无任何快照 → 200 + null 三件套（冻结件 §2.1：不是 404）。"""
        resp = client.get(_STATE_URL, params=_GET_SINGLE)
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_id"] is None
        assert data["fresh"] is False
        assert data["snapshot"] is None


# ── AC-P0-2：freshness 用注入时钟（≤30s fresh，≥31s stale，重报重来）──


class TestFreshness:
    """AC-P0-2：freshness 以插件收到时刻计（不信任 client ts）——Agent 拿到的
    界面对象必须有可信的新鲜度语义，过期即如实 stale 而非假装新鲜。
    """

    @pytest.mark.parametrize(
        ("age_seconds", "expect_fresh"),
        [
            (10.0, True),
            (30.0, True),  # 状态机：fresh(收到后 ≤30s)
            (31.0, False),  # AC：≥31s → stale
        ],
        ids=["within-10s", "boundary-30s", "stale-31s"],
    )
    def test_freshness_boundary_driven_by_injected_clock(
        self, clock: _FakeClock, client: TestClient, age_seconds: float, expect_fresh: bool
    ) -> None:
        client.post(_STATE_URL, json=_valid_body())
        clock.advance(age_seconds)

        data = client.get(_STATE_URL, params=_GET_SINGLE).json()
        assert data["fresh"] is expect_fresh

    def test_client_ts_does_not_affect_freshness(self, clock: _FakeClock, client: TestClient) -> None:
        """freshness 只看插件收到时刻：client 自报 ts 漂移（伪造远古/未来时刻）
        不得改变 fresh 判定（冻结件 §四 时间行：不信任 client 时钟）。"""
        drifted = _valid_body(ts=1)  # client 声称 1970 年
        client.post(_STATE_URL, json=drifted)

        data = client.get(_STATE_URL, params=_GET_SINGLE).json()
        assert data["fresh"] is True, "client ts 仅存档，freshness 不依赖它"

    def test_rereport_replaces_snapshot_and_resets_freshness(self, clock: _FakeClock, client: TestClient) -> None:
        """快照生命周期：reported → fresh → stale →(同 client 再报) 替换重来。"""
        client.post(_STATE_URL, json=_valid_body(state={"collapsed": False, "width_ratio": 0.3, "active_view": "sessions", "unread_count": 2}))
        clock.advance(31)
        assert client.get(_STATE_URL, params=_GET_SINGLE).json()["fresh"] is False

        rereport = _valid_body(state={"collapsed": True, "width_ratio": 0.5, "active_view": "tasks", "unread_count": 0})
        client.post(_STATE_URL, json=rereport)

        data = client.get(_STATE_URL, params=_GET_SINGLE).json()
        assert data["fresh"] is True, "同 client 重报后 freshness 重来"
        assert data["snapshot"]["state"] == rereport["state"], "同 client 连发后者替换"


# ── AC-P0-5：多端各占缓存槽，指定端/最近端/全量降序 ────────────────────


class TestMultiClient:
    """AC-P0-5：多端并存各报各缓存（冻结件 §四 并发行）——调试端点必须能
    区分「哪一端上报了什么」，否则多端场景不可观测。"""

    def test_two_clients_pinpoint_latest_and_all_desc(self, clock: _FakeClock, client: TestClient) -> None:
        body_a = _valid_body(client_id="fe-alpha", state={"collapsed": False, "width_ratio": 0.3, "active_view": "sessions", "unread_count": 2})
        body_b = _valid_body(client_id="fe-beta", state={"collapsed": True, "width_ratio": 0.7, "active_view": "tasks", "unread_count": 0})

        client.post(_STATE_URL, json=body_a)
        clock.advance(5)
        client.post(_STATE_URL, json=body_b)

        # 指定端：?client_id= 精确命中对应缓存槽（内容区分，非仅 id 回显）
        picked = client.get(_STATE_URL, params={"region": "sidebar", "client_id": "fe-alpha"}).json()
        assert picked["client_id"] == "fe-alpha"
        assert picked["snapshot"]["state"] == body_a["state"]

        # 默认：返回最近 received 的单端
        latest = client.get(_STATE_URL, params=_GET_SINGLE).json()
        assert latest["client_id"] == "fe-beta"
        assert latest["snapshot"]["state"] == body_b["state"]

        # all=1：全端返回且按 received_at 降序（后到的 fe-beta 在前）
        listing = client.get(_STATE_URL, params={"region": "sidebar", "all": "1"}).json()
        assert set(listing) == {"region", "clients"}
        assert listing["region"] == "sidebar"
        assert [c["client_id"] for c in listing["clients"]] == ["fe-beta", "fe-alpha"]
        received = [c["received_at"] for c in listing["clients"]]
        assert received[0] > received[1], "all=1 按 received_at 降序"
        assert listing["clients"][0]["snapshot"]["state"] == body_b["state"]
        assert listing["clients"][1]["snapshot"]["state"] == body_a["state"]
        for entry in listing["clients"]:
            assert set(entry) == {"client_id", "received_at", "fresh", "snapshot"}


# ── AC-P0-6：POST fail-closed（违规 400，且不落缓存）──────────────────


class TestFailClosedPost:
    """AC-P0-6：插件侧 fail-closed 校验（冻结件 §2.1 + §四 边界表）——
    脏快照入库会污染 Agent 的界面对象，违规必须 400 且零残留。"""

    @pytest.mark.parametrize(
        ("mutate", "case"),
        [
            (lambda b: {k: v for k, v in b.items() if k != "client_id"}, "missing-client_id"),
            (lambda b: {**b, "client_id": ""}, "empty-client_id"),
            (lambda b: {**b, "client_id": "x" * 65}, "client_id-over-64-chars"),
            (lambda b: {**b, "region": "dock"}, "region-not-sidebar"),
            (lambda b: {**b, "items": _items(101)}, "items-over-100"),
            (lambda b: {**b, "sessions": _sessions(51)}, "sessions-over-50"),
            (lambda b: {**b, "state": {**b["state"], "unread_count": -1}}, "negative-unread_count"),
        ],
    )
    def test_violations_rejected_400_without_polluting_cache(
        self, client: TestClient, mutate: Any, case: str
    ) -> None:
        resp = client.post(_STATE_URL, json=mutate(_valid_body()))
        assert resp.status_code == 400, f"违规必须 400（fail-closed）：{case}"

        data = client.get(_STATE_URL, params=_GET_SINGLE).json()
        assert data["snapshot"] is None, f"违规上报不得落缓存：{case}"

    def test_boundary_limits_exactly_accept_100_items_50_sessions(self, client: TestClient) -> None:
        """边界内（items=100 / sessions=50）放行——拒收线在超限处而非整数处。"""
        body = _valid_body(items=_items(100), sessions=_sessions(50))
        resp = client.post(_STATE_URL, json=body)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ── AC-P0-3：ui_describe 无快照→no_snapshot；有快照→ok/stale 且过 schema ──


class TestUiDescribe:
    """AC-P0-3：工具读缓存返回界面对象；输出过 plugin.json 声明的 output_schema
    （tool_core fail-closed 消费——schema 与实际输出脱节 = 工具面直接断）。"""

    @pytest.fixture
    def ui_describe_schema(self) -> dict[str, Any]:
        manifest = json.loads((_PKG / "plugin.json").read_text(encoding="utf-8"))
        tools = manifest.get("capabilities", {}).get("tools", [])
        matches = [t for t in tools if t.get("name") == "ui_describe"]
        assert matches, "plugin.json 未声明 ui_describe 工具（§2.2 声明位置）"
        schema = matches[0].get("output_schema")
        assert isinstance(schema, dict), "ui_describe 未全量声明 output_schema（§2.2）"
        return schema

    def test_manifest_declares_frozen_output_schema(self, ui_describe_schema: dict[str, Any]) -> None:
        """声明面契约（§2.2 逐字）：required [status, region]；status ∈ ok|stale|no_snapshot。"""
        assert set(ui_describe_schema["required"]) == {"status", "region"}
        assert set(ui_describe_schema["properties"]["status"]["enum"]) == {"ok", "stale", "no_snapshot"}
        assert "region" in ui_describe_schema["properties"]
        assert "snapshot" in ui_describe_schema["properties"]

    def test_no_snapshot_reports_no_snapshot_and_passes_schema(
        self, ui_bridge: Any, ui_describe_schema: dict[str, Any]
    ) -> None:
        out = ui_bridge.ui_describe({"region": "sidebar"})
        jsonschema.validate(out, ui_describe_schema)
        assert out["status"] == "no_snapshot"
        assert out["region"] == "sidebar"

    def test_fresh_snapshot_reports_ok_with_complete_snapshot(
        self, clock: _FakeClock, client: TestClient, ui_bridge: Any, ui_describe_schema: dict[str, Any]
    ) -> None:
        body = _valid_body()
        client.post(_STATE_URL, json=body)

        out = ui_bridge.ui_describe({})  # 空参数：region 默认 sidebar（§2.2 input_schema）
        jsonschema.validate(out, ui_describe_schema)
        assert out["status"] == "ok"
        assert out["region"] == "sidebar"

        snapshot = out["snapshot"]
        assert snapshot["state"] == body["state"]
        assert snapshot["sessions"] == body["sessions"]
        assert snapshot["actions"] == body["actions"]
        assert isinstance(snapshot["items"], list) and len(snapshot["items"]) == len(body["items"])

    def test_stale_snapshot_reports_stale_with_snapshot_intact(
        self, clock: _FakeClock, client: TestClient, ui_bridge: Any, ui_describe_schema: dict[str, Any]
    ) -> None:
        body = _valid_body()
        client.post(_STATE_URL, json=body)
        clock.advance(31)

        out = ui_bridge.ui_describe({"region": "sidebar"})
        jsonschema.validate(out, ui_describe_schema)
        assert out["status"] == "stale"
        assert out["snapshot"]["state"] == body["state"], "stale 时快照仍完整返回（§2.2）"

    def test_client_id_targets_specific_slot(
        self, clock: _FakeClock, client: TestClient, ui_bridge: Any, ui_describe_schema: dict[str, Any]
    ) -> None:
        body_a = _valid_body(client_id="fe-alpha", state={"collapsed": False, "width_ratio": 0.3, "active_view": "sessions", "unread_count": 2})
        body_b = _valid_body(client_id="fe-beta", state={"collapsed": True, "width_ratio": 0.7, "active_view": "tasks", "unread_count": 0})
        client.post(_STATE_URL, json=body_a)
        clock.advance(2)
        client.post(_STATE_URL, json=body_b)

        out = ui_bridge.ui_describe({"region": "sidebar", "client_id": "fe-alpha"})
        jsonschema.validate(out, ui_describe_schema)
        assert out["snapshot"]["state"] == body_a["state"], "client_id 定向取对应端的快照"
