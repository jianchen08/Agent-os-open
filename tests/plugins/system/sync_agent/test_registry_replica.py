# @feature: FP-0.2.多端同步 分布式注册表与对等信任 | @ci: python-coverage
"""注册表副本面测试（A 档对等信任，ADR 2026-10-22-distributed-registry-peer-trust）：

- RegistryReplica：原子落盘/损坏容错/本地核验（哈希恒等比对，未命中 None）；
- 引擎周期拉取：到期单拍拉 /registry → 副本落盘；失败旧副本保留 + 独立退避；
  未配置副本路径 = 面关（零拉取零落盘）。

时序断言走注入式单调钟（禁零延迟 mock，test_agent_core 同律）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from agent_core import AgentConfig, SyncAgentEngine
from agent_state import AgentStateStore
from outbox_store import OutboxStore
from registry_replica import RegistryReplica, token_sha256

pytestmark = pytest.mark.unit


class MonoClock:
    """注入式单调钟（退避判定推进用）。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, s: float) -> None:
        self.value += s


def test_replica_save_load_roundtrip_and_verify(tmp_path: Path) -> None:
    replica = RegistryReplica(tmp_path / "sync_agent" / "registry_replica.json")
    assert replica.load() is None
    assert replica.devices() == []
    assert replica.fetched_at() == ""
    replica.save(
        devices=[
            {"device_id": "phone-b", "token_hash": token_sha256("tok-phone")},
            {"device_id": "legacy", "token_hash": ""},
        ],
        fetched_at="2026-10-22T10:00:00+00:00",
    )
    data = replica.load()
    assert data is not None
    assert data["fetched_at"] == "2026-10-22T10:00:00+00:00"
    assert len(data["devices"]) == 2
    assert replica.verify("tok-phone") == "phone-b"
    assert replica.verify("tok-rogue") is None, "未命中 None（裁决归调用方）"
    assert replica.verify("") is None
    # 无哈希存量条目不顶名：空 stored 永不与任何 presented 恒等
    assert replica.verify(token_sha256("")) is None


def test_replica_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "replica.json"
    path.write_text("{not-json", encoding="utf-8")
    replica = RegistryReplica(path)
    assert replica.load() is None
    assert replica.devices() == []
    assert replica.verify("tok-x") is None


def test_replica_save_replaces_previous_without_tmp_residue(tmp_path: Path) -> None:
    path = tmp_path / "replica.json"
    replica = RegistryReplica(path)
    replica.save(devices=[{"device_id": "a"}], fetched_at="t1")
    replica.save(devices=[{"device_id": "b"}], fetched_at="t2")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["fetched_at"] == "t2"
    assert not list(tmp_path.glob("*.tmp")), "落盘后无临时文件残留"


class _GetStub:
    """get 出站桩：记录调用、按脚本回放负载/异常（post 面本套件不触）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.payloads: list[dict[str, Any]] = []

    async def __call__(self, url: str, token: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.payloads.pop(0) if self.payloads else {"events": [], "cursor": 0}


def _engine(
    tmp_path: Path, mono: MonoClock, **overrides: Any
) -> tuple[SyncAgentEngine, _GetStub, AgentConfig]:
    get = _GetStub()
    cfg = AgentConfig(
        relay_url="http://relay.test/ext/sync_relay",
        token="tok-self",
        device_id="desktop-a",
        device_tier="desktop",
        app_version="1.0.0",
        watch_root=tmp_path / "config",
        poll_interval_secs=1.0,
        heartbeat_interval_secs=30.0,
        **overrides,
    )
    engine = SyncAgentEngine(
        cfg,
        state=AgentStateStore(tmp_path / "state.db"),
        outbox=OutboxStore(tmp_path / "outbox.jsonl"),
        post_json=get,
        get_json=get,
        sleep_fn=lambda _d: asyncio.sleep(0),
        clock_mono=mono,
        now_fn=lambda: "2026-10-22T10:00:00+00:00",
    )
    return engine, get, cfg


def test_engine_pull_registry_persists_snapshot_then_backs_off_on_failure(
    tmp_path: Path,
) -> None:
    replica_path = tmp_path / "sync_agent" / "registry_replica.json"
    mono = MonoClock()
    engine, get, _cfg = _engine(tmp_path, mono, registry_replica_path=replica_path)
    get.payloads.append(
        {
            "devices": [{"device_id": "cloud-c", "token_hash": token_sha256("tok-cloud")}],
            "fetched_at": "2026-10-22T10:00:00+00:00",
        }
    )
    asyncio.run(engine._pull_registry_when_due())
    assert get.calls == ["http://relay.test/ext/sync_relay/registry"]
    replica = engine.registry_replica
    assert replica is not None
    assert replica.verify("tok-cloud") == "cloud-c", "拉取得到的哈希即刻可本地核验"

    # 退避窗口内不重拉（节奏 = 心跳间隔 30s）
    asyncio.run(engine._pull_registry_when_due())
    assert len(get.calls) == 1

    # 失败：旧副本保留 + last_error 留痕（中枢离线核验不失效），独立退避 1s 起
    mono.advance(31.0)
    get.error = RuntimeError("relay down")
    asyncio.run(engine._pull_registry_when_due())
    assert len(get.calls) == 2
    assert "registry_pull" in engine.status.last_error
    assert json.loads(replica_path.read_text(encoding="utf-8"))["fetched_at"] == (
        "2026-10-22T10:00:00+00:00"
    )
    # 失败后进入退避窗口：不重试
    asyncio.run(engine._pull_registry_when_due())
    assert len(get.calls) == 2


def test_engine_without_replica_path_has_no_registry_face(tmp_path: Path) -> None:
    engine, get, _cfg = _engine(tmp_path, MonoClock())
    assert engine.registry_replica is None
    asyncio.run(engine._pull_registry_when_due())
    assert get.calls == [], "副本路径未配置 = 面关零拉取"
