"""审查修复批回归（P0 MF）：存量回填/周期断链/取值域/ack 记账/聚合口径/治理接线。"""
# @feature: FP-0.2.二 mode_personal 个人助理模式包 M0 | @ci: python-coverage
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
for _p in (str(_PKG), str(_PKG.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pim_governance import govern  # noqa: E402
from pim_recurrence import last_occurrence, next_occurrence  # noqa: E402
from pim_store import PimStore  # noqa: E402
from pim_trigger_sync import PimTriggerSync, _first_scheduled_slot  # noqa: E402

_NOW = datetime(2026, 10, 13, 10, 0)


class FakeClock:
    def __init__(self, start: str) -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance_days(self, n: int) -> None:
        from datetime import timedelta

        self.now += timedelta(days=n)

    def advance_to(self, iso: str) -> None:
        self.now = datetime.fromisoformat(iso)


def _store(tmp_path):
    return PimStore(tmp_path, clock=FakeClock("2026-10-13T08:00"))


def test_invalid_weekly_returns_none():
    assert next_occurrence("weekly:7", "2026-10-13 09:00", _NOW) is None
    assert next_occurrence("weekly:99", "2026-10-13 09:00", _NOW) is None
    assert last_occurrence("weekly:7", "2026-10-13 09:00", _NOW) is None


def test_invalid_monthly_returns_none():
    assert next_occurrence("monthly:0", "2026-10-13 09:00", _NOW) is None
    assert next_occurrence("monthly:32", "2026-10-13 09:00", _NOW) is None
    assert last_occurrence("monthly:0", "2026-10-13 09:00", _NOW) is None


def test_slot_monthly_advances_and_invalid_none():
    base = datetime(2026, 10, 20, 9, 0)
    slot = _first_scheduled_slot("monthly:5", base)
    assert slot is not None
    assert slot.day == 5
    assert slot > base
    assert _first_scheduled_slot("weekly:9", base) is None
    assert _first_scheduled_slot("monthly:0", base) is None


def test_store_rejects_invalid_repeat(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="repeat 须为"):
        s.create("todo", {"title": "x", "repeat": "weekly:99"})
    with pytest.raises(ValueError, match="repeat 须为"):
        s.create("todo", {"title": "x", "repeat": "monthly:0"})


def test_store_accepts_valid_repeat(tmp_path):
    s = _store(tmp_path)
    r, _ = s.create("todo", {"title": "x", "repeat": "weekly:2", "due": "2026-10-14 09:00"})
    assert r["repeat"] == "weekly:2"


def test_event_reminder_mode_validated(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="reminder_mode 须为"):
        s.create("event", {"title": "e", "at": "2026-10-14 10:00", "reminder_mode": "yolo"})


def test_legacy_card_backfills_agent_classified(tmp_path):
    d = tmp_path / "pim"
    d.mkdir()
    (d / "todos.jsonl").write_text(
        '{"id": "abc123", "title": "旧卡", "status": "backlog", "repeat": "daily", "created": "2026-10-01T08:00"}\n',
        encoding="utf-8",
    )
    s = PimStore(d, clock=FakeClock("2026-10-13T10:00"))
    assert s.get("todo", "abc123")["reminder_mode"] == "agent_classified"


def test_fired_synthetic_occurrence_chains(tmp_path):
    s = _store(tmp_path)

    async def _noop(method, params):
        return {"items": []}

    r, _ = s.create("todo", {"title": "晨卡", "repeat": "daily"})  # 无 due：合成路径
    sync = PimTriggerSync(s, _noop, clock=s._clock)
    asyncio.run(
        sync._on_trigger_fired({
            "trigger_source": "mode_personal",
            "entity": "todo",
            "item_id": r["id"],
            "occurrence": "2026-10-13 09:00",
        })
    )
    assert s.get("todo", r["id"])["due"] == "2026-10-14 09:00"  # 序列落盘，周期不再断链


def test_ack_records_op_and_idempotent(tmp_path):
    s = _store(tmp_path)
    r, _ = s.create("todo", {"title": "x", "due": "2026-10-13 09:00"})
    s._clock.advance_to("2026-10-13T10:00")  # 已到点 ack 才有坐标可记（R2 到点语义）
    before = len(s.list_ops())
    s.ack_reminder("todo", r["id"])
    assert len(s.list_ops()) == before + 1
    assert any("ack_reminder" in str(v) for v in s.list_ops()[-1].values())
    s.ack_reminder("todo", r["id"])  # 同坐标幂等：零变化不记账
    assert len(s.list_ops()) == before + 1


def test_today_reminders_and_overdue_exempt(tmp_path):
    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    s.create("todo", {"title": "直弹", "due": "2026-10-13 09:00"})
    s.create("todo", {"title": "旧重复", "due": "2026-10-11 09:00", "repeat": "daily"})
    t = s.today()
    assert [x["title"] for x in t["reminders"]] == ["直弹", "旧重复"]
    assert all(not str(x.get("repeat") or "") for x in t["todos"]["overdue"])


def test_none_mode_normalized_and_patch_null_rejected(tmp_path):
    s = _store(tmp_path)
    r, _ = s.create("todo", {"title": "x", "reminder_mode": None})
    assert r["reminder_mode"] == "direct"  # None 同缺省归 direct
    s.update("todo", r["id"], {"reminder_mode": None})  # PATCH null 归一为 direct
    assert s.get("todo", r["id"])["reminder_mode"] == "direct"


def test_startup_governance_runs(tmp_path):
    import server as server_mod

    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    gov = server_mod._run_startup_governance(s)
    assert "due_filled" in gov
    assert "legacy_removed" in gov


# ── 第2轮审查修复（R2/R4/R6/R7 + diff-coverage 缺口）──


def test_reminder_future_card_not_popped(tmp_path):
    """R2：未到点不弹、提前 ack 无坐标可写（真到点仍会提醒）。"""
    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    r, _ = s.create("todo", {"title": "晚卡", "due": "2026-10-13 18:00"})
    assert s.today()["reminders"] == []
    s.ack_reminder("todo", r["id"])
    assert not s.get("todo", r["id"]).get("reminder_ack_for")
    s._clock.advance_to("2026-10-13T19:00")
    assert [x["id"] for x in s.today()["reminders"]] == [r["id"]]
    s.ack_reminder("todo", r["id"])
    assert s.today()["reminders"] == []


def test_future_repeat_occurrence_not_effective(tmp_path):
    """R2（repeat 面）：到点坐标只取 <=now 最近一次，未来发生不算。"""
    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    r, _ = s.create("todo", {"title": "w", "due": "2026-10-20 09:00", "repeat": "weekly:2"})
    assert s.effective_occurrence(s.get("todo", r["id"]))["_occurrence_effective"] == ""


def test_null_mode_loaded_card_keeps_agent_classified(tmp_path):
    """R4：存量落盘 null → 回填 agent_classified（不落 direct、不注销触发器）。"""
    d = tmp_path / "pim"
    d.mkdir()
    (d / "todos.jsonl").write_text(
        '{"id": "null001", "title": "旧卡", "status": "backlog", "reminder_mode": null, "created": "2026-10-01T08:00"}\n',
        encoding="utf-8",
    )
    s = PimStore(d, clock=FakeClock("2026-10-13T10:00"))
    assert s.get("todo", "null001")["reminder_mode"] == "agent_classified"


def test_validate_normalizes_missing_or_null_mode(tmp_path):
    """R4 补充（_validate 归一）：缺省/None → direct；非法值仍拒绝。"""
    s = _store(tmp_path)
    r, _ = s.create("todo", {"title": "x"})
    assert r["reminder_mode"] == "direct"
    s.update("todo", r["id"], {"reminder_mode": None})
    assert s.get("todo", r["id"])["reminder_mode"] == "direct"
    with pytest.raises(ValueError, match="reminder_mode 须为"):
        s.update("todo", r["id"], {"reminder_mode": "yolo"})


def test_doing_card_active_and_remindable(tmp_path):
    """R6：doing 活跃口径单源——doing direct 卡可弹，触发器侧判定同样活跃。"""
    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    r, _ = s.create("todo", {"title": "d", "due": "2026-10-13 09:00", "status": "doing"})
    assert [x["id"] for x in s.today()["reminders"]] == [r["id"]]
    assert PimTriggerSync._is_active("todo", s.get("todo", r["id"])) is True


def test_govern_survives_invalid_repeat_card(tmp_path):
    """R7：非法存量 repeat 卡不中断启动治理（逐卡容错，表内其余卡照补）。"""
    d = tmp_path / "pim"
    d.mkdir()
    (d / "todos.jsonl").write_text(
        '{"id": "bad00001", "title": "运动", "status": "backlog", "repeat": "weekly:99", "created": "2026-10-01T08:00"}\n'
        '{"id": "good0001", "title": "保健品", "status": "backlog", "created": "2026-10-01T08:00"}\n',
        encoding="utf-8",
    )
    s = PimStore(d, clock=FakeClock("2026-10-13T10:00"))
    gov = govern(s)
    assert [x["id"] for x in gov["due_filled"]] == ["good0001"]


def test_on_load_wires_governance_focus_sync(tmp_path):
    """R3 覆盖缺口：_on_load 接线（get_store/set_focus_default_minutes/govern）。"""
    import server as server_mod

    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    server_mod.set_store(s)
    try:
        asyncio.run(server_mod._on_load({}))
        assert s._focus_default_minutes == 25
        assert "reminders" in s.today()
    finally:
        asyncio.run(server_mod._on_unload({}))
        server_mod.set_store(None)


def test_agent_classified_card_not_in_reminders(tmp_path):
    """R3 覆盖缺口（_pending_reminders 过滤分支）：agent_classified 卡不进弹卡聚合。"""
    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    s.create("todo", {"title": "a", "due": "2026-10-13 09:00", "reminder_mode": "agent_classified"})
    assert s.today()["reminders"] == []


def test_effective_occurrence_empty_time_fields(tmp_path):
    """R2 补充（diff-coverage 缺行）：due/at 皆空的卡无有效坐标（early return 分支）。"""
    s = _store(tmp_path)
    r, _ = s.create("todo", {"title": "n"})
    rec = s.get("todo", r["id"])
    assert not rec.get("due")
    assert not rec.get("at")
    assert s.effective_occurrence(rec)["_occurrence_effective"] == ""


def test_on_load_survives_governance_failure(tmp_path, monkeypatch):
    """M2（终审）：治理抛非 ValueError 异常不得打断 on_load——同步层/计时器仍启动。"""
    import server as server_mod

    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    server_mod.set_store(s)

    def _boom(store):
        raise RuntimeError("模拟治理期 IO 故障")

    monkeypatch.setattr(server_mod, "_run_startup_governance", _boom)
    try:
        asyncio.run(server_mod._on_load({}))
        assert server_mod._trigger_sync is not None
        assert server_mod._focus_timer is not None
    finally:
        asyncio.run(server_mod._on_unload({}))
        server_mod.set_store(None)


def test_governance_title_normalized_match(tmp_path):
    """GAP-1（验证报告实测）：真实卡标题带 emoji 前缀/空格/长描述，治理表精确匹配命中 0/3
    → 归一化（去 emoji 等符号前缀 + 去空白）后子串匹配应全命中。"""
    d = tmp_path / "pim"
    d.mkdir()
    (d / "todos.jsonl").write_text(
        '{"id": "real00001", "title": "💼 本职工作：今日任务盘点与处理（固定日程细节待补充）", "status": "backlog", "repeat": "daily", "created": "2026-10-01T08:00"}\n'
        '{"id": "real00002", "title": "🌙 作息：23:50 前入睡", "status": "backlog", "repeat": "daily", "created": "2026-10-01T08:00"}\n'
        '{"id": "real00003", "title": ".contact Boss打招呼", "status": "backlog", "repeat": "daily", "created": "2026-10-01T08:00"}\n'
        '{"id": "real00004", "title": "临时杂事（不在表内）", "status": "backlog", "repeat": "daily", "created": "2026-10-01T08:00"}\n',
        encoding="utf-8",
    )
    s = PimStore(d, clock=FakeClock("2026-10-13T09:00"))
    gov = govern(s)
    dues = {x["id"]: x["due"] for x in gov["due_filled"]}
    assert dues.get("real00001") == "2026-10-14 08:30"  # 本职工作 08:30 已过(09:00) → 次日（AC-4 已过取次日）
    assert dues.get("real00002") == "2026-10-13 23:50"
    assert dues.get("real00003") == "2026-10-13 10:00"  # Boss打招呼 10:00 未到 → 当日
    assert "real00004" not in dues  # 表外不越界


def test_direct_repeat_without_due_still_reminds(tmp_path):
    """GAP-2（验证报告实测）：direct repeat 无 due 卡两条提醒路径均不触达（永不提醒）
    → 弹卡路径补到点合成：无 due/at 的 repeat 卡按合成起点（当日 09:00）折叠到
    <=now 最近一次发生作判定坐标。"""
    s = PimStore(tmp_path, clock=FakeClock("2026-10-13T10:00"))
    r, _ = s.create("todo", {"title": "每日站会", "repeat": "daily"})  # 缺省 direct、无 due
    rec = s.get("todo", r["id"])
    assert rec["reminder_mode"] == "direct"
    assert not rec.get("due")
    # daily：当日 09:00 起点已过 → 最近一次发生=当日 09:00（会弹）
    assert s.effective_occurrence(rec)["_occurrence_effective"] != ""
    t = s.today()
    assert [x["id"] for x in t["reminders"]] == [r["id"]]
    # weekly 合成：起点=当日 09:00（序列开端），首个匹配日到点前无有效坐标（不弹）；
    # 拨钟过首个匹配日 09:00 后，最近一次发生=该日 09:00（弹卡）。
    s2 = PimStore(tmp_path / "w", clock=FakeClock("2026-10-13T10:00"))
    r2, _ = s2.create("todo", {"title": "周会", "repeat": "weekly:2"})
    rec2 = s2.get("todo", r2["id"])
    first_slot = _first_scheduled_slot("weekly:2", datetime(2026, 10, 13, 9, 0))
    assert first_slot is not None
    assert s2.effective_occurrence(rec2)["_occurrence_effective"] == ""  # 未到点不弹
    s2._clock.now = first_slot.replace(hour=9, minute=1)
    assert s2.effective_occurrence(s2.get("todo", r2["id"]))["_occurrence_effective"] == first_slot.strftime("%Y-%m-%d %H:%M")
