# @feature: FP-0.2.多端同步 借权免登 | @ci: python-coverage
"""sync_agent 借权/入网/状态面事件循环活性锁（合宿心跳不冻）。

事故根因（2026-11-12 异步阻塞审计 A-4）：``_http_json`` 是同步 stdlib
``urllib`` 单发（单跳 timeout 15s，借权链 3 跳 = 45s），被 async 链
``_handle_borrow_auth → _relay_verify / _mint_admin_session → _login_admin``
与 ``_handle_enroll → _post_enroll`` 直调——整段跑在 light_stable 合宿宿主
事件循环里，超 30s 即 watchdog ``os._exit(1)``。

锁面（断行为）：
- 出站单发（verify / borrow_auth / login / enroll）出事件循环：假身同步阻塞
  0.25s/跳，心跳间隔不得出现 ≥0.1s 空窗；且假身线程 ≠ 事件循环线程；
- 同插件同类面：``/status``（引擎 ``status_dict`` 含 file-channel git 探针）、
  ``/backup/status``、``/backup/points``（备份仓库 git）同步体同刀出循环；
- 返回/异常语义不变：借权成功信封、入网换发、302/403 映射照旧。

网络边界（stdlib urlopen 实身）与引擎实例按外部依赖替换为假身。
"""

from __future__ import annotations

import base64
import contextlib
import importlib
import json
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker

pytestmark = pytest.mark.unit

MINTED = {
    "access_token": "at-borrow",
    "refresh_token": "rt-borrow",
    "expires_in": 1800,
    "token_type": "bearer",
}

PAIRED_ENV = {
    "AGENTOS_SYNC_RELAY_URL": "http://relay.example:9300/ext/sync_relay",
    "AGENTOS_SYNC_TOKEN": "tok-self",
    "AGENTOS_ADMIN_PASSWORD": "admin-pw",
    "AGENTOS_KERNEL_PORT": "9100",
    # 引擎单拍拉长：测试窗口内只跑启动那一次（不叠加噪声）
    "AGENTOS_SYNC_POLL_INTERVAL": "3600",
}

def _reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **env: str) -> Any:
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import server as agent_server

    return importlib.reload(agent_server)


@contextlib.asynccontextmanager
async def _loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **env: str
) -> AsyncIterator[Any]:
    module = _reload(monkeypatch, tmp_path, **env)
    await module._on_load({})
    try:
        yield module
    finally:
        await module._on_unload({})


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    data = resp["data"]
    body = json.loads(base64.b64decode(data["body"]))
    return {"success": resp.get("success"), "status": data["status"], "body": body}


def _fake_http_json(
    delay: float, threads: list[int], urls: list[str], *, status: int = 200
) -> Any:
    """``_http_json`` 假身（网络边界）：同步阻塞 delay 秒并记录执行线程。"""

    def _fake(
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
        time.sleep(delay)
        if status >= 300:
            return status, {"error": {"message": "nope"}}
        if url.endswith("/verify"):
            return 200, {"valid": True, "device_id": "phone-b"}
        if url.endswith("/auth/login"):
            return 200, dict(MINTED)
        if url.endswith("/enroll"):
            return 200, {"device_token": "tok-enrolled", "tenant_id": "tenant-1"}
        return 200, {"ok": True}

    return _fake


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：出站单发（urlopen 面）出事件循环
# ============================================================


async def test_relay_verify_and_borrow_auth_outbound_off_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """relay /verify 与目标侧 borrow_auth 两跳出站期间心跳持续打点。"""
    async with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        threads: list[int] = []
        urls: list[str] = []
        monkeypatch.setattr(module, "_http_json", _fake_http_json(0.25, threads, urls))
        loop_thread = threading.get_ident()

        async with LoopTicker() as ticker:
            verify = await module._relay_verify("http://relay.example:9300", "tok-x")
            posted = await module._post_borrow_auth(
                "http://target.example:9100/ext/sync_agent/borrow_auth", "dev-1", "tok-self"
            )

    assert ticker.max_gap < MAX_TICK_GAP
    assert verify == (200, {"valid": True, "device_id": "phone-b"})
    assert posted[0] == 200
    assert len(threads) == 2
    assert all(tid != loop_thread for tid in threads), "出站单发必须在事件循环线程之外"


async def test_login_admin_outbound_off_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """本机内核 login 换发（回环出站，超时 15s）出事件循环。"""
    async with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        threads: list[int] = []
        urls: list[str] = []
        monkeypatch.setattr(module, "_http_json", _fake_http_json(0.25, threads, urls))
        loop_thread = threading.get_ident()

        async with LoopTicker() as ticker:
            status, body = await module._login_admin("admin-pw", "9100")

    assert ticker.max_gap < MAX_TICK_GAP
    assert status == 200
    assert body is not None
    assert body["access_token"] == "at-borrow"
    assert threads
    assert all(tid != loop_thread for tid in threads)


async def test_borrow_auth_handler_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """端到端借权链（副本未命中 → relay /verify → login 换发，2×0.25s）：心跳
    不冻，且签发信封仍是 admin 会话 + verified_device。"""
    async with _loaded(monkeypatch, tmp_path, **PAIRED_ENV) as module:
        threads: list[int] = []
        urls: list[str] = []
        monkeypatch.setattr(module, "_http_json", _fake_http_json(0.25, threads, urls))

        async with LoopTicker() as ticker:
            resp = await module._handle_borrow_auth(
                {"authorization": "Bearer tok-requesting-device"}
            )

    assert ticker.max_gap < MAX_TICK_GAP
    unwrapped = _unwrap(resp)
    assert unwrapped["status"] == 200
    assert unwrapped["body"]["access_token"] == "at-borrow"
    assert unwrapped["body"]["verified_device"] == "phone-b"
    assert len(threads) == 2, f"借权应两跳出站，实际 {urls}"


async def test_enroll_outbound_off_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """登录即入网（30s 长超时出站）期间心跳持续打点，凭据落盘与换发照常。"""
    async with _loaded(monkeypatch, tmp_path) as module:
        threads: list[int] = []
        urls: list[str] = []
        monkeypatch.setattr(module, "_http_json", _fake_http_json(0.25, threads, urls))
        loop_thread = threading.get_ident()
        restarted: list[bool] = []

        async def _noop_restart() -> None:
            restarted.append(True)

        # 引擎重建（装配 + 旧单拍任务取消）非本项被测面，按外部生命周期缝替换。
        monkeypatch.setattr(module, "_restart_engine", _noop_restart)
        raw = json.dumps(
            {"relay_url": "http://relay.example:9300/ext/sync_relay", "admin_jwt": "jwt-admin"}
        )

        async with LoopTicker() as ticker:
            resp = await module.http_handle(
                path="/ext/sync_agent/enroll", method="POST", raw_body=raw
            )

    assert ticker.max_gap < MAX_TICK_GAP
    unwrapped = _unwrap(resp)
    assert unwrapped["status"] == 200
    assert unwrapped["body"]["paired"] is True
    assert unwrapped["body"]["tenant"] == "tenant-1"
    assert restarted == [True], "入网成功后引擎必须重建"
    assert threads
    assert all(tid != loop_thread for tid in threads)


# ============================================================
# 同插件同类面：状态/备份观测端点（引擎与备份仓库 git 探针）
# ============================================================


async def test_status_endpoints_keep_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """/status 与 get_status 工具读引擎状态（含 file-channel git 探针）出循环。"""
    async with _loaded(monkeypatch, tmp_path) as module:

        class _SlowEngine:
            async def stop(self) -> None:
                return None

            def status_dict(self) -> dict[str, Any]:
                time.sleep(0.25)
                return {"device_id": "dev-x", "tier": "desktop"}

        monkeypatch.setattr(module, "_engine", _SlowEngine())

        async with LoopTicker() as ticker:
            resp = await module.http_handle(path="/ext/sync_agent/status", method="GET")
            tool_view = await module.get_status()

    assert ticker.max_gap < MAX_TICK_GAP
    assert _unwrap(resp)["body"]["device_id"] == "dev-x"
    assert tool_view["paired"] is True
    assert tool_view["device_id"] == "dev-x"


async def test_backup_observability_endpoints_keep_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """/backup/status 与 /backup/points（备份仓库 git log/探针）出循环。"""
    async with _loaded(monkeypatch, tmp_path) as module:

        class _SlowBackup:
            def status(self) -> dict[str, Any]:
                time.sleep(0.25)
                return {"git_available": True, "commits": 1}

            def points(self) -> list[dict[str, Any]]:
                time.sleep(0.25)
                return [{"sha": "abc", "time": "t", "subject": "s"}]

        def _backup_of() -> Any:
            return _SlowBackup()

        monkeypatch.setattr(module, "_backup_of", _backup_of)

        async with LoopTicker() as ticker:
            status_resp = await module.http_handle(
                path="/ext/sync_agent/backup/status", method="GET"
            )
            points_resp = await module.http_handle(
                path="/ext/sync_agent/backup/points", method="GET"
            )

    assert ticker.max_gap < MAX_TICK_GAP
    assert _unwrap(status_resp)["body"]["commits"] == 1
    assert _unwrap(points_resp)["body"]["points"][0]["sha"] == "abc"
