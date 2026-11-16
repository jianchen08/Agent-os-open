# @feature: FP-0.2.多端同步 2c 执行通道 | @ci: python-coverage
"""sync_agent 引擎 exec 通道钩子接线测试（R18 诚实缺口的正式化，ADR D8/D13'）。

断行为：
- 注册/心跳 capabilities 携带 exec_channel 能力子键（D8 端侧采集契约经引擎
  既有注册/心跳面上报；未启用 = 子键缺席，注册表零噪音）；
- 验证集周期拉取（属主端验证面经引擎传输口周期刷新；拉取失败独立退避且旧集
  保留——验证面不因单次失败失明；未启用 = 零拉取零状态面）。

时序全走注入 fake clock / MonoClock（禁零延迟 mock）；relay 交互走注入式
路由桩（按 URL 分发，真实依赖关键断言另配验收车道）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_core import AgentConfig, SyncAgentEngine
from agent_state import AgentStateStore
from exec_channel import ExecChannelConfig
from outbox_store import OutboxStore

pytestmark = pytest.mark.unit

DEV_A = "desktop-a"
NOW_ISO = "2026-10-17T08:00:00+00:00"
NOW = datetime(2026, 10, 17, 8, 0, 0, tzinfo=UTC)
EXEC_CAPABILITY = {
    "mcp_url": "http://127.0.0.1:9103/mcp",
    "allowed_roots": ["D:/workspaces"],
    "protocol_version": "1",
}


class MonoClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, s: float) -> None:
        self.value += s


class SleepRecorder:
    async def __call__(self, delay: float) -> None:
        pass


class RoutingRelay:
    """按 URL 后缀分发的注入式 relay 桩（记录上行，按脚本回放下行）。"""

    def __init__(self, tokens: list[dict[str, Any]] | None = None) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.get_urls: list[str] = []
        self.exec_tokens: list[dict[str, Any]] = tokens or []
        self.get_error: Exception | None = None

    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        self.posts.append((url, payload))

    async def get(self, url: str, token: str) -> dict[str, Any]:
        if self.get_error is not None:
            raise self.get_error
        self.get_urls.append(url)
        if url.endswith("/exec_tokens"):
            return {"device": DEV_A, "tokens": self.exec_tokens}
        return {"events": [], "cursor": 0}


def _engine(
    tmp_path: Path,
    relay: RoutingRelay,
    *,
    exec_channel: ExecChannelConfig | None = None,
) -> tuple[SyncAgentEngine, RoutingRelay, MonoClock]:
    mono = MonoClock()
    config = AgentConfig(
        relay_url="http://relay.test/ext/sync_relay",
        token=f"tok-{DEV_A}",
        device_id=DEV_A,
        device_tier="desktop",
        app_version="1.0.0",
        watch_root=tmp_path / "config",
        poll_interval_secs=1.0,
        heartbeat_interval_secs=30.0,
        exec_channel=exec_channel,
        exec_verification_path=tmp_path / "data" / "exec_verification.json",
    )
    engine = SyncAgentEngine(
        config,
        state=AgentStateStore(tmp_path / "state.db"),
        outbox=OutboxStore(tmp_path / "outbox.jsonl"),
        post_json=relay.post,
        get_json=relay.get,
        sleep_fn=SleepRecorder(),
        clock_mono=mono,
        now_fn=lambda: NOW_ISO,
    )
    return engine, relay, mono


def _exec_config() -> ExecChannelConfig:
    return ExecChannelConfig(
        mcp_url="http://127.0.0.1:9103/mcp", allowed_roots=("D:/workspaces",)
    )


def _registration_payload(relay: RoutingRelay) -> dict[str, Any]:
    url, payload = next(p for p in relay.posts if p[0].endswith("/devices"))
    return payload


def _heartbeat_payload(relay: RoutingRelay) -> dict[str, Any]:
    url, payload = next(p for p in relay.posts if p[0].endswith("/devices/heartbeat"))
    return payload


class TestCapabilitySubkeyOnRegistration:
    def test_bootstrap_registration_carries_exec_channel_subkey(self, tmp_path: Path) -> None:
        """注册 capabilities 携带 exec_channel 子键（D8：allowed_roots 即领地声明）。"""
        relay = RoutingRelay()
        engine, relay, mono = _engine(tmp_path, relay, exec_channel=_exec_config())

        asyncio.run(engine.bootstrap())

        capabilities = _registration_payload(relay)["capabilities"]
        assert capabilities["exec_channel"] == EXEC_CAPABILITY
        assert "config" in capabilities["asset_types"]

    def test_registration_without_exec_channel_omits_subkey(self, tmp_path: Path) -> None:
        """未启用 exec 通道 = 子键缺席（注册表零噪音，D8 缺省不上报）。"""
        relay = RoutingRelay()
        engine, relay, mono = _engine(tmp_path, relay)

        asyncio.run(engine.bootstrap())

        capabilities = _registration_payload(relay)["capabilities"]
        assert "exec_channel" not in capabilities


class TestCapabilitySubkeyOnHeartbeat:
    def test_heartbeat_carries_exec_channel_subkey(self, tmp_path: Path) -> None:
        """心跳携带同一能力子键（配置漂移无需重自举即刷新注册表）。"""
        relay = RoutingRelay()
        engine, relay, mono = _engine(tmp_path, relay, exec_channel=_exec_config())

        asyncio.run(engine.tick())

        capabilities = _heartbeat_payload(relay)["capabilities"]
        assert capabilities["exec_channel"] == EXEC_CAPABILITY

    def test_heartbeat_without_exec_channel_omits_subkey(self, tmp_path: Path) -> None:
        relay = RoutingRelay()
        engine, relay, mono = _engine(tmp_path, relay)

        asyncio.run(engine.tick())

        assert "capabilities" not in _heartbeat_payload(relay) or (
            "exec_channel" not in _heartbeat_payload(relay).get("capabilities", {})
        )


class TestVerificationSetPeriodicPull:
    def _fetched_token(self) -> dict[str, Any]:
        return {
            "token": "exec-token-for-owner",
            "issued_by": "initiator-x",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }

    def test_pull_refreshes_verification_face(self, tmp_path: Path) -> None:
        """验证集周期拉取：引擎验证面可验证发起端 Bearer（授权回执）。"""
        relay = RoutingRelay(tokens=[self._fetched_token()])
        engine, relay, mono = _engine(tmp_path, relay, exec_channel=_exec_config())

        asyncio.run(engine.tick())

        assert any(u.endswith("/exec_tokens") for u in relay.get_urls)
        receipt = engine.exec_token_channel.verify("exec-token-for-owner", now=NOW)
        assert receipt is not None
        assert receipt["issued_by"] == "initiator-x"
        status = engine.status_dict()["exec_channel"]
        assert status["capability"] == EXEC_CAPABILITY
        assert status["verification"]["tokens_persisted"] == 1
        assert status["verification"]["synced_at"] == NOW_ISO

    def test_pull_holds_cadence_between_ticks(self, tmp_path: Path) -> None:
        """验证集拉取按心跳节奏节流：退避窗口内重复 tick 不重复拉取。"""
        relay = RoutingRelay(tokens=[self._fetched_token()])
        engine, relay, mono = _engine(tmp_path, relay, exec_channel=_exec_config())

        asyncio.run(engine.tick())
        asyncio.run(engine.tick())

        pulls = [u for u in relay.get_urls if u.endswith("/exec_tokens")]
        assert len(pulls) == 1

    def test_pull_failure_backs_off_and_keeps_old_set(self, tmp_path: Path) -> None:
        """拉取失败：独立退避 + 旧验证集保留（验证面不失明，错误是值）。"""
        relay = RoutingRelay(tokens=[self._fetched_token()])
        engine, relay, mono = _engine(tmp_path, relay, exec_channel=_exec_config())
        asyncio.run(engine.tick())

        relay.get_error = RuntimeError("relay 503")
        relay.get_urls.clear()
        mono.advance(31.0)  # 越过心跳节奏窗，拉取臂重新到期
        asyncio.run(engine.tick())
        assert engine.exec_token_channel.verify("exec-token-for-owner", now=NOW) is not None
        assert "exec_pull" in engine.status.last_error
        assert [u for u in relay.get_urls if u.endswith("/exec_tokens")] == []

        # 退避窗口内不重试（独立退避面）。
        asyncio.run(engine.tick())
        assert [u for u in relay.get_urls if u.endswith("/exec_tokens")] == []

    def test_unconfigured_exec_channel_skips_pull_and_status(self, tmp_path: Path) -> None:
        """未启用 = 零拉取零状态面（与能力子键缺席同口径）。"""
        relay = RoutingRelay(tokens=[self._fetched_token()])
        engine, relay, mono = _engine(tmp_path, relay)

        asyncio.run(engine.tick())

        assert not any(u.endswith("/exec_tokens") for u in relay.get_urls)
        assert "exec_channel" not in engine.status_dict()
        assert engine.exec_token_channel is None
