"""pim 实体域存储——七实体 JSONL 自持 data/（ADR 2026-10-12-personal-pim-plugin-owned-storage）。

实体与文件（包内 ``data/``，upsert-by-id 整文件原子重写 temp+rename + 内存索引）：

- todo/event/habit/preference → ``todos.jsonl`` / ``events.jsonl`` /
  ``habits.jsonl`` / ``preferences.jsonl``（按 id / key upsert）
- journal → ``journal_YYYYMM.jsonl`` 按月分文件（key = date）
- partner → ``partner.json`` 整读整写（单实体）
- op_log → ``oplog.jsonl`` append-only 环形保留 50（撤销数据面 + 审计面）

操作记账契约：一切状态变更经 :meth:`record_op` 落 op（``items`` = 逆操作
清单 ``[{entity, id, before, after}]``）；``undo(op_id)`` 按 items 逆序回滚
（before=None = 撤销创建 = 删除记录），undo 本身追加 ``undo`` 型 op 不改史。
complete 附带伙伴结算（xp/streak），同一 op 内一并可逆。

时钟经 ``clock`` 注入（时序测试用 fake clock）；错误是值：缺 id 抛
KeyError、非法字段值抛 ValueError，由调用方（REST/工具）翻译对外形态。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from _root_api import atomic_write_text
from pim_parse import DEFAULT_PRIORITY

logger = logging.getLogger(__name__)

# todo 状态机枚举（backlog|today|doing|done|failed|wait）
TODO_STATUSES = ("backlog", "today", "doing", "done", "failed", "wait")
# event 状态机枚举（planned=活跃；doing=已确认/同意；wait=已转交；其余=终态）
EVENT_STATUSES = ("planned", "doing", "wait", "done", "cancelled", "failed")
# event 活跃集合（空 = 历史数据/未写状态，按活跃处理）
EVENT_ACTIVE_STATUSES = ("planned", "")
# 优先级枚举（P0 最高）
PRIORITIES = ("P0", "P1", "P2")
# 方向枚举（我欠人/人欠我/无）
DIRECTIONS = ("i_owe", "owes_me", "none")
# op_log 环形保留条数
OP_LOG_KEEP = 50
# complete 完成一次的伙伴经验值（M0 常量；结算规则深化属 M2/M3）
COMPLETE_XP = 10

_RECORD_FILES = {
    "todo": "todos.jsonl",
    "event": "events.jsonl",
    "habit": "habits.jsonl",
    "preference": "preferences.jsonl",
}


def new_id() -> str:
    """实体 id（uuid4 短 hex；个人级数据量无碰撞面）。"""
    return uuid.uuid4().hex[:12]


def habit_streak(habit: dict[str, Any], today: date) -> int:
    """habit.streak 派生值：history（ISO 日期串列表）中截至今天/昨天的连续天数。

    今天已打卡 = 从今天倒数；未打卡 = 从昨天倒数（连击不断，今天还有机会）。
    """
    days = {str(d) for d in habit.get("history") or [] if isinstance(d, str)}
    cursor = today if today.isoformat() in days else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# habit.schedule 的 M0 词表：分隔符 + 「每天/daily」通配 + 「每周X」周几
_SCHEDULE_SPLIT_RE = re.compile(r"[,，/;；\s]+")
_WEEKDAY_TOKENS = {"每周一": 0, "每周二": 1, "每周三": 2, "每周四": 3,
                   "每周五": 4, "每周六": 5, "每周日": 6, "每周天": 6}


def _habit_due_today(schedule: str, today: date) -> bool:
    """schedule 词表判定今日是否到期（M0 基础组：每天/daily + 每周X）。

    未识别 token 忽略（不误报到期）；空 schedule = 每日习惯（缺省到期）。
    """
    tokens = [t for t in _SCHEDULE_SPLIT_RE.split(schedule) if t]
    if not tokens:
        return True
    for token in tokens:
        if token in ("每天", "每日", "daily"):
            return True
        weekday = _WEEKDAY_TOKENS.get(token)
        if weekday is not None and weekday == today.weekday():
            return True
    return False


class PimStore:
    """个人实体域存储（进程内单例语义，包 data/ 目录为持久真值）。"""

    def __init__(
        self,
        data_dir: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._dir = Path(data_dir)
        self._clock = clock or datetime.now
        self._dir.mkdir(parents=True, exist_ok=True)
        # 内存索引：id/key → record（dict 可变拷贝源，落盘前整文件重写）
        self._records: dict[str, dict[str, dict[str, Any]]] = {
            entity: {} for entity in _RECORD_FILES
        }
        self._journal: dict[str, dict[str, Any]] = {}
        self._partner: dict[str, Any] = {}
        self._ops: list[dict[str, Any]] = []
        # 番茄默认时长（插件配置注入面；单次 start_focus 可覆盖）
        self._focus_default_minutes = 25
        # 变更观察者（触发器同步适配层；None = 未接线）。回调同步执行，
        # 只做事件入队，不阻塞存储主路径。
        self._on_change: (
            Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None], None] | None
        ) = None
        self._load()

    @property
    def data_dir(self) -> Path:
        """数据目录（调度器等侧车状态的持久落点；store 为目录唯一所有者）。"""
        return self._dir

    def set_on_change(
        self,
        callback: Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None], None]
        | None,
    ) -> None:
        """注入变更观察者（mode_personal 触发器同步适配层接线）。"""
        self._on_change = callback

    def _notify_change(
        self,
        entity: str,
        key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            callback(entity, key, before, after)
        except Exception:  # noqa: BLE001 — 观察者是副作用面，失败不回滚存储
            logger.exception(
                "[pim_store] 变更观察者失败（存储已落盘）| entity=%s id=%s", entity, key
            )

    # ── 装载 ──

    def _load(self) -> None:
        for entity, filename in _RECORD_FILES.items():
            path = self._dir / filename
            for line in _read_lines(path):
                record = _loads(line)
                if record is not None:
                    key = self._key_of(entity, record)
                    if key:
                        self._records[entity][key] = record
        # journal 按月分文件（journal_YYYYMM.jsonl）
        for path in sorted(self._dir.glob("journal_*.jsonl")):
            for line in _read_lines(path):
                record = _loads(line)
                if isinstance(record, dict) and record.get("date"):
                    self._journal[str(record["date"])] = record
        partner = _loads((self._dir / "partner.json").read_text(encoding="utf-8")) \
            if (self._dir / "partner.json").is_file() else None
        self._partner = partner if isinstance(partner, dict) else self._default_partner()
        for line in _read_lines(self._dir / "oplog.jsonl"):
            record = _loads(line)
            if record is not None:
                self._ops.append(record)

    @staticmethod
    def _key_of(entity: str, record: dict[str, Any]) -> str:
        if entity == "preference":
            return str(record.get("key", ""))
        return str(record.get("id", ""))

    @staticmethod
    def _default_partner() -> dict[str, Any]:
        return {"level": 1, "xp": 0, "streak": 0, "freezes": 0, "trees": 0, "season_id": None}

    # ── 通用 CRUD（todo/event/habit/preference 同构面）──

    def create(self, entity: str, fields: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """建记录（生成 id/created 缺省值 + 枚举校验），记账 op 并落盘。

        Returns:
            (record, op_id)——op_id 随回执出站（可撤销）。
        """
        record = dict(fields)
        record["id"] = new_id()
        record["created"] = self._clock().isoformat()
        if entity == "todo":
            # 记录形状完整化：缺省字段落盘即带（读方零缺键分支）
            record.setdefault("status", "backlog")
            record.setdefault("progress", 0)
            record.setdefault("priority", DEFAULT_PRIORITY)
            record.setdefault("direction", "none")
        elif entity == "event":
            if not record.get("status"):
                record["status"] = "planned"
        self._validate(entity, record)
        self._put(entity, record)
        op_id = self.record_op(f"{entity}.create", [{
            "entity": entity, "id": record["id"], "before": None, "after": record,
        }])
        self._notify_change(entity, record["id"], None, dict(record))
        return dict(record), op_id

    def get(self, entity: str, key: str) -> dict[str, Any] | None:
        record = self._records[entity].get(key)
        return dict(record) if record is not None else None

    def list_records(self, entity: str, *, order: bool = True) -> list[dict[str, Any]]:
        records = [dict(r) for r in self._records[entity].values()]
        if order:
            records.sort(key=lambda r: str(r.get("created", "")))
        return records

    def update(self, entity: str, key: str, patch: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """部分更新（patch 浅合并；id/created 不可改），记账 op 并落盘。"""
        current = self._records[entity].get(key)
        if current is None:
            raise KeyError(key)
        forbidden = set(patch) & {"id"} if entity != "preference" else set(patch) & {"key"}
        if forbidden:
            raise ValueError(f"不可修改字段: {sorted(forbidden)}")
        updated = {**current, **patch}
        self._validate(entity, updated)
        self._put(entity, updated)
        op_id = self.record_op(f"{entity}.update", [{
            "entity": entity, "id": key, "before": current, "after": updated,
        }])
        self._notify_change(entity, key, current, updated)
        return dict(updated), op_id

    def mark_reminded(self, entity: str, key: str, occurrence: str) -> None:
        """系统标记：记录本次到点事实已投递（不记 op、不触发观察者）。

        仅用于触发器同步层的幂等判据（同一次 occurrence 不重复注册），
        不是用户状态变更，因此不进入 op_log/undo 面。
        """
        current = self._records[entity].get(key)
        if current is None:
            return
        self._put(entity, {**current, "last_reminded_for": occurrence})

    def _remove(self, entity: str, key: str) -> dict[str, Any]:
        """删除记录（undo 专用内部面，不独立记账——undo op 整体记账）。"""
        record = self._records[entity].pop(key, None)
        if record is None:
            raise KeyError(key)
        self._flush(entity)
        return record

    def _put(self, entity: str, record: dict[str, Any]) -> None:
        self._records[entity][self._key_of(entity, record)] = record
        self._flush(entity)

    def _flush(self, entity: str) -> None:
        records = list(self._records[entity].values())
        atomic_write_text(self._dir / _RECORD_FILES[entity], _dump_lines(records))

    def _validate(self, entity: str, record: dict[str, Any]) -> None:
        if entity == "todo":
            if not str(record.get("title", "")).strip():
                raise ValueError("todo.title 不得为空")
            if record.get("status", "backlog") not in TODO_STATUSES:
                raise ValueError(f"todo.status 须为 {TODO_STATUSES} 之一: {record.get('status')!r}")
            if record.get("priority", DEFAULT_PRIORITY) not in PRIORITIES:
                raise ValueError(f"todo.priority 须为 {PRIORITIES} 之一: {record.get('priority')!r}")
            if record.get("direction", "none") not in DIRECTIONS:
                raise ValueError(f"todo.direction 须为 {DIRECTIONS} 之一: {record.get('direction')!r}")
            progress = record.get("progress", 0)
            if not isinstance(progress, int) or not 0 <= progress <= 100:
                raise ValueError(f"todo.progress 须为 0-100 整数: {progress!r}")
        elif entity == "event":
            if not str(record.get("title", "")).strip():
                raise ValueError("event.title 不得为空")
            if not str(record.get("at", "")).strip():
                raise ValueError("event.at 不得为空")
            if record.get("status", "planned") not in EVENT_STATUSES and record.get("status"):
                raise ValueError(
                    f"event.status 须为 {EVENT_STATUSES} 之一: {record.get('status')!r}"
                )
        elif entity == "habit":
            if not str(record.get("title", "")).strip():
                raise ValueError("habit.title 不得为空")
            if not str(record.get("schedule", "")).strip():
                raise ValueError("habit.schedule 不得为空")
        elif entity == "preference":
            if not str(record.get("key", "")).strip():
                raise ValueError("preference.key 不得为空")

    # ── todo 状态机执行侧（op 面共用逻辑，工具与 REST 同轨）──

    def complete_todo(self, todo_id: str) -> tuple[dict[str, Any], str]:
        """完成：status=done + progress=100 + done_at + 伙伴结算（同一 op 可逆）。

        Returns:
            (完成态 todo, op_id)。
        """
        current = self._records["todo"].get(todo_id)
        if current is None:
            raise KeyError(todo_id)
        now = self._clock()
        done = {**current, "status": "done", "progress": 100, "done_at": now.isoformat()}
        self._validate("todo", done)
        partner_before, partner_after = self._settle_partner(now)
        self._put("todo", done)
        self._write_partner()
        op_id = self.record_op("todo.complete", [
            {"entity": "todo", "id": todo_id, "before": current, "after": done},
            {"entity": "partner", "id": "partner", "before": partner_before, "after": partner_after},
        ])
        self._notify_change("todo", todo_id, current, done)
        return dict(done), op_id

    def defer_todo(self, todo_id: str, new_due: str | None = None) -> tuple[dict[str, Any], str]:
        """改期：due = 显式新值或明天（clock 注入）；status 回 backlog（重进日程）。"""
        current = self._records["todo"].get(todo_id)
        if current is None:
            raise KeyError(todo_id)
        due = new_due or (self._clock().date() + timedelta(days=1)).isoformat()
        deferred = {**current, "due": due, "status": "backlog"}
        self._validate("todo", deferred)
        self._put("todo", deferred)
        op_id = self.record_op("todo.defer", [{
            "entity": "todo", "id": todo_id, "before": current, "after": deferred,
        }])
        self._notify_change("todo", todo_id, current, deferred)
        return dict(deferred), op_id

    def split_todo(self, todo_id: str, parts: list[str]) -> tuple[list[dict[str, Any]], str]:
        """拆小：parts（子项标题列表）各建新 todo（domain/due/priority 继承）。

        原 todo 不动（进度由调用方按需 PATCH）；拆出项记入同一 op（整体可逆）。
        """
        current = self._records["todo"].get(todo_id)
        if current is None:
            raise KeyError(todo_id)
        if not parts or not all(isinstance(p, str) and p.strip() for p in parts):
            raise ValueError("split.parts 须为非空标题列表")
        items: list[dict[str, Any]] = []
        created: list[dict[str, Any]] = []
        for part in parts:
            record = {
                "title": part.strip(),
                "domain": current.get("domain"),
                "due": current.get("due"),
                "priority": current.get("priority", DEFAULT_PRIORITY),
                "status": "backlog",
                "created": self._clock().isoformat(),
                "id": new_id(),
            }
            self._validate("todo", record)
            self._put("todo", record)
            items.append({"entity": "todo", "id": record["id"], "before": None, "after": record})
            created.append(dict(record))
        op_id = self.record_op("todo.split", items)
        for record in created:
            self._notify_change("todo", str(record["id"]), None, record)
        return created, op_id

    # ── event 状态机执行侧（与 todo op 同轨：工具/REST 共用）──

    def accept(self, entity: str, item_id: str) -> tuple[dict[str, Any], str]:
        """同意：todo 转 doing + preauthorized；event 转 doing（已确认）。

        同一动作面收敛旧幕僚卡「同意」语义，LLM 不再需要日程代码代劳。
        """
        current = self._records[entity].get(item_id)
        if current is None:
            raise KeyError(item_id)
        if entity == "todo":
            accepted = {**current, "status": "doing", "preauthorized": True}
        elif entity == "event":
            accepted = {**current, "status": "doing"}
        else:
            raise ValueError(f"accept 只支持 todo/event: {entity}")
        self._validate(entity, accepted)
        self._put(entity, accepted)
        op_id = self.record_op(f"{entity}.accept", [{
            "entity": entity, "id": item_id, "before": current, "after": accepted,
        }])
        self._notify_change(entity, item_id, current, accepted)
        return dict(accepted), op_id

    def complete_event(self, event_id: str) -> tuple[dict[str, Any], str]:
        """event 完成（已参加/已处理）：status=done + done_at。"""
        current = self._records["event"].get(event_id)
        if current is None:
            raise KeyError(event_id)
        done = {**current, "status": "done", "done_at": self._clock().isoformat()}
        self._validate("event", done)
        self._put("event", done)
        op_id = self.record_op("event.complete", [{
            "entity": "event", "id": event_id, "before": current, "after": done,
        }])
        self._notify_change("event", event_id, current, done)
        return dict(done), op_id

    def defer_event(self, event_id: str, new_at: str | None = None) -> tuple[dict[str, Any], str]:
        """event 改期：at 更新，status 回 planned（重新进入提醒面）。"""
        current = self._records["event"].get(event_id)
        if current is None:
            raise KeyError(event_id)
        at = new_at or self._next_day_like(str(current.get("at", "")))
        deferred = {**current, "at": at, "status": "planned"}
        self._validate("event", deferred)
        self._put("event", deferred)
        op_id = self.record_op("event.defer", [{
            "entity": "event", "id": event_id, "before": current, "after": deferred,
        }])
        self._notify_change("event", event_id, current, deferred)
        return dict(deferred), op_id

    def cancel_event(self, event_id: str) -> tuple[dict[str, Any], str]:
        """event 撤销：status=cancelled（终态，不再提醒）。"""
        current = self._records["event"].get(event_id)
        if current is None:
            raise KeyError(event_id)
        cancelled = {**current, "status": "cancelled"}
        self._validate("event", cancelled)
        self._put("event", cancelled)
        op_id = self.record_op("event.cancel", [{
            "entity": "event", "id": event_id, "before": current, "after": cancelled,
        }])
        self._notify_change("event", event_id, current, cancelled)
        return dict(cancelled), op_id

    def _next_day_like(self, value: str) -> str:
        """按原字段粒度取「+1 天」建议时刻（无法解析 = 明天此刻）。"""
        try:
            base = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            base = self._clock()
        if "T" in value:
            return (base + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        return (base + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

    def unhandled_events(self) -> list[dict[str, Any]]:
        """已过时刻且仍活跃的 event（无管道可注入时的「未决」兜底读面）。"""
        now = self._clock()
        out: list[dict[str, Any]] = []
        for event in self.list_records("event"):
            if str(event.get("status") or "") not in EVENT_ACTIVE_STATUSES:
                continue
            try:
                at = datetime.fromisoformat(str(event.get("at", "")))
            except (TypeError, ValueError):
                continue
            if at <= now:
                out.append(event)
        return out

    # ── 番茄钟（调度器计时面：focus_started_at 由 start/stop/complete 三口独占变更）──

    def start_focus(
        self, todo_id: str, duration_minutes: int | None = None
    ) -> tuple[dict[str, Any], str]:
        """番茄启动：status=doing + focus_started_at + 本次时长（记账 op 可逆）。

        时长口径：显式 duration_minutes（LLM/用户单次覆盖）优先；缺省取插件
        配置注入的默认值（配置默认 25，store 由 on_load/on_config_changed 更新）。

        Raises:
            KeyError: todo 不存在；ValueError: 时长非法。
        """
        current = self._records["todo"].get(todo_id)
        if current is None:
            raise KeyError(todo_id)
        minutes = self._focus_default_minutes if duration_minutes is None else duration_minutes
        if not isinstance(minutes, int) or not 1 <= minutes <= 1440:
            raise ValueError(f"focus 时长须为 1-1440 整数分钟: {minutes!r}")
        started = {
            **current,
            "status": "doing",
            "focus_started_at": self._clock().isoformat(),
            "focus_duration_minutes": minutes,
        }
        self._validate("todo", started)
        self._put("todo", started)
        op_id = self.record_op("todo.focus_start", [{
            "entity": "todo", "id": todo_id, "before": current, "after": started,
        }])
        self._notify_change("todo", todo_id, current, started)
        return dict(started), op_id

    def set_focus_default_minutes(self, minutes: Any) -> None:
        """更新番茄默认时长（配置注入面；非法值保持原值）。"""
        try:
            value = int(minutes)
        except (TypeError, ValueError):
            return
        if 1 <= value <= 1440:
            self._focus_default_minutes = value

    def focus_default_minutes(self) -> int:
        """当前番茄默认时长（配置值或出厂默认 25）。"""
        return self._focus_default_minutes

    def stop_focus(self, todo_id: str) -> tuple[dict[str, Any], str]:
        """番茄手动停止：清 focus_started_at（不落树——计时未满零落账）。

        Raises:
            KeyError: todo 不存在；ValueError: 无进行中的番茄。
        """
        current = self._records["todo"].get(todo_id)
        if current is None:
            raise KeyError(todo_id)
        if not current.get("focus_started_at"):
            raise ValueError(f"todo 无进行中的番茄: {todo_id}")
        stopped = {**current, "focus_started_at": None}
        self._validate("todo", stopped)
        self._put("todo", stopped)
        op_id = self.record_op("todo.focus_stop", [{
            "entity": "todo", "id": todo_id, "before": current, "after": stopped,
        }])
        self._notify_change("todo", todo_id, current, stopped)
        return dict(stopped), op_id

    def record_focus_complete(self, todo_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
        """番茄计时到自动落账（0 决策）：1 番茄=1 树 + 清计时标记，同一 op 可逆。

        Returns:
            (落账态 todo, 结算后 partner, op_id)。

        Raises:
            KeyError: todo 不存在；ValueError: 无进行中的番茄。
        """
        current = self._records["todo"].get(todo_id)
        if current is None:
            raise KeyError(todo_id)
        if not current.get("focus_started_at"):
            raise ValueError(f"todo 无进行中的番茄: {todo_id}")
        settled = {**current, "focus_started_at": None}
        self._validate("todo", settled)
        before = dict(self._partner)
        partner_after = {**self._partner, "trees": int(self._partner.get("trees", 0)) + 1}
        self._partner = partner_after
        self._put("todo", settled)
        self._write_partner()
        op_id = self.record_op("todo.focus_complete", [
            {"entity": "todo", "id": todo_id, "before": current, "after": settled},
            {"entity": "partner", "id": "partner", "before": before, "after": partner_after},
        ])
        self._notify_change("todo", todo_id, current, settled)
        return dict(settled), dict(partner_after), op_id

    # ── 伙伴结算 ──

    def _settle_partner(self, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
        """complete 结算：xp 累加、level 派生、streak 按完成日连续性推进。

        Returns:
            (结算前快照, 结算后快照)——同一 op 的可逆 items 两翼。
        """
        before = dict(self._partner)
        today = now.date().isoformat()
        last = str(self._partner.get("last_done_date", "") or "")
        xp = int(self._partner.get("xp", 0)) + COMPLETE_XP
        streak = int(self._partner.get("streak", 0))
        if last != today:
            streak = streak + 1 if last == (now.date() - timedelta(days=1)).isoformat() else 1
        self._partner = {
            **self._partner,
            "xp": xp,
            "level": xp // 100 + 1,
            "streak": streak,
            "last_done_date": today,
        }
        return before, dict(self._partner)

    def get_partner(self) -> dict[str, Any]:
        return dict(self._partner)

    def now(self) -> datetime:
        """当前业务时刻（clock 注入面；capture 行解析等确定性消费）。"""
        return self._clock()

    def _write_partner(self) -> None:
        atomic_write_text(self._dir / "partner.json", json.dumps(self._partner, ensure_ascii=False))

    # ── journal / preference / habit 打卡 ──

    def upsert_journal(self, journal_date: str, fields: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """journal 按日 upsert（key = date；整文件按月重写）。"""
        if not journal_date.strip():
            raise ValueError("journal.date 不得为空")
        before = self._journal.get(journal_date)
        record = {**(before or {}), **fields, "date": journal_date}
        self._journal[journal_date] = record
        month_file = self._dir / f"journal_{journal_date[:7].replace('-', '')}.jsonl"
        month_records = [r for d, r in self._journal.items() if d.startswith(journal_date[:7])]
        atomic_write_text(month_file, _dump_lines(month_records))
        op_id = self.record_op("journal.upsert", [{
            "entity": "journal", "id": journal_date, "before": before, "after": record,
        }])
        return dict(record), op_id

    def list_journal(self, month: str | None = None) -> list[dict[str, Any]]:
        dates = sorted(self._journal)
        if month:
            dates = [d for d in dates if d.startswith(month)]
        return [dict(self._journal[d]) for d in dates]

    def set_preference(self, key: str, value: Any) -> tuple[dict[str, Any], str]:
        """『同类免问』数据面：key(domain+动作) → value(auto_approve)，upsert。"""
        before = self._records["preference"].get(key)
        record = {"key": key, "value": value, "ts": self._clock().isoformat()}
        self._put("preference", record)
        op_id = self.record_op("preference.set", [{
            "entity": "preference", "id": key, "before": before, "after": record,
        }])
        return dict(record), op_id

    def checkin_habit(self, habit_id: str) -> tuple[dict[str, Any], str | None]:
        """habit 打卡：history 追加今天（幂等——同日重复打卡零变化不记账）。"""
        current = self._records["habit"].get(habit_id)
        if current is None:
            raise KeyError(habit_id)
        today = self._clock().date().isoformat()
        history = list(current.get("history") or [])
        if today in history:
            return dict(current), None
        updated = {**current, "history": [*history, today]}
        self._put("habit", updated)
        op_id = self.record_op("habit.checkin", [{
            "entity": "habit", "id": habit_id, "before": current, "after": updated,
        }])
        return dict(updated), op_id

    # ── op_log（append-only 环形 50）与撤销 ──

    def record_op(self, kind: str, items: list[dict[str, Any]]) -> str:
        """落操作账：环形截尾 + 落盘；返回 op_id（回执携带 → 可撤销）。"""
        op_id = new_id()
        self._ops.append({
            "op_id": op_id,
            "ts": self._clock().isoformat(),
            "kind": kind,
            "items": items,
        })
        if len(self._ops) > OP_LOG_KEEP:
            self._ops = self._ops[-OP_LOG_KEEP:]
        atomic_write_text(self._dir / "oplog.jsonl", _dump_lines(self._ops))
        return op_id

    def list_ops(self, limit: int = 20) -> list[dict[str, Any]]:
        """读尾（缺省 20 条，undo 过的带 undone 标记）。"""
        undone = self._undone_op_ids()
        return [
            {**op, "undone": op["op_id"] in undone}
            for op in self._ops[-limit:]
        ]

    def get_op(self, op_id: str) -> dict[str, Any] | None:
        for op in self._ops:
            if op["op_id"] == op_id:
                return dict(op)
        return None

    def undo(self, op_id: str) -> dict[str, Any]:
        """按 items 逆序回滚（before=None 撤创建=删除）；重复撤销拒绝。

        undo 本身追加 ``undo`` 型 op（append-only 审计面不改史），undone 判定
        派生自 oplog 扫描（重启后仍然成立）。
        """
        op = self.get_op(op_id)
        if op is None:
            raise KeyError(op_id)
        if op_id in self._undone_op_ids():
            raise ValueError(f"op 已撤销: {op_id}")
        for item in reversed(op.get("items") or []):
            entity, key = str(item["entity"]), str(item["id"])
            before = item.get("before")
            if before is None:
                if entity == "journal":
                    self._journal.pop(key, None)
                    self._flush_journal_month(key)
                elif entity == "partner":
                    self._partner = self._default_partner()
                    self._write_partner()
                else:
                    self._remove(entity, key)
            elif entity == "journal":
                self._journal[key] = dict(before)
                self._flush_journal_month(key)
            elif entity == "partner":
                self._partner = dict(before)
                self._write_partner()
            else:
                self._put(entity, dict(before))
        undo_op_id = self.record_op("undo", [{
            "entity": "op", "id": op_id, "before": op, "after": None,
        }])
        for item in op.get("items") or []:
            entity, key = str(item["entity"]), str(item["id"])
            if entity in ("journal", "partner", "op"):
                continue
            self._notify_change(entity, key, None, self.get(entity, key))
        return {"undo_of": op_id, "op_id": undo_op_id}

    def _undone_op_ids(self) -> set[str]:
        return {
            str(item["id"])
            for op in self._ops if op.get("kind") == "undo"
            for item in (op.get("items") or [])
            if isinstance(item, dict) and item.get("entity") == "op"
        }

    def _flush_journal_month(self, journal_date: str) -> None:
        month = journal_date[:7]
        month_records = [r for d, r in self._journal.items() if d.startswith(month)]
        atomic_write_text(
            self._dir / f"journal_{month.replace('-', '')}.jsonl",
            _dump_lines(month_records),
        )

    # ── 聚合读面（GET /today 与工具 query scope=today 单源）──

    def today(self) -> dict[str, Any]:
        """今日三栏聚合：逾期/今日/进行中 todo + 今日事件 + 今日到期习惯 +
        今日 journal + partner + op 尾（晨报数据源）。"""
        now = self._clock()
        today_str = now.date().isoformat()
        todos = self.list_records("todo")
        active = [t for t in todos if t.get("status") in ("backlog", "today", "doing")]
        overdue = [t for t in active if str(t.get("due", "")) and str(t["due"])[:10] < today_str]
        due_today = [t for t in active if str(t.get("due", ""))[:10] == today_str]
        events_today = [
            e for e in self.list_records("event") if str(e.get("at", ""))[:10] == today_str
        ]
        habits = [
            {**h, "streak": habit_streak(h, now.date()),
             "due_today": _habit_due_today(str(h.get("schedule", "") or ""), now.date())}
            for h in self.list_records("habit")
        ]
        return {
            "date": today_str,
            "todos": {"overdue": overdue, "today": due_today, "doing": [
                t for t in active if t.get("status") == "doing"
            ]},
            "backlog_count": sum(1 for t in todos if t.get("status") == "backlog"),
            "events": events_today,
            "events_overdue": self.unhandled_events(),
            "habits": habits,
            "journal": self._journal.get(today_str),
            "partner": self.get_partner(),
            "op_log": self.list_ops(),
        }


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _dump_lines(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


def _loads(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("[pim_store] 损坏行跳过（不拖垮其余数据）| line=%r", line[:80])
        return None
    return record if isinstance(record, dict) else None
