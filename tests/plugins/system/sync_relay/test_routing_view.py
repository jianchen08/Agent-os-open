# @feature: FP-0.2.多端同步 2c 分布式路由 | @ci: python-coverage
"""sync_relay routing_view 路由表只读读面测试（ADR 2c 草案 D8：前缀路由数据源）。

断行为（build_routing_view 可观察输出 + http.handle 路由契约）：
- 空注册表 = 合法空视图（不报错，路由侧据此本端自治兜底）；
- 位阶排序 + 在线窗口边界（含恰好压线 = 在线）；
- capabilities 自由 JSON 透传（节点模型扩展子键零耦合）；
- 端点鉴权 = per-device token（与 events GET 同律），声明面登记。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from relay_store import RelayStore
from routing_view import build_routing_view

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 17, 8, 0, 0, tzinfo=UTC)
WINDOW_SECS = 90.0


def _register(
    store: RelayStore,
    device_id: str,
    tier: str,
    *,
    heartbeat_age_secs: float = 0.0,
    capabilities: dict[str, Any] | None = None,
) -> None:
    hb = NOW - timedelta(seconds=heartbeat_age_secs)
    store.upsert_device(
        device_id=device_id,
        tier=tier,
        app_version="1.2.3",
        capabilities=capabilities or {},
        now_iso=hb.isoformat(),
    )


class TestBuildRoutingView:
    def test_empty_registry_is_valid_empty_view(self, tmp_path: Path) -> None:
        view = build_routing_view(
            RelayStore(tmp_path / "relay.db"), now=NOW, window_secs=WINDOW_SECS, requested_by="d-a"
        )
        assert view["devices"] == []
        assert view["online_window_secs"] == WINDOW_SECS
        assert view["requested_by"] == "d-a"

    def test_devices_sorted_by_tier_rank_then_device_id(self, tmp_path: Path) -> None:
        store = RelayStore(tmp_path / "relay.db")
        _register(store, "p-phone", "phone")
        _register(store, "d-desktop", "desktop")
        _register(store, "c-cloud", "cloud")
        _register(store, "x-unknown", "")
        view = build_routing_view(store, now=NOW, window_secs=WINDOW_SECS, requested_by="c-cloud")
        ids = [d["device_id"] for d in view["devices"]]
        ranks = [d["tier_rank"] for d in view["devices"]]
        assert ids == ["c-cloud", "d-desktop", "p-phone", "x-unknown"]
        # 性质断言：rank 非降序（层级权威位阶序，D2 收窄三用之③路由输入）
        assert ranks == sorted(ranks)
        assert set(ranks) == {0, 1, 2, 3}

    def test_online_boundary_inclusive_and_stale_expired(self, tmp_path: Path) -> None:
        store = RelayStore(tmp_path / "relay.db")
        _register(store, "fresh", "desktop", heartbeat_age_secs=0.0)
        _register(store, "boundary", "desktop", heartbeat_age_secs=WINDOW_SECS)
        _register(store, "stale", "desktop", heartbeat_age_secs=WINDOW_SECS + 1.0)
        view = build_routing_view(store, now=NOW, window_secs=WINDOW_SECS, requested_by="fresh")
        online = {d["device_id"]: d["online"] for d in view["devices"]}
        assert online == {"fresh": True, "boundary": True, "stale": False}

    def test_capabilities_passthrough_and_structured_online_fields(
        self, tmp_path: Path
    ) -> None:
        caps = {
            "plugins": {"bash_tool": "1.0.1", "file": "1.0.0"},
            "degraded": ["browser"],
            "resource": {"cpu_cores": 8, "mem_mb": 16384, "battery": {"level": 0.9, "power_mode": "plugged"}},
            "exec_channel": {"mcp_url": "http://127.0.0.1:9201/mcp", "allowed_roots": ["D:\\ws"]},
        }
        store = RelayStore(tmp_path / "relay.db")
        _register(store, "d-desktop", "desktop", capabilities=caps)
        view = build_routing_view(store, now=NOW, window_secs=WINDOW_SECS, requested_by="d-desktop")
        (device,) = view["devices"]
        assert device["capabilities"] == caps
        assert device["last_heartbeat_at"] == NOW.isoformat()
        assert device["app_version"] == "1.2.3"
        assert device["tier"] == "desktop"

    def test_two_devices_same_tier_keep_deterministic_order(self, tmp_path: Path) -> None:
        store = RelayStore(tmp_path / "relay.db")
        _register(store, "desk-b", "desktop")
        _register(store, "desk-a", "desktop")
        view = build_routing_view(store, now=NOW, window_secs=WINDOW_SECS, requested_by="desk-a")
        assert [d["device_id"] for d in view["devices"]] == ["desk-a", "desk-b"]

    def test_address_and_storage_passthrough_only_when_reported(self, tmp_path: Path) -> None:
        """直连面透传（ADR 2026-10-21）：上报了才出现，未上报如实缺省不编造。"""
        store = RelayStore(tmp_path / "relay.db")
        store.upsert_device(
            device_id="d-full",
            tier="desktop",
            app_version="1.0.0",
            capabilities={},
            now_iso=NOW.isoformat(),
            address="http://192.168.1.8:9100",
            storage={"free_bytes": 10, "total_bytes": 20},
        )
        _register(store, "d-bare", "desktop")
        view = build_routing_view(store, now=NOW, window_secs=WINDOW_SECS, requested_by="d-full")
        by_id = {d["device_id"]: d for d in view["devices"]}
        assert by_id["d-full"]["address"] == "http://192.168.1.8:9100"
        assert by_id["d-full"]["storage"] == {"free_bytes": 10, "total_bytes": 20}
        assert "address" not in by_id["d-bare"], "未上报地址的条目不带空串占位"
        assert "storage" not in by_id["d-bare"]

    def test_is_hub_flag_only_marks_configured_hub(self, tmp_path: Path) -> None:
        """is_hub 仅落在 hub_device_id 命中的条目上；未配置时零噪音。"""
        store = RelayStore(tmp_path / "relay.db")
        _register(store, "c-cloud", "cloud")
        _register(store, "d-desktop", "desktop")

        marked = build_routing_view(
            store, now=NOW, window_secs=WINDOW_SECS, requested_by="d-desktop",
            hub_device_id="c-cloud",
        )
        flags = {d["device_id"]: d.get("is_hub") for d in marked["devices"]}
        assert flags == {"c-cloud": True, "d-desktop": None}

        plain = build_routing_view(store, now=NOW, window_secs=WINDOW_SECS, requested_by="d-desktop")
        assert all("is_hub" not in d for d in plain["devices"]), "未配置中枢身份时键整体缺席"


class TestRoutingViewEndpoint:
    @pytest.fixture
    def relay(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("AGENTOS_SYNC_RELAY_TOKENS", "device-a:tok-a,device-b:tok-b")
        import server as relay_server

        module = __import__("importlib", fromlist=["reload"]).reload(relay_server)
        import asyncio

        asyncio.run(module._on_load({}))
        yield module
        asyncio.run(module._on_unload({}))

    def _register_via_endpoint(self, relay: Any, device: str, token: str, caps: dict[str, Any]) -> None:
        import asyncio

        resp = asyncio.run(
            relay.http_handle(
                path="/ext/sync_relay/devices",
                method="POST",
                raw_body=json.dumps(
                    {"device_id": device, "tier": "desktop", "app_version": "1.0.0", "capabilities": caps}
                ),
                headers={"authorization": f"Bearer {token}"},
            )
        )
        assert resp["data"]["status"] == 200

    def _get_routing_view(self, relay: Any, token: str) -> tuple[int, dict[str, Any]]:
        import asyncio
        import base64

        resp = asyncio.run(
            relay.http_handle(
                path="/ext/sync_relay/routing_view",
                method="GET",
                headers={"authorization": f"Bearer {token}"},
            )
        )
        data = resp["data"]
        body = json.loads(base64.b64decode(data["body"]))
        return int(data["status"]), body

    def test_route_requires_per_device_token(self, relay: Any) -> None:
        status, _ = self._get_routing_view(relay, "wrong-token")
        assert status == 403

    def test_route_returns_view_with_requester_bound_device(self, relay: Any) -> None:
        self._register_via_endpoint(relay, "device-a", "tok-a", {"resource": {"cpu_cores": 4}})
        self._register_via_endpoint(relay, "device-b", "tok-b", {"resource": {"cpu_cores": 16}})
        status, body = self._get_routing_view(relay, "tok-a")
        assert status == 200
        assert body["requested_by"] == "device-a"
        assert {d["device_id"] for d in body["devices"]} == {"device-a", "device-b"}

    def test_manifest_declares_routing_view_endpoint(self) -> None:
        manifest = json.loads(
            (Path(__file__).resolve().parents[4] / "plugins" / "shared" / "sync_relay" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        routes = {e["path"]: e for e in manifest["http_endpoints"]}
        entry = routes["/ext/sync_relay/routing_view"]
        assert entry["method"] == "GET"
        assert entry["auth"] == "none"  # per-device token 自管（与 events GET 同律）
