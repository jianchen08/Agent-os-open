# @feature: FP-0.2.多端同步 2b sync_relay | @ci: python-coverage
"""sync_relay 核心测试：token 防顶名、ULID 幂等、无 HLC 拒收、属主执法、
入网基线层级权威、在链判定、登录直签发。

断行为（公共入口 process_events / select_baseline / is_online 的可观察输出）；
同契约 ≥2 组区分度输入。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from relay_core import (
    BatchOutcome,
    EnrollOutcome,
    EnvelopeError,
    TokenAuth,
    enroll_device,
    is_online,
    parse_tokens_env,
    process_events,
    select_baseline,
    token_sha256,
)
from relay_store import RelayStore

pytestmark = pytest.mark.unit

NOW = "2026-10-15T08:00:00+00:00"


def _hlc(physical: int, logical: int = 0, device: str = "device-a") -> dict[str, Any]:
    return {"physical_ms": physical, "logical": logical, "device_id": device}


def _event(
    ulid: str = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    device: str = "device-a",
    asset_type: str = "config",
    asset_id: str = "policies.yaml",
    physical: int = 1_000,
    **overrides: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ulid": ulid,
        "device_id": device,
        "asset_type": asset_type,
        "asset_id": asset_id,
        "version": 1,
        "updated_at": "2026-10-15T07:00:00+00:00",
        "payload": {"content": "a: 1"},
        "hlc": _hlc(physical, device=device),
    }
    base.update(overrides)
    return base


def _store(tmp_path: Path) -> RelayStore:
    return RelayStore(tmp_path / "relay.db")


def _auth() -> TokenAuth:
    return TokenAuth({"device-a": "tok-a", "device-b": "tok-b"})


class TestParseTokens:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a:t1,b:t2", {"a": "t1", "b": "t2"}),
            (" a : t1 , , c : ", {"a": "t1"}),
            ("", {}),
            ("no-colon-pair", {}),
        ],
        ids=["normal", "sloppy", "empty", "malformed"],
    )
    def test_parse(self, raw: str, expected: dict[str, str]) -> None:
        assert parse_tokens_env(raw) == expected


class TestProcessEventEnforcement:
    def test_happy_batch_accepted_with_hlc(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        outcome = process_events(
            store,
            [_event(), _event(ulid="01ARZ3NDEKTSV4RRFFQ69G5FAW", physical=2_000)],
            auth=_auth(),
            bearer="tok-a",
            received_at=NOW,
        )
        assert isinstance(outcome, BatchOutcome)
        assert outcome.accepted == 2
        assert outcome.duplicates == 0
        assert store.events.count_events() == 2

    def test_bad_token_rejected_whole_envelope(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        outcome = process_events(
            store, [_event()], auth=_auth(), bearer="wrong", received_at=NOW
        )
        assert isinstance(outcome, EnvelopeError)
        assert outcome.status == 401
        assert store.events.count_events() == 0

    @pytest.mark.parametrize(
        "envelope",
        ["not-a-list", [], None],
        ids=["not-list", "empty-list", "missing-events-key"],
    )
    def test_envelope_rejections(self, tmp_path: Path, envelope: Any) -> None:
        store = _store(tmp_path)
        outcome = process_events(store, envelope, auth=_auth(), bearer="tok-a", received_at=NOW)
        assert isinstance(outcome, EnvelopeError)
        assert store.events.count_events() == 0

    @pytest.mark.parametrize(
        "raw",
        [
            _event() | {"hlc": None},
            _event(device="device-b"),  # B 的 token 绑定不符 → 顶名拒绝
            {},
            "junk",
        ],
        ids=["missing-hlc", "device-mismatch-anti-spoof", "empty-object", "junk-entry"],
    )
    def test_entry_rejections_dead_lettered(self, tmp_path: Path, raw: Any) -> None:
        store = _store(tmp_path)
        outcome = process_events(store, [raw], auth=_auth(), bearer="tok-a", received_at=NOW)
        assert isinstance(outcome, BatchOutcome)
        assert outcome.accepted == 0
        assert len(outcome.dead_lettered) == 1
        assert store.events.count_events() == 0
        assert store.list_dead_letters(), "违约事件死信留痕"

    def test_good_and_bad_entries_mixed_batch_not_blocked(self, tmp_path: Path) -> None:
        """坏事件不阻塞批次（trace_ingestion 同律）：好事件照常入账。"""
        store = _store(tmp_path)
        outcome = process_events(
            store,
            [_event(), _event() | {"hlc": None}, _event(ulid="01ARZ3NDEKTSV4RRFFQ69G5FAW", physical=2_000)],
            auth=_auth(), bearer="tok-a", received_at=NOW,
        )
        assert isinstance(outcome, BatchOutcome)
        assert outcome.accepted == 2
        assert len(outcome.dead_lettered) == 1
        assert store.events.count_events() == 2

    def test_replay_same_ulid_counted_duplicate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        first = process_events(store, [_event()], auth=_auth(), bearer="tok-a", received_at=NOW)
        second = process_events(store, [_event()], auth=_auth(), bearer="tok-a", received_at=NOW)
        assert isinstance(first, BatchOutcome)
        assert isinstance(second, BatchOutcome)
        assert first.accepted == 1
        assert second.duplicates == 1
        assert store.events.count_events() == 1, "断网续传重发零重复"


class TestOwnerEnforcement:
    def test_first_writer_becomes_owner(self, tmp_path: Path) -> None:
        """属主面（会话）：发起设备创建；属主本人后续追加放行。"""
        store = _store(tmp_path)
        create = _event(asset_type="session", asset_id="sess-1", device="device-a")
        outcome = process_events(store, [create], auth=_auth(), bearer="tok-a", received_at=NOW)
        assert isinstance(outcome, BatchOutcome)
        assert outcome.accepted == 1
        append = _event(
            ulid="01ARZ3NDEKTSV4RRFFQ69G5FAW",
            asset_type="session",
            asset_id="sess-1",
            device="device-a",
            physical=2_000,
        )
        outcome2 = process_events(store, [append], auth=_auth(), bearer="tok-a", received_at=NOW)
        assert isinstance(outcome2, BatchOutcome)
        assert outcome2.accepted == 1
        assert store.get_owner("session/sess-1") == "device-a"

    @pytest.mark.parametrize("intruder", ["device-b"], ids=["other-device"])
    def test_other_device_write_rejected(self, tmp_path: Path, intruder: str) -> None:
        """B 端改 A 属主会话 = 拒绝（死信留痕，不入账本）。"""
        store = _store(tmp_path)
        process_events(
            store,
            [_event(asset_type="session", asset_id="sess-1", device="device-a")],
            auth=_auth(), bearer="tok-a", received_at=NOW,
        )
        intrude = _event(
            ulid="01ARZ3NDEKTSV4RRFFQ69G5FAW",
            asset_type="session",
            asset_id="sess-1",
            device=intruder,
            physical=9_000,  # HLC 更大也赢不了——属主面不走 LWW
        )
        outcome = process_events(
            store, [intrude], auth=_auth(), bearer="tok-b", received_at=NOW
        )
        assert isinstance(outcome, BatchOutcome)
        assert outcome.accepted == 0
        assert outcome.dead_lettered
        assert "owner_violation" in outcome.dead_lettered[0]["reason"]
        assert store.events.count_events() == 1, "属主违规不入事件账本"

    def test_config_face_has_no_owner(self, tmp_path: Path) -> None:
        """共享面（config）无属主：双端先后写均入账（HLC-LWW 在应用端裁决）。"""
        store = _store(tmp_path)
        process_events(
            store, [_event(device="device-a")], auth=_auth(), bearer="tok-a", received_at=NOW
        )
        outcome = process_events(
            store,
            [_event(ulid="01ARZ3NDEKTSV4RRFFQ69G5FAW", device="device-b", physical=2_000)],
            auth=_auth(), bearer="tok-b", received_at=NOW,
        )
        assert isinstance(outcome, BatchOutcome)
        assert outcome.accepted == 1
        assert store.get_owner("config/policies.yaml") is None

    @pytest.mark.parametrize("asset_type", ["message", "task"], ids=["message", "task"])
    def test_t1_record_types_are_owner_scoped(self, tmp_path: Path, asset_type: str) -> None:
        """T1 三类 DB 逻辑记录皆属主 scoped：首写定属主，他端写死信拒绝。"""
        store = _store(tmp_path)
        create = _event(asset_type=asset_type, asset_id="rec-1", device="device-a")
        outcome = process_events(store, [create], auth=_auth(), bearer="tok-a", received_at=NOW)
        assert isinstance(outcome, BatchOutcome)
        assert outcome.accepted == 1
        intrude = _event(
            ulid="01ARZ3NDEKTSV4RRFFQ69G5FAW",
            asset_type=asset_type,
            asset_id="rec-1",
            device="device-b",
            physical=9_000,  # HLC 更大也赢不了——属主面不走 LWW
        )
        outcome2 = process_events(
            store, [intrude], auth=_auth(), bearer="tok-b", received_at=NOW
        )
        assert isinstance(outcome2, BatchOutcome)
        assert outcome2.accepted == 0
        assert "owner_violation" in outcome2.dead_lettered[0]["reason"]
        assert store.get_owner(f"{asset_type}/rec-1") == "device-a"


class TestSelectBaseline:
    def test_empty_and_tier_authority(self) -> None:
        assert select_baseline([], {}) == []

    def test_tier_beats_recency_for_baseline(self) -> None:
        """层级权威①：手机更新的事件输给云端较旧事件（基线与改动权威分域）。"""
        events = [
            validate(_event(device="phone-b", physical=5_000)),  # 手机最新
            validate(_event(device="cloud-c", physical=1_000)),  # 云端较旧
        ]
        baseline = select_baseline(events, {"phone-b": "phone", "cloud-c": "cloud"})
        assert len(baseline) == 1
        assert baseline[0].device_id == "cloud-c"

    @pytest.mark.parametrize(
        ("tiers", "expected_device"),
        [
            ({"desktop-a": "desktop", "phone-b": "phone"}, "desktop-a"),
            ({"phone-b": "phone"}, "phone-b"),
            ({}, "phone-b"),  # 无 tier 记录按最低位阶，仍出基线
        ],
        ids=["desktop-over-phone", "phone-alone", "unknown-tier"],
    )
    def test_same_tier_and_missing_tier(self, tiers: dict[str, str], expected_device: str) -> None:
        events = [
            validate(_event(device="desktop-a", physical=1_000, asset_id="x.yaml")),
            validate(_event(device="phone-b", physical=2_000, asset_id="x.yaml")),
        ]
        baseline = select_baseline(events, tiers)
        assert [e.device_id for e in baseline] == [expected_device]

    def test_non_conflicting_assets_all_survive(self) -> None:
        """不同资产各自出基线（非冲突不丢）。"""
        events = [
            validate(_event(device="cloud-c", physical=1_000, asset_id="a.yaml")),
            validate(_event(device="phone-b", physical=2_000, asset_id="b.yaml")),
        ]
        baseline = select_baseline(events, {"cloud-c": "cloud", "phone-b": "phone"})
        assert {e.asset_id for e in baseline} == {"a.yaml", "b.yaml"}


def validate(raw: dict[str, Any]) -> Any:
    from sync_event import validate_event

    return validate_event(raw)


class TestIsOnline:
    _NOW = datetime(2026, 10, 15, 8, 0, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("heartbeat", "window", "expected"),
        [
            ("2026-10-15T07:59:30+00:00", 90.0, True),
            ("2026-10-15T07:58:00+00:00", 90.0, False),
            ("2026-10-15T07:59:30+00:00", 20.0, False),
            ("garbage", 90.0, False),
            ("", 90.0, False),
        ],
        ids=["in-window", "stale", "narrow-window", "garbage", "empty"],
    )
    def test_window_semantics(self, heartbeat: str, window: float, expected: bool) -> None:
        assert is_online(heartbeat, self._NOW, window) is expected

    def test_naive_timestamp_treated_as_utc(self) -> None:
        naive = (self._NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S")
        assert is_online(naive, self._NOW, 90.0) is True


class TestEnrollPairing:
    """登录即入网直签发（ADR 2026-11-02-login-as-enrollment）：签发/幂等换发/动态鉴权。

    admin JWT 由内核执法（端点 auth:"admin"），core 层只断注册签发面行为。
    """

    _NOW = datetime(2026, 10, 15, 8, 0, 0, tzinfo=UTC)

    def _enroll(
        self,
        store: Any,
        device: str = "phone-b",
        now: datetime | None = None,
        issued_token: str = "issued-fixed",
    ) -> Any:
        return enroll_device(
            store,
            device_id=device,
            tier="phone",
            app_version="1.0.0",
            capabilities={},
            tenant_id="tenant-home",
            now=now or self._NOW,
            token_gen=lambda: issued_token,
        )

    def test_enroll_registers_device_and_yields_token(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        outcome = self._enroll(store)
        assert isinstance(outcome, EnrollOutcome)
        assert outcome.device_id == "phone-b"
        assert outcome.device_token == "issued-fixed"
        assert outcome.tenant_id == "tenant-home"
        registered = store.get_device("phone-b")
        assert registered is not None, "直签发即注册入注册表"
        assert registered["tier"] == "phone"
        assert registered["token_hash"] == token_sha256("issued-fixed"), (
            "对等信任哈希随签发即落（注册表复制面，A 档）"
        )

    def test_reenroll_rebind_replaces_token_hash(self, tmp_path: Path) -> None:
        """同设备重入网（换 token）：哈希随重签顶旧——副本核验认最新绑定。"""
        store = _store(tmp_path)
        assert isinstance(self._enroll(store, issued_token="tok-one"), EnrollOutcome)
        assert isinstance(self._enroll(store, issued_token="tok-two"), EnrollOutcome)
        assert store.get_device("phone-b")["token_hash"] == token_sha256("tok-two")
        assert token_sha256("tok-one") != token_sha256("tok-two")

    def test_missing_device_id_rejected_zero_side_effect(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        outcome = enroll_device(
            store,
            device_id="",
            tier="phone",
            app_version="",
            capabilities={},
            tenant_id="tenant-home",
            now=self._NOW,
        )
        assert isinstance(outcome, EnvelopeError)
        assert outcome.status == 400
        assert store.list_device_tokens() == {}, "违约零副作用（不签发不注册）"

    def test_issued_token_authenticates_like_env_tokens(self, tmp_path: Path) -> None:
        """直签发即入鉴权面：issued token 通行，未签发凭据不通行（fail-closed 不松）。"""
        store = _store(tmp_path)
        auth = TokenAuth({}, issued_source=store.list_device_tokens)
        assert auth.device_for("anything") is None, "两源皆空一律拒"
        self._enroll(store)
        assert auth.device_for("issued-fixed") == "phone-b"
        assert auth.device_for("not-issued") is None
        assert auth.configured_devices == ["phone-b"]

    def test_issued_token_rotates_on_reenroll(self, tmp_path: Path) -> None:
        """同设备二次入网顶旧 token（重签即换发），旧 token 失效。"""
        store = _store(tmp_path)
        self._enroll(store, issued_token="tok-first")
        outcome = self._enroll(store, issued_token="tok-rotated")
        assert isinstance(outcome, EnrollOutcome)
        assert store.list_device_tokens() == {"phone-b": "tok-rotated"}, "旧 token 被顶掉"
