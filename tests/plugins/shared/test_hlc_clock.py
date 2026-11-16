# @feature: FP-0.2.多端同步 2b HLC | @ci: python-coverage
"""HLC 混合逻辑时钟单测（ADR 2026-10-15-multi-device-sync-hlc-lww D2/D3）。

断行为：单调（本机只进不退，wall 回拨/停走含内）、保因果（observe 后严格
大于被观察者）、全序可比（跨端 device_id 决胜）、线格式 roundtrip。
时序用注入 fake clock（毫秒推进可控），不用零延迟 mock。
"""

from __future__ import annotations

import pytest
from hlc_clock import HlcTimestamp, HybridLogicalClock

pytestmark = pytest.mark.unit


class FakeClock:
    """毫秒 fake clock：手动推进（时序不变量断言用，非零延迟 mock）。"""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


def _stamp(physical: int, logical: int, device: str) -> HlcTimestamp:
    return HlcTimestamp(physical_ms=physical, logical=logical, device_id=device)


class TestIssueMonotonic:
    """issue 单调：同一时钟发出的戳严格递增；wall 停走/回拨不破坏单调。"""

    @pytest.mark.parametrize("start_ms", [1_000_000, 5_000_000_000], ids=["small", "large"])
    def test_consecutive_issues_strictly_increase(self, start_ms: int) -> None:
        clock = HybridLogicalClock("dev-a", clock_ms=FakeClock(start_ms))
        stamps = [clock.now() for _ in range(5)]
        assert all(stamps[i] < stamps[i + 1] for i in range(len(stamps) - 1))

    def test_wall_stall_bumps_logical_not_physical(self) -> None:
        """wall 停走（同毫秒连发）：物理分量守住，逻辑计数器递增。"""
        fake = FakeClock(7_777)
        clock = HybridLogicalClock("dev-a", clock_ms=fake)
        first = clock.now()
        second = clock.now()  # wall 未动
        fake.advance(3)
        third = clock.now()  # wall 前进
        assert first.physical_ms == second.physical_ms == 7_777
        assert second.logical == first.logical + 1
        assert third.physical_ms == 7_780
        assert third.logical == 0
        assert first < second < third

    def test_wall_regression_keeps_monotonic(self) -> None:
        """wall 回拨（NTP 跳变/改时）：物理分量不回退，逻辑计数器兜底。"""
        fake = FakeClock(9_000)
        clock = HybridLogicalClock("dev-a", clock_ms=fake)
        before = clock.now()
        fake.advance(-5_000)  # 人为把钟拨回去
        after = clock.now()
        assert after.physical_ms == before.physical_ms
        assert after > before


class TestObserveCausality:
    """observe 保因果：观察到远端戳后，本机后续戳严格大于远端。"""

    @pytest.mark.parametrize(
        ("local_start", "remote_stamp", "wall_advance"),
        [
            (1_000, _stamp(900, 5, "peer"), 0),  # 远端物理更旧
            (1_000, _stamp(1_050, 0, "peer"), 0),  # 远端物理领先本机 wall
            (1_000, _stamp(1_000, 7, "peer"), 0),  # 同毫秒同端冲突带
            (1_000, _stamp(1_050, 3, "peer"), 100),  # wall 已越过远端
        ],
        ids=["remote-stale", "remote-ahead", "same-ms", "wall-ahead"],
    )
    def test_after_observe_local_beats_remote(
        self, local_start: int, remote_stamp: HlcTimestamp, wall_advance: int
    ) -> None:
        fake = FakeClock(local_start)
        clock = HybridLogicalClock("dev-a", clock_ms=fake)
        clock.now()  # 初始化状态
        fake.advance(wall_advance)
        clock.observe(remote_stamp)
        nxt = clock.now()
        assert nxt > remote_stamp
        assert nxt.device_id == "dev-a"

    def test_observe_of_observe_chains_causally(self) -> None:
        """A→B→A 消息链：每跳后新戳严格大于链上全部前序戳。"""
        a = HybridLogicalClock("a", clock_ms=FakeClock(2_000))
        b = HybridLogicalClock("b", clock_ms=FakeClock(2_000))
        s1 = a.now()
        b.observe(s1)
        s2 = b.now()
        a.observe(s2)
        s3 = a.now()
        assert s2 > s1
        assert s3 > s2
        assert s3 > s1

    def test_state_restoration_continues_monotonic(self) -> None:
        """跨重启：恢复 (physical, logical) 后续发戳严格大于重启前。"""
        fake = FakeClock(3_000)
        clock = HybridLogicalClock("dev-a", clock_ms=fake)
        last = clock.now()
        physical, logical = clock.state
        reborn = HybridLogicalClock("dev-a", clock_ms=FakeClock(3_000), initial=(physical, logical))
        nxt = reborn.now()
        assert nxt > last


class TestTotalOrder:
    """全序：(physical, logical, device_id) 字典序，跨端恒可比。"""

    @pytest.mark.parametrize(
        ("left", "right", "expected_lt"),
        [
            (_stamp(100, 0, "a"), _stamp(200, 0, "b"), True),
            (_stamp(100, 1, "a"), _stamp(100, 2, "b"), True),
            (_stamp(100, 5, "a"), _stamp(100, 5, "b"), True),  # device_id 决胜
            (_stamp(100, 5, "b"), _stamp(100, 5, "a"), False),
        ],
        ids=["physical", "logical", "device-tiebreak", "device-tiebreak-reverse"],
    )
    def test_ordering(
        self, left: HlcTimestamp, right: HlcTimestamp, expected_lt: bool
    ) -> None:
        assert (left < right) is expected_lt

    def test_wire_roundtrip(self) -> None:
        raw = {"physical_ms": 42, "logical": 7, "device_id": "phone-1"}
        assert HlcTimestamp.from_dict(raw).to_dict() == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "not-a-dict",
            {"physical_ms": -1, "logical": 0, "device_id": "a"},
            {"physical_ms": 1, "logical": -2, "device_id": "a"},
            {"physical_ms": 1, "logical": 0, "device_id": ""},
            {"physical_ms": 1, "logical": 0},
            {"physical_ms": True, "logical": 0, "device_id": "a"},
        ],
        ids=[
            "not-dict",
            "negative-physical",
            "negative-logical",
            "empty-device",
            "missing-device",
            "bool-physical",
        ],
    )
    def test_wire_violations_rejected(self, raw: object) -> None:
        with pytest.raises(ValueError, match="hlc"):
            HlcTimestamp.from_dict(raw)
