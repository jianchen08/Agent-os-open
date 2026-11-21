"""pim 数据治理（P0②）：启动幂等通道——9 张日常 repeat 卡按标题时间表补 due +
4 张早期重叠遗留卡按 ID 清理（delete 记账可 undo 恢复）。

治理语义（任务书 P0 时间表 + 台账 AC-4~AC-7）：

- due 已存在不覆盖；当日时刻已过的卡 due=次日；
- 表外卡不受影响；表内卡不存在时跳过（只补存量，不建卡）；
- 遗留卡 ID 不存在时跳过不报错；删除经 op 记账，undo(op_id) 可恢复。

运行形态：插件启动时跑一次（幂等，二次运行零变化）；用户真实卡数据落
用户空间 data/，治理在此工作空间以「通道 + 测试 fixture」形态落地，真实
数据生效待插件同步后首启自动执行。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# 9 张日常卡标题 → 到点时刻表（任务书 P0 时间表）


def _normalize_title(title: str) -> str:
    """标题归一化（GAP-1）：剥掉首部 emoji/符号/空白，仅留文字主体。"""
    stripped = title.strip()
    while stripped and not (stripped[0].isalnum() or "\u4e00" <= stripped[0] <= "\u9fff"):
        stripped = stripped[1:].strip()
    return "".join(stripped.split())

DAILY_DUE_TABLE: dict[str, str] = {
    "Boss打招呼": "10:00",
    "本职工作": "08:30",
    "华为刷题": "19:30",
    "恋爱聊天": "22:00",
    "大厂精调": "23:00",
    "作息": "23:50",
    "运动": "18:00",
    "保健品": "12:00",
    "AgentOS": "12:00",
}

# 早期重叠遗留卡 ID（任务书清单）：前三张与 9 卡语义重叠，投递周为一次性遗留。
LEGACY_CARD_IDS: tuple[str, ...] = (
    "24a6c59af581",  # 锻炼30分钟
    "a6a6ebbffe43",  # 1点之前睡觉
    "3b690e670dd9",  # 找女孩子聊天30分钟
    "c0b5e43d1112",  # 投递周
)


def _next_slot(now: datetime, hhmm: str) -> str:
    """当日 hhmm 时刻；已过取次日（AC-4）。"""
    hour, minute = (int(part) for part in hhmm.split(":", 1))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot <= now:
        slot += timedelta(days=1)
    return slot.strftime("%Y-%m-%d %H:%M")


def govern(store: Any) -> dict[str, list[dict[str, str]]]:
    """幂等治理一轮：9 卡补 due + 4 遗留卡清理。

    Returns:
        动作清单（调用方落台账/日志）：``due_filled`` 每项
        ``{id, title, due}``；``legacy_removed`` 每项 ``{id, title, op_id}``。
    """
    now = store.now()
    due_filled: list[dict[str, str]] = []
    for record in store.list_records("todo"):
        title = str(record.get("title") or "")
        # GAP-1：真实卡标题带 emoji 前缀/空格/长描述，精确匹配命中 0/3——
        # 剥离首部符号与空白后对表键做子串匹配（双侧同规归一，防误伤表外卡）。
        norm_title = _normalize_title(title)
        hhmm = None
        for key, value in DAILY_DUE_TABLE.items():
            if _normalize_title(key) and _normalize_title(key) in norm_title:
                hhmm = value
                break
        if hhmm is None or str(record.get("due") or ""):
            continue
        try:
            due = _next_slot(now, hhmm)
            store.update("todo", str(record["id"]), {"due": due})
        except ValueError as exc:
            # 单卡数据不合规（如存量非法 repeat）跳过并留痕，不中断启动治理
            logger.warning("[pim_governance] 跳过标题 %r 补 due: %s", title, exc)
            continue
        due_filled.append({"id": str(record["id"]), "title": title, "due": due})

    legacy_removed: list[dict[str, str]] = []
    for card_id in LEGACY_CARD_IDS:
        entity = "todo" if store.get("todo", card_id) is not None else "event"
        record = store.get(entity, card_id)
        if record is None:
            continue
        _deleted, op_id = store.delete(entity, card_id)
        legacy_removed.append(
            {"id": card_id, "title": str(record.get("title") or ""), "op_id": op_id}
        )
    return {"due_filled": due_filled, "legacy_removed": legacy_removed}
