# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""time_iso 公共模块契约测试 — UTC 时间戳单点 + 跨格式排序归一。

覆盖 ``plugins/shared/time_iso.py``：

- ``now_iso_utc``：``+00:00`` 后缀、微秒精度、UTC 时区（解析回读校验）、
  单调性（后调用的时刻不早于先调用）；
- ``iso_sort_key``：``Z`` 后缀秒级存量与 ``+00:00`` 微秒新格式混存时按真实
  时刻排序（直接字符串比较会因 'Z' > '.' 产生跨格式乱序——hindsight
  list_items 排序兼容的根因）；不可解析值回退字符串桶不炸。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # TDD 分层：纯单测，零外部依赖（tests/plugins 强制）

_SHARED_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import time_iso  # noqa: E402, I001  (需先推 sys.path 再导入；isort 不识别平铺裸模块——per-file-ignores 先例)


# ── now_iso_utc：格式契约 ───────────────────────────────────────────────


def test_now_iso_utc_format_and_roundtrip() -> None:
    value = time_iso.now_iso_utc()
    assert value.endswith("+00:00")  # 统一 +00:00 后缀（多数派既有格式）
    parsed = datetime.fromisoformat(value)  # 可解析
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    # 微秒精度（非秒级截断）
    assert "." in value


def test_now_iso_utc_monotonic() -> None:
    """性质：两次调用严格不回退（时钟单调，落库排序的前提）。"""
    first = datetime.fromisoformat(time_iso.now_iso_utc())
    second = datetime.fromisoformat(time_iso.now_iso_utc())
    assert second >= first


# ── iso_sort_key：跨格式混存排序归一 ─────────────────────────────────────


def test_sort_key_z_suffix_vs_offset_suffix_chronological() -> None:
    """同秒边界：'Z' 后缀（存量）与 '+00:00' 微秒（新写入）混存按真实时刻排序。

    字符串直排会乱序：'Z'(0x5A) > '.'(0x2E) 使秒级整点排在同秒任意微秒值
    之后，而其真实时刻（.000000）最早——归一后按时刻正序。
    """
    old_format = "2026-09-11T08:00:00Z"  # 存量：秒级 Z 后缀（= .000000）
    later_micros = "2026-09-11T08:00:00.999999+00:00"
    earlier_micros = "2026-09-11T08:00:00.000001+00:00"
    keys = {v: time_iso.iso_sort_key(v) for v in (old_format, later_micros, earlier_micros)}
    # 时刻序：Z(.000000) < .000001 < .999999 —— 与字符串直排（Z 最大）相反
    assert keys[old_format] < keys[earlier_micros] < keys[later_micros]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("2026-09-11T08:00:00Z", "2026-09-12T08:00:00Z"),  # 存量格式内部可排序
        ("2026-09-11T08:00:00+00:00", "2026-09-12T08:00:00+00:00"),  # 新格式内部可排序
    ],
)
def test_sort_key_orders_within_same_format(a: str, b: str) -> None:
    assert time_iso.iso_sort_key(a) < time_iso.iso_sort_key(b)


def test_sort_key_unparsable_falls_back_to_string_bucket() -> None:
    """不可解析值（测试夹具 't0' 等）归字符串桶：不炸、可排序、排在可解析之后。"""
    key = time_iso.iso_sort_key("t0")
    assert key == (1, "t0")
    assert time_iso.iso_sort_key("2026-09-11T08:00:00Z") < key
    assert time_iso.iso_sort_key("t0") == time_iso.iso_sort_key("t0")
    assert time_iso.iso_sort_key("t0") < time_iso.iso_sort_key("t1")


def test_sort_key_rejects_non_string() -> None:
    """非字符串输入无时间语义 → TypeError（防静默吞错）。"""
    with pytest.raises(TypeError):
        time_iso.iso_sort_key(None)  # type: ignore[arg-type]
