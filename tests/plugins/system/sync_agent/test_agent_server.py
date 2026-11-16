# @feature: FP-0.2.多端同步 2b sync_agent | @ci: python-coverage
"""sync_agent server 装配测试：未配对装载、登录入网流（直签发→凭据→引擎重建）、
解析优先级、状态面。"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **env: str) -> Any:
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import server as agent_server

    return importlib.reload(agent_server)


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    data = resp["data"]
    body = json.loads(base64.b64decode(data["body"]))
    return {"success": resp.get("success"), "status": data["status"], "body": body}


def _post(module: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = asyncio.run(
        module.http_handle(path=path, method="POST", raw_body=json.dumps(body))
    )
    return _unwrap(resp)


def test_unpaired_loads_idle_for_enroll(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """入网流契约：无 relay/token 配置不拒载——引擎停转、paired=false、状态可查。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    try:
        assert module._engine is None, "未配对引擎必须停转"
        assert module._assembly is not None, "装配面在（enroll 端点可用）"
        status = asyncio.run(module.get_status())
        assert status["loaded"] is True
        assert status["paired"] is False
        assert status["device_id"]
    finally:
        asyncio.run(module._on_unload({}))


def test_post_enroll_and_error_message_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """_post_enroll 出站透传（_http_json monkeypatch 面）+ 错误体三分支翻译。"""
    module = _reload(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_http_json(
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, dict[str, Any] | None]:
        captured.update(method=method, url=url, token=token, payload=payload, timeout=timeout)
        return 200, {"device_token": "t"}

    monkeypatch.setattr(module, "_http_json", fake_http_json)
    status, body = module._post_enroll("http://r/ext/sync_relay/enroll", "jwt", {"a": 1})
    assert (status, body) == (200, {"device_token": "t"})
    assert captured == {
        "method": "POST",
        "url": "http://r/ext/sync_relay/enroll",
        "token": "jwt",
        "payload": {"a": 1},
        "timeout": 30.0,
    }

    assert module._relay_error_message({"error": {"code": "x", "message": "m"}}) == "m"
    assert module._relay_error_message({"error": "原样串"}) == "原样串"
    assert module._relay_error_message({}) == ""
    assert module._relay_error_message(None) == ""


def test_enroll_persists_credential_and_starts_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """登录入网流：直签发成功 → 凭据落 data/ → 引擎按入网 relay 地址即时重建。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    relay_url = "http://192.168.1.10:9300/ext/sync_relay/"

    def fake_enroll(
        url: str, admin_jwt: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        assert url == "http://192.168.1.10:9300/ext/sync_relay/enroll", "直签发打到入网基址（尾斜杠收敛）"
        assert admin_jwt == "jwt-admin-1", "admin JWT 原样作 Bearer（登录即入网凭据锚）"
        assert payload["device_id"], "直签发携带本端设备身份"
        return 200, {"device_token": "tok-issued-1", "tenant_id": "tenant-x"}

    monkeypatch.setattr(module, "_post_enroll", fake_enroll)
    try:
        resp = _post(
            module,
            "/ext/sync_agent/enroll",
            {"relay_url": relay_url, "admin_jwt": "jwt-admin-1"},
        )
        assert resp["status"] == 200, resp
        assert resp["body"]["paired"] is True
        assert resp["body"]["relay_url"] == "http://192.168.1.10:9300/ext/sync_relay"
        assert module._engine is not None, "入网后引擎即时重建"
        assert module._engine.config.relay_url == "http://192.168.1.10:9300/ext/sync_relay"
        assert module._engine.config.token == "tok-issued-1"
        state = module._assembly.state
        assert state.load_credential("device_token") == "tok-issued-1", "凭据持久化（重启续效）"
        assert state.load_credential("relay_url") == "http://192.168.1.10:9300/ext/sync_relay"
        assert state.load_credential("tenant") == "tenant-x"
        status = asyncio.run(module.get_status())
        assert status["paired"] is True
    finally:
        asyncio.run(module._on_unload({}))


def test_enroll_resets_pairing_ledger_keeps_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """重入网清账：游标/资产/已应用作废（needs_bootstrap 转真），HLC 时钟保留。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    state = module._assembly.state
    from hlc_clock import HlcTimestamp

    state.save_clock(HlcTimestamp(physical_ms=5_000, logical=2, device_id="dev"))
    state.save_cursor(42)

    def fake_enroll(
        url: str, admin_jwt: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        return 200, {"device_token": "tok-issued-2"}

    monkeypatch.setattr(module, "_post_enroll", fake_enroll)
    try:
        resp = _post(
            module,
            "/ext/sync_agent/enroll",
            {"relay_url": "http://r/ext/sync_relay", "admin_jwt": "jwt"},
        )
        assert resp["status"] == 200
        assert state.load_cursor() is None, "旧账本游标作废"
        assert state.asset_count() == 0
        clock = state.load_clock()
        assert clock is not None, "HLC 时钟跨入网保留"
        assert clock[1] == (5_000, 2), "HLC 时钟跨入网单调保留"
        assert module._engine is not None
        assert module._engine.needs_bootstrap()
    finally:
        asyncio.run(module._on_unload({}))


def test_enroll_rejects_missing_fields_without_side_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """入网体违约（缺 relay_url/缺 admin_jwt/空体）即拒：不产生凭据、不起引擎。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    state = module._assembly.state
    try:
        for bad in (
            {"admin_jwt": "jwt"},
            {"relay_url": "http://r/ext/sync_relay"},
            {},
        ):
            resp = _post(module, "/ext/sync_agent/enroll", bad)
            assert resp["status"] == 400, f"入网体违约应 400: {bad!r}"
        assert state.load_credential("device_token") is None
        assert module._engine is None
    finally:
        asyncio.run(module._on_unload({}))


def test_enroll_translates_upstream_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """上游翻译：relay 拒绝（错误体 message）与不可达（status=0）均 502 上抛，不落凭据。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    body = {"relay_url": "http://r/ext/sync_relay", "admin_jwt": "jwt"}

    def refusing(url: str, admin_jwt: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        return 401, {"error": {"code": "auth", "message": "入网凭据未通过核验"}}

    monkeypatch.setattr(module, "_post_enroll", refusing)
    try:
        resp = _post(module, "/ext/sync_agent/enroll", body)
        assert resp["status"] == 502
        assert "未通过核验" in resp["body"]["error"]["message"]
        assert module._assembly.state.load_credential("device_token") is None
        assert module._engine is None
    finally:
        asyncio.run(module._on_unload({}))

    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))

    def unreachable(url: str, admin_jwt: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        return 0, None

    monkeypatch.setattr(module, "_post_enroll", unreachable)
    try:
        resp = _post(module, "/ext/sync_agent/enroll", body)
        assert resp["status"] == 502
        assert "不可达" in resp["body"]["error"]["message"]
        assert module._assembly.state.load_credential("device_token") is None
    finally:
        asyncio.run(module._on_unload({}))


def test_identity_payload_carries_exec_channel_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """入网身份载荷同刀携带 exec_channel 能力子键（D8；未启用 = 子键缺席）。"""
    module = _reload(monkeypatch, tmp_path, AGENTOS_EXEC_MCP_URL="http://127.0.0.1:9103/mcp")
    asyncio.run(module._on_load({}))
    try:
        capabilities = module._identity_payload()["capabilities"]
        assert capabilities["exec_channel"] == {
            "mcp_url": "http://127.0.0.1:9103/mcp",
            "allowed_roots": [],
            "protocol_version": "1",
        }
    finally:
        asyncio.run(module._on_unload({}))


def test_identity_payload_without_exec_channel_omits_subkey(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    try:
        assert "exec_channel" not in module._identity_payload()["capabilities"]
    finally:
        asyncio.run(module._on_unload({}))


def test_resolve_pairing_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """解析序：env > 配对凭据 > settings.yaml（relay_url/token 同源成对解析）。"""
    module = _reload(monkeypatch, tmp_path)
    state = module.AgentStateStore(tmp_path / "s.db")
    settings = {"relay_url": "http://settings-relay/ext/sync_relay"}
    # 仅 settings：兜底
    monkeypatch.delenv("AGENTOS_SYNC_RELAY_URL", raising=False)
    monkeypatch.delenv("AGENTOS_SYNC_TOKEN", raising=False)
    assert module._resolve_pairing(settings, state) == ("http://settings-relay/ext/sync_relay", "")
    # 凭据 > settings
    state.save_credential("relay_url", "http://cred-relay/ext/sync_relay")
    state.save_credential("device_token", "tok-cred")
    assert module._resolve_pairing(settings, state) == (
        "http://cred-relay/ext/sync_relay",
        "tok-cred",
    )
    # env > 凭据
    monkeypatch.setenv("AGENTOS_SYNC_RELAY_URL", "http://env-relay/ext/sync_relay/")
    monkeypatch.setenv("AGENTOS_SYNC_TOKEN", "tok-env")
    url, token = module._resolve_pairing(settings, state)
    assert (url, token) == ("http://env-relay/ext/sync_relay", "tok-env"), "env 优先且尾斜杠收敛"


def test_env_wins_over_settings_and_engine_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    module = _reload(
        monkeypatch,
        tmp_path,
        AGENTOS_SYNC_RELAY_URL="http://127.0.0.1:1/ext/sync_relay/",
        AGENTOS_SYNC_TOKEN="tok-x",
        AGENTOS_DEVICE_ID="dev-env",
        AGENTOS_DEVICE_TIER="phone",
        AGENTOS_SYNC_WATCH_DIR=str(watch),
        AGENTOS_SYNC_POLL_INTERVAL="0.5",
    )
    asyncio.run(module._on_load({}))
    try:
        engine = module._engine
        assert engine is not None
        cfg = engine.config
        assert cfg.relay_url == "http://127.0.0.1:1/ext/sync_relay", "尾斜杠收敛"
        assert cfg.device_id == "dev-env"
        assert cfg.device_tier == "phone"
        assert cfg.watch_root == watch
        assert cfg.poll_interval_secs == 0.5
        assert cfg.heartbeat_interval_secs == 30.0, "未注入用缺省"
        assert asyncio.run(module.get_status())["paired"] is True
    finally:
        asyncio.run(module._on_unload({}))
    assert module._engine is None


def test_bad_tier_refuses_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    module = _reload(
        monkeypatch,
        tmp_path,
        AGENTOS_SYNC_RELAY_URL="http://r/ext/sync_relay",
        AGENTOS_SYNC_TOKEN="t",
        AGENTOS_DEVICE_TIER="watch",
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(module._on_load({}))
    assert "device_tier" in str(excinfo.value)


def test_get_status_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    module = _reload(
        monkeypatch,
        tmp_path,
        AGENTOS_SYNC_RELAY_URL="http://r/ext/sync_relay",
        AGENTOS_SYNC_TOKEN="t",
        AGENTOS_SYNC_WATCH_DIR=str(tmp_path),
    )
    unloaded = asyncio.run(module.get_status())
    assert unloaded == {"loaded": False}

    asyncio.run(module._on_load({}))
    try:
        status = asyncio.run(module.get_status())
        assert status["loaded"] is True
        assert status["paired"] is True
        assert status["bootstrap_done"] is False
        assert status["pending"] == 0
        assert status["device_id"], "device_id 缺省取 hostname 非空"
        assert status["relay_url"] == "http://r/ext/sync_relay", "状态面带 relay 地址（设备页直连中枢内核数据源）"
    finally:
        asyncio.run(module._on_unload({}))


def test_status_endpoint_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """/ext/sync_agent/status 面 paired 可查（设置页同步区数据源）。"""
    module = _reload(
        monkeypatch,
        tmp_path,
        AGENTOS_SYNC_RELAY_URL="http://r/ext/sync_relay",
        AGENTOS_SYNC_TOKEN="t",
        AGENTOS_SYNC_WATCH_DIR=str(tmp_path),
    )
    asyncio.run(module._on_load({}))
    try:
        resp = _unwrap(asyncio.run(module.http_handle(path="/ext/sync_agent/status", method="GET")))
        assert resp["status"] == 200
        assert resp["body"]["paired"] is True
    finally:
        asyncio.run(module._on_unload({}))


# ── 覆盖补刃：enroll 卫语句 / 上游翻译 / 重入网停机 / 路由兜底 ──

def test_enroll_routes_and_guards(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """路由兜底与卫语句：未装载 503/未知路由 404/非 JSON 体 400/缺入网体字段 400。"""
    module = _reload(monkeypatch, tmp_path)
    # 未装载（_assembly None）：enroll 503、status loaded False、未知路由 404
    resp = _post(module, "/ext/sync_agent/enroll", {"relay_url": "http://r/ext/sync_relay"})
    assert resp["status"] == 503
    resp = _unwrap(asyncio.run(module.http_handle(path="/ext/sync_agent/status", method="GET")))
    assert resp["body"] == {"loaded": False}
    resp = _unwrap(asyncio.run(module.http_handle(path="/ext/sync_agent/nope", method="GET")))
    assert resp["status"] == 404

    asyncio.run(module._on_load({}))
    try:
        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/enroll", method="POST", raw_body="not-json{"
                )
            )
        )
        assert resp["status"] == 400
        assert "JSON 对象" in resp["body"]["error"]["message"]
        # 形态合法但缺 relay 基址 / 缺 admin JWT
        resp = _post(module, "/ext/sync_agent/enroll", {"admin_jwt": "jwt"})
        assert resp["status"] == 400
        assert "relay_url" in resp["body"]["error"]["message"]
        resp = _post(module, "/ext/sync_agent/enroll", {"relay_url": "http://r/ext/sync_relay"})
        assert resp["status"] == 400
        assert "admin_jwt" in resp["body"]["error"]["message"]
    finally:
        asyncio.run(module._on_unload({}))


def test_enroll_missing_device_token_is_upstream_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """签发响应缺 device_token = relay 协议违约 → 502，不落凭据。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))

    def empty_enroll(
        url: str, admin_jwt: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        return 200, {}

    monkeypatch.setattr(module, "_post_enroll", empty_enroll)
    try:
        resp = _post(
            module,
            "/ext/sync_agent/enroll",
            {"relay_url": "http://r/ext/sync_relay", "admin_jwt": "jwt"},
        )
        assert resp["status"] == 502
        assert "device_token" in resp["body"]["error"]["message"]
        assert module._assembly.state.load_credential("device_token") is None
        assert module._engine is None
    finally:
        asyncio.run(module._on_unload({}))


def test_repair_while_paired_stops_running_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """已入网再 enroll（重入网）：旧引擎停机重建，凭据顶旧。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))

    def enroll_seq(
        url: str, admin_jwt: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        relay_host = url.split("//")[1].split("/")[0]
        return 200, {"device_token": f"tok-{relay_host}"}

    monkeypatch.setattr(module, "_post_enroll", enroll_seq)
    try:
        first = _post(
            module,
            "/ext/sync_agent/enroll",
            {"relay_url": "http://r1/ext/sync_relay", "admin_jwt": "jwt"},
        )
        assert first["status"] == 200
        assert module._engine is not None
        first_engine = module._engine

        second = _post(
            module,
            "/ext/sync_agent/enroll",
            {"relay_url": "http://r2/ext/sync_relay", "admin_jwt": "jwt"},
        )
        assert second["status"] == 200
        assert module._engine is not None
        assert module._engine is not first_engine, "旧引擎已停机，新引擎重建"
        assert module._engine.config.relay_url == "http://r2/ext/sync_relay", "凭据顶旧"
        assert module._engine.config.token == "tok-r2", "token 取直签发结果"
    finally:
        asyncio.run(module._on_unload({}))


def test_status_route_unpaired_reports_paired_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """已装载未配对：/ext/sync_agent/status 路由面 paired=false（设置页配对引导）。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    try:
        resp = _unwrap(asyncio.run(module.http_handle(path="/ext/sync_agent/status", method="GET")))
        assert resp["status"] == 200
        assert resp["body"]["loaded"] is True
        assert resp["body"]["paired"] is False
    finally:
        asyncio.run(module._on_unload({}))


# ── 云端权威设备注册表透传（R30 数据面销项：设备页接生产权威）──────────

_ROUTING_VIEW: dict[str, Any] = {
    "devices": [
        {
            "device_id": "phone-b",
            "tier": "phone",
            "tier_rank": 2,
            "app_version": "1.2.0",
            "online": True,
            "last_heartbeat_at": "2026-10-19T06:00:00+00:00",
            "capabilities": {"asset_types": ["config"]},
        },
        {
            "device_id": "desktop-a",
            "tier": "desktop",
            "tier_rank": 1,
            "app_version": "1.2.0",
            "online": False,
            "last_heartbeat_at": "2026-10-19T05:00:00+00:00",
            "capabilities": {},
        },
    ],
    "online_window_secs": 90.0,
    "requested_by": "desktop-a",
}


def _paired_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """已配对装载（env 配对；引擎随装载启动，用后须 _on_unload）。"""
    module = _reload(
        monkeypatch,
        tmp_path,
        AGENTOS_SYNC_RELAY_URL="http://r/ext/sync_relay",
        AGENTOS_SYNC_TOKEN="t",
        AGENTOS_SYNC_WATCH_DIR=str(tmp_path),
    )
    asyncio.run(module._on_load({}))
    return module


def _get_relay_devices(module: Any) -> dict[str, Any]:
    resp = asyncio.run(
        module.http_handle(path="/ext/sync_agent/relay/devices", method="GET")
    )
    return _unwrap(resp)


def test_relay_devices_passthrough_maps_cloud_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """透传主道：云端 routing_view → 设备卡契约（在线态/档位/版本/心跳逐字段直传）。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        async def fake_pull() -> dict[str, Any]:
            return {**_ROUTING_VIEW, "devices": [{**d} for d in _ROUTING_VIEW["devices"]]}

        monkeypatch.setattr(module._engine, "pull_routing_view", fake_pull)
        resp = _get_relay_devices(module)
        assert resp["status"] == 200
        body = resp["body"]
        assert body["source"] == "cloud"
        assert body["degraded"] is False
        assert body["online_window_secs"] == 90.0
        assert body["fetched_at"], "透传时刻必须携带（缓存新鲜度呈现面）"
        assert "error" not in body, "成功面不带降级字段（禁假装）"
        assert [d["device_id"] for d in body["devices"]] == ["phone-b", "desktop-a"]
        by_id = {d["device_id"]: d for d in body["devices"]}
        assert by_id["phone-b"]["online"] is True
        assert by_id["phone-b"]["tier"] == "phone"
        assert by_id["phone-b"]["app_version"] == "1.2.0"
        assert by_id["phone-b"]["last_heartbeat_at"] == "2026-10-19T06:00:00+00:00"
        assert by_id["desktop-a"]["online"] is False
    finally:
        asyncio.run(module._on_unload({}))


def test_relay_devices_degrades_honestly_when_cloud_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """云端不可达：空 devices + degraded + error（禁静默回本地空表假装成功）。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        async def failing_pull() -> dict[str, Any]:
            raise RuntimeError("relay 503: down")

        monkeypatch.setattr(module._engine, "pull_routing_view", failing_pull)
        resp = _get_relay_devices(module)
        assert resp["status"] == 200, "降级是 200 信封（前端按数据消费降级标记）"
        body = resp["body"]
        assert body["devices"] == []
        assert body["degraded"] is True
        assert body["source"] == "cloud"
        assert "云端注册表暂不可达" in body["error"]
        assert "503" in body["error"], "上游拒绝细节进 error（排障面）"
    finally:
        asyncio.run(module._on_unload({}))


def test_relay_devices_unpaired_reports_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """未配对（引擎停转）：无云端注册表可透传 → 诚实降级非空成功。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    try:
        assert module._engine is None
        resp = _get_relay_devices(module)
        assert resp["status"] == 200
        body = resp["body"]
        assert body["devices"] == []
        assert body["degraded"] is True
        assert body["source"] == "none"
        assert "配对" in body["error"]
    finally:
        asyncio.run(module._on_unload({}))


def test_relay_devices_short_ttl_cache_and_no_negative_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """短 TTL 缓存：窗口内命中零云端调用；超窗重拉；降级响应不缓存（恢复即见）。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        clock = {"now": 1000.0}
        monkeypatch.setattr(module, "_registry_mono", lambda: clock["now"])
        calls = {"n": 0}

        async def fake_pull() -> dict[str, Any]:
            calls["n"] += 1
            return {**_ROUTING_VIEW, "devices": [{**d} for d in _ROUTING_VIEW["devices"]]}

        monkeypatch.setattr(module._engine, "pull_routing_view", fake_pull)
        first = _get_relay_devices(module)
        second = _get_relay_devices(module)
        assert calls["n"] == 1, "TTL 窗口内第二发命中缓存（零云端调用）"
        assert second["body"]["fetched_at"] == first["body"]["fetched_at"], (
            "缓存命中返回原透传体（fetched_at 如实反映首次拉取时刻）"
        )
        clock["now"] += 6.0
        _get_relay_devices(module)
        assert calls["n"] == 2, "超窗重拉（心跳态要新鲜，禁长缓存）"

        async def failing_pull() -> dict[str, Any]:
            calls["n"] += 1
            raise RuntimeError("relay 503: down")

        monkeypatch.setattr(module._engine, "pull_routing_view", failing_pull)
        clock["now"] += 6.0
        degraded = _get_relay_devices(module)
        assert degraded["body"]["degraded"] is True
        assert calls["n"] == 3
        again = _get_relay_devices(module)
        assert again["body"]["degraded"] is True
        assert calls["n"] == 4, "降级响应不缓存：同刻再拉仍打云端（禁负缓存掩盖恢复）"
    finally:
        asyncio.run(module._on_unload({}))


# ── 直连面透传 + 注册表地址实测探活（ADR 2026-10-21-device-card-direct-connect）──


def test_relay_devices_passthrough_address_storage_is_hub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """直连面透传：routing_view 携带 address/storage/is_hub 才透出，缺省不编造。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        view = {
            "devices": [
                {
                    "device_id": "cloud-c",
                    "tier": "cloud",
                    "tier_rank": 0,
                    "app_version": "1.0.0",
                    "online": True,
                    "last_heartbeat_at": "2026-10-19T06:00:00+00:00",
                    "capabilities": {},
                    "address": "https://cloud.example.com",
                    "storage": {"free_bytes": 1, "total_bytes": 2},
                    "is_hub": True,
                },
                {
                    "device_id": "phone-b",
                    "tier": "phone",
                    "tier_rank": 2,
                    "app_version": "1.2.0",
                    "online": True,
                    "last_heartbeat_at": "2026-10-19T06:00:00+00:00",
                    "capabilities": {},
                },
            ],
        }

        async def fake_pull() -> dict[str, Any]:
            return {**_ROUTING_VIEW, "devices": [{**d} for d in view["devices"]]}

        monkeypatch.setattr(module._engine, "pull_routing_view", fake_pull)
        body = _get_relay_devices(module)["body"]
        by_id = {d["device_id"]: d for d in body["devices"]}
        assert by_id["cloud-c"]["address"] == "https://cloud.example.com"
        assert by_id["cloud-c"]["storage"] == {"free_bytes": 1, "total_bytes": 2}
        assert by_id["cloud-c"]["is_hub"] is True
        assert "address" not in by_id["phone-b"], "云端未携带时键整体缺席（旧云端兼容）"
        assert "storage" not in by_id["phone-b"]
        assert "is_hub" not in by_id["phone-b"]
    finally:
        asyncio.run(module._on_unload({}))


def _probe_via_endpoint(module: Any, body: dict[str, Any]) -> dict[str, Any]:
    return _post(module, "/ext/sync_agent/probe", body)


_PROBE_REGISTRY = {
    "devices": [
        {
            "device_id": "desktop-a",
            "tier": "desktop",
            "tier_rank": 1,
            "app_version": "1.0.0",
            "online": True,
            "last_heartbeat_at": "2026-10-19T06:00:00+00:00",
            "capabilities": {},
            "address": "http://10.0.0.8:9100",
        },
        {
            "device_id": "phone-b",
            "tier": "phone",
            "tier_rank": 2,
            "app_version": "1.0.0",
            "online": True,
            "last_heartbeat_at": "2026-10-19T06:00:00+00:00",
            "capabilities": {},
        },
    ],
}


def test_probe_maps_registry_addresses_and_reports_no_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """探活主道：有地址卡按注册表地址实测；无地址卡如实报 no_address（不假装可探）。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        probed: list[str] = []

        async def fake_pull() -> dict[str, Any]:
            return {**_ROUTING_VIEW, "devices": [{**d} for d in _PROBE_REGISTRY["devices"]]}

        async def fake_health(address: str) -> dict[str, Any]:
            probed.append(address)
            assert address == "http://10.0.0.8:9100", "探活地址唯一来源 = 云端注册表"
            return {"ok": True, "status": 200, "latency_ms": 12}

        monkeypatch.setattr(module._engine, "pull_routing_view", fake_pull)
        monkeypatch.setattr(module, "_probe_health", fake_health)
        resp = _probe_via_endpoint(
            module, {"device_ids": ["desktop-a", "phone-b", "desktop-a"]}
        )
        assert resp["status"] == 200
        results = resp["body"]["results"]
        assert len(results) == 2, "device_ids 去重（同 id 单条结果）"
        by_id = {r["device_id"]: r for r in results}
        assert by_id["desktop-a"] == {
            "device_id": "desktop-a",
            "address": "http://10.0.0.8:9100",
            "ok": True,
            "status": 200,
            "latency_ms": 12,
        }
        assert probed == ["http://10.0.0.8:9100"], "无地址设备不打探活"
        assert by_id["phone-b"]["ok"] is False
        assert by_id["phone-b"]["error"] == "no_address"
        assert by_id["phone-b"]["address"] is None
    finally:
        asyncio.run(module._on_unload({}))


def test_probe_input_validation_rejects_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """入参硬界：非对象体/缺 device_ids/空数组/非字符串条目/超上限一律 400。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        assert _probe_via_endpoint(module, {"other": 1})["status"] == 400
        assert _probe_via_endpoint(module, {"device_ids": []})["status"] == 400
        assert _probe_via_endpoint(module, {"device_ids": ["  ", ""]})["status"] == 400
        assert _probe_via_endpoint(module, {"device_ids": [f"d{i}" for i in range(33)]})[
            "status"
        ] == 400
    finally:
        asyncio.run(module._on_unload({}))


def test_probe_unpaired_degrades_honestly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """未配对（引擎停转且缓存空）：503 诚实降级，禁假装探活成功。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    try:
        resp = _probe_via_endpoint(module, {"device_ids": ["desktop-a"]})
        assert resp["status"] == 503
        assert "云端注册表暂不可达" in resp["body"]["error"]["message"]
    finally:
        asyncio.run(module._on_unload({}))


def test_probe_uses_fresh_registry_cache_without_cloud_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """短 TTL 缓存命中：探活零云端调用（地址面消费缓存，不重拉注册表）。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        module._registry_cache = {
            "devices": [
                {
                    "device_id": "desktop-a",
                    "address": "http://10.0.0.8:9100",
                }
            ],
        }
        module._registry_cached_at = module._registry_mono()

        async def failing_pull() -> dict[str, Any]:
            raise AssertionError("缓存新鲜期内禁打云端")

        async def fake_health(address: str) -> dict[str, Any]:
            return {"ok": False, "status": 0, "latency_ms": 1, "error": "ConnectError"}

        monkeypatch.setattr(module._engine, "pull_routing_view", failing_pull)
        monkeypatch.setattr(module, "_probe_health", fake_health)
        resp = _probe_via_endpoint(module, {"device_ids": ["desktop-a"]})
        assert resp["status"] == 200
        assert resp["body"]["results"][0]["ok"] is False, "探活失败是值（实测不可达）"
    finally:
        asyncio.run(module._on_unload({}))


def test_probe_route_registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """路由分发：/probe 挂 POST，其他方法 404（不落镜像只读兜底分支）。"""
    module = _reload(monkeypatch, tmp_path)
    asyncio.run(module._on_load({}))
    try:
        resp = asyncio.run(
            module.http_handle(path="/ext/sync_agent/probe", method="GET", raw_body="")
        )
        assert resp["data"]["status"] == 404
    finally:
        asyncio.run(module._on_unload({}))


def test_probe_drops_foreign_loopback_self_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """回环自报只对自报者本人可达：非本机设备的回环地址不进探活集（防假阳探到自己）。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        module._registry_cache = {
            "devices": [
                {"device_id": "cloud-c", "address": "http://127.0.0.1:9100"},
                {"device_id": "desktop-lan", "address": "http://192.168.1.8:9100"},
                {"device_id": module._assembly.device_id, "address": "http://127.0.0.1:9100"},
            ],
        }
        module._registry_cached_at = module._registry_mono()
        probed: list[str] = []

        async def fake_health(address: str) -> dict[str, Any]:
            probed.append(address)
            return {"ok": True, "status": 200, "latency_ms": 5}

        monkeypatch.setattr(module, "_probe_health", fake_health)
        resp = _probe_via_endpoint(
            module,
            {"device_ids": ["cloud-c", "desktop-lan", module._assembly.device_id]},
        )
        assert resp["status"] == 200
        by_id = {r["device_id"]: r for r in resp["body"]["results"]}
        assert probed == [
            "http://192.168.1.8:9100",
            "http://127.0.0.1:9100",
        ], "只实测非回环与本机回环目标（他端回环剔除）"
        assert by_id["cloud-c"]["error"] == "no_address", "他端回环自报如实报不可探"
        assert by_id[module._assembly.device_id]["ok"] is True, "本机自己的回环自报合法可探"
    finally:
        asyncio.run(module._on_unload({}))


# ── A 档注册表副本：中枢离线时设备页呈现面与探活回退 ──────────────────
# （ADR 2026-10-22-distributed-registry-peer-trust：每台设备持有全网注册表副本）


def _seed_replica_file(
    module: Any, devices: list[dict[str, Any]], fetched_at: str = "2026-10-22T10:00:00+00:00"
) -> None:
    from registry_replica import RegistryReplica

    RegistryReplica(
        module._assembly.data_root / "sync_agent" / "registry_replica.json"
    ).save(devices=devices, fetched_at=fetched_at)


def test_relay_devices_peer_loopback_address_not_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """他端回环自报不透出（存量坏行容错）：视同无地址，呈现回落心跳语义。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        async def fake_pull() -> dict[str, Any]:
            return {
                "devices": [
                    {
                        "device_id": "cloud-c",
                        "tier": "cloud",
                        "app_version": "1.0.0",
                        "online": True,
                        "last_heartbeat_at": "2026-10-22T06:00:00+00:00",
                        "address": "http://127.0.0.1:9100",
                    },
                    {
                        "device_id": module._assembly.device_id,
                        "tier": "desktop",
                        "app_version": "1.0.0",
                        "online": True,
                        "last_heartbeat_at": "2026-10-22T06:00:00+00:00",
                        "address": "http://127.0.0.1:9100",
                    },
                ]
            }

        monkeypatch.setattr(module._engine, "pull_routing_view", fake_pull)
        resp = _get_relay_devices(module)
        by_id = {d["device_id"]: d for d in resp["body"]["devices"]}
        assert "address" not in by_id["cloud-c"], "他端回环不透出（禁死连接卡）"
        assert by_id[module._assembly.device_id]["address"] == "http://127.0.0.1:9100", (
            "本机行保留（呈现面本就滤本机，不改写事实）"
        )
    finally:
        asyncio.run(module._on_unload({}))


def test_relay_devices_falls_back_to_local_replica_when_cloud_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """云端不可达 + 副本在案：呈现本机副本（source=replica + degraded + 截至时刻）。

    分布式口径的呈现面：每台设备持有全网注册表，中枢离线不白屏设备页；
    token_hash 只活在副本层，不出前端契约。
    """
    module = _paired_module(monkeypatch, tmp_path)
    try:
        _seed_replica_file(
            module,
            [
                {
                    "device_id": "phone-b",
                    "tier": "phone",
                    "app_version": "2.0.0",
                    "online": True,
                    "last_heartbeat_at": "2026-10-22T09:00:00+00:00",
                    "address": "http://192.168.1.20:9100",
                    "token_hash": "cafe",
                }
            ],
        )

        async def failing_pull() -> dict[str, Any]:
            raise RuntimeError("relay 503: down")

        monkeypatch.setattr(module._engine, "pull_routing_view", failing_pull)
        resp = _get_relay_devices(module)
        assert resp["status"] == 200
        body = resp["body"]
        assert body["source"] == "replica"
        assert body["degraded"] is True
        assert "截至" in body["error"]
        assert body["fetched_at"] == "2026-10-22T10:00:00+00:00"
        assert [d["device_id"] for d in body["devices"]] == ["phone-b"]
        assert body["devices"][0]["address"] == "http://192.168.1.20:9100"
        assert "token_hash" not in body["devices"][0], "哈希不出前端契约"
    finally:
        asyncio.run(module._on_unload({}))


def test_probe_falls_back_to_replica_addresses_when_cloud_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """云端不可达 + 副本在案：探活地址回落副本（中枢离线照样探活已知设备）。"""
    module = _paired_module(monkeypatch, tmp_path)
    try:
        _seed_replica_file(
            module,
            [
                {
                    "device_id": "desktop-a",
                    "address": "http://10.0.0.8:9100",
                    "token_hash": "cafe",
                },
                {"device_id": "phone-b", "token_hash": "beef"},
            ],
        )
        probed: list[str] = []

        async def failing_pull() -> dict[str, Any]:
            raise RuntimeError("relay down")

        async def fake_health(address: str) -> dict[str, Any]:
            probed.append(address)
            return {"ok": True, "status": 200, "latency_ms": 5}

        monkeypatch.setattr(module._engine, "pull_routing_view", failing_pull)
        monkeypatch.setattr(module, "_probe_health", fake_health)
        resp = _probe_via_endpoint(module, {"device_ids": ["desktop-a", "phone-b"]})
        assert resp["status"] == 200
        by_id = {r["device_id"]: r for r in resp["body"]["results"]}
        assert probed == ["http://10.0.0.8:9100"], "副本有址设备照常实测"
        assert by_id["phone-b"]["error"] == "no_address", "副本无址设备如实报不可探"
    finally:
        asyncio.run(module._on_unload({}))
