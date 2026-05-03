"""Evolution 模块类型定义测试。

覆盖所有数据类型和枚举的正确性验证。
"""

from __future__ import annotations

from evolution.types import (
    CapabilityGap,
    EvolutionRecord,
    EvolutionResult,
    EvolutionStatus,
    FilterLayer,
    FilterResult,
    GeneratedArtifact,
    GenerationType,
    SecurityReport,
)


class TestFilterLayer:
    """FilterLayer 枚举测试。"""

    def test_tool_value(self) -> None:
        """TOOL 层值为 'tool'。"""
        assert FilterLayer.TOOL.value == "tool"

    def test_config_value(self) -> None:
        """CONFIG 层值为 'config'。"""
        assert FilterLayer.CONFIG.value == "config"

    def test_plugin_value(self) -> None:
        """PLUGIN 层值为 'plugin'。"""
        assert FilterLayer.PLUGIN.value == "plugin"

    def test_core_value(self) -> None:
        """CORE 层值为 'core'。"""
        assert FilterLayer.CORE.value == "core"

    def test_all_members_count(self) -> None:
        """FilterLayer 共有 4 个成员。"""
        assert len(FilterLayer) == 4

    def test_from_value(self) -> None:
        """通过字符串值构造 FilterLayer。"""
        assert FilterLayer("tool") == FilterLayer.TOOL
        assert FilterLayer("config") == FilterLayer.CONFIG
        assert FilterLayer("plugin") == FilterLayer.PLUGIN
        assert FilterLayer("core") == FilterLayer.CORE


class TestEvolutionStatus:
    """EvolutionStatus 枚举测试。"""

    def test_all_status_values(self) -> None:
        """所有状态值正确。"""
        assert EvolutionStatus.IDLE.value == "idle"
        assert EvolutionStatus.ANALYZING.value == "analyzing"
        assert EvolutionStatus.GENERATING.value == "generating"
        assert EvolutionStatus.REVIEWING.value == "reviewing"
        assert EvolutionStatus.LOADING.value == "loading"
        assert EvolutionStatus.COMPLETED.value == "completed"
        assert EvolutionStatus.FAILED.value == "failed"
        assert EvolutionStatus.ROLLING_BACK.value == "rolling_back"

    def test_all_members_count(self) -> None:
        """EvolutionStatus 共有 8 个成员。"""
        assert len(EvolutionStatus) == 8


class TestGenerationType:
    """GenerationType 枚举测试。"""

    def test_builtin_tool_value(self) -> None:
        assert GenerationType.BUILTIN_TOOL.value == "builtin_tool"

    def test_mcp_server_value(self) -> None:
        assert GenerationType.MCP_SERVER.value == "mcp_server"

    def test_all_members_count(self) -> None:
        assert len(GenerationType) == 2


class TestCapabilityGap:
    """CapabilityGap 数据类测试。"""

    def test_creation_with_all_fields(self) -> None:
        """完整字段创建。"""
        gap = CapabilityGap(
            missing_capability="file search",
            required_by="test_agent",
            priority=8,
            suggested_layer=FilterLayer.PLUGIN,
            context={"key": "value"},
        )
        assert gap.missing_capability == "file search"
        assert gap.required_by == "test_agent"
        assert gap.priority == 8
        assert gap.suggested_layer == FilterLayer.PLUGIN
        assert gap.context == {"key": "value"}

    def test_defaults(self) -> None:
        """默认值正确。"""
        gap = CapabilityGap(
            missing_capability="test",
            required_by="test",
        )
        assert gap.priority == 5
        assert gap.suggested_layer == FilterLayer.PLUGIN
        assert gap.context == {}

    def test_context_isolation(self) -> None:
        """每个实例的 context 字典独立。"""
        gap1 = CapabilityGap(missing_capability="a", required_by="a")
        gap2 = CapabilityGap(missing_capability="b", required_by="b")
        gap1.context["key"] = "value"
        assert "key" not in gap2.context


class TestFilterResult:
    """FilterResult 数据类测试。"""

    def test_creation_with_required_fields(self) -> None:
        """仅必填字段创建。"""
        gap = CapabilityGap(missing_capability="test", required_by="test")
        result = FilterResult(gap=gap)
        assert result.gap is gap
        assert result.tool_layer_result is None
        assert result.config_layer_result is None
        assert result.plugin_layer_result is None
        assert result.core_layer_result is None
        assert result.recommended_action == ""
        assert result.recommended_layer == FilterLayer.PLUGIN

    def test_creation_with_all_fields(self) -> None:
        """全字段创建。"""
        gap = CapabilityGap(missing_capability="test", required_by="test")
        result = FilterResult(
            gap=gap,
            tool_layer_result="found",
            config_layer_result="not found",
            plugin_layer_result="generate",
            core_layer_result="skip",
            recommended_action="generate plugin",
            recommended_layer=FilterLayer.PLUGIN,
        )
        assert result.tool_layer_result == "found"
        assert result.recommended_action == "generate plugin"


class TestGeneratedArtifact:
    """GeneratedArtifact 数据类测试。"""

    def test_creation_defaults(self) -> None:
        """默认值正确。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code="pass",
            file_path="test.py",
        )
        assert artifact.contract_valid is False
        assert artifact.contract_errors == []

    def test_creation_with_contract(self) -> None:
        """带契约校验结果创建。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.MCP_SERVER,
            code="pass",
            file_path="server.py",
            contract_valid=True,
            contract_errors=[],
        )
        assert artifact.contract_valid is True


class TestSecurityReport:
    """SecurityReport 数据类测试。"""

    def test_defaults(self) -> None:
        """默认值正确。"""
        report = SecurityReport()
        assert report.passed is False
        assert report.static_analysis_issues == []
        assert report.sandbox_result is None
        assert report.permission_issues == []
        assert report.resource_violations == []
        assert report.overall_risk == "unknown"

    def test_with_results(self) -> None:
        """带检查结果创建。"""
        report = SecurityReport(
            passed=True,
            static_analysis_issues=[],
            sandbox_result={"success": True},
            overall_risk="low",
        )
        assert report.passed is True
        assert report.sandbox_result == {"success": True}


class TestEvolutionRecord:
    """EvolutionRecord 数据类测试。"""

    def test_creation(self) -> None:
        """基本创建。"""
        record = EvolutionRecord(
            record_id="evo_001",
            timestamp="2024-01-01T00:00:00",
            capability_gap="test gap",
        )
        assert record.record_id == "evo_001"
        assert record.status == EvolutionStatus.IDLE
        assert record.error_message == ""
        assert record.rollback_point is None

    def test_with_all_optional_fields(self) -> None:
        """带所有可选字段创建。"""
        gap = CapabilityGap(missing_capability="test", required_by="test")
        record = EvolutionRecord(
            record_id="evo_002",
            timestamp="2024-01-01T00:00:00",
            capability_gap="test",
            filter_result=FilterResult(gap=gap),
            status=EvolutionStatus.COMPLETED,
            error_message="",
            rollback_point="cp_001",
        )
        assert record.filter_result is not None
        assert record.status == EvolutionStatus.COMPLETED
        assert record.rollback_point == "cp_001"


class TestEvolutionResult:
    """EvolutionResult 数据类测试。"""

    def test_defaults(self) -> None:
        """默认值正确。"""
        result = EvolutionResult()
        assert result.success is False
        assert result.record is None
        assert result.loaded_plugin_name == ""
        assert result.message == ""

    def test_with_success(self) -> None:
        """成功结果创建。"""
        result = EvolutionResult(
            success=True,
            loaded_plugin_name="test_plugin",
            message="进化成功",
        )
        assert result.success is True
        assert result.loaded_plugin_name == "test_plugin"
