"""Evolution 模块集成测试。

验证模块间协作的正确性：
- trigger → engine 完整闭环
- __init__.py 导出完整性
- 模块间类型兼容性
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evolution import (
    CodeGenerator,
    EvolutionEngine,
    EvolutionLog,
    EvolutionTrigger,
    GapAnalyzer,
    HotLoader,
    RollbackManager,
    SecurityReviewer,
    TriggerEvent,
    TriggerMode,
    TriggerResult,
    create_evolution_engine,
)
from evolution.trigger_types import make_timestamp
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


# =========================================================================
# __init__.py 导出完整性测试
# =========================================================================


class TestModuleExports:
    """验证 evolution 包的公共 API 导出。"""

    def test_export_evolution_trigger(self) -> None:
        """EvolutionTrigger 可从 evolution 包导入。"""
        from evolution import EvolutionTrigger
        assert EvolutionTrigger is not None

    def test_export_trigger_mode(self) -> None:
        """TriggerMode 可从 evolution 包导入。"""
        from evolution import TriggerMode
        assert TriggerMode is not None

    def test_export_trigger_event(self) -> None:
        """TriggerEvent 可从 evolution 包导入。"""
        from evolution import TriggerEvent
        assert TriggerEvent is not None

    def test_export_trigger_result(self) -> None:
        """TriggerResult 可从 evolution 包导入。"""
        from evolution import TriggerResult
        assert TriggerResult is not None

    def test_export_evolution_engine(self) -> None:
        """EvolutionEngine 可从 evolution 包导入。"""
        from evolution import EvolutionEngine
        assert EvolutionEngine is not None

    def test_export_create_evolution_engine(self) -> None:
        """create_evolution_engine 工厂函数可导入。"""
        from evolution import create_evolution_engine
        assert callable(create_evolution_engine)

    def test_export_gap_analyzer(self) -> None:
        """GapAnalyzer 可导入。"""
        from evolution import GapAnalyzer
        assert GapAnalyzer is not None

    def test_export_code_generator(self) -> None:
        """CodeGenerator 可导入。"""
        from evolution import CodeGenerator
        assert CodeGenerator is not None

    def test_export_security_reviewer(self) -> None:
        """SecurityReviewer 可导入。"""
        from evolution import SecurityReviewer
        assert SecurityReviewer is not None

    def test_export_hot_loader(self) -> None:
        """HotLoader 可导入。"""
        from evolution import HotLoader
        assert HotLoader is not None

    def test_export_evolution_log(self) -> None:
        """EvolutionLog 可导入。"""
        from evolution import EvolutionLog
        assert EvolutionLog is not None

    def test_export_rollback_manager(self) -> None:
        """RollbackManager 可导入。"""
        from evolution import RollbackManager
        assert RollbackManager is not None

    def test_export_all_types(self) -> None:
        """所有类型定义可导入。"""
        from evolution import (
            EvolutionStatus,
            FilterLayer,
            GenerationType,
            CapabilityGap,
            FilterResult,
            GeneratedArtifact,
            SecurityReport,
            EvolutionRecord,
            EvolutionResult,
        )
        # 验证都是类/枚举
        assert EvolutionStatus is not None
        assert FilterLayer is not None
        assert GenerationType is not None

    def test___all___completeness(self) -> None:
        """__all__ 包含所有预期的公共接口。"""
        import evolution
        expected = {
            "create_evolution_engine",
            "EvolutionEngine",
            "EvolutionTrigger",
            "GapAnalyzer",
            "CodeGenerator",
            "SecurityReviewer",
            "HotLoader",
            "EvolutionLog",
            "RollbackManager",
            "TriggerMode",
            "TriggerEvent",
            "TriggerResult",
            "EvolutionStatus",
            "FilterLayer",
            "GenerationType",
            "CapabilityGap",
            "FilterResult",
            "GeneratedArtifact",
            "SecurityReport",
            "EvolutionRecord",
            "EvolutionResult",
        }
        actual = set(evolution.__all__)
        missing = expected - actual
        assert not missing, f"__all__ 缺少: {missing}"


# =========================================================================
# Trigger → Engine 真实闭环测试
# =========================================================================


class TestTriggerEngineIntegration:
    """Trigger 与真实 Engine 的集成测试。"""

    @pytest.fixture
    def engine(self, tmp_path: Path) -> EvolutionEngine:
        """创建真实 EvolutionEngine（使用临时目录）。"""
        return create_evolution_engine(
            log_dir=str(tmp_path / "logs"),
            storage_dir=str(tmp_path / "checkpoints"),
            base_path=str(tmp_path),
        )

    @pytest.fixture
    def trigger_auto(self, engine: EvolutionEngine) -> EvolutionTrigger:
        """创建自动模式的触发器（使用真实引擎）。"""
        return EvolutionTrigger(
            evolution_engine=engine,
            mode=TriggerMode.AUTO,
            max_triggers_per_minute=10,
        )

    @pytest.fixture
    def trigger_suggest(self, engine: EvolutionEngine) -> EvolutionTrigger:
        """创建建议模式的触发器（使用真实引擎）。"""
        return EvolutionTrigger(
            evolution_engine=engine,
            mode=TriggerMode.SUGGEST,
        )

    def test_auto_trigger_full_loop(self, trigger_auto: EvolutionTrigger) -> None:
        """自动模式：工具未找到 → 触发 → 引擎执行完整闭环。

        验证 trigger 能正确调用 engine.evolve() 并返回有意义的 TriggerResult。
        """
        result = trigger_auto.check_tool_not_found(
            tool_name="data_analyzer",
            tool_args={"query": "SELECT * FROM data"},
        )

        # 应该触发了进化流程
        assert result.triggered is True
        assert isinstance(result, TriggerResult)

        # 应该记录了触发事件
        history = trigger_auto.get_trigger_history()
        assert len(history) == 1
        assert history[0].trigger_type == "tool_not_found"
        assert "data_analyzer" in history[0].capability

    def test_suggest_trigger_no_execution(
        self, trigger_suggest: EvolutionTrigger,
    ) -> None:
        """建议模式：只返回建议，不执行进化。"""
        result = trigger_suggest.check_tool_not_found(
            tool_name="image_compress",
            tool_args={"quality": 80},
        )

        assert result.triggered is False
        assert result.suggestion is not None
        assert isinstance(result.suggestion, str)
        assert len(result.suggestion) > 0

    def test_capability_gap_triggers_evolution(
        self, trigger_auto: EvolutionTrigger,
    ) -> None:
        """Agent 报告能力缺口 → 触发进化。"""
        result = trigger_auto.check_capability_gap(
            capability_description="需要 PDF 合并能力",
            context={"task_id": "task-456"},
        )

        assert result.triggered is True
        history = trigger_auto.get_trigger_history()
        assert len(history) == 1
        assert history[0].trigger_type == "capability_gap"

    def test_manual_trigger_with_real_engine(
        self, trigger_auto: EvolutionTrigger,
    ) -> None:
        """手动触发使用真实引擎执行。"""
        result = trigger_auto.trigger_evolution(
            capability="JSON 格式化工具",
            context={"priority": "high"},
        )

        assert result.triggered is True
        assert isinstance(result, TriggerResult)

        # 手动触发不受 suggest 模式限制
        history = trigger_auto.get_trigger_history()
        assert len(history) == 1
        assert history[0].trigger_type == "manual"

    def test_multiple_triggers_record_history(
        self, trigger_auto: EvolutionTrigger,
    ) -> None:
        """多次触发累积历史记录。"""
        trigger_auto.check_tool_not_found("tool_1", {})
        trigger_auto.check_capability_gap("能力_2", {"key": "val"})
        trigger_auto.trigger_evolution("能力_3", {})

        history = trigger_auto.get_trigger_history()
        assert len(history) == 3
        # 最新在前
        assert history[0].trigger_type == "manual"
        assert history[1].trigger_type == "capability_gap"
        assert history[2].trigger_type == "tool_not_found"

    def test_trigger_event_has_valid_timestamp(
        self, trigger_auto: EvolutionTrigger,
    ) -> None:
        """触发事件的时间戳有效。"""
        trigger_auto.check_tool_not_found("test_tool", {})
        history = trigger_auto.get_trigger_history()
        assert len(history) == 1

        event = history[0]
        # 时间戳能被解析
        from datetime import datetime
        parsed = datetime.fromisoformat(event.timestamp)
        assert parsed is not None

    def test_trigger_event_to_dict_serializable(
        self, trigger_auto: EvolutionTrigger,
    ) -> None:
        """触发事件可序列化为字典。"""
        trigger_auto.check_tool_not_found("tool_x", {"arg": "val"})
        history = trigger_auto.get_trigger_history()
        event = history[0]

        d = event.to_dict()
        assert isinstance(d, dict)
        assert d["trigger_type"] == "tool_not_found"
        assert d["trigger_id"] == event.trigger_id


# =========================================================================
# Trigger ↔ Engine 状态协调测试
# =========================================================================


class TestTriggerEngineStateCoordination:
    """验证 trigger 与 engine 之间的状态协调。"""

    @pytest.fixture
    def engine(self, tmp_path: Path) -> EvolutionEngine:
        """创建真实引擎。"""
        return create_evolution_engine(
            log_dir=str(tmp_path / "logs"),
            storage_dir=str(tmp_path / "checkpoints"),
            base_path=str(tmp_path),
        )

    def test_engine_status_after_trigger(
        self, engine: EvolutionEngine,
    ) -> None:
        """触发进化后引擎状态最终回到终态。"""
        trigger = EvolutionTrigger(
            evolution_engine=engine,
            mode=TriggerMode.AUTO,
        )
        trigger.check_tool_not_found("some_tool", {})

        # 引擎状态应该是终态之一
        assert engine.get_status() in (
            EvolutionStatus.COMPLETED,
            EvolutionStatus.FAILED,
            EvolutionStatus.ROLLING_BACK,
        )

    def test_rate_limit_with_real_engine(
        self, tmp_path: Path,
    ) -> None:
        """频率限制与真实引擎协同工作。"""
        engine = create_evolution_engine(
            log_dir=str(tmp_path / "logs"),
            storage_dir=str(tmp_path / "checkpoints"),
            base_path=str(tmp_path),
        )
        trigger = EvolutionTrigger(
            evolution_engine=engine,
            max_triggers_per_minute=2,
        )

        # 前两次成功
        r1 = trigger.check_tool_not_found("tool_1", {})
        assert r1.triggered is True

        r2 = trigger.check_tool_not_found("tool_2", {})
        assert r2.triggered is True

        # 第三次被频率限制阻止
        r3 = trigger.check_tool_not_found("tool_3", {})
        assert r3.triggered is False

    def test_suggest_mode_trigger_evolution_bypasses(
        self, tmp_path: Path,
    ) -> None:
        """建议模式下 trigger_evolution 仍然执行（手动触发不受建议模式限制）。"""
        engine = create_evolution_engine(
            log_dir=str(tmp_path / "logs"),
            storage_dir=str(tmp_path / "checkpoints"),
            base_path=str(tmp_path),
        )
        trigger = EvolutionTrigger(
            evolution_engine=engine,
            mode=TriggerMode.SUGGEST,
        )

        # 建议模式下 check_tool_not_found 不执行
        r1 = trigger.check_tool_not_found("tool_a", {})
        assert r1.triggered is False
        assert r1.suggestion is not None

        # 但手动触发仍然执行
        r2 = trigger.trigger_evolution("某能力", {})
        assert r2.triggered is True


# =========================================================================
# 子模块协作测试
# =========================================================================


class TestSubmoduleCollaboration:
    """验证子模块之间的协作。"""

    def test_gap_analyzer_to_filter_result_types(self) -> None:
        """GapAnalyzer 输出与 FilterResult 类型兼容。"""
        analyzer = GapAnalyzer()
        gap = analyzer.analyze_gap("搜索文件能力", {"tool_name": "file_search"})

        assert isinstance(gap, CapabilityGap)
        assert gap.missing_capability == "搜索文件能力"

        filter_result = analyzer.four_layer_filter(gap)
        assert isinstance(filter_result, FilterResult)
        assert filter_result.gap is gap

    def test_code_generator_output_compatible_with_security_reviewer(self) -> None:
        """CodeGenerator 输出与 SecurityReviewer 输入兼容。"""
        generator = CodeGenerator()
        artifact = generator.generate_builtin_tool(
            name="test_tool",
            description="A test tool",
            parameters={
                "type": "object",
                "properties": {"input": {"type": "string"}},
            },
        )

        assert isinstance(artifact, GeneratedArtifact)

        reviewer = SecurityReviewer()
        report = reviewer.review(artifact)

        assert isinstance(report, SecurityReport)

    def test_evolution_result_from_trigger_matches_types(self) -> None:
        """TriggerResult 中的 evolution_result 与 EvolutionResult 类型匹配。"""
        evo_result = EvolutionResult(
            success=True,
            loaded_plugin_name="test_plugin",
            message="成功",
        )
        trigger_result = TriggerResult(
            triggered=True,
            evolution_result=evo_result,
            message="触发完成",
        )

        assert isinstance(trigger_result.evolution_result, EvolutionResult)
        assert trigger_result.evolution_result.loaded_plugin_name == "test_plugin"
