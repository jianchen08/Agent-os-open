# @feature: FP-0.2.多端同步 借权免登 | @ci: python-coverage
"""配对设备借权（relay 目标面）：verify 轻验证 + borrow_auth 换发 admin 会话。

断行为（ADR 草案 2026-10-22-borrowed-device-auth，用户裁定借权会话等同 admin）：
- verify = per-device token → 绑定设备（未命中 403 不泄露存在性）；
- borrow_auth = 有效 token + 换发成功 → login 契约原样透传附 verified_device；
- 失败闭面 = 无效 token 403（换发不发生）；管理口令缺失 503（显式失败不降级）。
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """装好内存态的 server 模块（test_relay_server 同款：store 指向 tmp、tokens 注入）。"""
    import asyncio

    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTOS_SYNC_RELAY_TOKENS", "device-a:tok-a,device-b:tok-b")
    import server as relay_server

    module = importlib.reload(relay_server)
    asyncio.run(module._on_load({}))
    yield module
    asyncio.run(module._on_unload({}))

MINTED = {
    "access_token": "at-borrow",
    "refresh_token": "rt-borrow",
    "expires_in": 1800,
    "token_type": "bearer",
    "must_change_password": False,
}


def test_verify_returns_bound_device(relay: Any) -> None:
    import asyncio

    resp = _unwrap(
        asyncio.run(
            relay.http_handle(
                path="/ext/sync_relay/verify",
                method="GET",
                headers={"authorization": "Bearer tok-a"},
            )
        )
    )
    assert resp["status"] == 200
    assert resp["body"]["valid"] is True
    assert resp["body"]["device_id"] == "device-a"


def test_verify_rejects_unknown_token_without_existence_leak(relay: Any) -> None:
    import asyncio

    resp = _unwrap(
        asyncio.run(
            relay.http_handle(
                path="/ext/sync_relay/verify",
                method="GET",
                headers={"authorization": "Bearer tok-not-registered"},
            )
        )
    )
    assert resp["status"] == 403


def test_borrow_auth_mints_admin_session_for_paired_device(
    relay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    async def fake_login(password: str, port: str) -> tuple[int, dict[str, Any] | None]:
        seen["password"] = password
        seen["port"] = port
        return 200, dict(MINTED)

    monkeypatch.setattr(relay, "_login_admin", fake_login)
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "seeded-password-32-chars-xxxxxxx")

    resp = _post(relay, "/ext/sync_relay/borrow_auth", None, token="tok-b")
    assert resp["status"] == 200
    assert resp["body"]["access_token"] == "at-borrow"
    assert resp["body"]["refresh_token"] == "rt-borrow"
    assert resp["body"]["verified_device"] == "device-b"
    assert seen["password"] == "seeded-password-32-chars-xxxxxxx"


def test_borrow_auth_rejects_unknown_token_without_minting(
    relay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint_calls: list[int] = []

    async def fake_login(password: str, port: str) -> tuple[int, dict[str, Any] | None]:
        mint_calls.append(1)
        return 200, dict(MINTED)

    monkeypatch.setattr(relay, "_login_admin", fake_login)
    resp = _post(relay, "/ext/sync_relay/borrow_auth", None, token="tok-not-registered")
    assert resp["status"] == 403
    assert mint_calls == [], "无效 token 不得触发换发（失败闭面）"


def test_borrow_auth_fails_closed_without_admin_password(
    relay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTOS_ADMIN_PASSWORD", raising=False)
    resp = _post(relay, "/ext/sync_relay/borrow_auth", None, token="tok-a")
    assert resp["status"] == 503
    assert "借权换发失败" in resp["body"]["error"]["message"]


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    data = resp["data"]
    body = json.loads(__import__("base64").b64decode(data["body"]))
    return {"success": resp.get("success"), "status": data["status"], "body": body}


def _post(
    module: Any,
    path: str,
    body: dict[str, Any] | None,
    token: str = "tok-a",
) -> dict[str, Any]:
    import asyncio

    headers = {"authorization": f"Bearer {token}"}
    return _unwrap(
        asyncio.run(
            module.http_handle(
                path=path,
                method="POST",
                raw_body=json.dumps(body) if body is not None else "",
                headers=headers,
            )
        )
    )
