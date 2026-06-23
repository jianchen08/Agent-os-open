"""AC 状态矩阵追踪器 — 单元测试。

验证 ACMatrixTracker 能正确追踪 15 条 AC 状态（通过/部分通过/未通过/未测试）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_utils.ac_matrix import (
    ACStatus,
    ACMatrixTracker,
    AC_DEFINITIONS,
    collect_ac_test_mapping,
    derive_status,
    normalize_ac_id,
)


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


# ── AC-ID 归一化 ──────────────────────────────────────


class TestNormalizeAcId:
    """normalize_ac_id 各种写法归一化。"""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("AC-1", "AC-1"),
            ("AC_1", "AC-1"),
            ("ac-01", "AC-1"),
            ("AC001", "AC-1"),
            ("test_AC001_create", "AC-1"),
            ("AC:1", "AC-1"),
            ("not_an_ac", "not_an_ac"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_ac_id(raw) == expected


# ── 自动状态推导 ──────────────────────────────────────


class TestDeriveStatus:
    """derive_status 确定性推导。"""

    def test_no_tests_is_not_tested(self):
        assert derive_status([], {"x": "passed"}) == ACStatus.NOT_TESTED

    def test_all_passed(self):
        assert derive_status(["t1", "t2"], {"t1": "passed", "t2": "passed"}) == ACStatus.PASSED

    def test_any_failed(self):
        assert derive_status(["t1", "t2"], {"t1": "passed", "t2": "failed"}) == ACStatus.FAILED

    def test_any_error(self):
        assert derive_status(["t1"], {"t1": "error"}) == ACStatus.FAILED

    def test_not_run_treated_as_not_tested(self):
        # 全是未跑过的 → 归为未测试，不能算通过
        assert derive_status(["t1"], {}) == ACStatus.NOT_TESTED

    def test_mixed_passed_not_run_is_not_tested(self):
        # passed + not_run 混合：不能算全绿，也不算红 → 未测试
        assert derive_status(["t1", "t2"], {"t1": "passed"}) == ACStatus.NOT_TESTED


# ── collect_ac_test_mapping ──────────────────────────


class TestCollectAcTestMapping:
    """从测试目录 grep AC 标记。"""

    def test_collect_from_test_names(self, tmp_path):
        f = tmp_path / "test_coupon.py"
        f.write_text(
            "def test_AC001_create_coupon():\n    pass\n\n"
            "def test_AC002_list_coupons():\n    pass\n",
            encoding="utf-8",
        )
        mapping = collect_ac_test_mapping(tmp_path)
        assert normalize_ac_id("AC001") in mapping
        assert normalize_ac_id("AC002") in mapping

    def test_collect_from_mark_decorator(self, tmp_path):
        f = tmp_path / "test_x.py"
        f.write_text(
            '@pytest.mark.ac("AC-3")\ndef test_something():\n    pass\n',
            encoding="utf-8",
        )
        mapping = collect_ac_test_mapping(tmp_path)
        assert "AC-3" in mapping

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        assert collect_ac_test_mapping(tmp_path / "nope") == {}

    def test_no_ac_markers_returns_empty(self, tmp_path):
        f = tmp_path / "test_plain.py"
        f.write_text("def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
        assert collect_ac_test_mapping(tmp_path) == {}


# ── from_test_results 端到端 ─────────────────────────


class TestFromTestResults:
    """从测试结果一次性构造矩阵。"""

    def test_pure_derivation_no_manual_update(self):
        """核心断言：状态完全由测试结果推导，不需要手动盖章。"""
        defs = [
            {"id": "AC-1", "title": "创建券", "category": "功能"},
            {"id": "AC-2", "title": "删除券", "category": "功能"},
            {"id": "AC-3", "title": "未实现", "category": "功能"},
        ]
        mapping = {
            "AC-1": ["t.py::test_AC001_create"],
            "AC-2": ["t.py::test_AC002_delete"],
        }
        results = {
            "t.py::test_AC001_create": "passed",
            "t.py::test_AC002_delete": "failed",
        }
        tracker = ACMatrixTracker.from_test_results(defs, mapping, results)

        ac1 = next(e for e in tracker.entries if e.ac_id == "AC-1")
        ac2 = next(e for e in tracker.entries if e.ac_id == "AC-2")
        ac3 = next(e for e in tracker.entries if e.ac_id == "AC-3")

        assert ac1.status == ACStatus.PASSED
        assert ac2.status == ACStatus.FAILED
        assert ac3.status == ACStatus.NOT_TESTED  # 无测试 → 未测试（门禁红灯）
        assert "自动推导" in ac1.evidence

    def test_from_definitions_uses_statement_fallback(self):
        """AC 无 title 时回退到 statement 字段。"""
        defs = [{"id": "AC-9", "statement": "券列表可见", "category": "功能"}]
        tracker = ACMatrixTracker.from_definitions(defs)
        entry = tracker.entries[0]
        assert entry.title == "券列表可见"
