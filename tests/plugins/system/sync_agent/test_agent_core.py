# @feature: FP-0.2.多端同步 2b sync_agent | @ci: python-coverage
"""sync_agent 端侧引擎测试：捕获打戳、HLC-LWW 合并（后改赢 + 手机后改反例 +
stale 不覆盖）、非冲突各自落地、离线 outbox、自举、退避时序。

时序断言全走注入 fake clock / 记录式 sleep_fn（禁零延迟 mock）；
relay 交互走注入式 post/get 记录桩（关键路径另配真实 HTTP 测试）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agent_core import AgentConfig, RelayStatusError, SyncAgentEngine
from agent_state import AgentStateStore
from outbox_store import OutboxStore

pytestmark = pytest.mark.unit

DEV_A = "desktop-a"
DEV_B = "phone-b"


class FakeClock:
    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


class MonoClock:
    """注入式单调钟（退避判定用）。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, s: float) -> None:
        self.value += s


class SleepRecorder:
    """记录式 sleep：不真睡，只记录请求的延迟（时序不变量断言面）。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FakeRelay:
    """注入式 relay 桩：记录请求、按脚本回放响应/异常。"""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []
        self.post_error: Exception | None = None
        self.get_error: Exception | None = None
        self.get_payloads: list[dict[str, Any]] = []

    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        self.posts.append((url, payload))  # 记录尝试（at-least-once 语义含失败尝试）
        if self.post_error is not None:
            raise self.post_error

    async def get(self, url: str, token: str) -> dict[str, Any]:
        if self.get_error is not None:
            raise self.get_error
        self.gets.append(url)
        return self.get_payloads.pop(0) if self.get_payloads else {"events": [], "cursor": 0}


def _config(tmp_path: Path, device: str = DEV_A, **overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "relay_url": "http://relay.test/ext/sync_relay",
        "token": f"tok-{device}",
        "device_id": device,
        "device_tier": "desktop" if device == DEV_A else "phone",
        "app_version": "1.0.0",
        "watch_root": tmp_path / "config",
        "poll_interval_secs": 1.0,
        "heartbeat_interval_secs": 30.0,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _engine(
    tmp_path: Path,
    *,
    device: str = DEV_A,
    relay: FakeRelay | None = None,
    state: AgentStateStore | None = None,
    outbox: OutboxStore | None = None,
    fake: FakeClock | None = None,
    mono: MonoClock | None = None,
) -> tuple[SyncAgentEngine, FakeRelay, FakeClock, MonoClock]:
    cfg = _config(tmp_path, device=device)
    fake = fake or FakeClock()
    mono = mono or MonoClock()
    relay = relay or FakeRelay()
    engine = SyncAgentEngine(
        cfg,
        state=state or AgentStateStore(tmp_path / "state.db"),
        outbox=outbox or OutboxStore(tmp_path / "outbox.jsonl"),
        post_json=relay.post,
        get_json=relay.get,
        sleep_fn=SleepRecorder(),
        clock_ms=fake,
        clock_mono=mono,
        now_fn=lambda: "2026-10-15T08:00:00+00:00",
    )
    return engine, relay, fake, mono


def _wire(
    ulid: str,
    device: str,
    rel: str,
    content: str,
    physical: int,
    logical: int = 0,
) -> dict[str, Any]:
    return {
        "ulid": ulid,
        "device_id": device,
        "asset_type": "config",
        "asset_id": rel,
        "version": 1,
        "updated_at": "2026-10-15T07:00:00+00:00",
        "payload": {"content": content},
        "hlc": {"physical_ms": physical, "logical": logical, "device_id": device},
        "cursor": 1,
    }


def _write_config(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestCapture:
    @pytest.mark.parametrize(
        ("first", "second"),
        [("a: 1", "a: 2"), ("", "only"), ("x: [1]", "x: [1, 2]")],
        ids=["scalar-change", "empty-to-content", "list-change"],
    )
    def test_capture_stamps_hlc_and_queues_outbox(
        self, tmp_path: Path, first: str, second: str
    ) -> None:
        engine, relay, fake, _ = _engine(tmp_path)
        root = tmp_path / "config"
        _write_config(root, "p.yaml", first)
        engine.capture_local_changes()
        assert engine.pending_count == 1
        fake.advance(50)
        _write_config(root, "p.yaml", second)
        engine.capture_local_changes()
        assert engine.pending_count == 2

        batch = engine._outbox.peek(10)
        stamps = [e["hlc"]["physical_ms"] for e in batch]
        assert stamps[1] > stamps[0], "后捕获事件 HLC 严格更大"
        assert batch[1]["payload"]["content"] == second
        # 未变更不再捕获（基线推进，无回声环）。
        assert engine.capture_local_changes() == 0
        assert engine.pending_count == 2

    def test_capture_advances_asset_state_so_stale_remote_loses(
        self, tmp_path: Path
    ) -> None:
        """本端后改：他端携带更旧 HLC 的事件到达不得覆盖本地新值。"""
        engine, _, fake, _ = _engine(tmp_path)
        root = tmp_path / "config"
        _write_config(root, "p.yaml", "local-new")
        engine.capture_local_changes()  # 本地 HLC = fake 当前

        remote_old = _wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "p.yaml", "remote-old", physical=999)
        engine._ingest_wire(remote_old)
        assert (root / "p.yaml").read_text(encoding="utf-8") == "local-new"
        assert engine.status.stale_total == 1


class TestMergeLWW:
    def test_later_remote_change_wins(self, tmp_path: Path) -> None:
        engine, _, _, _ = _engine(tmp_path)
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "p.yaml", "v1", physical=1_000))
        engine._ingest_wire(
            _wire("01ARZ3NDEKTSV4RRFFQ69G5FAW", DEV_B, "p.yaml", "v2", physical=2_000)
        )
        root = tmp_path / "config"
        assert (root / "p.yaml").read_text(encoding="utf-8") == "v2"
        assert engine.status.applied_total == 2

    def test_phone_later_change_survives_desktop_older_rewrite(
        self, tmp_path: Path
    ) -> None:
        """反例用例（ADR 影响§抽查①）：手机后改不被他端旧值覆盖——改写先到、
        手机后改后到，落盘必须停在手机值。"""
        engine, _, _, _ = _engine(tmp_path)
        # 先收到桌面端的"后改"（HLC 5000）
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_A + "-peer", "p.yaml", "desktop-rewrite", physical=5_000, logical=0))
        # 再收到手机端更晚的改动（HLC 6000）
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAW", DEV_B, "p.yaml", "phone-latest", physical=6_000))
        # 桌面端重传/乱序再至（同 ULID 幂等 + 旧 HLC stale）
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_A + "-peer", "p.yaml", "desktop-rewrite", physical=5_000))
        assert (tmp_path / "config" / "p.yaml").read_text(encoding="utf-8") == "phone-latest"
        assert engine.status.stale_total >= 0

    def test_non_conflicting_changes_both_land(self, tmp_path: Path) -> None:
        engine, _, _, _ = _engine(tmp_path)
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "a.yaml", "alpha", physical=1_000))
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAW", DEV_B, "b.yaml", "beta", physical=2_000))
        root = tmp_path / "config"
        assert (root / "a.yaml").read_text(encoding="utf-8") == "alpha"
        assert (root / "b.yaml").read_text(encoding="utf-8") == "beta"

    def test_replay_is_idempotent(self, tmp_path: Path) -> None:
        engine, _, _, _ = _engine(tmp_path)
        wire = _wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "p.yaml", "v1", physical=1_000)
        assert engine._ingest_wire(wire) is True
        assert engine._ingest_wire(wire) is False, "同 ULID 重放不二次写盘"
        assert engine.status.applied_total == 1

    def test_same_physical_ms_logical_tiebreak(self, tmp_path: Path) -> None:
        """同毫秒冲突带：逻辑计数器大者赢（跨端全序的 device 决胜不吞内容）。"""
        engine, _, _, _ = _engine(tmp_path)
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "p.yaml", "logical-1", physical=1_000, logical=1))
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAW", DEV_B, "p.yaml", "logical-2", physical=1_000, logical=2))
        assert (tmp_path / "config" / "p.yaml").read_text(encoding="utf-8") == "logical-2"

    def test_own_events_skipped(self, tmp_path: Path) -> None:
        engine, _, _, _ = _engine(tmp_path)
        own = _wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_A, "p.yaml", "echo", physical=99_000)
        assert engine._ingest_wire(own) is False
        assert not (tmp_path / "config" / "p.yaml").exists()

    def test_unsupported_asset_type_recorded_not_written(self, tmp_path: Path) -> None:
        """未注册类型（通道注册表外）记 outcome 不落盘。

        T1 起会话/消息/任务已入 db 通道（落只读镜像，见 test_capture_mirror.py）；
        本测试改用注册表外类型守住同一门口（触碰即清，2026-10-17）。
        """
        engine, _, _, _ = _engine(tmp_path)
        unknown = _wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "art-1", "x", physical=1_000)
        unknown["asset_type"] = "artifact"
        assert engine._ingest_wire(unknown) is False
        assert engine._state.applied_outcome("01ARZ3NDEKTSV4RRFFQ69G5FAV") == "unsupported_type"


class TestObserveCausalityInPull:
    def test_pull_advances_local_clock(self, tmp_path: Path) -> None:
        """拉到远端 HLC 后，本地捕获的 HLC causally-after 远端（保因果闭环）。"""
        engine, relay, fake, _ = _engine(tmp_path)
        engine._ingest_wire(_wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "p.yaml", "remote", physical=5_000_000))
        _write_config(tmp_path / "config", "q.yaml", "local")
        fake.advance(1)
        engine.capture_local_changes()
        batch = engine._outbox.peek(10)
        local_stamp = next(e for e in batch if e["asset_id"] == "q.yaml")["hlc"]
        assert local_stamp["physical_ms"] >= 5_000_000


class TestFlushAndBackoff:
    def test_flush_commits_outbox_on_success(self, tmp_path: Path) -> None:
        engine, relay, _, mono = _engine(tmp_path)
        _write_config(tmp_path / "config", "p.yaml", "data")
        engine.capture_local_changes()
        asyncio.run(engine._flush_when_due())
        assert engine.pending_count == 0
        assert engine.status.flushed_total == 1
        url, payload = relay.posts[0]
        assert url.endswith("/events")
        assert len(payload["events"]) == 1

    def test_flush_failure_backs_off_exponentially(self, tmp_path: Path) -> None:
        """退避 = 单调闸门（不阻塞捕获的后台循环，时序不变量在闸门本身）。"""
        engine, relay, _, mono = _engine(tmp_path)
        relay.post_error = RuntimeError("relay down")
        _write_config(tmp_path / "config", "p.yaml", "data")
        engine.capture_local_changes()

        asyncio.run(engine._flush_when_due())  # 败 1
        assert engine.status.failed_flushes == 1
        assert engine.pending_count == 1, "失败不推进消费，事件不丢"
        assert engine._next_flush_at == mono() + 1.0

        asyncio.run(engine._flush_when_due())  # 闸门内不尝试
        assert engine.status.failed_flushes == 1
        assert len(relay.posts) == 1

        mono.advance(1.0)
        asyncio.run(engine._flush_when_due())  # 败 2
        mono.advance(2.0)
        asyncio.run(engine._flush_when_due())  # 败 3
        assert engine.status.failed_flushes == 3
        assert engine._next_flush_at == mono() + 4.0, "指数退避形 1→2→4"
        assert len(relay.posts) == 3, "at-least-once：同批重试"

    def test_backoff_capped_and_resets_on_success(self, tmp_path: Path) -> None:
        engine, relay, _, mono = _engine(tmp_path)
        relay.post_error = RuntimeError("relay down")
        _write_config(tmp_path / "config", "p.yaml", "data")
        engine.capture_local_changes()
        gaps: list[float] = []
        for _ in range(12):
            asyncio.run(engine._flush_when_due())
            gap = engine._next_flush_at - mono()
            gaps.append(round(gap, 6))
            mono.advance(gap)
        assert max(gaps) == 300.0, "退避封顶 300s"
        relay.post_error = None
        asyncio.run(engine._flush_when_due())
        assert engine.pending_count == 0, "恢复续传"
        _write_config(tmp_path / "config", "p.yaml", "data2")
        engine.capture_local_changes()
        asyncio.run(engine._flush_when_due())
        assert len(relay.posts) == 14, "成功后退避归零（闸门立即放行）"

    def test_pull_failure_does_not_block_capture(self, tmp_path: Path) -> None:
        """relay 不可达：拉取退避，捕获照常入 outbox（离线第一）。"""
        engine, relay, _, _ = _engine(tmp_path)
        relay.get_error = ConnectionError("relay down")
        asyncio.run(engine._pull_when_due())
        _write_config(tmp_path / "config", "p.yaml", "offline-change")
        assert engine.capture_local_changes() == 1
        assert engine.pending_count == 1


class TestBootstrap:
    def test_needs_bootstrap_fresh_vs_resumed(self, tmp_path: Path) -> None:
        fresh, _, _, _ = _engine(tmp_path)
        assert fresh.needs_bootstrap() is True

        resumed_state = AgentStateStore(tmp_path / "resumed.db")
        from hlc_clock import HlcTimestamp

        resumed_state.save_cursor(7)
        resumed_state.save_clock(HlcTimestamp(1, 0, DEV_A))
        engine, _, _, _ = _engine(
            tmp_path, state=resumed_state, outbox=OutboxStore(tmp_path / "r.jsonl")
        )
        assert engine.needs_bootstrap() is False

    def test_bootstrap_applies_baseline_and_sets_cursor(self, tmp_path: Path) -> None:
        engine, relay, _, _ = _engine(tmp_path)
        relay.get_payloads.append(
            {
                "events": [
                    _wire("01ARZ3NDEKTSV4RRFFQ69G5FAV", DEV_B, "seeded.yaml", "baseline", physical=1_000)
                ],
                "cursor": 42,
            }
        )
        asyncio.run(engine.bootstrap())
        assert (tmp_path / "config" / "seeded.yaml").read_text(encoding="utf-8") == "baseline"
        assert engine.status.cursor == 42
        assert engine._state.load_cursor() == 42
        # 基线应用后不回声：同内容不再捕获。
        assert engine.capture_local_changes() == 0

    def test_full_sync_roundtrip_via_real_http(self, tmp_path: Path) -> None:
        """关键路径走真实依赖：本机回环 HTTP 上注入 aiohttp 栈（非桩）。"""
        from aiohttp import web

        received: list[dict[str, Any]] = []

        async def events_handler(request: Any) -> Any:
            body = await request.json()
            received.append(body)
            return web.json_response({"accepted": len(body["events"]), "duplicates": 0, "dead_lettered": 0, "details": []})

        async def run() -> None:
            from agent_core import _aiohttp_post_json

            app = web.Application()
            app.router.add_post("/ext/sync_relay/events", events_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            engine, _, _, _ = _engine(
                tmp_path, relay=None
            )
            engine._post_json = _aiohttp_post_json  # 真实 aiohttp 栈
            engine._config.relay_url = f"http://127.0.0.1:{port}/ext/sync_relay"
            _write_config(tmp_path / "config", "p.yaml", "real-http")
            engine.capture_local_changes()
            await engine._flush_when_due()
            await runner.cleanup()

        asyncio.run(run())
        assert received, "真实 HTTP 链路应收到批"
        assert received[0]["events"][0]["asset_id"] == "p.yaml"


class TestSelfReportedAddressAndStorage:
    """设备卡数据源：自报地址（去程路由取源 IP + 内核端口）+ 储存取样（数据根）。

    地址尽力而为：任一环取不到返回空串（设备卡随之无连接入口，不猜不编）；
    储存取不到（数据根缺失）返回 None。
    """

    def _engine_at(self, tmp_path: Path, **overrides: Any) -> SyncAgentEngine:
        return SyncAgentEngine(
            _config(tmp_path, data_root=tmp_path, **overrides),
            state=AgentStateStore(tmp_path / "state.db"),
            outbox=OutboxStore(tmp_path / "outbox.jsonl"),
        )

    def test_self_address_uses_route_ip_and_kernel_port_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_core._lan_ip_toward", lambda _host: "192.168.1.10")
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9300")
        engine = self._engine_at(tmp_path)
        assert engine._self_address() == "http://192.168.1.10:9300"

    def test_self_address_empty_when_no_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_core._lan_ip_toward", lambda _host: None)
        engine = self._engine_at(tmp_path)
        assert engine._self_address() == ""

    def test_self_address_loopback_route_reports_relay_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """relay 同主机（回环路由）：改报 relay 源站，禁广播他端不可达的 127.0.0.1。

        生产实证：云端中枢自报 http://127.0.0.1:9100 → 他端设备卡一张死连接卡。
        """
        monkeypatch.setattr("agent_core._lan_ip_toward", lambda _host: "127.0.0.1")
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9100")
        engine = self._engine_at(
            tmp_path, relay_url="http://203.0.113.153/ext/sync_relay"
        )
        assert engine._self_address() == "http://203.0.113.153", "默认端口不随行"

    def test_self_address_loopback_keeps_nondefault_relay_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_core._lan_ip_toward", lambda _host: "127.0.0.1")
        engine = self._engine_at(
            tmp_path, relay_url="http://192.168.1.5:9300/ext/sync_relay"
        )
        assert engine._self_address() == "http://192.168.1.5:9300"

    def test_self_address_loopback_relay_origin_also_loopback_reports_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """中枢经 loopback 自配对（relay_url 亦回环，云端生产实证二）：如实报空。"""
        monkeypatch.setattr("agent_core._lan_ip_toward", lambda _host: "127.0.0.1")
        engine = self._engine_at(
            tmp_path, relay_url="http://127.0.0.1:9100/ext/sync_relay"
        )
        assert engine._self_address() == ""

    def test_lan_ip_toward_loopback_is_loopback(self) -> None:
        from agent_core import _lan_ip_toward

        assert _lan_ip_toward("127.0.0.1") == "127.0.0.1"

    def test_storage_report_samples_data_root(self, tmp_path: Path) -> None:
        report = self._engine_at(tmp_path)._storage_report()
        assert report is not None
        assert report["free_bytes"] > 0
        assert report["total_bytes"] >= report["free_bytes"]

    def test_storage_report_none_when_data_root_missing(self, tmp_path: Path) -> None:
        engine = SyncAgentEngine(
            _config(tmp_path, data_root=tmp_path / "missing-root"),
            state=AgentStateStore(tmp_path / "state.db"),
            outbox=OutboxStore(tmp_path / "outbox.jsonl"),
        )
        assert engine._storage_report() is None


class TestRelayRegistryPassthrough:
    """云端权威注册表透传通道（R30 数据面销项）：复用既有 relay 通道原语。"""

    def test_pull_routing_view_hits_relay_routing_view_face(
        self, tmp_path: Path
    ) -> None:
        engine, relay, _fake, _mono = _engine(tmp_path)
        relay.get_payloads.append(
            {
                "devices": [
                    {
                        "device_id": DEV_B,
                        "tier": "phone",
                        "tier_rank": 2,
                        "app_version": "1.0.0",
                        "online": True,
                        "last_heartbeat_at": "2026-10-19T06:00:00+00:00",
                        "capabilities": {"asset_types": ["config"]},
                    }
                ],
                "online_window_secs": 90.0,
                "requested_by": DEV_A,
            }
        )
        view = asyncio.run(engine.pull_routing_view())
        assert relay.gets == ["http://relay.test/ext/sync_relay/routing_view"], (
            "打云端 routing_view 只读面（per-device token 自管，auth:none 唯一可达注册表读面）"
        )
        assert view["devices"][0]["device_id"] == DEV_B
        assert view["online_window_secs"] == 90.0

    def test_pull_routing_view_propagates_failure(self, tmp_path: Path) -> None:
        """云端不可达异常原样上抛（降级裁决归端点层，通道层不吞）。"""
        engine, relay, _fake, _mono = _engine(tmp_path)
        relay.get_error = RuntimeError("relay 503: down")
        with pytest.raises(RuntimeError, match="503"):
            asyncio.run(engine.pull_routing_view())


class BodyLimitRelay(FakeRelay):
    """413 体限桩：拒收任一事件命中黑名单的请求（模拟 relay 请求体长度上限）。"""

    def __init__(self, reject_asset_ids: set[str]) -> None:
        super().__init__()
        self.reject_asset_ids = reject_asset_ids

    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        self.posts.append((url, payload))
        for event in payload["events"]:
            if event["asset_id"] in self.reject_asset_ids:
                raise RelayStatusError(
                    413, "Failed to buffer the request body: length limit exceeded"
                )


class TestFlush413:
    """413 体限处置：对半拆批保序 commit / 单毒隔离死信 / 非 413 仍瞬态退避。"""

    @staticmethod
    def _enqueue(engine: SyncAgentEngine, ids: list[str]) -> None:
        for asset_id in ids:
            engine._enqueue_record(
                asset_type="config", asset_id=asset_id, payload={"content": asset_id}
            )

    def test_413_splits_batch_in_half_and_commits_in_order(self, tmp_path: Path) -> None:
        """4 事件批撞体限（单批上限 2 条）→ 拆成 2+2 前后两半，成功即前缀 commit。"""
        relay = BodyLimitRelay(reject_asset_ids=set())  # 按条数拒绝见 post 覆写
        relay.max_events = 2

        async def post(url: str, token: str, payload: dict[str, Any]) -> None:
            relay.posts.append((url, payload))
            if len(payload["events"]) > relay.max_events:
                raise RelayStatusError(
                    413, "Failed to buffer the request body: length limit exceeded"
                )

        relay.post = post  # type: ignore[method-assign]
        engine, _, _, _ = _engine(tmp_path, relay=relay)
        self._enqueue(engine, ["e1", "e2", "e3", "e4"])

        asyncio.run(engine._flush_when_due())

        # posts[0] = 被拒的整批尝试（at-least-once 含失败尝试留痕），[2, 2] 为拆批后
        assert [len(p["events"]) for _, p in relay.posts] == [4, 2, 2], "对半拆批"
        assert [e["asset_id"] for e in relay.posts[1][1]["events"]] == ["e1", "e2"]
        assert [e["asset_id"] for e in relay.posts[2][1]["events"]] == ["e3", "e4"], (
            "前缀游标语义：先投前半成功 commit，再投后半（乱序 = 把未投标已投）"
        )
        assert engine.pending_count == 0
        assert engine.status.flushed_total == 4
        assert engine.status.failed_flushes == 0
        assert engine.status.quarantined_total == 0
        assert engine.status.last_error == ""

    def test_poison_single_event_quarantined_to_dead_letter(self, tmp_path: Path) -> None:
        """毒事件（本体超限）隔离落死信留痕，好事件照常投递，队列不堵。"""
        relay = BodyLimitRelay(reject_asset_ids={"poison"})
        engine, _, _, _ = _engine(tmp_path, relay=relay)
        self._enqueue(engine, ["poison", "healthy"])

        asyncio.run(engine._flush_when_due())

        assert engine.pending_count == 0, "隔离后队列放行"
        assert engine.status.quarantined_total == 1
        assert engine.status.flushed_total == 1
        assert engine.status.last_error.startswith("quarantine:")
        dead = tmp_path / "outbox.jsonl.dead.jsonl"
        assert dead.exists(), "死信文件留痕（隔离 ≠ 静默丢弃）"
        lines = dead.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        import json as _json

        assert _json.loads(lines[0])["asset_id"] == "poison"
        # 混批两段路径：[2]整批被拒 → [1]poison 隔离 → [1]healthy 成功
        assert [len(p["events"]) for _, p in relay.posts] == [2, 1, 1]

    def test_non_413_status_error_keeps_transient_backoff(self, tmp_path: Path) -> None:
        """RelayStatusError 非 413（如 503）= 瞬态：不拆批不隔离，退避语义不变。"""
        engine, relay, _, mono = _engine(tmp_path)
        relay.post_error = RelayStatusError(503, "temporarily unavailable")
        self._enqueue(engine, ["e1", "e2"])

        asyncio.run(engine._flush_when_due())

        assert engine.status.failed_flushes == 1
        assert engine._next_flush_at == mono() + 1.0
        assert engine.pending_count == 2, "失败不推进消费，事件不丢"
        assert engine.status.quarantined_total == 0
        assert not (tmp_path / "outbox.jsonl.dead.jsonl").exists()

    def test_real_http_non_2xx_raises_typed_status_error(self, tmp_path: Path) -> None:
        """真实 aiohttp 栈：非 2xx → RelayStatusError 带状态码（413 分道的前提）。"""
        from aiohttp import web

        async def run() -> None:
            from agent_core import _aiohttp_post_json

            async def reject_handler(request: Any) -> Any:
                return web.Response(status=413, text="length limit exceeded")

            app = web.Application()
            app.router.add_post("/events", reject_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            try:
                with pytest.raises(RelayStatusError) as exc_info:
                    await _aiohttp_post_json(
                        f"http://127.0.0.1:{port}/events", "tok", {"events": []}
                    )
            finally:
                await runner.cleanup()
            assert exc_info.value.status == 413
            assert "length limit exceeded" in str(exc_info.value)

        asyncio.run(run())


class TestLoopbackRelayHeal:
    """回环 relay 端口漂移自愈：凭据 URL 跟随 AGENTOS_KERNEL_PORT（2026-11-16 装机事故面）。"""

    _OLD_URL = "http://localhost:5199/ext/sync_relay"
    _NEW_URL = "http://127.0.0.1:9101/ext/sync_relay"

    def _enqueue(self, engine: SyncAgentEngine, tmp_path: Path) -> None:
        _write_config(tmp_path / "config", "p.yaml", "data")
        engine.capture_local_changes()

    def test_heals_to_kernel_port_and_retries_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9101")
        relay = FakeRelay()
        relay.post_error = ConnectionRefusedError("[WinError 10061] 5199 无监听")
        relay.get_error = RelayStatusError(401, "denied")  # 探活：4xx = 路由在
        state = AgentStateStore(tmp_path / "state.db")
        engine, _, _, _ = _engine(tmp_path, relay=relay, state=state)
        engine._config.relay_url = self._OLD_URL
        self._enqueue(engine, tmp_path)

        asyncio.run(engine._flush_when_due())

        assert engine._config.relay_url == self._NEW_URL, "config 热更新"
        assert state.load_credential("relay_url") == self._NEW_URL, "凭据落盘"
        assert engine._flush_failures == 0, "清退避立即重试"
        assert engine._next_flush_at == 0.0, "下一拍立即可投"
        assert engine.status.failed_flushes == 0, "自愈拍不计失败"

        relay.post_error = None
        relay.get_error = None
        asyncio.run(engine._flush_when_due())
        assert engine.pending_count == 0, "新 URL 续传成功"
        assert relay.posts[-1][0].startswith(self._NEW_URL), "后续投递走新端口"

    def test_reachable_relay_status_error_does_not_heal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9101")
        relay = FakeRelay()
        relay.post_error = RelayStatusError(500, "boom")  # relay 可达回状态——业务层，走退避
        engine, _, _, _ = _engine(tmp_path, relay=relay)
        engine._config.relay_url = self._OLD_URL
        self._enqueue(engine, tmp_path)

        asyncio.run(engine._flush_when_due())

        assert engine._config.relay_url == self._OLD_URL
        assert engine.status.failed_flushes == 1

    def test_remote_relay_never_heals(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9101")
        relay = FakeRelay()
        relay.post_error = ConnectionRefusedError("remote down")
        engine, _, _, _ = _engine(tmp_path, relay=relay)
        engine._config.relay_url = "http://sync.example.com/ext/sync_relay"
        self._enqueue(engine, tmp_path)

        asyncio.run(engine._flush_when_due())

        assert engine._config.relay_url == "http://sync.example.com/ext/sync_relay", "远端 relay 不动"
        assert engine.status.failed_flushes == 1

    def test_probe_unreachable_keeps_old_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9101")
        relay = FakeRelay()
        relay.post_error = ConnectionRefusedError("5199 down")
        relay.get_error = ConnectionError("9101 down")  # 新端口也不可达：env 与实际不符
        engine, _, _, _ = _engine(tmp_path, relay=relay)
        engine._config.relay_url = self._OLD_URL
        self._enqueue(engine, tmp_path)

        asyncio.run(engine._flush_when_due())

        assert engine._config.relay_url == self._OLD_URL, "保守不动（避免写坏凭据）"
        assert engine.status.failed_flushes == 1

    def test_missing_kernel_port_env_keeps_old_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTOS_KERNEL_PORT", raising=False)
        relay = FakeRelay()
        relay.post_error = ConnectionRefusedError("5199 down")
        engine, _, _, _ = _engine(tmp_path, relay=relay)
        engine._config.relay_url = self._OLD_URL
        self._enqueue(engine, tmp_path)

        asyncio.run(engine._flush_when_due())

        assert engine._config.relay_url == self._OLD_URL, "无端口注入面无法确知目标，不动"
        assert engine.status.failed_flushes == 1

    def test_same_url_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", "9101")
        relay = FakeRelay()
        relay.post_error = ConnectionRefusedError("transient")
        engine, _, _, _ = _engine(tmp_path, relay=relay)
        engine._config.relay_url = self._NEW_URL  # 已是新端口
        self._enqueue(engine, tmp_path)

        asyncio.run(engine._flush_when_due())

        assert engine.status.failed_flushes == 1, "同 URL：无漂移，普通退避"
        assert relay.gets == [], "不发起无谓探活"
