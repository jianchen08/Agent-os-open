# @feature: FP-0.2.二 mode_personal 个人助理模式包 M0 | @ci: python-coverage
"""P0② 数据治理单测：9 张日常 repeat 卡补 due（标题时间表；已过取次日；幂等不覆盖）
+ 4 张早期重叠遗留卡按 ID 清理（可 undo；缺 ID 跳过不报错）。表外卡不受影响。
关键路径走真实依赖：tmp_path 真实文件读写，零 mock 文件面；时钟注入 fake clock。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parent  # plugins/shared/（_root_api 等共享模块所在）


def _load(name: str, mod_name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pim_store = _load("pim_store", "mode_personal_test_gov_store")
pim_governance = _load("pim_governance", "mode_personal_test_governance")


class FakeClock:
    """可拨动 fake clock（时序测试注入）。"""

    def __init__(self, start: str = "2026-10-12T09:00:00") -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> Any:
    return pim_store.PimStore(tmp_path / "data", clock=FakeClock())


class TestDailyDueFill:
    @pytest.mark.parametrize(
        ("title", "hhmm"),
        [
            ("Boss打招呼", "10:00"),
            ("本职工作", "08:30"),
            ("华为刷题", "19:30"),
            ("恋爱聊天", "22:00"),
            ("大厂精调", "23:00"),
            ("作息", "23:50"),
            ("运动", "18:00"),
            ("保健品", "12:00"),
            ("AgentOS", "12:00"),
        ],
    )
    def test_table_time_applied(self, store: Any, title: str, hhmm: str) -> None:
        """9 张时间表逐张核对（NOW=09:00：本职工作 08:30 已过 → 次日；其余当日）。"""
        todo, _ = store.create("todo", {"title": title, "repeat": "daily"})
        result = pim_governance.govern(store)
        expect_day = "2026-10-13" if title == "本职工作" else "2026-10-12"
        assert todo["id"] in {f["id"] for f in result["due_filled"]}
        assert {f["title"]: f["due"] for f in result["due_filled"]}.get(title) == (
            f"{expect_day} {hhmm}"
        )
        stored = store.get("todo", todo["id"])
        assert stored is not None
        assert stored["due"] == f"{expect_day} {hhmm}"

    def test_existing_due_not_overwritten(self, store: Any) -> None:
        """due 已存在不覆盖（AC-5）。"""
        todo, _ = store.create(
            "todo", {"title": "华为刷题", "repeat": "daily", "due": "2026-10-12 21:00"}
        )
        result = pim_governance.govern(store)
        assert result["due_filled"] == []
        stored = store.get("todo", todo["id"])
        assert stored is not None
        assert stored["due"] == "2026-10-12 21:00"

    def test_idempotent_second_run_zero_change(self, store: Any) -> None:
        """治理幂等（AC-5）：二次运行零变化。"""
        store.create("todo", {"title": "华为刷题", "repeat": "daily"})
        first = pim_governance.govern(store)
        assert first["due_filled"]
        second = pim_governance.govern(store)
        assert second["due_filled"] == []
        assert second["legacy_removed"] == []

    def test_off_table_card_untouched(self, store: Any) -> None:
        """表外卡不受影响（AC-7）。"""
        todo, _ = store.create("todo", {"title": "临时杂事", "repeat": "daily"})
        result = pim_governance.govern(store)
        assert result["due_filled"] == []
        stored = store.get("todo", todo["id"])
        assert stored is not None
        assert not stored.get("due")

    def test_absent_table_cards_skipped(self, store: Any) -> None:
        """表内卡不存在时跳过（治理只补存量，不建卡）。"""
        store.create("todo", {"title": "运动", "repeat": "daily"})
        result = pim_governance.govern(store)
        assert len(result["due_filled"]) == 1


class TestLegacyRemoval:
    """4 张早期重叠遗留卡（任务书 ID 清单）：按 ID 删除且可 undo；缺 ID 跳过。"""

    LEGACY_TODOS = (
        ("24a6c59af581", "锻炼30分钟"),
        ("a6a6ebbffe43", "1点之前睡觉"),
        ("3b690e670dd9", "找女孩子聊天30分钟"),
    )
    LEGACY_EVENT = ("c0b5e43d1112", "投递周")

    def _seed_legacy(self, store: Any) -> None:
        for card_id, title in self.LEGACY_TODOS:
            store._put("todo", {
                "id": card_id,
                "title": title,
                "status": "today",
                "reminder_mode": "direct",
                "created": "2026-10-01T09:00:00",
            })
        event_id, event_title = self.LEGACY_EVENT
        store._put("event", {
            "id": event_id,
            "title": event_title,
            "at": "2026-10-05 09:00",
            "status": "planned",
            "reminder_mode": "direct",
            "created": "2026-10-01T09:00:00",
        })

    def test_four_legacy_cards_removed(self, store: Any) -> None:
        self._seed_legacy(store)
        result = pim_governance.govern(store)
        removed_ids = {r["id"] for r in result["legacy_removed"]}
        assert removed_ids == {
            "24a6c59af581",
            "a6a6ebbffe43",
            "3b690e670dd9",
            "c0b5e43d1112",
        }
        for card_id, _title in self.LEGACY_TODOS:
            assert store.get("todo", card_id) is None
        assert store.get("event", self.LEGACY_EVENT[0]) is None

    def test_removed_cards_undoable(self, store: Any) -> None:
        """AC-6：删除经 op 记账可 undo 恢复。"""
        self._seed_legacy(store)
        result = pim_governance.govern(store)
        op_ids = [r["op_id"] for r in result["legacy_removed"]]
        assert op_ids
        assert all(op_ids)
        for entry in result["legacy_removed"]:
            store.undo(entry["op_id"])
        assert store.get("todo", "24a6c59af581") is not None
        assert store.get("todo", "24a6c59af581")["title"] == "锻炼30分钟"
        assert store.get("event", self.LEGACY_EVENT[0]) is not None

    def test_missing_legacy_id_skipped(self, store: Any) -> None:
        """AC-6：ID 不存在时跳过不报错。"""
        store._put("todo", {
            "id": "24a6c59af581",
            "title": "锻炼30分钟",
            "status": "today",
            "reminder_mode": "direct",
            "created": "2026-10-01T09:00:00",
        })
        result = pim_governance.govern(store)
        assert len(result["legacy_removed"]) == 1
        assert result["legacy_removed"][0]["id"] == "24a6c59af581"
