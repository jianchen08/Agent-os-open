# @feature: FP-0.2.二 mode_personal 个人助理模式包 M0 | @ci: python-coverage
"""pim 实体域存储单测：CRUD / undo 幂等 / 伙伴结算 / habit 派生 / op 环形。

关键路径走真实依赖：tmp_path 真实文件读写（JSONL 落盘/重载），零 mock 文件面；
时钟为外部依赖，注入 fake clock（测试纪律：时序类外部依赖允许注入）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parents[1]  # plugins/shared/


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


pim_store = _load("pim_store", "mode_personal_test_pim_store")


class FakeClock:
    """可拨动 fake clock（时序测试注入）。"""

    def __init__(self, start: str = "2026-10-12T09:00:00") -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance_days(self, n: int) -> None:
        from datetime import timedelta

        self.now += timedelta(days=n)

    def advance_minutes(self, n: int) -> None:
        from datetime import timedelta

        self.now += timedelta(minutes=n)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    return pim_store.PimStore(tmp_path / "data", clock=FakeClock())


class TestTodoCrud:
    def test_create_carries_full_shape(self, store: Any) -> None:
        todo, op_id = store.create("todo", {"title": "交周报", "domain": "工作"})
        assert todo["status"] == "backlog"
        assert todo["progress"] == 0
        assert todo["priority"] == "P2"
        assert todo["direction"] == "none"
        assert todo["created"] == "2026-10-12T09:00:00"
        assert op_id

    def test_explicit_fields_win_over_defaults(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "急", "priority": "P0", "status": "today"})
        assert todo["priority"] == "P0"
        assert todo["status"] == "today"

    def test_update_patch_merges(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "A", "domain": "工作"})
        updated, _ = store.update("todo", todo["id"], {"progress": 40})
        assert updated["progress"] == 40
        assert updated["domain"] == "工作"  # 未触碰字段原样

    def test_update_rejects_protected_fields(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "A"})
        with pytest.raises(ValueError, match="不可修改字段"):
            store.update("todo", todo["id"], {"id": "forged"})

    def test_get_missing_is_none_and_update_raises(self, store: Any) -> None:
        assert store.get("todo", "nope") is None
        with pytest.raises(KeyError):
            store.update("todo", "nope", {"progress": 1})

    @pytest.mark.parametrize("patch", [
        {"status": "bogus"},
        {"priority": "P9"},
        {"progress": 101},
        {"progress": -1},
        {"title": ""},
    ])
    def test_enum_and_range_validation(self, store: Any, patch: dict) -> None:
        todo, _ = store.create("todo", {"title": "A"})
        with pytest.raises(ValueError):
            store.update("todo", todo["id"], patch)

    def test_persistence_roundtrip(self, tmp_path: Path) -> None:
        """落盘-重载一致性（真实文件依赖，非内存假象）。"""
        data_dir = tmp_path / "data"
        store = pim_store.PimStore(data_dir, clock=FakeClock())
        todo, op_id = store.create("todo", {"title": "持久", "due": "2026-10-13"})
        reloaded = pim_store.PimStore(data_dir)
        assert reloaded.get("todo", todo["id"])["title"] == "持久"
        assert reloaded.list_ops(50)[-1]["op_id"] == op_id


class TestEventsAndHabits:
    def test_event_requires_at(self, store: Any) -> None:
        with pytest.raises(ValueError, match="at"):
            store.create("event", {"title": "例会"})
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        assert event["at"] == "2026-10-13 10:00"

    def test_habit_checkin_idempotent_same_day(self, store: Any) -> None:
        habit, _ = store.create("habit", {"title": "跑步", "schedule": "每天"})
        once, op1 = store.checkin_habit(habit["id"])
        twice, op2 = store.checkin_habit(habit["id"])
        assert once["history"] == ["2026-10-12"]
        assert twice["history"] == once["history"]
        assert op2 is None  # 同日重复打卡零变化不记账

    def test_habit_streak_derived(self) -> None:
        """streak 派生：今天已打卡从今天倒数，未打卡从昨天倒数（连击不断）。"""
        from datetime import date as _date

        checked_today = {"history": ["2026-10-10", "2026-10-11", "2026-10-12"]}
        assert pim_store.habit_streak(checked_today, _date(2026, 10, 12)) == 3
        not_yet_today = {"history": ["2026-10-10", "2026-10-11"]}
        assert pim_store.habit_streak(not_yet_today, _date(2026, 10, 12)) == 2  # 今天还有机会
        broken = {"history": ["2026-10-09", "2026-10-11"]}
        assert pim_store.habit_streak(broken, _date(2026, 10, 12)) == 1  # 断档只数最近段
        assert pim_store.habit_streak({"history": []}, _date(2026, 10, 12)) == 0


class TestPartnerSettle:
    def test_complete_settles_partner(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "A"})
        done, _ = store.complete_todo(todo["id"])
        assert done["status"] == "done"
        assert done["progress"] == 100
        assert "done_at" in done
        partner = store.get_partner()
        assert partner["xp"] == 10
        assert partner["streak"] == 1
        assert partner["level"] == 1

    def test_level_derives_from_xp(self, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """level = xp//100+1（性质断言：xp 跨档 level 升档）。"""
        todo, _ = store.create("todo", {"title": "A"})
        monkeypatch.setattr(pim_store, "COMPLETE_XP", 100)
        done, _ = store.complete_todo(todo["id"])
        assert store.get_partner()["xp"] == 100
        assert store.get_partner()["level"] == 2

    def test_streak_continues_next_day_breaks_after_gap(self, tmp_path: Path) -> None:
        clock = FakeClock()
        store = pim_store.PimStore(tmp_path / "data", clock=clock)
        todo, _ = store.create("todo", {"title": "A"})
        store.complete_todo(todo["id"])
        clock.advance_days(1)
        todo2, _ = store.create("todo", {"title": "B"})
        store.complete_todo(todo2["id"])
        assert store.get_partner()["streak"] == 2  # 昨天完成过 → 连续
        clock.advance_days(3)
        todo3, _ = store.create("todo", {"title": "C"})
        store.complete_todo(todo3["id"])
        assert store.get_partner()["streak"] == 1  # 断档 → 重新计数

    def test_same_day_repeat_complete_keeps_streak(self, store: Any) -> None:
        for title in ("A", "B"):
            todo, _ = store.create("todo", {"title": title})
            store.complete_todo(todo["id"])
        assert store.get_partner()["streak"] == 1  # 同日多完成不重复计连击
        assert store.get_partner()["xp"] == 20


class TestFocusTimer:
    """番茄计时面（调度器结算的数据面）：start/stop/complete 三口独占变更。"""

    def test_start_focus_sets_doing_and_marker(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        started, op_id = store.start_focus(todo["id"])
        assert started["status"] == "doing"
        assert started["focus_started_at"] == "2026-10-12T09:00:00"
        assert op_id

    def test_stop_focus_clears_marker(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        store.start_focus(todo["id"])
        stopped, _ = store.stop_focus(todo["id"])
        assert stopped["focus_started_at"] is None
        assert stopped["status"] == "doing"  # 停止不改状态，落账由人决定

    def test_stop_without_active_focus_rejected(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "闲事"})
        with pytest.raises(ValueError, match="无进行中的番茄"):
            store.stop_focus(todo["id"])

    def test_focus_complete_adds_tree_and_clears_marker(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        store.start_focus(todo["id"])
        settled, partner, op_id = store.record_focus_complete(todo["id"])
        assert settled["focus_started_at"] is None
        assert partner["trees"] == 1
        assert op_id

    def test_focus_complete_requires_active_focus(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "闲事"})
        with pytest.raises(ValueError, match="无进行中的番茄"):
            store.record_focus_complete(todo["id"])

    def test_focus_complete_undo_reversible(self, tmp_path: Path) -> None:
        """同一 op 两翼可逆：undo 后 todo 标记回滚、树数退回。"""
        clock = FakeClock()
        store = pim_store.PimStore(tmp_path / "data", clock=clock)
        todo, _ = store.create("todo", {"title": "专注"})
        store.start_focus(todo["id"])
        _settled, _partner, op_id = store.record_focus_complete(todo["id"])
        store.undo(op_id)
        restored = store.get("todo", todo["id"])
        assert restored["focus_started_at"] == "2026-10-12T09:00:00"
        assert store.get_partner()["trees"] == 0

    def test_focus_missing_todo_raises(self, store: Any) -> None:
        with pytest.raises(KeyError):
            store.start_focus("nope")
        with pytest.raises(KeyError):
            store.stop_focus("nope")
        with pytest.raises(KeyError):
            store.record_focus_complete("nope")


class TestJournalAndPreference:
    def test_journal_upsert_by_date(self, store: Any) -> None:
        store.upsert_journal("2026-10-12", {"highlight": "A"})
        store.upsert_journal("2026-10-12", {"mood": "good"})
        entry = store.list_journal()[0]
        assert entry["highlight"] == "A"
        assert entry["mood"] == "good"  # upsert 保留既有键

    def test_preference_upsert_by_key(self, store: Any) -> None:
        store.set_preference("工作:转交", True)
        store.set_preference("工作:转交", False)
        assert store.get("preference", "工作:转交")["value"] is False


class TestUndo:
    def test_undo_create_removes_record(self, store: Any) -> None:
        todo, op_id = store.create("todo", {"title": "A"})
        store.undo(op_id)
        assert store.get("todo", todo["id"]) is None

    def test_undo_update_restores_before(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "A", "progress": 0})
        updated, op_id = store.update("todo", todo["id"], {"progress": 50})
        store.undo(op_id)
        assert store.get("todo", todo["id"])["progress"] == 0

    def test_undo_complete_restores_todo_and_partner(self, store: Any) -> None:
        """complete 双翼（todo+partner）一次 op 整体可逆。"""
        todo, _ = store.create("todo", {"title": "A"})
        _, op_id = store.complete_todo(todo["id"])
        store.undo(op_id)
        restored = store.get("todo", todo["id"])
        assert restored["status"] == "backlog"
        assert "done_at" not in restored
        partner = store.get_partner()
        assert partner["xp"] == 0
        assert partner["streak"] == 0

    def test_undo_split_removes_all_parts(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "A"})
        created, op_id = store.split_todo(todo["id"], ["a", "b"])
        store.undo(op_id)
        assert all(store.get("todo", c["id"]) is None for c in created)
        assert store.get("todo", todo["id"]) is not None  # 原 todo 不动

    def test_double_undo_rejected(self, store: Any) -> None:
        todo, op_id = store.create("todo", {"title": "A"})
        store.undo(op_id)
        with pytest.raises(ValueError, match="已撤销"):
            store.undo(op_id)

    def test_undo_unknown_op_raises(self, store: Any) -> None:
        with pytest.raises(KeyError):
            store.undo("nope")

    def test_undo_survives_reload(self, tmp_path: Path) -> None:
        """undo 判定派生自 oplog 扫描：重启后重复撤销仍被拒（审计面持久）。"""
        data_dir = tmp_path / "data"
        store = pim_store.PimStore(data_dir, clock=FakeClock())
        todo, op_id = store.create("todo", {"title": "A"})
        store.undo(op_id)
        reloaded = pim_store.PimStore(data_dir)
        with pytest.raises(ValueError, match="已撤销"):
            reloaded.undo(op_id)


class TestOpLogRing:
    def test_ring_keeps_last_50(self, store: Any, tmp_path: Path) -> None:
        todo, _ = store.create("todo", {"title": "A"})
        for i in range(60):
            store.update("todo", todo["id"], {"progress": i % 101})
        ops = store.list_ops(1000)
        assert len(ops) == 50
        # 落盘文件同步截尾（重载后仍 50）
        reloaded = pim_store.PimStore(tmp_path / "data")
        assert len(reloaded.list_ops(1000)) == 50

    def test_list_ops_marks_undone(self, store: Any) -> None:
        todo, op_id = store.create("todo", {"title": "A"})
        store.undo(op_id)
        ops = {o["op_id"]: o for o in store.list_ops(50)}
        assert ops[op_id]["undone"] is True


class TestTodayAggregation:
    def test_today_buckets(self, store: Any) -> None:
        overdue, _ = store.create("todo", {"title": "逾期", "due": "2026-10-10"})
        due_today, _ = store.create("todo", {"title": "今日", "due": "2026-10-12"})
        future, _ = store.create("todo", {"title": "未来", "due": "2026-10-20"})
        store.update("todo", overdue["id"], {"status": "doing"})
        _ = future
        today = store.today()
        assert today["date"] == "2026-10-12"
        assert [t["title"] for t in today["todos"]["overdue"]] == ["逾期"]
        assert [t["title"] for t in today["todos"]["today"]] == ["今日"]
        assert today["todos"]["doing"][0]["title"] == "逾期"  # doing 独立列（与 overdue 可并存）
        assert today["partner"] == store.get_partner()

    def test_today_done_not_active(self, store: Any) -> None:
        """done/fail 不进活跃桶（逾期列不含已完成）。"""
        todo, _ = store.create("todo", {"title": "逾期", "due": "2026-10-10"})
        store.complete_todo(todo["id"])
        today = store.today()
        assert today["todos"]["overdue"] == []
        assert today["backlog_count"] == 0

    def test_today_habits_due_by_schedule_word(self, store: Any) -> None:
        daily, _ = store.create("habit", {"title": "跑步", "schedule": "每天"})
        weekly, _ = store.create("habit", {"title": "周报", "schedule": "每周一"})
        none_today, _ = store.create("habit", {"title": "周日事", "schedule": "每周日"})
        today = store.today()
        due = {h["title"]: h["due_today"] for h in today["habits"]}
        assert due["跑步"] is True
        assert due["周报"] is True  # 2026-10-12 是周一
        assert due["周日事"] is False

    def test_today_includes_journal_and_events(self, store: Any) -> None:
        store.upsert_journal("2026-10-12", {"highlight": "A"})
        store.create("event", {"title": "例会", "at": "2026-10-12 10:00"})
        store.create("event", {"title": "明天会", "at": "2026-10-13 10:00"})
        today = store.today()
        assert today["journal"]["highlight"] == "A"
        assert [e["title"] for e in today["events"]] == ["例会"]


class TestCorruptLineTolerance:
    def test_corrupt_jsonl_line_skipped(self, tmp_path: Path) -> None:
        """损坏行跳过不拖垮其余数据（fail-soft 读面）。"""
        data_dir = tmp_path / "data"
        store = pim_store.PimStore(data_dir, clock=FakeClock())
        store.create("todo", {"title": "好记录"})
        todo2, _ = store.create("todo", {"title": "第二条"})
        path = data_dir / "todos.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        lines.insert(0, "{corrupted json")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        reloaded = pim_store.PimStore(data_dir)
        assert reloaded.get("todo", todo2["id"])["title"] == "第二条"
        assert len(reloaded.list_records("todo")) == 2


class TestEventStateMachine:
    """event 状态机（2026-10-20 改造）：活跃/处置划分、accept/cancel/undo。"""

    def test_event_defaults_to_planned(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        assert event["status"] == "planned"

    def test_event_invalid_status_rejected(self, store: Any) -> None:
        with pytest.raises(ValueError):
            store.create("event", {"title": "例会", "at": "2026-10-13 10:00", "status": "bogus"})

    def test_accept_todo_sets_doing_and_preauthorized(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "交周报"})
        accepted, op_id = store.accept("todo", todo["id"])
        assert accepted["status"] == "doing"
        assert accepted["preauthorized"] is True
        assert op_id

    def test_accept_event_marks_doing(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        accepted, _ = store.accept("event", event["id"])
        assert accepted["status"] == "doing"
        assert store.unhandled_events() == []

    def test_complete_event_sets_done_at(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        done, _ = store.complete_event(event["id"])
        assert done["status"] == "done"
        assert done["done_at"] == "2026-10-12T09:00:00"

    def test_defer_event_moves_at_and_reactivates(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        store.accept("event", event["id"])
        deferred, _ = store.defer_event(event["id"], "2026-10-14 10:00")
        assert deferred["at"] == "2026-10-14 10:00"
        assert deferred["status"] == "planned"

    def test_cancel_event_terminal(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        cancelled, _ = store.cancel_event(event["id"])
        assert cancelled["status"] == "cancelled"

    def test_accept_event_undo_restores_planned(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        _, op_id = store.accept("event", event["id"])
        store.undo(op_id)
        restored = store.get("event", event["id"])
        assert restored is not None
        assert restored["status"] == "planned"

    def test_unhandled_events_only_past_active(self, store: Any) -> None:
        store.create("event", {"title": "已过", "at": "2026-10-12 08:00"})
        store.create("event", {"title": "未来", "at": "2026-10-12 10:00"})
        done, _ = store.create("event", {"title": "已处理", "at": "2026-10-12 07:00"})
        store.complete_event(done["id"])
        titles = [e["title"] for e in store.unhandled_events()]
        assert titles == ["已过"]


class TestChangeObserver:
    def test_create_update_undo_notify(self, store: Any) -> None:
        seen: list[tuple[str, str, bool, bool]] = []

        def on_change(entity: str, key: str, before: Any, after: Any) -> None:
            seen.append((entity, key, before is None, after is None))

        store.set_on_change(on_change)
        todo, _ = store.create("todo", {"title": "A"})
        store.update("todo", todo["id"], {"title": "B"})
        store.undo(store.list_ops(1)[0]["op_id"])
        assert seen[0] == ("todo", todo["id"], True, False)
        assert seen[1] == ("todo", todo["id"], False, False)
        assert seen[-1] == ("todo", todo["id"], True, False)

    def test_mark_reminded_is_silent(self, store: Any) -> None:
        seen: list[str] = []
        store.set_on_change(lambda entity, key, before, after: seen.append(key))
        todo, _ = store.create("todo", {"title": "A"})
        seen.clear()
        ops_before = len(store.list_ops(50))
        store.mark_reminded("todo", todo["id"], "2026-10-12 09:00")
        assert seen == []
        assert len(store.list_ops(50)) == ops_before
        assert store.get("todo", todo["id"])["last_reminded_for"] == "2026-10-12 09:00"


class TestFocusDurationConfig:
    def test_default_is_25(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        started, _ = store.start_focus(todo["id"])
        assert started["focus_duration_minutes"] == 25

    def test_configured_default_used(self, store: Any) -> None:
        store.set_focus_default_minutes(40)
        todo, _ = store.create("todo", {"title": "专注"})
        started, _ = store.start_focus(todo["id"])
        assert started["focus_duration_minutes"] == 40

    def test_explicit_duration_overrides_config(self, store: Any) -> None:
        store.set_focus_default_minutes(40)
        todo, _ = store.create("todo", {"title": "专注"})
        started, _ = store.start_focus(todo["id"], 10)
        assert started["focus_duration_minutes"] == 10

    def test_invalid_duration_rejected(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        with pytest.raises(ValueError):
            store.start_focus(todo["id"], 0)

    def test_invalid_config_keeps_previous(self, store: Any) -> None:
        store.set_focus_default_minutes(40)
        store.set_focus_default_minutes("bogus")
        assert store.focus_default_minutes() == 40
