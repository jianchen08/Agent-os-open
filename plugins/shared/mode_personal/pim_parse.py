"""pim 行解析纯函数——页面快速添加行与 inbox.md 文件旁路的唯一解析入口。

`parse_line(line) -> Record | None`：M0 基础组 = checkbox 状态（可省，默认
未完成）+ @时间（今天/明天/ISO 日期[ 时间]/每天/每周X）+ #领域 + !优先级；
M2 捕获组 = 相对时间（@后天/@下周X/@明晚8点/@N分钟后）+ 重复词（@每月N号 →
``monthly:<day>``）+ direction 句式判向（``我欠…``=i_owe / ``…欠我``=owes_me，
从整句提取、句式词保留在标题）。低置信（剥除标记后无标题，或时间词形越界）
= 返回 None / 标记原样留标题，调用方升级主会话 LLM capture 链（fast path
规则 + slow path LLM，Todoist 本地 NLP 同思路）；I1 聊天捕获走 pim 工具强
schema，不经本函数。

纯函数：不读文件、不取时钟——时间参照经 ``now`` 参数注入（日期词形只用
日期部分；``N分钟后`` 等时刻词形用完整时刻），缺省取本地当前时刻（调用方
为 REST/工具入口，传 store 时钟；单测显式传值保证确定性）。

两入口同一函数零分叉（POST /capture 与 inbox.md 扫描，parity 单测钉死）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

# checkbox 前缀（GFM 清单行形态，页面入口可省）
_CHECKBOX_RE = re.compile(r"^-\s*\[(?P<mark>[ xX])\]\s*")

# @时间标记（M0 基础组 + M2 相对时间组）
_WEEKDAY_NAMES = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
# 相对日偏移（今天=当日；后天=+2，删值实验锚）
_AT_DAY_OFFSETS = {"今天": 0, "明日": 1, "明天": 1, "后天": 2}
_AT_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{2}):(\d{2}))?$")
_AT_DAILY = "每天"
_AT_WEEKLY_RE = re.compile(r"^每周([一二三四五六日天])$")
# 下周X = 下一自然周的周X（非"未来最近的周X"）
_AT_NEXT_WEEK_RE = re.compile(r"^下周([一二三四五六日天])$")
# [今/明/后]晚(上)?N点[半]：晚 = 12 小时制下午/晚间档（1-11 点 +12；12 点语义
# 歧义不吞词；13-23 点原样）
_AT_EVENING_RE = re.compile(r"^(今|明|后)天?晚(?:上)?(\d{1,2})点(半)?$")
# [今/明/后]天?[早上|上午]?N点[半]：不带晚 = 时刻原样（8 点/早上 8 点 = 08:00）
_AT_POINT_RE = re.compile(r"^(今|明|后)天?(?:早上|上午)?(\d{1,2})点(半)?$")
_DAY_OFFSETS = {"今": 0, "明": 1, "后": 2}
# N分钟后（1-999；相对完整时刻展开）
_AT_MINUTES_RE = re.compile(r"^(\d{1,3})分钟后$")
# 每月N号（1-31 直通为 monthly:<day>；越界不吞词）
_AT_MONTHLY_RE = re.compile(r"^每月(\d{1,2})号$")

# !优先级（P0 最高；大小写不敏感）
_PRIORITY_RE = re.compile(r"^!([pP])([0-2])$")

# 裸时钟词（@YYYY-MM-DD 与 HH:MM 被空格拆成两词时的并词形态）
_TIME_ONLY_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# #领域：# 后接非空白短标签
_DOMAIN_RE = re.compile(r"^#(\S{1,16})$")

# direction 句式（整句提取，先出现者胜；句式词保留在标题）
_I_OWE = "我欠"
_OWES_ME = "欠我"

# 记录缺省优先级（auto P0-P2 的缺省档：无显式 !Pn = P2）
DEFAULT_PRIORITY = "P2"

# repeat 取值域词表（单一真相源）：weekly 周几 0-6（weekday() 同轴）、monthly 几号 1-31。
# pim_parse（自然语言解析出口）与 pim_recurrence（推进判定）共用，防两套真值漂移。
WEEKDAY_MIN, WEEKDAY_MAX = 0, 6
MONTH_DAY_MIN, MONTH_DAY_MAX = 1, 31


def validate_repeat_target(kind: str, target: int) -> bool:
    """repeat 目标值合法性（词表单点，公共契约——pim_recurrence/pim_store 跨模块消费）。"""
    if kind == "weekly":
        return WEEKDAY_MIN <= target <= WEEKDAY_MAX
    if kind == "monthly":
        return MONTH_DAY_MIN <= target <= MONTH_DAY_MAX
    return False


def parse_iso_dt(value: str) -> datetime | None:
    """ISO 串（``YYYY-MM-DD`` / ``YYYY-MM-DD HH:MM``）→ datetime；非法 = None。

    实体时间字段（todo.due/event.at/focus_started_at）的统一解析口
    （调度器到点比较与幕僚卡建议时刻共用，单一真值）。
    """
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_iso_like(value: str, moment: datetime) -> str:
    """按 ``value`` 的粒度格式化 ``moment``：带时刻 → ``YYYY-MM-DD HH:MM``，
    纯日期 → ISO 日期（repeat 改期/建议改期保持原字段粒度）。"""
    if " " in value or "T" in value:
        return moment.strftime("%Y-%m-%d %H:%M")
    return moment.date().isoformat()


def parse_line(line: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """一行自然语言事项 → todo 记录字段；无法提取标题 = None（低置信）。

    识别组（M0 基础组）：checkbox（``- [ ]``/``- [x]``，可省默认未完成）、
    @时间（``@今天``/``@明天``/``@YYYY-MM-DD[ HH:MM]``/``@每天``/``@每周X``，
    未识别形态原样留在标题）、``#领域``、``!P0|!P1|!P2``。
    识别组（M2 捕获组）：相对时间（``@后天``/``@下周X``/``@[今|明|后]晚N点[半]``/
    ``@[今|明|后]N点[半]``/``@N分钟后``）、重复词（``@每月N号``）、direction
    句式（``我欠…``/``…欠我``，整句判定）。领域/优先级/重复多个出现时取最后
    一个（后写胜，幂等）。

    Returns:
        Record dict：``{title, done, due, repeat, domain, priority, direction}``；
        ``due`` 为 ISO 串（``YYYY-MM-DD`` 或带 `` HH:MM``），无时间标记 = None；
        ``repeat`` 为 ``daily`` / ``weekly:<0-6>`` / ``monthly:<1-31>``；
        ``direction`` 为 ``i_owe`` / ``owes_me``，无句式 = None；
        无标题（纯标记行/空行）= None。
    """
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text:
        return None
    done = False
    m = _CHECKBOX_RE.match(text)
    if m:
        done = m.group("mark").lower() == "x"
        text = text[m.end():]
    direction = _detect_direction(text)

    due: str | None = None
    repeat: str | None = None
    domain: str | None = None
    priority: str | None = None
    title_words: list[str] = []
    tokens = text.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        bare_date = (
            token.startswith("@")
            and _AT_DATE_RE.match(token[1:]) is not None
            and _AT_DATE_RE.match(token[1:]).group(4) is None  # type: ignore[union-attr]
        )
        if bare_date and i < len(tokens) and _TIME_ONLY_RE.match(tokens[i]):
            # ``@YYYY-MM-DD HH:MM`` 两词形态（空格拆词）：时钟词并入 @ 标记
            parsed_at = _parse_at_token(f"{token} {tokens[i]}", now)
            if parsed_at is not None:
                i += 1
        else:
            parsed_at = _parse_at_token(token, now)
        if parsed_at is not None:
            field, value = parsed_at
            if field == "due":
                due = value
            else:
                repeat = value
            continue
        m_domain = _DOMAIN_RE.match(token)
        if m_domain:
            domain = m_domain.group(1)
            continue
        m_pri = _PRIORITY_RE.match(token)
        if m_pri:
            priority = f"P{m_pri.group(2)}"
            continue
        title_words.append(token)
    title = " ".join(title_words).strip()
    if not title:
        return None
    return {
        "title": title,
        "done": done,
        "due": due,
        "repeat": repeat,
        "domain": domain,
        "priority": priority or DEFAULT_PRIORITY,
        "direction": direction,
    }


def _detect_direction(text: str) -> str | None:
    """整句 direction 判定：先出现的句式胜（位置序，无硬编码偏好）。"""
    candidates = [
        (pos, name)
        for pos, name in ((text.find(_I_OWE), "i_owe"), (text.find(_OWES_ME), "owes_me"))
        if pos >= 0
    ]
    if not candidates:
        return None
    return min(candidates)[1]


def _due_with_time(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()} {hour:02d}:{minute:02d}"


def _parse_at_token(token: str, now: datetime | None) -> tuple[str, str] | None:
    """@时间单词 → (字段, 值)；非 @ 开头 / 未识别形态 = None（留在标题）。

    字段 ``due`` = 截止（ISO 日期或日期+时间），``repeat`` = 重复
    （``daily`` / ``weekly:<0-6>`` / ``monthly:<1-31>``，0=周一）。相对词形
    按 ``now`` 注入值展开（缺省本地当前时刻；``N分钟后`` 等时刻词形必须
    有完整时刻，日期级参照不足以展开）。
    """
    if not token.startswith("@"):
        return None
    body = token[1:]
    reference = now if now is not None else datetime.now()

    offset = _AT_DAY_OFFSETS.get(body)
    if offset is not None:
        return ("due", (reference.date() + timedelta(days=offset)).isoformat())
    m_next_week = _AT_NEXT_WEEK_RE.match(body)
    if m_next_week:
        target = _WEEKDAY_NAMES[m_next_week.group(1)]
        # 下一自然周：本周剩余天数 + 目标周几（恒落在 1-7 天后的下一周）
        delta = 7 - reference.weekday() + target
        return ("due", (reference.date() + timedelta(days=delta)).isoformat())

    m_evening = _AT_EVENING_RE.match(body)
    if m_evening:
        hour = int(m_evening.group(2))
        if hour == 0 or hour == 12 or hour > 23:
            return None  # 晚 0/12 点语义歧义、越界 = 未识别，留在标题
        if hour < 12:
            hour += 12
        minute = 30 if m_evening.group(3) else 0
        day = reference.date() + timedelta(days=_DAY_OFFSETS[m_evening.group(1)])
        return ("due", _due_with_time(day, hour, minute))

    m_minutes = _AT_MINUTES_RE.match(body)
    if m_minutes:
        minutes = int(m_minutes.group(1))
        if minutes <= 0:
            return None  # 零时长无意义 = 未识别
        return ("due", (reference + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M"))

    m_monthly = _AT_MONTHLY_RE.match(body)
    if m_monthly:
        day_of_month = int(m_monthly.group(1))
        if not validate_repeat_target("monthly", day_of_month):
            return None
        return ("repeat", f"monthly:{day_of_month}")

    if body == _AT_DAILY:
        return ("repeat", "daily")
    m_weekly = _AT_WEEKLY_RE.match(body)
    if m_weekly:
        return ("repeat", f"weekly:{_WEEKDAY_NAMES[m_weekly.group(1)]}")
    m_date = _AT_DATE_RE.match(body)
    if m_date:
        y, mo, d = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        try:
            due_date = date(y, mo, d)
        except ValueError:
            return None  # 非 ISO 合法日期（如 2026-13-40）= 未识别，留在标题
        hh, mm = m_date.group(4), m_date.group(5)
        return ("due", due_date.isoformat() + (f" {hh}:{mm}" if hh and mm else ""))
    m_point = _AT_POINT_RE.match(body)
    if m_point:
        hour = int(m_point.group(2))
        minute = 30 if m_point.group(3) else 0
        if hour > 23:
            return None
        day = reference.date() + timedelta(days=_DAY_OFFSETS[m_point.group(1)])
        return ("due", _due_with_time(day, hour, minute))
    return None
