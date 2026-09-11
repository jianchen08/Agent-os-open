# @feature: FP-0.2.五 资源治理 | @ci: python-coverage
"""plugins/shared/bounded_dict.py 行为测试（TTL + MAX 双收敛）。

时钟属外部依赖：用可编程 fake clock 注入（不用零延迟 mock 真睡）。
关键行为各配 ≥2 组区分输入（过期/未过期、逐出顺序两种形态、
合法/非法配置）。配置回退（0/负值/env 非法）经收敛行为可观察验证，
不戳私有态。
"""

from __future__ import annotations

import pytest

from bounded_dict import BoundedDict

pytestmark = pytest.mark.unit

_DEFAULT_TTL = 86400.0  # 24h
_DEFAULT_MAX = 1024


class _FakeClock:
    """可编程时钟：测试里显式推进。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(ttl: float = 100.0, max_entries: int = 10) -> tuple[BoundedDict, _FakeClock]:
    clock = _FakeClock()
    return BoundedDict(ttl_seconds=ttl, max_entries=max_entries, clock=clock), clock


class TestTtlSweep:
    def test_expired_entry_swept_on_next_write(self) -> None:
        """过期条目在下一次写入时被清扫（TTL 收敛）。"""
        d, clock = _make(ttl=100.0)
        d["k1"] = {"v": 1}
        clock.advance(101.0)  # 越过 TTL
        d["k2"] = {"v": 2}
        assert "k1" not in d
        assert len(d) == 1
        assert d["k2"] == {"v": 2, "ts": clock.now}

    def test_unexpired_entry_survives_write(self) -> None:
        """TTL 内的条目写入后仍存活（≥2 组区分输入：未过期形态）。"""
        d, clock = _make(ttl=100.0)
        d["k1"] = {"v": 1}
        clock.advance(99.0)  # 未到 TTL
        d["k2"] = {"v": 2}
        assert "k1" in d
        assert len(d) == 2

    def test_entry_stamps_ts_on_write(self) -> None:
        """条目写入即盖 ts 时间戳（读取面多 ts 字段，原字段保留）。"""
        d, clock = _make()
        d["k"] = {"v": "x", "status": "completed"}
        assert d["k"]["ts"] == clock.now
        assert d["k"]["v"] == "x"
        assert d["k"]["status"] == "completed"

    def test_overwrite_updates_ts(self) -> None:
        """同 key 覆写刷新 ts（ts 跟随最后一次写入）。"""
        d, clock = _make()
        d["k"] = {"v": 1}
        first_ts = d["k"]["ts"]
        clock.advance(50.0)
        d["k"] = {"v": 2}
        assert d["k"]["ts"] == first_ts + 50.0
        assert d["k"]["v"] == 2


class TestMaxEvict:
    def test_oldest_evicted_when_over_limit(self) -> None:
        """超限按 ts 逐出最旧（硬上限兜底异常灌写）。"""
        d, clock = _make(max_entries=3)
        for i in range(4):
            d[f"k{i}"] = {"i": i}
            clock.advance(1.0)
        assert set(d.keys()) == {"k1", "k2", "k3"}  # 最旧的 k0 被逐出
        assert len(d) == 3

    def test_rewritten_entry_counts_as_new_ts(self) -> None:
        """覆写过的条目 ts 更新 → 不再是"最旧"，逐出避开它。"""
        d, clock = _make(max_entries=3)
        d["old"] = {"v": 0}
        clock.advance(1.0)
        d["a"] = {"v": 1}
        clock.advance(1.0)
        d["b"] = {"v": 2}
        clock.advance(1.0)
        d["old"] = {"v": 0}  # 覆写 old：ts 变最新
        clock.advance(1.0)
        d["c"] = {"v": 3}  # 超限：最旧的是 a（非 old）
        assert "old" in d
        assert "a" not in d
        assert len(d) == 3

    def test_single_slot_keeps_latest_only(self) -> None:
        """max=1 极端：每次写入只保留最新一条（边界形态）。"""
        d, _ = _make(max_entries=1)
        d["k1"] = {"v": 1}
        d["k2"] = {"v": 2}
        assert list(d.keys()) == ["k2"]
        assert len(d) == 1


class TestConfigDefense:
    def test_nonpositive_ttl_falls_back_to_24h(self) -> None:
        """TTL 0/负值 → 回退默认 24h：跨 24h 时钟后条目被清扫（行为可观察）。"""
        for bad_ttl in (0, -1.0):
            clock = _FakeClock()
            d = BoundedDict(ttl_seconds=bad_ttl, max_entries=5, clock=clock)
            d["k"] = {"v": 1}
            clock.advance(_DEFAULT_TTL + 1)
            d["other"] = {"v": 2}
            assert "k" not in d, f"ttl={bad_ttl} 未按默认 24h 收敛"

    def test_nonpositive_ttl_boundary_keeps_entry(self) -> None:
        """回退后恰在 24h 整点未越界：条目存活（TTL 判定为严格大于）。"""
        clock = _FakeClock()
        d = BoundedDict(ttl_seconds=0, max_entries=5, clock=clock)
        d["k"] = {"v": 1}
        clock.advance(_DEFAULT_TTL)
        d["other"] = {"v": 2}
        assert "k" in d

    def test_nonpositive_max_falls_back_to_default(self) -> None:
        """MAX 0/负值 → 回退默认 1024：灌写超限后被截在 1024（行为可观察）。"""
        for bad_max in (0, -3):
            clock = _FakeClock()
            d = BoundedDict(ttl_seconds=10**9, max_entries=bad_max, clock=clock)
            for i in range(_DEFAULT_MAX + 1):
                d[f"k{i}"] = {"i": i}
            assert len(d) == _DEFAULT_MAX, f"max={bad_max} 未按默认 1024 收敛"
            assert f"k{_DEFAULT_MAX}" in d  # 最新条目在
            assert "k0" not in d  # 最旧条目被逐出

    def test_env_overrides_take_effect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 覆盖 TTL/MAX：各自按覆盖值收敛。"""
        monkeypatch.setenv("AGENTOS_BOUNDED_DICT_TTL_SECONDS", "3600")
        monkeypatch.setenv("AGENTOS_BOUNDED_DICT_MAX_ENTRIES", "16")
        clock = _FakeClock()
        d = BoundedDict(clock=clock)
        d["k"] = {"v": 1}
        clock.advance(3601)
        d["k2"] = {"v": 2}
        assert "k" not in d, "TTL 未按 env=3600 收敛"
        for i in range(20):
            d[f"m{i}"] = {"i": i}
        assert len(d) == 16, "MAX 未按 env=16 收敛"

    @pytest.mark.parametrize("raw", ["abc", "0", "-5"])
    def test_env_invalid_values_fall_back(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """env 非法值（非数值/0/负）回退默认：MAX 行为可观察截在 1024。"""
        monkeypatch.setenv("AGENTOS_BOUNDED_DICT_TTL_SECONDS", raw)
        monkeypatch.setenv("AGENTOS_BOUNDED_DICT_MAX_ENTRIES", raw)
        clock = _FakeClock()
        d = BoundedDict(clock=clock)
        for i in range(_DEFAULT_MAX + 1):
            d[f"k{i}"] = {"i": i}
        assert len(d) == _DEFAULT_MAX


class TestDictFacade:
    def test_read_face_matches_dict_semantics(self) -> None:
        """get/contains/迭代/删除/pop 与内置 dict 读面一致（接入方零感知）。"""
        d: BoundedDict = BoundedDict(ttl_seconds=100, max_entries=10, clock=_FakeClock())
        d["a"] = {"v": 1}
        assert d.get("a") is not None
        assert d.get("missing") is None
        assert "a" in d
        assert set(iter(d)) == {"a"}
        assert list(d.keys()) == ["a"]
        entry = d.pop("a")
        assert entry["v"] == 1
        assert "a" not in d

    def test_instances_are_independent(self) -> None:
        """两实例独立收敛（各自 ts/容量互不串扰）。"""
        clock_a, clock_b = _FakeClock(1.0), _FakeClock(2.0)
        a: BoundedDict = BoundedDict(ttl_seconds=10, max_entries=2, clock=clock_a)
        b: BoundedDict = BoundedDict(ttl_seconds=10, max_entries=2, clock=clock_b)
        a["x"] = {"v": 1}
        b["y"] = {"v": 2}
        assert a["x"]["ts"] == 1.0
        assert b["y"]["ts"] == 2.0
        assert len(a) == 1 and len(b) == 1
