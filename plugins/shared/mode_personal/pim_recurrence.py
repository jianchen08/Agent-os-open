"""pim 重复事项的下一次发生时刻（纯函数，无轮询/出卡依赖）。

从原自持调度器抽出，供触发器同步层做 repeat 再武装。追赶语义：错过的多次
发生（停机/欠账）折叠到未来最近一次，不逐次补卡；保持原字段粒度。
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from pim_parse import format_iso_like, parse_iso_dt


def next_occurrence(repeat: str, value: str, now: datetime) -> str | None:
    """repeat 事项的下一次发生时刻；无合法下一次 = None（按一次性收口）。"""
    base = parse_iso_dt(value)
    if base is None:
        return None
    candidate: datetime | None = None
    if repeat == "daily":
        candidate = base + timedelta(days=1)
        while candidate <= now:
            candidate += timedelta(days=1)
    elif repeat.startswith("weekly:"):
        try:
            target = int(repeat.split(":", 1)[1])
        except ValueError:
            return None
        candidate = base
        while candidate.weekday() != target:
            candidate += timedelta(days=1)
        while candidate <= now:
            candidate += timedelta(days=7)
    elif repeat.startswith("monthly:"):
        try:
            target_day = int(repeat.split(":", 1)[1])
        except ValueError:
            return None
        year, month = base.year, base.month
        for _ in range(1200):  # 百年量级上界（防呆；正常首个合法月即命中）
            if target_day <= calendar.monthrange(year, month)[1]:
                attempt = datetime(year, month, target_day, base.hour, base.minute)
                if attempt > now:
                    candidate = attempt
                    break
            month += 1
            if month > 12:
                month, year = 1, year + 1
    if candidate is None:
        return None
    return format_iso_like(value, candidate)
