# @feature: FP-0.2.多端同步 借权免登 | @ci: python-coverage
"""sync_agent 借权两面测试：borrow_auth（member 目标换发）+ borrow（出站代理）。

断行为（ADR 草案 2026-10-22-borrowed-device-auth）：
- borrow_auth = relay /verify 核验请求方同注册表 → 换发本内核 admin 会话；
  无效 token 403（不换发）；本机未配对 503（借权面不存在语义）；
- borrow = 仅已登录本机前端代理：按 target_is_hub 选目标端点、代持本机
  token、上游错误透传（403/401/503 直传，网络失败归一 502）。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.unit

from registry_replica import RegistryReplica  # noqa: E402 —— conftest 先置插件目录入 sys.path

MINTED = {
    "access_token": "at-borrow",
    "refresh_token": "rt-borrow",
    "expires_in": 1800,
    "token_type": "bearer",
}

PAIRED_ENV = {
    "AGENTOS_SYNC_RELAY_URL": "http://relay.example:9300/ext/sync_relay",
    "AGENTOS_SYNC_TOKEN": "tok-self",
}


def _reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **env: str) -> Any:
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import server as agent_server

    return importlib.reload(agent_server)


@contextlib.contextmanager
def _loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **env: str
) -> Iterator[Any]:
    module = _reload(monkeypatch, tmp_path, **env)
    asyncio.run(module._on_load({}))
    try:
        yield module
    finally:
        asyncio.run(module._on_unload({}))


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    data = resp["data"]
    body = json.loads(__import__("base64").b64decode(data["body"]))
    return {"success": resp.get("success"), "status": data["status"], "body": body}


def test_borrow_auth_mints_after_relay_verify_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        verify_calls: list[tuple[str, str]] = []

        async def fake_verify(
            relay_url: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            verify_calls.append((relay_url, token))
            return 200, {"valid": True, "device_id": "phone-b"}

        async def fake_login(
            password: str, port: str
        ) -> tuple[int, dict[str, Any] | None]:
            return 200, dict(MINTED)

        monkeypatch.setattr(module, "_relay_verify", fake_verify)
        monkeypatch.setattr(module, "_login_admin", fake_login)
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "seeded-password-32-chars-xxxx")

        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow_auth",
                    method="POST",
                    raw_body="{}",
                    headers={"authorization": "Bearer tok-requester"},
                )
            )
        )
        assert resp["status"] == 200
        assert resp["body"]["refresh_token"] == "rt-borrow"
        assert resp["body"]["verified_device"] == "phone-b"
        assert verify_calls == [
            ("http://relay.example:9300/ext/sync_relay", "tok-requester")
        ]


def test_borrow_auth_rejects_unverified_requester(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:

        async def fake_verify(
            relay_url: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            return 403, None

        mint_calls: list[int] = []

        async def fake_login(
            password: str, port: str
        ) -> tuple[int, dict[str, Any] | None]:
            mint_calls.append(1)
            return 200, dict(MINTED)

        monkeypatch.setattr(module, "_relay_verify", fake_verify)
        monkeypatch.setattr(module, "_login_admin", fake_login)

        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow_auth",
                    method="POST",
                    raw_body="{}",
                    headers={"authorization": "Bearer tok-rogue"},
                )
            )
        )
        assert resp["status"] == 403
        assert mint_calls == [], "未通过注册表验证不得换发"


def test_borrow_auth_requires_pairing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    with _loaded(monkeypatch, tmp_path) as module:
        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow_auth",
                    method="POST",
                    raw_body="{}",
                    headers={"authorization": "Bearer tok-x"},
                )
            )
        )
        assert resp["status"] == 503


# ── A 档对等信任：目标端本地核验（副本比对优先，relay 回退）──────────


def _seed_replica(module: Any, devices: list[dict[str, Any]]) -> None:
    """向本机副本落盘注入设备条目（borrow_auth 本地核验的数据源）。"""
    RegistryReplica(
        module._assembly.data_root / "sync_agent" / "registry_replica.json"
    ).save(devices=devices, fetched_at="2026-10-22T10:00:00+00:00")


def test_borrow_auth_local_replica_verdict_skips_relay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """副本命中：本地比对即核验通过——中枢离线照样借权（A 档销项：不再回中枢）。"""
    import hashlib

    with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        _seed_replica(
            module,
            devices=[
                {
                    "device_id": "phone-b",
                    "token_hash": hashlib.sha256(b"tok-requester").hexdigest(),
                }
            ],
        )

        async def forbidden_verify(
            relay_url: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            raise AssertionError("副本命中不得回打 relay /verify")

        async def fake_login(
            password: str, port: str
        ) -> tuple[int, dict[str, Any] | None]:
            return 200, dict(MINTED)

        monkeypatch.setattr(module, "_relay_verify", forbidden_verify)
        monkeypatch.setattr(module, "_login_admin", fake_login)
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "seeded-password-32-chars-xxxx")

        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow_auth",
                    method="POST",
                    raw_body="{}",
                    headers={"authorization": "Bearer tok-requester"},
                )
            )
        )
        assert resp["status"] == 200
        assert resp["body"]["verified_device"] == "phone-b"
        assert resp["body"]["access_token"] == "at-borrow"


def test_borrow_auth_falls_back_to_relay_when_replica_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """副本未命中（新设备复制窗口/存量无哈希行）：回退 relay /verify 核验。"""
    import hashlib

    with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        _seed_replica(
            module,
            devices=[
                {
                    "device_id": "phone-b",
                    "token_hash": hashlib.sha256(b"tok-other").hexdigest(),
                }
            ],
        )
        verify_calls: list[tuple[str, str]] = []

        async def fake_verify(
            relay_url: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            verify_calls.append((relay_url, token))
            return 200, {"valid": True, "device_id": "cloud-c"}

        async def fake_login(
            password: str, port: str
        ) -> tuple[int, dict[str, Any] | None]:
            return 200, dict(MINTED)

        monkeypatch.setattr(module, "_relay_verify", fake_verify)
        monkeypatch.setattr(module, "_login_admin", fake_login)
        # 部署契约自足：借权入口要求 admin 口令注入（缺失 = 503 短路，走不到
        # relay 回退分支）——依赖宿主环境残留会让无口令环境假红（兄弟用例同款）
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "seeded-password-32-chars-xxxx")

        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow_auth",
                    method="POST",
                    raw_body="{}",
                    headers={"authorization": "Bearer tok-brand-new"},
                )
            )
        )
        assert resp["status"] == 200
        assert resp["body"]["verified_device"] == "cloud-c"
        assert verify_calls == [
            ("http://relay.example:9300/ext/sync_relay", "tok-brand-new")
        ], "副本未命中必须走 relay 回退（不静默放行）"


def test_borrow_proxies_to_member_endpoint_with_own_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """成员目标自动判别：先探中枢端点（成员内核无此路由 → 404）→ 成员端点成交。"""
    with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        calls: list[tuple[str, str, str]] = []

        async def fake_post(
            endpoint: str, device_id: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            calls.append((endpoint, device_id, token))
            if endpoint.endswith("/ext/sync_relay/borrow_auth"):
                return 404, None  # 成员内核无中枢路由（自动判别的回退依据）
            return 200, dict(MINTED)

        monkeypatch.setattr(module, "_post_borrow_auth", fake_post)

        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow",
                    method="POST",
                    raw_body=json.dumps({"target_base": "http://192.168.1.10:9100"}),
                )
            )
        )
        assert resp["status"] == 200
        assert resp["body"]["access_token"] == "at-borrow"
        assert calls == [
            (
                "http://192.168.1.10:9100/ext/sync_relay/borrow_auth",
                module._assembly.device_id,
                "tok-self",
            ),
            (
                "http://192.168.1.10:9100/ext/sync_agent/borrow_auth",
                module._assembly.device_id,
                "tok-self",
            ),
        ]


def test_borrow_routes_hub_target_to_relay_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        calls: list[str] = []

        async def fake_post(
            endpoint: str, device_id: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            calls.append(endpoint)
            return 200, dict(MINTED)

        monkeypatch.setattr(module, "_post_borrow_auth", fake_post)

        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow",
                    method="POST",
                    raw_body=json.dumps(
                        {"target_base": "http://162.14.69.153", "target_is_hub": True}
                    ),
                )
            )
        )
        assert resp["status"] == 200
        assert calls == ["http://162.14.69.153/ext/sync_relay/borrow_auth"]


def test_borrow_passthrough_upstream_rejection_and_network_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:

        async def rejected(
            endpoint: str, device_id: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            return 403, {"error": {"message": "请求方 per-device token 未通过注册表验证"}}

        monkeypatch.setattr(module, "_post_borrow_auth", rejected)
        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow",
                    method="POST",
                    raw_body=json.dumps({"target_base": "http://10.0.0.8:9100"}),
                )
            )
        )
        assert resp["status"] == 403
        assert "未通过注册表验证" in resp["body"]["error"]["message"]

        async def dead(
            endpoint: str, device_id: str, token: str
        ) -> tuple[int, dict[str, Any] | None]:
            return 0, None

        monkeypatch.setattr(module, "_post_borrow_auth", dead)
        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow",
                    method="POST",
                    raw_body=json.dumps({"target_base": "http://10.0.0.8:9100"}),
                )
            )
        )
        assert resp["status"] == 502


def test_borrow_requires_pairing(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    with _loaded(monkeypatch, tmp_path) as module:
        resp = _unwrap(
            asyncio.run(
                module.http_handle(
                    path="/ext/sync_agent/borrow",
                    method="POST",
                    raw_body=json.dumps({"target_base": "http://10.0.0.8:9100"}),
                )
            )
        )
        assert resp["status"] == 503
