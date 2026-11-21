# @feature: FP-0.2.二 mode_personal repeat 再武装 | @ci: python-coverage
"""next_occurrence 纯函数单测：daily/weekly/monthly、错过折叠、非法输入。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parent


def _load(name: str, mod_name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


recurrence = _load("pim_recurrence", "mode_personal_test_recurrence")


class TestNextOccurrence:
    def test_daily(self) -> None:
        assert recurrence.next_occurrence(
            "daily", "2026-10-12 09:00", datetime(2026, 10, 12, 10, 0)
        ) == "2026-10-13 09:00"

    def test_daily_folds_missed_days(self) -> None:
        assert recurrence.next_occurrence(
            "daily", "2026-10-01 09:00", datetime(2026, 10, 12, 10, 0)
        ) == "2026-10-13 09:00"

    def test_weekly_aligns_and_folds(self) -> None:
        # 2026-10-12 是周一；下一个周一同日
        assert recurrence.next_occurrence(
            "weekly:0", "2026-10-12 09:00", datetime(2026, 10, 12, 10, 0)
        ) == "2026-10-19 09:00"

    def test_monthly_skips_short_month(self) -> None:
        assert recurrence.next_occurrence(
            "monthly:31", "2026-01-31 09:00", datetime(2026, 1, 31, 10, 0)
        ) == "2026-03-31 09:00"

    def test_invalid_value_or_repeat(self) -> None:
        assert recurrence.next_occurrence("daily", "bad", datetime(2026, 10, 12)) is None
        assert recurrence.next_occurrence("weekly:x", "2026-10-12 09:00", datetime(2026, 10, 12)) is None
        assert recurrence.next_occurrence("yearly", "2026-10-12 09:00", datetime(2026, 10, 12)) is None

    def test_date_only_keeps_date_granularity(self) -> None:
        assert recurrence.next_occurrence(
            "daily", "2026-10-12", datetime(2026, 10, 12, 10, 0)
        ) == "2026-10-13"


class TestLastOccurrence:
    """last_occurrence 纯函数：<=now 最近一次发生；base 未来 = None（P0④弹卡坐标）。"""

    NOW = datetime(2026, 10, 13, 20, 0, 0)  # 2026-10-13 为周二(1)

    def test_daily_folds_to_most_recent(self) -> None:
        assert (
            recurrence.last_occurrence("daily", "2026-10-12 19:30", self.NOW)
            == "2026-10-13 19:30"
        )
        assert (
            recurrence.last_occurrence("daily", "2026-10-13 19:30", self.NOW)
            == "2026-10-13 19:30"
        )

    def test_daily_future_base_returns_none(self) -> None:
        assert recurrence.last_occurrence("daily", "2026-10-14 08:00", self.NOW) is None

    def test_weekly_targets_most_recent_weekday(self) -> None:
        assert (
            recurrence.last_occurrence("weekly:1", "2026-10-01 09:00", self.NOW)
            == "2026-10-13 09:00"
        )
        assert (
            recurrence.last_occurrence("weekly:0", "2026-10-01 09:00", self.NOW)
            == "2026-10-12 09:00"
        )

    def test_weekly_no_occurrence_between_base_and_now_returns_none(self) -> None:
        # base=10-08（周四）到 now=10-09 20:00 之间无周二 → None
        assert (
            recurrence.last_occurrence("weekly:1", "2026-10-08 09:00", datetime(2026, 10, 9, 20, 0, 0))
            is None
        )

    def test_monthly_recent_day(self) -> None:
        assert (
            recurrence.last_occurrence("monthly:12", "2026-09-12 12:00", self.NOW)
            == "2026-10-12 12:00"
        )

    def test_invalid_repeat_returns_none(self) -> None:
        assert recurrence.last_occurrence("bogus", "2026-10-12 08:00", self.NOW) is None

    def test_invalid_value_returns_none(self) -> None:
        assert recurrence.last_occurrence("daily", "not-a-time", self.NOW) is None
