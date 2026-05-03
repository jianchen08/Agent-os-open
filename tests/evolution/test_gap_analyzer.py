"""能力缺口分析与四层筛选模块测试。

覆盖 GapAnalyzer 的核心功能：
- analyze_gap: 能力缺口分析
- four_layer_filter: 四层筛选（TOOL → CONFIG → PLUGIN → CORE）
- _infer_suggested_layer: 关键词推断
- 边界条件与异常处理
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from evolution.gap_analyzer import GapAnalyzer
from evolution.types import CapabilityGap, FilterLayer, FilterResult


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def mock_tool_registry() -> MagicMock:
    """模拟工具注册中心。"""
    registry = MagicMock()
    registry.search.return_value = []
    return registry


@pytest.fixture
def mock_config_store() -> MagicMock:
    """模拟配置存储。"""
    store = MagicMock()
    store.search.return_value = []
    store.get.return_value = None
    return store


@pytest.fixture
def mock_plugin_registry() -> MagicMock:
    """模拟插件注册中心。"""
    registry = MagicMock()
    registry.search.return_value = []
    return registry


@pytest.fixture
def analyzer() -> GapAnalyzer:
    """无依赖的 GapAnalyzer 实例。"""
    return GapAnalyzer()


# =========================================================================
# analyze_gap 测试
# =========================================================================


class TestAnalyzeGap:
    """analyze_gap 方法测试。"""

    def test_analyze_gap_returns_capability_gap(self, analyzer: GapAnalyzer) -> None:
        """分析缺口返回 CapabilityGap 结构。"""
        gap = analyzer.analyze_gap("file search capability")

        assert isinstance(gap, CapabilityGap)
        assert gap.missing_capability == "file search capability"
        assert gap.required_by == "unknown"
        assert gap.priority == 5
        assert isinstance(gap.suggested_layer, FilterLayer)

    def test_analyze_gap_with_context(self, analyzer: GapAnalyzer) -> None:
        """带上下文的缺口分析。"""
        gap = analyzer.analyze_gap(
            "test capability",
            context={
                "required_by": "agent_1",
                "priority": 3,
            },
        )
        assert gap.required_by == "agent_1"
        assert gap.priority == 3

    def test_analyze_gap_priority_clamping_high(self, analyzer: GapAnalyzer) -> None:
        """优先级上限为 10。"""
        gap = analyzer.analyze_gap("test", context={"priority": 15})
        assert gap.priority == 10

    def test_analyze_gap_priority_clamping_low(self, analyzer: GapAnalyzer) -> None:
        """优先级下限为 1。"""
        gap = analyzer.analyze_gap("test", context={"priority": -5})
        assert gap.priority == 1

    def test_analyze_gap_priority_boundary_max(self, analyzer: GapAnalyzer) -> None:
        """优先级刚好为 10。"""
        gap = analyzer.analyze_gap("test", context={"priority": 10})
        assert gap.priority == 10

    def test_analyze_gap_priority_boundary_min(self, analyzer: GapAnalyzer) -> None:
        """优先级刚好为 1。"""
        gap = analyzer.analyze_gap("test", context={"priority": 1})
        assert gap.priority == 1

    def test_analyze_gap_none_context(self, analyzer: GapAnalyzer) -> None:
        """context 为 None 时使用默认值。"""
        gap = analyzer.analyze_gap("test", context=None)
        assert gap.required_by == "unknown"
        assert gap.priority == 5

    def test_analyze_gap_context_suggested_layer_override(self, analyzer: GapAnalyzer) -> None:
        """上下文指定 suggested_layer 时覆盖推断。"""
        gap = analyzer.analyze_gap(
            "test capability",
            context={"suggested_layer": "config"},
        )
        assert gap.suggested_layer == FilterLayer.CONFIG

    def test_analyze_gap_context_suggested_layer_invalid(self, analyzer: GapAnalyzer) -> None:
        """上下文指定无效 suggested_layer 时回退到推断。"""
        gap = analyzer.analyze_gap(
            "test capability",
            context={"suggested_layer": "invalid_layer"},
        )
        assert isinstance(gap.suggested_layer, FilterLayer)

    def test_analyze_gap_context_preserved(self, analyzer: GapAnalyzer) -> None:
        """上下文完整保留。"""
        ctx = {"key1": "val1", "key2": 42}
        gap = analyzer.analyze_gap("test", context=ctx)
        assert gap.context == ctx


# =========================================================================
# four_layer_filter 测试
# =========================================================================


class TestFourLayerFilter:
    """four_layer_filter 四层筛选测试。"""

    def test_four_layer_filter_tool_found(
        self, analyzer: GapAnalyzer, mock_tool_registry: MagicMock,
    ) -> None:
        """第一层工具层找到匹配工具时直接返回。"""
        mock_tool = MagicMock()
        mock_tool.name = "search_tool"
        mock_tool_registry.search.return_value = [mock_tool]

        gap = CapabilityGap(missing_capability="search files", required_by="test")
        result = analyzer.four_layer_filter(gap, tool_registry=mock_tool_registry)

        assert result.recommended_layer == FilterLayer.TOOL
        assert "search_tool" in result.tool_layer_result

    def test_four_layer_filter_config_layer(
        self, analyzer: GapAnalyzer, mock_config_store: MagicMock,
    ) -> None:
        """第二层配置层找到匹配。"""
        mock_config_store.search.return_value = [{"key": "value"}]

        gap = CapabilityGap(missing_capability="config option", required_by="test")
        result = analyzer.four_layer_filter(gap, config_store=mock_config_store)

        assert result.recommended_layer == FilterLayer.CONFIG

    def test_four_layer_filter_config_layer_via_get_key(
        self, analyzer: GapAnalyzer, mock_config_store: MagicMock,
    ) -> None:
        """第二层配置层通过 config_keys 上下文找到配置。"""
        mock_config_store.search.return_value = []
        mock_config_store.get.return_value = "value"

        gap = CapabilityGap(
            missing_capability="some feature",
            required_by="test",
            context={"config_keys": ["feature_flag"]},
        )
        result = analyzer.four_layer_filter(gap, config_store=mock_config_store)

        assert result.recommended_layer == FilterLayer.CONFIG
        assert "feature_flag" in result.config_layer_result

    def test_four_layer_filter_plugin_layer(
        self, analyzer: GapAnalyzer, mock_plugin_registry: MagicMock,
    ) -> None:
        """第三层插件层找到已有插件。"""
        mock_plugin_registry.search.return_value = [{"name": "my_plugin"}]

        gap = CapabilityGap(missing_capability="data convert", required_by="test")
        result = analyzer.four_layer_filter(gap, plugin_registry=mock_plugin_registry)

        assert result.recommended_layer == FilterLayer.PLUGIN
        assert "my_plugin" in result.plugin_layer_result

    def test_four_layer_filter_plugin_layer_no_registry(self, analyzer: GapAnalyzer) -> None:
        """第三层插件层 - 无注册中心时默认生成新插件。"""
        gap = CapabilityGap(missing_capability="custom capability", required_by="test")
        result = analyzer.four_layer_filter(gap)

        assert result.recommended_layer == FilterLayer.PLUGIN
        assert "生成新插件" in result.recommended_action

    def test_four_layer_filter_core_layer(self, analyzer: GapAnalyzer) -> None:
        """第四层核心层 - 能力描述含'核心'关键词时到达 CORE 层。"""
        gap = CapabilityGap(missing_capability="核心代码修改", required_by="test")
        result = analyzer.four_layer_filter(gap)

        assert result.recommended_layer == FilterLayer.CORE
        assert result.core_layer_result is not None
        assert "核心" in result.core_layer_result

    def test_four_layer_filter_core_layer_english_keyword(self, analyzer: GapAnalyzer) -> None:
        """第四层核心层 - 能力描述含 'core' 英文关键词。"""
        gap = CapabilityGap(missing_capability="core modification", required_by="test")
        result = analyzer.four_layer_filter(gap)

        assert result.recommended_layer == FilterLayer.CORE

    def test_filter_order_is_correct(self, analyzer: GapAnalyzer) -> None:
        """验证四层筛选严格按顺序执行。"""
        gap = CapabilityGap(
            missing_capability="custom capability",
            required_by="test",
        )
        result = analyzer.four_layer_filter(gap)

        # 每一层都应该有检查结果记录
        assert result.tool_layer_result is not None
        assert result.config_layer_result is not None
        assert result.plugin_layer_result is not None

    def test_filter_uses_constructor_registry(self) -> None:
        """构造时传入的 registry 在 four_layer_filter 中使用。"""
        mock_tool = MagicMock()
        mock_tool.name = "built_in_tool"
        registry = MagicMock()
        registry.search.return_value = [mock_tool]

        analyzer = GapAnalyzer(tool_registry=registry)
        gap = CapabilityGap(missing_capability="search", required_by="test")
        result = analyzer.four_layer_filter(gap)

        assert result.recommended_layer == FilterLayer.TOOL

    def test_filter_override_registry(
        self, analyzer: GapAnalyzer, mock_tool_registry: MagicMock,
    ) -> None:
        """four_layer_filter 参数覆盖构造时的 registry。"""
        mock_tool = MagicMock()
        mock_tool.name = "override_tool"
        mock_tool_registry.search.return_value = [mock_tool]

        gap = CapabilityGap(missing_capability="search", required_by="test")
        result = analyzer.four_layer_filter(gap, tool_registry=mock_tool_registry)

        assert result.recommended_layer == FilterLayer.TOOL
        assert "override_tool" in result.tool_layer_result

    def test_tool_layer_exception_handled(
        self, analyzer: GapAnalyzer,
    ) -> None:
        """工具层检查异常时不崩溃，继续到下一层。"""
        registry = MagicMock()
        registry.search.side_effect = RuntimeError("search broken")

        gap = CapabilityGap(missing_capability="search", required_by="test")
        result = analyzer.four_layer_filter(gap, tool_registry=registry)

        # 异常后继续到下一层
        assert result.tool_layer_result is not None
        assert "异常" in result.tool_layer_result

    def test_config_layer_exception_handled(
        self, analyzer: GapAnalyzer,
    ) -> None:
        """配置层检查异常时不崩溃。"""
        config_store = MagicMock()
        config_store.search.side_effect = RuntimeError("config broken")

        gap = CapabilityGap(missing_capability="config", required_by="test")
        result = analyzer.four_layer_filter(gap, config_store=config_store)

        assert result.config_layer_result is not None
        assert "异常" in result.config_layer_result

    def test_plugin_layer_exception_handled(
        self, analyzer: GapAnalyzer,
    ) -> None:
        """插件层检查异常时仍然在 PLUGIN 层。"""
        plugin_registry = MagicMock()
        plugin_registry.search.side_effect = RuntimeError("plugin broken")

        gap = CapabilityGap(missing_capability="custom", required_by="test")
        result = analyzer.four_layer_filter(gap, plugin_registry=plugin_registry)

        assert result.recommended_layer == FilterLayer.PLUGIN

    def test_filter_result_references_original_gap(self, analyzer: GapAnalyzer) -> None:
        """FilterResult 中的 gap 引用原始 CapabilityGap。"""
        gap = CapabilityGap(missing_capability="test", required_by="test")
        result = analyzer.four_layer_filter(gap)

        assert result.gap is gap


# =========================================================================
# _infer_suggested_layer 测试
# =========================================================================


class TestInferSuggestedLayer:
    """关键词推断测试。"""

    def test_file_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("file read", {}) == FilterLayer.TOOL

    def test_search_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("search data", {}) == FilterLayer.TOOL

    def test_http_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("http request", {}) == FilterLayer.TOOL

    def test_api_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("api call", {}) == FilterLayer.TOOL

    def test_config_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("config setting", {}) == FilterLayer.CONFIG

    def test_setting_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("setting update", {}) == FilterLayer.CONFIG

    def test_parameter_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("parameter change", {}) == FilterLayer.CONFIG

    def test_enable_keyword(self, analyzer: GapAnalyzer) -> None:
        assert analyzer._infer_suggested_layer("enable feature", {}) == FilterLayer.CONFIG

    def test_default_is_plugin(self, analyzer: GapAnalyzer) -> None:
        """无匹配关键词时默认为 PLUGIN。"""
        assert analyzer._infer_suggested_layer("custom analysis", {}) == FilterLayer.PLUGIN

    def test_context_override(self, analyzer: GapAnalyzer) -> None:
        """上下文中的 suggested_layer 优先。"""
        result = analyzer._infer_suggested_layer(
            "file read", {"suggested_layer": "core"}
        )
        assert result == FilterLayer.CORE
