# @feature: FP-0.2.多端同步 2b sync_relay | @ci: python-coverage
"""sync_relay server 端点分发测试（http.handle 契约：路由表/鉴权/响应形态）。"""

from __future__ import annotations

import importlib
from datetime import UTC
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """装好内存态的 server 模块（store 指向 tmp、tokens 注入）。"""
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTOS_SYNC_RELAY_TOKENS", "device-a:tok-a,device-b:tok-b")
    import server as relay_server

    module = importlib.reload(relay_server)
    # server 模块裸名导入（plugins/shared 也可能被其他测试先缓存）——reload 前
    # 依赖 conftest 的源目录晋升保证解析到本插件文件。
    import asyncio

    asyncio.run(module._on_load({}))
    yield module
    asyncio.run(module._on_unload({}))


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    """ToolExecutionResult/HttpHandleResponse → {status, body dict}（内核契约解包）。"""
    import base64
    import json

    data = resp["data"]
    body = json.loads(base64.b64decode(data["body"]))
    return {"success": resp.get("success"), "status": data["status"], "body": body}


def _post(
    module: Any,
    path: str,
    body: dict[str, Any] | None,
    token: str = "tok-a",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    import json

    headers = {"authorization": f"Bearer {token}", **(extra_headers or {})}
    resp = _run(
        module.http_handle(
            path=path,
            method="POST",
            raw_body=json.dumps(body) if body is not None else "",
            headers=headers,
        )
    )
    return _unwrap(resp)


def _get(
    module: Any, path: str, token: str | None = None, query: dict[str, str] | None = None
) -> dict[str, Any]:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    resp = _run(module.http_handle(path=path, method="GET", headers=headers, query=query or {}))
    return _unwrap(resp)


def _run(coro: Any) -> dict[str, Any]:
    import asyncio

    return asyncio.run(coro)


def _event(ulid: str, device: str = "device-a", physical: int = 1_000) -> dict[str, Any]:
    return {
        "ulid": ulid,
        "device_id": device,
        "asset_type": "config",
        "asset_id": "policies.yaml",
        "version": 1,
        "updated_at": "2026-10-15T07:00:00+00:00",
        "payload": {"content": "a: 1"},
        "hlc": {"physical_ms": physical, "logical": 0, "device_id": device},
    }


def test_unknown_route_404(relay: Any) -> None:
    resp = _unwrap(_run(relay.http_handle(path="/ext/sync_relay/nope", method="GET")))
    assert resp["status"] == 404


def test_receive_and_pull_roundtrip(relay: Any) -> None:
    resp = _post(relay, "/ext/sync_relay/events", {"events": [_event("01ARZ3NDEKTSV4RRFFQ69G5FAV")]})
    assert resp["status"] == 200
    assert resp["body"]["accepted"] == 1

    pulled = _get(relay, "/ext/sync_relay/events", token="tok-b", query={"since_cursor": "0"})
    assert pulled["status"] == 200
    assert pulled["body"]["count"] == 1
    event = pulled["body"]["events"][0]
    assert event["ulid"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert event["cursor"] >= 1
    assert pulled["body"]["device"] == "device-b", "拉取身份来自 token 绑定"


def test_pull_requires_token(relay: Any) -> None:
    resp = _get(relay, "/ext/sync_relay/events", query={"since_cursor": "0"})
    assert resp["status"] == 403


def test_register_and_heartbeat_then_devices_admin_view(relay: Any) -> None:
    reg = _post(
        relay, "/ext/sync_relay/devices",
        {"device_id": "device-a", "tier": "desktop", "app_version": "1.2.3"},
    )
    assert reg["status"] == 200
    assert reg["body"]["registered"]["tier"] == "desktop"

    hb = _post(
        relay, "/ext/sync_relay/devices/heartbeat",
        {"device_id": "device-a", "tier": "desktop"},
        token="tok-a",
    )
    assert hb["status"] == 200

    devices = _get(relay, "/ext/sync_relay/devices")
    assert devices["status"] == 200
    rows = {d["device_id"]: d for d in devices["body"]["devices"]}
    assert rows["device-a"]["online"] is True
    assert rows["device-a"]["app_version"] == "1.2.3"


def test_heartbeat_with_capabilities_updates_registry(relay: Any) -> None:
    """心跳携带能力面 → 注册表能力刷新（2c D8：exec_channel 子键随心跳更新）。"""
    _post(relay, "/ext/sync_relay/devices", {"device_id": "device-a", "tier": "desktop"})
    hb = _post(
        relay,
        "/ext/sync_relay/devices/heartbeat",
        {
            "device_id": "device-a",
            "tier": "desktop",
            "capabilities": {
                "asset_types": ["config"],
                "exec_channel": {"mcp_url": "http://x/mcp", "allowed_roots": ["D:/ws"]},
            },
        },
        token="tok-a",
    )
    assert hb["status"] == 200
    view = _get(relay, "/ext/sync_relay/routing_view", token="tok-a")
    row = next(d for d in view["body"]["devices"] if d["device_id"] == "device-a")
    assert row["capabilities"]["exec_channel"]["allowed_roots"] == ["D:/ws"]


def test_heartbeat_without_capabilities_preserves_registry(relay: Any) -> None:
    """心跳不带能力面 → 只 touch 在线时刻，注册表既有能力零漂移（旧端兼容）。"""
    _post(
        relay,
        "/ext/sync_relay/devices",
        {"device_id": "device-a", "tier": "desktop", "capabilities": {"asset_types": ["config"]}},
    )
    hb = _post(relay, "/ext/sync_relay/devices/heartbeat", {"device_id": "device-a"}, token="tok-a")
    assert hb["status"] == 200
    view = _get(relay, "/ext/sync_relay/routing_view", token="tok-a")
    row = next(d for d in view["body"]["devices"] if d["device_id"] == "device-a")
    assert row["capabilities"] == {"asset_types": ["config"]}


def test_register_device_mismatch_rejected(relay: Any) -> None:
    resp = _post(
        relay, "/ext/sync_relay/devices",
        {"device_id": "device-b", "tier": "phone"},
        token="tok-a",
    )
    assert resp["status"] == 403


def test_bootstrap_returns_baseline_and_cursor(relay: Any) -> None:
    _post(relay, "/ext/sync_relay/devices", {"device_id": "device-a", "tier": "cloud"})
    _post(
        relay, "/ext/sync_relay/events",
        {"events": [
            _event("01ARZ3NDEKTSV4RRFFQ69G5FAV", device="device-a", physical=1_000),
            _event("01ARZ3NDEKTSV4RRFFQ69G5FAW", device="device-a", physical=2_000),
        ]},
    )
    boot = _get(relay, "/ext/sync_relay/bootstrap", token="tok-b")
    assert boot["status"] == 200
    body = boot["body"]
    # 同一资产两条：基线只发位阶/HLC 裁定后的一条，cursor 定位账本头。
    assert len(body["events"]) == 1
    assert body["events"][0]["hlc"]["physical_ms"] == 2_000
    assert body["cursor"] >= 2
    assert body["tiers"]["device-a"] == "cloud"


def test_dead_letters_admin_view(relay: Any) -> None:
    _post(relay, "/ext/sync_relay/events", {"events": [_event("01ARZ3NDEKTSV4RRFFQ69G5FAV") | {"hlc": None}]})
    resp = _get(relay, "/ext/sync_relay/dead_letters")
    assert resp["status"] == 200
    assert resp["body"]["dead_letters"], "缺 HLC 拒收留痕"


def test_enroll_route_flow_issues_working_token(relay: Any) -> None:
    """登录即入网路由（ADR 2026-11-02-login-as-enrollment）：直签发（admin 面）
    → 新 token 全链路通行（心跳/拉取/注册表）。"""
    enrolled = _post(
        relay,
        "/ext/sync_relay/enroll",
        {"device_id": "phone-join", "tier": "phone", "app_version": "1.1.0"},
        token="",
    )
    assert enrolled["status"] == 200, "enroll 为 admin 声明面（内核已验后放行到 handler）"
    assert enrolled["body"]["device_token"], "直签发返回正式 per-device token"
    assert "relay_url" not in enrolled["body"], "调用方寻址即事实地址，响应无回显字段"

    device_token = enrolled["body"]["device_token"]
    hb = _post(relay, "/ext/sync_relay/devices/heartbeat", {"device_id": "phone-join"}, token=device_token)
    assert hb["status"] == 200, "签发 token 即时通行（动态鉴权源，无需重载）"
    pulled = _get(relay, "/ext/sync_relay/events", token=device_token, query={"since_cursor": "0"})
    assert pulled["status"] == 200
    assert pulled["body"]["device"] == "phone-join"

    devices = _get(relay, "/ext/sync_relay/devices")
    rows = {d["device_id"]: d for d in devices["body"]["devices"]}
    assert rows["phone-join"]["tier"] == "phone", "直签发即注册入设备注册表"

    reenrolled = _post(
        relay,
        "/ext/sync_relay/enroll",
        {"device_id": "phone-join", "tier": "phone", "app_version": "1.1.0"},
        token="",
    )
    assert reenrolled["status"] == 200, "重复入网幂等换发（upsert 顶旧绑定）"
    assert reenrolled["body"]["device_token"] != device_token, "换发产生新 token，旧 token 失效"
    stale = _post(relay, "/ext/sync_relay/devices/heartbeat", {"device_id": "phone-join"}, token=device_token)
    assert stale["status"] == 403, "旧 token 换发后不通行"


def test_enroll_carries_kernel_tenant_header(relay: Any) -> None:
    """租户取内核注入的 x-agentos-tenant（签发时租户上下文落到 token 绑定）。"""
    enrolled = _post(
        relay,
        "/ext/sync_relay/enroll",
        {"device_id": "phone-t", "tier": "phone", "app_version": "1"},
        token="",
        extra_headers={"x-agentos-tenant": "tenant-home"},
    )
    assert enrolled["status"] == 200
    assert enrolled["body"]["tenant_id"] == "tenant-home", "内核租户头透传到签发结果"


def test_enroll_rejects_non_json_body(relay: Any) -> None:
    resp = _unwrap(
        _run(
            relay.http_handle(
                path="/ext/sync_relay/enroll", method="POST", raw_body="not-json{", headers={}
            )
        )
    )
    assert resp["status"] == 400


def test_enroll_rejects_missing_device_id(relay: Any) -> None:
    missing = _post(relay, "/ext/sync_relay/enroll", {"tier": "phone", "app_version": ""}, token="")
    assert missing["status"] == 400, "device_id 必填（设备自报身份）"


def test_register_carries_address_and_storage_then_heartbeat_updates(relay: Any) -> None:
    """设备卡数据源：注册携带 address/storage 落注册表；心跳刷新地址、未携带保留原值。"""
    reg = _post(
        relay,
        "/ext/sync_relay/devices",
        {
            "device_id": "device-a",
            "tier": "desktop",
            "app_version": "1.2.3",
            "address": "http://192.168.1.10:9100",
            "storage": {"free_bytes": 12_000_000_000, "total_bytes": 64_000_000_000},
        },
    )
    assert reg["status"] == 200
    assert reg["body"]["registered"]["address"] == "http://192.168.1.10:9100"
    assert reg["body"]["registered"]["storage"]["free_bytes"] == 12_000_000_000

    hb = _post(
        relay,
        "/ext/sync_relay/devices/heartbeat",
        {
            "device_id": "device-a",
            "tier": "desktop",
            "capabilities": {"asset_types": ["config"]},
            "address": "http://192.168.1.20:9100",
        },
        token="tok-a",
    )
    assert hb["status"] == 200
    devices = _get(relay, "/ext/sync_relay/devices")
    row = next(d for d in devices["body"]["devices"] if d["device_id"] == "device-a")
    assert row["address"] == "http://192.168.1.20:9100"
    assert row["storage"]["total_bytes"] == 64_000_000_000, "心跳未携带 storage 时保留原值"


def test_register_without_address_keeps_optional_fields_empty(relay: Any) -> None:
    """老端注册不带 address/storage → 可选字段空呈现（不编数据）。"""
    reg = _post(relay, "/ext/sync_relay/devices", {"device_id": "device-a", "tier": "desktop"})
    assert reg["status"] == 200
    assert reg["body"]["registered"]["address"] == ""
    assert reg["body"]["registered"]["storage"] is None


# ── 中枢拉取即 touch + is_hub 标记（ADR 2026-10-21-device-card-direct-connect）──


@pytest.fixture
def hub_relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """hub_device_id 已配置的 server 模块（中枢卡拉取证据优先语义面）。"""
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTOS_SYNC_RELAY_TOKENS", "device-a:tok-a,device-b:tok-b")
    import server as relay_server

    module = importlib.reload(relay_server)
    module.plugin._injected_config = {"settings": {"hub_device_id": "device-a"}}
    import asyncio

    asyncio.run(module._on_load({}))
    yield module
    asyncio.run(module._on_unload({}))


def test_routing_view_pull_touches_stale_hub_entry(hub_relay: Any) -> None:
    """任何已配对设备的成功拉取 = 中枢网络面活性证据 → 心跳过期的中枢条目被 touch 翻在线。"""
    from datetime import datetime, timedelta

    hub_relay._store.upsert_device(
        device_id="device-a",
        tier="cloud",
        app_version="1.0.0",
        capabilities={},
        now_iso=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    hub_relay._store.upsert_device(
        device_id="device-b",
        tier="desktop",
        app_version="1.0.0",
        capabilities={},
        now_iso=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    stale = hub_relay._store.get_device("device-a")
    assert stale is not None
    resp = _get(hub_relay, "/ext/sync_relay/routing_view", token="tok-b")
    assert resp["status"] == 200
    by_id = {d["device_id"]: d for d in resp["body"]["devices"]}
    assert by_id["device-a"]["online"] is True, "拉取即证据：中枢条目心跳被 touch 刷新"
    assert by_id["device-a"]["is_hub"] is True
    assert "is_hub" not in by_id["device-b"], "非中枢条目不带标记"


def test_routing_view_without_hub_config_no_touch_no_flag(relay: Any) -> None:
    """未配置 hub_device_id：pull 不触碰任何条目、视图零 is_hub 噪音（行为不变）。"""
    import json
    from datetime import datetime, timedelta

    relay._store.upsert_device(
        device_id="device-a",
        tier="desktop",
        app_version="1.0.0",
        capabilities={},
        now_iso=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    body = json.dumps(
        {"device_id": "device-b", "tier": "desktop", "app_version": "1.0.0"}
    )
    _run(
        relay.http_handle(
            path="/ext/sync_relay/devices",
            method="POST",
            raw_body=body,
            headers={"authorization": "Bearer tok-b"},
        )
    )
    status = _get(relay, "/ext/sync_relay/routing_view", token="tok-b")
    assert status["status"] == 200
    by_id = {d["device_id"]: d for d in status["body"]["devices"]}
    assert by_id["device-a"]["online"] is False, "无中枢配置：过期心跳不被拉取翻绿"
    assert all("is_hub" not in d for d in status["body"]["devices"])


# ── 全网注册表复制快照 + 对等信任哈希（A 档，ADR
#    2026-10-22-distributed-registry-peer-trust）──────────────────────────


def test_registry_snapshot_requires_token_and_carries_hash(relay: Any) -> None:
    """复制快照：非成员零暴露（403）；成员拿全量表（含哈希），明文 token 恒不出域。"""
    import hashlib
    import json

    anon = _unwrap(_run(relay.http_handle(path="/ext/sync_relay/registry", method="GET")))
    assert anon["status"] == 403

    _post(relay, "/ext/sync_relay/devices/heartbeat", {"device_id": "device-a", "tier": "desktop"})
    _post(
        relay,
        "/ext/sync_relay/devices/heartbeat",
        {"device_id": "device-b", "tier": "phone"},
        token="tok-b",
    )
    resp = _get(relay, "/ext/sync_relay/registry", token="tok-b")
    assert resp["status"] == 200
    body = resp["body"]
    by_id = {d["device_id"]: d for d in body["devices"]}
    assert set(by_id) == {"device-a", "device-b"}, "快照 = 全网注册表（非请求方自己的行）"
    assert by_id["device-a"]["token_hash"] == hashlib.sha256(b"tok-a").hexdigest()
    assert by_id["device-b"]["token_hash"] == hashlib.sha256(b"tok-b").hexdigest()
    assert by_id["device-a"]["online"] is True
    assert body["requested_by"] == "device-b"
    assert body["fetched_at"]
    rendered = json.dumps(body)
    assert "tok-a" not in rendered, "明文 token 不出库"
    assert "tok-b" not in rendered, "明文 token 不出库"


def test_register_backfills_token_hash_for_env_preset_token(relay: Any) -> None:
    """env 预置 token 无兑换签发面：注册时哈希随行回填（A 档迁移面）。"""
    import hashlib

    _post(relay, "/ext/sync_relay/devices", {"device_id": "device-a"})
    row = relay._store.get_device("device-a")
    assert row is not None
    assert row["token_hash"] == hashlib.sha256(b"tok-a").hexdigest()


def test_heartbeat_backfills_hash_for_legacy_row(relay: Any) -> None:
    """存量行（列新增前注册）：一次心跳即自愈收敛（换绑 token 同理）。"""
    import hashlib

    _post(relay, "/ext/sync_relay/devices", {"device_id": "device-a"})
    relay._store.set_device_token_hash(device_id="device-a", token_hash="")
    assert relay._store.get_device("device-a")["token_hash"] == ""
    _post(relay, "/ext/sync_relay/devices/heartbeat", {"device_id": "device-a"})
    assert relay._store.get_device("device-a")["token_hash"] == hashlib.sha256(
        b"tok-a"
    ).hexdigest()


def test_heartbeat_loopback_address_is_normalized_to_empty(relay: Any) -> None:
    """回环自报入库即清（全网消费者必得死地址；本机找自己不经注册表）。"""
    _post(
        relay,
        "/ext/sync_relay/devices",
        {"device_id": "device-a", "address": "http://127.0.0.1:9100"},
    )
    assert relay._store.get_device("device-a")["address"] == ""
    _post(
        relay,
        "/ext/sync_relay/devices/heartbeat",
        {"device_id": "device-b", "address": "http://192.168.1.10:9100"},
        token="tok-b",
    )
    assert relay._store.get_device("device-b")["address"] == "http://192.168.1.10:9100", (
        "局域网自报不受影响"
    )
