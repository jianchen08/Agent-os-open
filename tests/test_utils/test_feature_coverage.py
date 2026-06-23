"""功能覆盖率追踪器 — 单元测试。

验证 FeatureCoverageTracker 能按 14 大类统计功能点覆盖率。
"""

from __future__ import annotations

import pytest

from tests.test_utils.feature_coverage import (
    FEATURE_CATEGORIES,
    FeatureCoverageTracker,
)


# ── 常量定义验证 ──────────────────────────────────────


class TestFeatureCategories:
    """FEATURE_CATEGORIES 常量验证。"""

    def test_has_14_categories(self):
        """验证 FEATURE_CATEGORIES 包含 14 个大类。"""
        assert len(FEATURE_CATEGORIES) == 14

    def test_all_categories_have_features(self):
        """验证每个大类至少有 1 个功能点。"""
        for cat, features in FEATURE_CATEGORIES.items():
            assert len(features) > 0, f"大类 '{cat}' 没有功能点定义"

    def test_each_feature_has_name_and_desc(self):
        """验证每个功能点都有 name 和 desc 字段。"""
        for cat, features in FEATURE_CATEGORIES.items():
            for feat in features:
                assert "name" in feat, f"大类 '{cat}' 中功能点缺少 name"
                assert "desc" in feat, f"大类 '{cat}' 中功能点缺少 desc"


# ── Tracker 核心功能 ──────────────────────────────────


class TestFeatureCoverageTracker:
    """FeatureCoverageTracker 核心功能测试。"""

    def test_init_total_features_count(self):
        """验证初始化后 total_features 等于所有大类功能点之和。"""
        tracker = FeatureCoverageTracker()
        expected = sum(len(features) for features in FEATURE_CATEGORIES.values())
        assert tracker.total_features == expected

    def test_init_zero_coverage(self):
        """验证初始化后覆盖率为 0。"""
        tracker = FeatureCoverageTracker()
        assert tracker.tested_features == 0
        assert tracker.coverage_rate == 0.0

    def test_mark_tested_single_feature(self):
        """验证标记单个功能点后覆盖率正确更新。"""
        tracker = FeatureCoverageTracker()
        total = tracker.total_features

        tracker.mark_tested("1. 对话与聊天", "主对话", test_names=["test_chat"])
        assert tracker.tested_features == 1
        assert tracker.coverage_rate == 1 / total

    def test_mark_tested_nonexistent_feature_is_noop(self):
        """验证标记不存在的功能点不影响统计。"""
        tracker = FeatureCoverageTracker()
        tracker.mark_tested("1. 对话与聊天", "不存在的功能")
        assert tracker.tested_features == 0

    def test_mark_tested_nonexistent_category_is_noop(self):
        """验证标记不存在的大类不影响统计。"""
        tracker = FeatureCoverageTracker()
        tracker.mark_tested("99. 不存在", "随便")
        assert tracker.tested_features == 0

    def test_mark_category_tested(self):
        """验证 mark_category_tested 标记整个大类所有功能点。"""
        tracker = FeatureCoverageTracker()
        chat_features = FEATURE_CATEGORIES["1. 对话与聊天"]
        expected_count = len(chat_features)

        tracker.mark_category_tested("1. 对话与聊天")
        assert tracker.tested_features == expected_count
        stats = tracker.category_stats("1. 对话与聊天")
        assert stats["tested"] == stats["total"]

    def test_category_stats(self):
        """验证 category_stats 返回正确的 total/tested 统计。"""
        tracker = FeatureCoverageTracker()
        tracker.mark_tested("4. 工具系统", "文件操作工具")
        tracker.mark_tested("4. 工具系统", "代码搜索")

        stats = tracker.category_stats("4. 工具系统")
        assert stats["total"] == len(FEATURE_CATEGORIES["4. 工具系统"])
        assert stats["tested"] == 2

    def test_category_stats_nonexistent_returns_zero(self):
        """验证不存在的类别返回 total=0, tested=0。"""
        tracker = FeatureCoverageTracker()
        stats = tracker.category_stats("99. 不存在")
        assert stats["total"] == 0
        assert stats["tested"] == 0

    def test_to_dict_schema(self):
        """验证 to_dict 返回正确的顶层 schema。"""
        tracker = FeatureCoverageTracker()
        tracker.mark_tested("1. 对话与聊天", "主对话")
        data = tracker.to_dict()

        assert "total" in data
        assert "tested" in data
        assert "rate" in data
        assert "categories" in data
        assert len(data["categories"]) == 14
        assert data["tested"] == 1

        # 验证每个 category 结构
        for cat_data in data["categories"]:
            assert "category" in cat_data
            assert "total" in cat_data
            assert "tested" in cat_data
            assert "rate" in cat_data
            assert "features" in cat_data

    def test_to_html_contains_all_categories(self):
        """验证 HTML 输出包含所有 14 个大类名称。"""
        tracker = FeatureCoverageTracker()
        tracker.mark_tested("1. 对话与聊天", "主对话")
        html = tracker.to_html()

        assert "feature-coverage" in html
        assert "coverage-table" in html
        for cat in FEATURE_CATEGORIES:
            # 检查类别名的关键词（去掉序号前缀）
            assert cat in html, f"HTML 中缺少大类: {cat}"

    def test_full_coverage_reaches_100_percent(self):
        """验证标记所有功能点后覆盖率达 100%。"""
        tracker = FeatureCoverageTracker()
        for cat in FEATURE_CATEGORIES:
            tracker.mark_category_tested(cat)
        assert tracker.coverage_rate == 1.0
        assert tracker.tested_features == tracker.total_features

    def test_mark_tested_with_note_and_test_names(self):
        """验证 mark_tested 正确记录 note 和 test_names。"""
        tracker = FeatureCoverageTracker()
        tracker.mark_tested(
            "1. 对话与聊天",
            "主对话",
            test_names=["test_stream_chat"],
            note="流式响应已验证",
        )
        item = next(
            i for i in tracker._items if i.category == "1. 对话与聊天" and i.name == "主对话"
        )
        assert item.tested is True
        assert "test_stream_chat" in item.test_names
        assert item.note == "流式响应已验证"
