# @feature: FP-0.2.多端同步 2c 执行通道 | @ci: python-coverage
"""exec token 双钥签发/验证集测试（ADR D13'：发起端签发 → 属主端验证）。

断行为（端点契约 + 存储时效面，ADR 2026-10-17-unified-path-tree-and-capability-plane）：
- 签发 = 发起端 sync token 鉴权 + 目标设备存在性 + 短时效（夹取上限）；
- 验证集 = 属主隔离（只回落到绑定自己的凭据）+ 时效过滤 + 过期物理清理；
- 分库续效 = ExecTokenStore 重建（模拟 relay 重启）后绑定仍在。
时效判定恒按注入时钟（零真实等待）。
"""

from __future__ import annotations

import base64
import importlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 17, 8, 0, 0, tzinfo=UTC)
TTL_SECS = 3600.0


# ---------------------------------------------------------------------------
# 存储面：ExecTokenStore（独立分库，双钥分离的存储承载）
# ---------------------------------------------------------------------------


def _bind(
    store: Any,
    token: str,
    *,
    device_id: str = "device-b",
    issued_by: str = "device-a",
    expires_at: datetime,
) -> None:
    store.insert(
        token=token,
        device_id=device_id,
        issued_by=issued_by,
        created_at=NOW.isoformat(),
        expires_at=expires_at.isoformat(),
    )


def test_store_binds_and_scopes_by_owner_device(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    _bind(store, "tok-1", expires_at=NOW + timedelta(hours=1))
    _bind(store, "tok-2", device_id="device-c", expires_at=NOW + timedelta(hours=1))

    mine = store.list_for("device-b")
    assert [t["token"] for t in mine] == ["tok-1"]
    assert mine[0]["issued_by"] == "device-a"
    assert store.list_for("device-a") == []  # 签发方不是属主，验证集不回流
    store.close()


def test_store_survives_reopen(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore

    path = tmp_path / "exec_tokens.db"
    store = ExecTokenStore(path)
    _bind(store, "tok-persist", expires_at=NOW + timedelta(hours=1))
    store.close()

    reopened = ExecTokenStore(path)
    assert [t["token"] for t in reopened.list_for("device-b")] == ["tok-persist"]
    reopened.close()


def test_prune_drops_only_expired(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    _bind(store, "tok-stale", expires_at=NOW - timedelta(seconds=1))
    _bind(store, "tok-fresh", expires_at=NOW + timedelta(hours=1))
    _bind(store, "tok-other", device_id="device-c", expires_at=NOW - timedelta(seconds=1))

    pruned = store.prune(NOW)
    assert pruned == 2
    assert [t["token"] for t in store.list_for("device-b")] == ["tok-fresh"]
    store.close()


# ---------------------------------------------------------------------------
# 逻辑面：签发（发起端）与验证集（属主端）
# ---------------------------------------------------------------------------


def _issue(store: Any, **overrides: Any) -> Any:
    from exec_tokens import issue_exec_token

    params: dict[str, Any] = {
        "issued_by": "device-a",
        "target_device": "device-b",
        "token": "tok-issue",
        "ttl_secs": TTL_SECS,
        "now": NOW,
        "device_exists": lambda d: d in {"device-a", "device-b"},
    }
    params.update(overrides)
    return issue_exec_token(store, **params)


def test_issue_returns_expiry_and_persists(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore
    from exec_tokens import verification_set

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    outcome = _issue(store)
    assert outcome.target_device == "device-b"
    assert outcome.issued_by == "device-a"
    delta = datetime.fromisoformat(outcome.expires_at) - NOW
    assert delta == timedelta(seconds=TTL_SECS)  # 时效 = 请求值（性质：夹取区间内保真）

    active = verification_set(store, device_id="device-b", now=NOW)
    assert [(t["token"], t["issued_by"]) for t in active] == [("tok-issue", "device-a")]
    store.close()


@pytest.mark.parametrize(
    ("overrides", "status_frag"),
    [
        ({"target_device": ""}, "target_device"),
        ({"target_device": "device-x"}, "不在注册表"),
        ({"token": ""}, "exec_token"),
        ({"ttl_secs": 0}, "ttl_secs"),
        ({"ttl_secs": -5}, "ttl_secs"),
        ({"ttl_secs": "abc"}, "ttl_secs"),
    ],
)
def test_issue_rejects_malformed_requests(tmp_path: Any, overrides: dict[str, Any], status_frag: str) -> None:
    from exec_token_store import ExecTokenStore

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    outcome = _issue(store, **overrides)
    assert hasattr(outcome, "message")
    assert status_frag in outcome.message
    assert store.list_for("device-b") == []  # 失败零登记
    store.close()


def test_issue_caps_ttl_to_short_lived_bound(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore
    from exec_tokens import MAX_TTL_SECS

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    outcome = _issue(store, ttl_secs=10 * MAX_TTL_SECS)
    delta = datetime.fromisoformat(outcome.expires_at) - NOW
    assert delta == timedelta(seconds=MAX_TTL_SECS)  # 短时效上限执法（可观察）
    store.close()


def test_issue_ttl_none_falls_back_to_default(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore
    from exec_tokens import DEFAULT_TTL_SECS

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    outcome = _issue(store, ttl_secs=None)
    delta = datetime.fromisoformat(outcome.expires_at) - NOW
    assert delta == timedelta(seconds=DEFAULT_TTL_SECS)  # 未带时效 = 缺省短时效
    store.close()


def test_naive_offset_expiry_treated_as_utc(tmp_path: Any) -> None:
    """无时区时效 = 按 UTC 补位（签发面与 prune 面同一裁决，不因格式差漂移）。"""
    from exec_token_store import ExecTokenStore
    from exec_tokens import verification_set

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    naive_live = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    naive_dead = (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    store.insert(
        token="tok-naive-live",
        device_id="device-b",
        issued_by="device-a",
        created_at=NOW.isoformat(),
        expires_at=naive_live,
    )
    store.insert(
        token="tok-naive-dead",
        device_id="device-b",
        issued_by="device-a",
        created_at=NOW.isoformat(),
        expires_at=naive_dead,
    )

    active = verification_set(store, device_id="device-b", now=NOW)
    assert [t["token"] for t in active] == ["tok-naive-live"]  # 验证集补位裁决
    assert [t["token"] for t in store.list_for("device-b")] == [
        "tok-naive-live"
    ]  # 过期行已随拉取物理清理（内联 prune 可观察）
    assert store.prune(NOW) == 0  # housekeeping 幂等
    store.close()


def test_reissue_same_token_refreshes_expiry(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    first = _issue(store, ttl_secs=TTL_SECS)
    second = _issue(store, ttl_secs=2 * TTL_SECS)
    assert second.expires_at > first.expires_at
    assert len(store.list_for("device-b")) == 1  # 同 token 幂等覆盖不增殖
    store.close()


def test_verification_set_filters_expired_and_prunes(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore
    from exec_tokens import verification_set

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    _bind(store, "tok-live", expires_at=NOW + timedelta(hours=1))
    _bind(store, "tok-dead", expires_at=NOW - timedelta(minutes=1))
    _bind(store, "tok-edge", expires_at=NOW)  # 恰好压线 = 仍有效（与 is_online 同律）

    active = verification_set(store, device_id="device-b", now=NOW)
    assert sorted(t["token"] for t in active) == ["tok-edge", "tok-live"]

    later = verification_set(store, device_id="device-b", now=NOW + timedelta(hours=2))
    assert later == []
    assert store.list_for("device-b") == []  # 过期行已被物理清理（again-with-early-now 可观察）
    store.close()


def test_verification_set_drops_malformed_expiry(tmp_path: Any) -> None:
    from exec_token_store import ExecTokenStore
    from exec_tokens import verification_set

    store = ExecTokenStore(tmp_path / "exec_tokens.db")
    store.insert(
        token="tok-broken",
        device_id="device-b",
        issued_by="device-a",
        created_at=NOW.isoformat(),
        expires_at="not-a-timestamp",
    )
    assert verification_set(store, device_id="device-b", now=NOW) == []  # 畸形时效 fail-closed
    store.close()


# ---------------------------------------------------------------------------
# 端点面：POST/GET /ext/sync_relay/exec_tokens（http.handle 分发契约）
# ---------------------------------------------------------------------------


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """装好内存态的 server 模块（store/exec store 指向 tmp、tokens 注入）。"""
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTOS_SYNC_RELAY_TOKENS", "device-a:tok-a,device-b:tok-b")
    import server as relay_server

    module = importlib.reload(relay_server)
    import asyncio

    asyncio.run(module._on_load({}))
    yield module
    asyncio.run(module._on_unload({}))


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    data = resp["data"]
    body = json.loads(base64.b64decode(data["body"]))
    return {"status": data["status"], "body": body}


def _run(coro: Any) -> dict[str, Any]:
    import asyncio

    return asyncio.run(coro)


def _post(module: Any, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    resp = _run(
        module.http_handle(
            path=path,
            method="POST",
            raw_body=json.dumps(body),
            headers={"authorization": f"Bearer {token}"},
        )
    )
    return _unwrap(resp)


def _get(module: Any, path: str, token: str | None) -> dict[str, Any]:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    resp = _run(module.http_handle(path=path, method="GET", headers=headers, query={}))
    return _unwrap(resp)


def _register_devices(module: Any) -> None:
    for device, token in (("device-a", "tok-a"), ("device-b", "tok-b")):
        resp = _post(
            module,
            "/ext/sync_relay/devices",
            {"device_id": device, "tier": "desktop", "app_version": "1.0.0", "capabilities": {}},
            token,
        )
        assert resp["status"] == 200


def test_endpoint_issue_and_fetch_roundtrip(relay: Any) -> None:
    _register_devices(relay)
    issued = _post(
        relay,
        "/ext/sync_relay/exec_tokens",
        {"target_device": "device-b", "exec_token": "tok-round", "ttl_secs": 600},
        "tok-a",
    )
    assert issued["status"] == 200
    assert set(issued["body"]) == {"issued", "target_device", "issued_by", "expires_at"}
    assert issued["body"]["issued"] is True
    assert issued["body"]["target_device"] == "device-b"
    assert issued["body"]["issued_by"] == "device-a"
    assert datetime.fromisoformat(issued["body"]["expires_at"]) > NOW

    owner_view = _get(relay, "/ext/sync_relay/exec_tokens", "tok-b")
    assert owner_view["status"] == 200
    assert owner_view["body"]["device"] == "device-b"
    assert [t["token"] for t in owner_view["body"]["tokens"]] == ["tok-round"]
    assert owner_view["body"]["tokens"][0]["issued_by"] == "device-a"

    issuer_view = _get(relay, "/ext/sync_relay/exec_tokens", "tok-a")
    assert issuer_view["body"]["tokens"] == []  # 验证集只回落属主


def test_endpoint_issue_requires_device_token(relay: Any) -> None:
    _register_devices(relay)
    assert _post(
        relay,
        "/ext/sync_relay/exec_tokens",
        {"target_device": "device-b", "exec_token": "tok-x"},
        "wrong-token",
    )["status"] == 403
    assert _get(relay, "/ext/sync_relay/exec_tokens", None)["status"] == 403


def test_endpoint_issue_validates_target_and_body(relay: Any) -> None:
    _register_devices(relay)
    unknown = _post(
        relay,
        "/ext/sync_relay/exec_tokens",
        {"target_device": "device-ghost", "exec_token": "tok-x"},
        "tok-a",
    )
    assert unknown["status"] == 404
    empty_token = _post(
        relay, "/ext/sync_relay/exec_tokens", {"target_device": "device-b", "exec_token": ""}, "tok-a"
    )
    assert empty_token["status"] == 400


def test_endpoint_verification_set_hides_expired(relay: Any) -> None:
    _register_devices(relay)
    issued = _post(
        relay,
        "/ext/sync_relay/exec_tokens",
        {"target_device": "device-b", "exec_token": "tok-short", "ttl_secs": 60},
        "tok-a",
    )
    expires_at = datetime.fromisoformat(issued["body"]["expires_at"])
    relay._exec_store.prune(expires_at + timedelta(seconds=1))
    owner_view = _get(relay, "/ext/sync_relay/exec_tokens", "tok-b")
    assert owner_view["body"]["tokens"] == []


def test_endpoint_issue_rejects_non_json_body(relay: Any) -> None:
    _register_devices(relay)
    resp = _run(
        relay.http_handle(
            path="/ext/sync_relay/exec_tokens",
            method="POST",
            raw_body="not-json",
            headers={"authorization": "Bearer tok-a"},
        )
    )
    assert _unwrap(resp)["status"] == 400


def test_endpoint_guards_fail_closed_when_store_not_loaded(
    relay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exec 分库未装载 = 双端点 502 fail-closed（端点在但全拒，装载不受阻同律）。"""
    monkeypatch.setattr(relay, "_exec_store", None)
    assert _get(relay, "/ext/sync_relay/exec_tokens", "tok-b")["status"] == 502
    assert (
        _post(
            relay,
            "/ext/sync_relay/exec_tokens",
            {"target_device": "device-b", "exec_token": "tok-x"},
            "tok-a",
        )["status"]
        == 502
    )


def test_exec_db_path_fails_loud_without_data_root(relay: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay, "user_data_dir", lambda: None)
    with pytest.raises(RuntimeError, match="USER_ROOT"):
        relay._default_exec_db_path()
