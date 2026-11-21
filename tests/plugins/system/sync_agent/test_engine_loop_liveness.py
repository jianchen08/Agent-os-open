# @feature: FP-0.2.多端同步 T2 文件类通道 git 化 | @ci: python-coverage
"""sync_agent 引擎单拍事件循环活性锁（同插件同类面，审计 A-3 同族）。

事故根因（2026-11-12 异步阻塞审计 A-3 同插件扫描）：``SyncAgentEngine.tick``
在合宿宿主事件循环上按 ``poll_interval_secs`` 周期跑，属主臂
``capture_file_scopes``（每 scope ``git add -A``/``status``/``commit``，单条
timeout 120s）与读者臂 ``pull_file_scopes``（``head`` rev-parse +
``apply_bundle`` fetch/reset）都是同步子进程——比备份更频繁的同类阻塞点。

锁面（断行为）：
- 单拍内属主臂 git 捕获出事件循环（假身同步阻塞 0.3s）；
- 读者臂 ``head``/``apply_bundle`` 出事件循环（各 0.25s）；
- 单拍语义不变：捕获计数、last_error 落字、bootstrap 出站照旧。
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any

import pytest
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker
from agent_core import AgentConfig, SyncAgentEngine
from agent_state import AgentStateStore
from file_channel import GitChannelError
from outbox_store import OutboxStore

pytestmark = pytest.mark.unit

DEVICE = "desktop-loop"


class _RelayStub:
    """relay 出站假身（外部边界）：零网络，记录载荷。"""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        del url, token
        self.posted.append(payload)

    async def get(self, url: str, token: str) -> dict[str, Any]:
        del url, token
        return {"events": [], "cursor": 0}


class _SleepRecorder:
    async def __call__(self, delay: float) -> None:
        del delay


def _engine(tmp_path: Path, *, file_transport: Any = None) -> tuple[SyncAgentEngine, _RelayStub]:
    relay = _RelayStub()
    engine = SyncAgentEngine(
        AgentConfig(
            relay_url="http://10.0.0.1:9300/ext/sync_relay",
            token="tok",
            device_id=DEVICE,
            device_tier="desktop",
            app_version="1.0.0",
            watch_root=tmp_path / "config",
            poll_interval_secs=3600.0,
            heartbeat_interval_secs=3600.0,
        ),
        state=AgentStateStore(tmp_path / "state.db"),
        outbox=OutboxStore(tmp_path / "outbox.jsonl"),
        post_json=relay.post,
        get_json=relay.get,
        sleep_fn=_SleepRecorder(),
        clock_ms=lambda: 1_000_000,
        clock_mono=lambda: 0.0,
        now_fn=lambda: "2026-11-12T00:00:00+00:00",
        file_transport=file_transport,
    )
    return engine, relay


class _SlowOwnerChannel:
    """属主臂 git 通道假身：capture_commit 同步阻塞（真实通道 = git 子进程）。"""

    owner_scopes = frozenset({"plugins"})
    reader_scopes: frozenset[str] = frozenset()

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.captured: list[str] = []

    def capture_commit(self, scope: str) -> str:
        self.captured.append(scope)
        time.sleep(self._delay)
        return "sha"


class _SlowReaderChannel:
    """读者臂 git 通道假身：head / apply_bundle 同步阻塞。"""

    owner_scopes: frozenset[str] = frozenset()
    reader_scopes = frozenset({"plugins"})
    delay = 0.25

    def __init__(self) -> None:
        self.heads: list[str] = []
        self.applied: list[str] = []

    def head(self, scope: str) -> str | None:
        self.heads.append(scope)
        time.sleep(self.delay)
        return None

    def apply_bundle(self, scope: str, bundle: bytes) -> None:
        del bundle
        self.applied.append(scope)
        time.sleep(self.delay)


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：单拍两臂 git 面出事件循环
# ============================================================


async def test_tick_offloads_owner_scope_git(tmp_path: Path) -> None:
    """单拍属主臂 git 捕获（0.3s）期间心跳持续打点；捕获计数照常。"""
    engine, relay = _engine(tmp_path)
    channel = _SlowOwnerChannel(delay=0.3)
    engine._file_channel = channel  # type: ignore[assignment]

    async with LoopTicker() as ticker:
        await engine.tick()

    assert ticker.max_gap < MAX_TICK_GAP
    assert channel.captured == ["plugins"]
    assert engine.status.bootstrap_done
    assert relay.posted, "自举/心跳出站面照旧"


async def test_pull_file_scopes_offloads_git_arms(tmp_path: Path) -> None:
    """读者臂 head（0.25s）+ apply_bundle（0.25s）出事件循环；bundle 落地照常。"""

    async def transport(scope: str, basis: str | None) -> bytes | None:
        del scope, basis
        return b"bundle-bytes"

    engine, _ = _engine(tmp_path, file_transport=transport)
    channel = _SlowReaderChannel()
    engine._file_channel = channel  # type: ignore[assignment]

    async with LoopTicker() as ticker:
        await engine.pull_file_scopes()

    assert ticker.max_gap < MAX_TICK_GAP
    assert channel.heads == ["plugins"]
    assert channel.applied == ["plugins"]


async def test_capture_git_failure_still_sets_last_error(tmp_path: Path) -> None:
    """失败语义不变：捕获异常只落 last_error（离线第一），不冒泡到单拍。"""

    class _BrokenChannel(_SlowOwnerChannel):
        def capture_commit(self, scope: str) -> str:
            del scope
            raise GitChannelError("git down")

    engine, _ = _engine(tmp_path)
    engine._file_channel = _BrokenChannel(delay=0.0)  # type: ignore[assignment]

    await engine.tick()

    assert "git down" in engine.status.last_error


def test_tick_is_coroutine() -> None:
    """tick 保持协程面（下放不改变调用契约）。"""
    assert inspect.iscoroutinefunction(SyncAgentEngine.tick)
