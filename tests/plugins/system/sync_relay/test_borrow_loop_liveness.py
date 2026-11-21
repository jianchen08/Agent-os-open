# @feature: FP-0.2.多端同步 借权免登 | @ci: python-coverage
"""sync_relay 借权换发事件循环活性锁（sync_agent 孪生实现同刀）。

事故根因（2026-11-12 异步阻塞审计 P2-8）：``_http_json`` 同步 stdlib urllib
单发（timeout 15s），被 ``_handle_borrow_auth → _mint_admin_session →
_login_admin`` 直调；seed 件禁共享根跨包 import（装机铁律），故与
``sync_agent/server.py`` 孪生实现两处同改（docstring 已写明「改动两处必须同刀」）。

锁面（断行为）：
- 换发链的出站单发（本机内核 login）出事件循环：假身同步阻塞 0.25s，心跳
  间隔不得出现 ≥0.1s 空窗；假身线程 ≠ 事件循环线程；
- 返回/异常语义不变：login 契约原样透传 + verified_device；口令缺失 503、
  无效 token 403 的既有面不受影响。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from typing import Any

import pytest
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker

pytestmark = pytest.mark.unit

MINTED = {
    "access_token": "at-borrow",
    "refresh_token": "rt-borrow",
    "expires_in": 1800,
    "token_type": "bearer",
    "must_change_password": False,
}


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """装好内存态的 server 模块（test_borrow_auth 同款：store 指向 tmp、tokens 注入）。"""
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTOS_SYNC_RELAY_TOKENS", "device-a:tok-a,device-b:tok-b")
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "admin-pw")
    monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9100")
    import server as relay_server

    module = importlib.reload(relay_server)
    asyncio.run(module._on_load({}))
    yield module
    asyncio.run(module._on_unload({}))


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    import base64

    data = resp["data"]
    return {
        "success": resp.get("success"),
        "status": data["status"],
        "body": json.loads(base64.b64decode(data["body"])),
    }


def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""

    async def _probe() -> None:
        async with LoopTicker() as ticker:
            time.sleep(0.3)
        assert ticker.max_gap >= 0.2

    asyncio.run(_probe())


def test_borrow_auth_mint_outbound_off_loop(
    relay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """借权换发（login 出站 0.25s）期间心跳持续打点；签发信封语义不变。"""
    threads: list[int] = []
    urls: list[str] = []

    def _fake_http_json(
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, dict[str, Any] | None]:
        del method, token, payload, timeout
        threads.append(threading.get_ident())
        urls.append(url)
        time.sleep(0.25)
        return 200, dict(MINTED)

    monkeypatch.setattr(relay, "_http_json", _fake_http_json)
    loop_thread = threading.get_ident()

    async def _run() -> dict[str, Any]:
        async with LoopTicker() as ticker:
            resp = await relay._handle_borrow_auth("tok-a")
        assert ticker.max_gap < MAX_TICK_GAP
        return resp

    resp = asyncio.run(_run())

    unwrapped = _unwrap(resp)
    assert unwrapped["status"] == 200
    assert unwrapped["body"]["access_token"] == "at-borrow"
    assert unwrapped["body"]["verified_device"] == "device-a"
    assert len(threads) == 1
    assert urls[0].endswith("/api/v1/auth/login")
    assert threads[0] != loop_thread, "出站单发必须在事件循环线程之外"


def test_borrow_auth_invalid_token_403_unchanged(relay: Any) -> None:
    """失败闭面不变：无效 per-device token 403 且不触发出站换发。"""
    resp = asyncio.run(relay._handle_borrow_auth("tok-unknown"))
    unwrapped = _unwrap(resp)
    assert unwrapped["status"] == 403
    assert unwrapped["body"]["error"]
