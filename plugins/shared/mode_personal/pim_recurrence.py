"""pim 重复事项的下一次发生时刻（纯函数，无轮询/出卡依赖）。

从原自持调度器抽出，供触发器同步层做 repeat 再武装。追赶语义：错过的多次
发生（停机/欠账）折叠到未来最近一次，不逐次补卡；保持原字段粒度。
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from pim_parse import format_iso_like, parse_iso_dt, validate_repeat_target


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
        if not validate_repeat_target("weekly", target):
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
        if not validate_repeat_target("monthly", target_day):
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


def last_occurrence(repeat: str, value: str, now: datetime) -> str | None:
    """repeat 事项 <=now 最近一次发生时刻；base 未来/无合法发生/非法输入 = None。

    P0④ direct 弹卡判定坐标：与 :func:`next_occurrence` 的追赶语义对称——
    错过的多次发生折叠到最近一次，不逐次补卡。weekly 首个周几候选即晚于
    now 时无发生（返回 None）；monthly 跳过早于 base 的同月 N 日（base 为
    首例时刻，其前不算发生）。
    """
    base = parse_iso_dt(value)
    if base is None or base > now:
        return None
    candidate: datetime | None = None
    if repeat == "daily":
        candidate = base + timedelta(days=(now - base).days)
    elif repeat.startswith("weekly:"):
        try:
            target = int(repeat.split(":", 1)[1])
        except ValueError:
            return None
        if not validate_repeat_target("weekly", target):
            return None
        candidate = base
        while candidate.weekday() != target:
            candidate += timedelta(days=1)
        if candidate > now:
            return None
        candidate += timedelta(weeks=(now - candidate).days // 7)
    elif repeat.startswith("monthly:"):
        try:
            target_day = int(repeat.split(":", 1)[1])
        except ValueError:
            return None
        if not validate_repeat_target("monthly", target_day):
            return None
        year, month = base.year, base.month
        for _ in range(1200):
            if target_day <= calendar.monthrange(year, month)[1]:
                attempt = datetime(year, month, target_day, base.hour, base.minute)
                if attempt > now:
                    break
                if attempt >= base:
                    candidate = attempt
            month += 1
            if month > 12:
                month, year = 1, year + 1
    else:
        return None
    if candidate is None:
        return None
    return format_iso_like(value, candidate)
