# @feature: FP-0.2.可观测性 E-0 ulid_keys | @ci: python-plugins-test
"""ulid_keys 共享模块测试：形态校验、唯一性、同毫秒单调（幂等键/排序地基）。"""

from __future__ import annotations

from ulid_keys import ULID_LENGTH, is_valid_ulid, new_ulid


class TestNewUlid:
    def test_shape_is_26_crockford_chars(self) -> None:
        value = new_ulid()
        assert len(value) == ULID_LENGTH
        assert is_valid_ulid(value)
        assert value == value.upper()

    def test_unique_over_burst(self) -> None:
        burst = {new_ulid() for _ in range(2000)}
        assert len(burst) == 2000

    def test_monotonic_within_same_millisecond(self) -> None:
        # 同进程同毫秒发号字典序单调（(origin_host, ulid) 排序不变量）；
        # 连发两枚必然同毫秒或跨毫秒，跨毫秒天然时序递增——两种情形都单调。
        a, b = new_ulid(), new_ulid()
        assert a < b

    def test_encodes_valid_timestamp_prefix(self) -> None:
        # 首字符是时间戳高位（48bit 毫秒 >> 45），当前纪元下必为 '0'..'2' 段。
        value = new_ulid()
        assert value[0] in "012"


class TestIsValidUlid:
    def test_rejects_wrong_length(self) -> None:
        assert not is_valid_ulid("")
        assert not is_valid_ulid(new_ulid()[:-1])
        assert not is_valid_ulid(new_ulid() + "0")

    def test_rejects_non_crockford_chars(self) -> None:
        bad = "U" * 26  # U 不在 Crockford 表
        assert not is_valid_ulid(bad)
        assert not is_valid_ulid("hello-world-" + "0" * 14)

    def test_accepts_lowercase_and_crockford_aliases(self) -> None:
        value = new_ulid()
        assert is_valid_ulid(value.lower())
        # O/I/L 别名（→0/1/1）宽容解码：全 O 26 字符形态合法（值意义无所谓）。
        assert is_valid_ulid("O" * 26)
        assert is_valid_ulid("I" * 26)
        assert is_valid_ulid("L" * 26)

    def test_rejects_non_string(self) -> None:
        assert not is_valid_ulid(None)  # type: ignore[arg-type]
        assert not is_valid_ulid(12345)  # type: ignore[arg-type]
