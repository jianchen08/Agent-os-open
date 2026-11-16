# @feature: FP-0.2.多端同步 2b sync_event | @ci: python-coverage
"""sync_event 事件模型 + 账本存储单测（G-M1.4 契约之外的回归面）。

契约面在 tests/test_mobile_gm1_contracts.py::TestSyncEventContract（G-M1.4，
9 条）；本文件补行为面：schema 违约参数化、cursor 发号性质、跨设备拉取、
落败旧值可查、内存/文件两态库。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from hlc_clock import HlcTimestamp  # noqa: E402
from sync_event import REQUIRED_FIELDS, SyncStore, asset_key_of, validate_event  # noqa: E402

VALID_ULIDS = ["01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FAW"]


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ulid": VALID_ULIDS[0],
        "device_id": "device-a",
        "asset_type": "config",
        "asset_id": "policies.yaml",
        "version": 3,
        "updated_at": "2026-10-13T08:00:00+00:00",
        "payload": {"content": "providers: {}"},
        "hlc": {"physical_ms": 1_000, "logical": 0, "device_id": "device-a"},
    }
    base.update(overrides)
    return base


class TestValidateEvent:
    @pytest.mark.parametrize("field", list(REQUIRED_FIELDS))
    def test_missing_required_field_rejected(self, field: str) -> None:
        raw = _event()
        del raw[field]
        with pytest.raises((ValueError, TypeError, KeyError), match="(必须|缺少)"):
            validate_event(raw)

    @pytest.mark.parametrize(
        "override",
        [
            {"ulid": "01ARZ3NDEKTSV4RRFFQ69G5FA"},  # 25 位
            {"ulid": "01ARZ3NDEKTSV4RRFFQ69G5FAU"},  # U 不在 Crockford 表
            {"ulid": "01arz3ndektsv4rrffq69g5fav"},  # 小写非规范形
            {"ulid": 123},
            {"version": -1},
            {"version": "3"},
            {"version": True},
            {"device_id": ""},
            {"asset_type": ""},
            {"updated_at": ""},
            {"payload": ["not", "dict"]},
            {"asset_id": 7},
            {"hlc": {"physical_ms": -1, "logical": 0, "device_id": "a"}},
            {"cursor": -5},
        ],
        ids=[
            "ulid-short",
            "ulid-non-crockford",
            "ulid-lowercase",
            "ulid-type",
            "version-negative",
            "version-string",
            "version-bool",
            "device-empty",
            "asset-type-empty",
            "updated-at-empty",
            "payload-list",
            "asset-id-type",
            "hlc-negative",
            "cursor-negative",
        ],
    )
    def test_field_violations_rejected(self, override: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="(必须|缺少|ulid)"):
            validate_event(_event(**override))

    def test_optional_fields_default(self) -> None:
        raw = _event()
        del raw["hlc"]
        del raw["payload"]
        del raw["asset_id"]
        event = validate_event(raw)
        assert event.hlc is None
        assert event.payload == {}
        assert event.asset_id == ""
        assert event.asset_key == "config"

    def test_asset_key_composition(self) -> None:
        assert asset_key_of("config", "a.yaml") == "config/a.yaml"
        assert asset_key_of("session", "") == "session"
        assert validate_event(_event()).asset_key == "config/policies.yaml"


class TestSyncStore:
    @pytest.mark.parametrize("db", [None, "file"], ids=["memory", "file"])
    def test_cursor_pull_monotonic_and_paginated(self, tmp_path: Path, db: str | None) -> None:
        third = "01ARZ3NDEKTSV4RRFFQ69G5FBX"
        store = SyncStore(None if db is None else tmp_path / "ledger.db")
        for i, ulid in enumerate(VALID_ULIDS + [third]):
            store.upsert(_event(ulid=ulid, version=i + 1))

        first = store.pull(0)
        cursors = [e.cursor for e in first]
        assert cursors == sorted(cursors)
        assert all(c > 0 for c in cursors)
        assert len(set(cursors)) == len(cursors), "cursor 严格唯一递增"

        watermark = cursors[1]
        rest = store.pull(watermark)
        assert [e.ulid for e in rest] == [third]
        assert all(e.cursor > watermark for e in rest)

    def test_replay_same_ulid_idempotent_across_shapes(self) -> None:
        """同 ULID 重放零重复；内容微调（重发同事件）也不产生第二行。"""
        store = SyncStore()
        assert store.upsert(_event()) is True
        assert store.upsert(_event()) is False
        assert store.upsert(_event(version=4)) is False, "幂等键是 ULID，version 变化不绕过"
        assert store.count_events() == 1

    def test_pull_across_devices_ordered_by_arrival(self) -> None:
        store = SyncStore()
        store.upsert(_event(ulid=VALID_ULIDS[0], device_id="device-a"))
        store.upsert(_event(ulid=VALID_ULIDS[1], device_id="device-b", asset_id="b.yaml"))
        pulled = store.pull(0)
        assert {e.device_id for e in pulled} == {"device-a", "device-b"}
        assert [e.cursor for e in pulled] == sorted(e.cursor for e in pulled)

    def test_hlc_roundtrip_through_ledger(self) -> None:
        store = SyncStore()
        store.upsert(_event())
        pulled = store.pull(0)[0]
        assert pulled.hlc == HlcTimestamp(1_000, 0, "device-a")
        raw = _event(ulid=VALID_ULIDS[1])
        del raw["hlc"]
        store.upsert(raw)
        assert [e for e in store.pull(0) if e.hlc is None][0].ulid == VALID_ULIDS[1]

    def test_events_for_asset_filters_and_orders(self) -> None:
        """单资产历史可查（落败旧值审计面）：跨资产过滤 + 到账序。"""
        store = SyncStore()
        store.upsert(_event(ulid=VALID_ULIDS[0], hlc={"physical_ms": 1_000, "logical": 0, "device_id": "a"}))
        store.upsert(_event(ulid=VALID_ULIDS[1], asset_id="other.yaml", hlc={"physical_ms": 2_000, "logical": 0, "device_id": "a"}))
        history = store.events_for_asset("config", "policies.yaml")
        assert [e.ulid for e in history] == [VALID_ULIDS[0]]
        assert store.max_cursor() == 2

    def test_invalid_event_rejected_at_ledger_door(self) -> None:
        store = SyncStore()
        with pytest.raises(ValueError, match="version"):
            store.upsert(_event(version=-1))
        assert store.count_events() == 0
