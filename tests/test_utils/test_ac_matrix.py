"""AC 状态矩阵追踪器 — 单元测试。

验证 ACMatrixTracker 能正确追踪 15 条 AC 状态（通过/部分通过/未通过/未测试）。
"""

from __future__ import annotations

import pytest

from tests.test_utils.ac_matrix import ACStatus, ACMatrixTracker, AC_DEFINITIONS


# ── 基本结构 ──────────────────────────────────────────


class TestACDefinitions:
    """AC 定义常量验证。"""

    def test_ac_definitions_has_15_entries(self):
        """验证 AC_DEFINITIONS 包含 15 条 AC。"""
        assert len(AC_DEFINITIONS) == 15

    def test_ac_definitions_have_required_keys(self):
        """验证每条 AC 定义都有 id / title / category 字段。"""
        for ac in AC_DEFINITIONS:
            assert "id" in ac, f"缺少 id 字段: {ac}"
            assert "title" in ac, f"缺少 title 字段: {ac}"
            assert "category" in ac, f"缺少 category 字段: {ac}"

    def test_ac_ids_are_unique(self):
        """验证所有 AC 编号唯一。"""
        ids = [ac["id"] for ac in AC_DEFINITIONS]
        assert len(ids) == len(set(ids)), "AC 编号存在重复"


class TestACStatusEnum:
    """ACStatus 枚举验证。"""

    def test_four_status_values(self):
        """验证 ACStatus 有 4 种状态。"""
        assert len(ACStatus) == 4
        assert ACStatus.PASSED.value == "passed"
        assert ACStatus.PARTIAL.value == "partial"
        assert ACStatus.FAILED.value == "failed"
        assert ACStatus.NOT_TESTED.value == "not_tested"


# ── Tracker 核心功能 ──────────────────────────────────


class TestACMatrixTracker:
    """ACMatrixTracker 核心功能测试。"""

    def test_init_has_15_entries_all_not_tested(self):
        """验证初始化后 15 条 AC 全部为 NOT_TESTED。"""
        tracker = ACMatrixTracker()
        entries = tracker.entries
        assert len(entries) == 15
        for entry in entries:
            assert entry.status == ACStatus.NOT_TESTED

    def test_update_changes_status_and_fields(self):
        """验证 update 能修改 AC 状态、evidence、test_names、detail。"""
        tracker = ACMatrixTracker()
        tracker.update(
            "AC-1",
            ACStatus.PASSED,
            evidence="RBAC 单测全部通过",
            test_names=["test_rbac_admin", "test_rbac_viewer"],
            detail="权限矩阵完整",
        )
        entry = next(e for e in tracker.entries if e.ac_id == "AC-1")
        assert entry.status == ACStatus.PASSED
        assert entry.evidence == "RBAC 单测全部通过"
        assert entry.test_names == ["test_rbac_admin", "test_rbac_viewer"]
        assert entry.detail == "权限矩阵完整"

    def test_update_nonexistent_ac_is_noop(self):
        """验证更新不存在的 AC 不报错，静默忽略。"""
        tracker = ACMatrixTracker()
        tracker.update("AC-99", ACStatus.PASSED)
        # 仍只有 15 条
        assert len(tracker.entries) == 15
        assert all(e.ac_id != "AC-99" for e in tracker.entries)

    def test_add_test_to_ac_appends(self):
        """验证 add_test_to_ac 追加测试用例名。"""
        tracker = ACMatrixTracker()
        tracker.add_test_to_ac("AC-2", "test_bug_fix_1")
        tracker.add_test_to_ac("AC-2", "test_bug_fix_2")
        entry = next(e for e in tracker.entries if e.ac_id == "AC-2")
        assert entry.test_names == ["test_bug_fix_1", "test_bug_fix_2"]

    def test_summary_counts_all_statuses(self):
        """验证 summary 统计各状态数量之和等于 15。"""
        tracker = ACMatrixTracker()
        tracker.update("AC-1", ACStatus.PASSED)
        tracker.update("AC-2", ACStatus.PASSED)
        tracker.update("AC-7", ACStatus.FAILED)
        tracker.update("AC-9", ACStatus.PARTIAL)

        counts = tracker.summary()
        assert counts["passed"] == 2
        assert counts["failed"] == 1
        assert counts["partial"] == 1
        assert counts["not_tested"] == 11
        assert sum(counts.values()) == 15

    def test_to_dict_returns_15_items_with_correct_schema(self):
        """验证 to_dict 返回 15 项且每项含必要字段。"""
        tracker = ACMatrixTracker()
        tracker.update("AC-1", ACStatus.PASSED, evidence="ev")
        data = tracker.to_dict()
        assert len(data) == 15
        first = data[0]
        assert first["id"] == "AC-1"
        assert first["status"] == "passed"
        for key in ("id", "title", "category", "status", "evidence", "test_names", "detail"):
            assert key in first, f"缺少字段: {key}"

    def test_to_html_contains_all_ac_ids(self):
        """验证 HTML 输出包含所有 15 个 AC 编号。"""
        tracker = ACMatrixTracker()
        tracker.update("AC-1", ACStatus.PASSED)
        tracker.update("AC-7", ACStatus.FAILED)
        tracker.update("AC-13", ACStatus.PARTIAL)
        html = tracker.to_html()
        for ac in AC_DEFINITIONS:
            assert ac["id"] in html, f"HTML 中缺少 {ac['id']}"
        assert "ac-matrix" in html
        assert "ac-table" in html

    def test_to_html_progress_bar_shows_passed_percentage(self):
        """验证 HTML 中进度条百分比正确。"""
        tracker = ACMatrixTracker()
        # 15 条中通过 3 条 → 20%
        for ac_id in ("AC-1", "AC-2", "AC-3"):
            tracker.update(ac_id, ACStatus.PASSED)
        html = tracker.to_html()
        assert "20%" in html

    def test_entries_order_matches_definitions(self):
        """验证 entries 返回顺序与 AC_DEFINITIONS 定义顺序一致。"""
        tracker = ACMatrixTracker()
        entry_ids = [e.ac_id for e in tracker.entries]
        def_ids = [ac["id"] for ac in AC_DEFINITIONS]
        assert entry_ids == def_ids
