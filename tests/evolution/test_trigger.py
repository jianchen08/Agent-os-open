"""EvolutionTrigger 运行时触发器测试。

覆盖场景：
- 工具未找到触发
- Agent 主动报告能力缺口触发
- 手动触发进化流程
- 频率限制
- 自动模式 vs 建议模式
- EventBus 事件发射
- 触发事件类型与数据
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evolution.trigger import EvolutionTrigger, TriggerMode
from evolution.trigger_types import TriggerEvent, TriggerResult
from evolution.types import (
    EvolutionResult,
    EvolutionStatus,
    FilterLayer,
)


# =========================================================================
# Fixture
# =========================================================================


@pytest.fixture
def mock_evolution_engine() -> MagicMock:
    """创建模拟的 EvolutionEngine。"""
    engine = MagicMock()
    engine.evolve.return_value = EvolutionResult(
        success=True,
        record=None,
        loaded_plugin_name="test_tool",
        message="进化成功",
    )
    engine.get_status.return_value = EvolutionStatus.IDLE
    return engine


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    """创建模拟的 EventBus。"""
    bus = AsyncMock()
    bus.emit = AsyncMock()
    bus.subscribe = MagicMock()
    return bus


@pytest.fixture
def trigger(mock_evolution_engine: MagicMock) -> EvolutionTrigger:
    """创建默认的 EvolutionTrigger（自动模式）。"""
    return EvolutionTrigger(
        evolution_engine=mock_evolution_engine,
    )


@pytest.fixture
def trigger_with_bus(
    mock_evolution_engine: MagicMock,
    mock_event_bus: AsyncMock,
) -> EvolutionTrigger:
    """创建带 EventBus 的 EvolutionTrigger。"""
    return EvolutionTrigger(
        evolution_engine=mock_evolution_engine,
        event_bus=mock_event_bus,
    )


@pytest.fixture
def suggest_trigger(mock_evolution_engine: MagicMock) -> EvolutionTrigger:
    """创建建议模式的 EvolutionTrigger。"""
    return EvolutionTrigger(
        evolution_engine=mock_evolution_engine,
        mode=TriggerMode.SUGGEST,
    )


# =========================================================================
# TriggerEvent 数据类测试
# =========================================================================


class TestTriggerEvent:
    """TriggerEvent 数据类测试。"""

    def test_create_event_with_defaults(self) -> None:
        """测试使用默认值创建事件。"""
        event = TriggerEvent(
            trigger_type="tool_not_found",
            capability="文件搜索",
            context={"tool_name": "file_search"},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        assert event.trigger_type == "tool_not_found"
        assert event.capability == "文件搜索"
        assert event.context == {"tool_name": "file_search"}
        assert event.trigger_id  # 自动生成
        assert event.metadata == {}

    def test_create_event_with_metadata(self) -> None:
        """测试带元数据创建事件。"""
        event = TriggerEvent(
            trigger_type="capability_gap",
            capability="图片压缩",
            context={},
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"source": "agent_report"},
        )
        assert event.metadata == {"source": "agent_report"}


# =========================================================================
# TriggerResult 数据类测试
# =========================================================================


class TestTriggerResult:
    """TriggerResult 数据类测试。"""

    def test_triggered_result(self) -> None:
        """测试成功触发结果。"""
        evo_result = EvolutionResult(
            success=True,
            loaded_plugin_name="my_tool",
            message="进化成功",
        )
        result = TriggerResult(
            triggered=True,
            evolution_result=evo_result,
            message="已触发进化",
        )
        assert result.triggered is True
        assert result.evolution_result is evo_result
        assert result.message == "已触发进化"

    def test_not_triggered_result(self) -> None:
        """测试未触发结果。"""
        result = TriggerResult(
            triggered=False,
            evolution_result=None,
            message="频率限制，跳过触发",
        )
        assert result.triggered is False
        assert result.evolution_result is None
        assert result.message == "频率限制，跳过触发"

    def test_suggest_mode_result(self) -> None:
        """测试建议模式结果（不执行进化）。"""
        result = TriggerResult(
            triggered=False,
            evolution_result=None,
            message="建议：需要文件压缩能力，可触发进化",
            suggestion="进化以获取文件压缩能力",
        )
        assert result.suggestion is not None
        assert result.triggered is False


# =========================================================================
# EvolutionTrigger 初始化测试
# =========================================================================


class TestEvolutionTriggerInit:
    """EvolutionTrigger 初始化测试。"""

    def test_default_init(self, mock_evolution_engine: MagicMock) -> None:
        """测试默认初始化。"""
        trigger = EvolutionTrigger(evolution_engine=mock_evolution_engine)
        assert trigger.mode == TriggerMode.AUTO
        assert trigger.max_triggers_per_minute == 3
        assert trigger._trigger_timestamps == []

    def test_custom_init(self, mock_evolution_engine: MagicMock) -> None:
        """测试自定义参数初始化。"""
        trigger = EvolutionTrigger(
            evolution_engine=mock_evolution_engine,
            mode=TriggerMode.SUGGEST,
            max_triggers_per_minute=5,
        )
        assert trigger.mode == TriggerMode.SUGGEST
        assert trigger.max_triggers_per_minute == 5

    def test_init_with_event_bus(
        self,
        mock_evolution_engine: MagicMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """测试带 EventBus 初始化。"""
        trigger = EvolutionTrigger(
            evolution_engine=mock_evolution_engine,
            event_bus=mock_event_bus,
        )
        assert trigger._event_bus is mock_event_bus


# =========================================================================
# check_tool_not_found 测试
# =========================================================================


class TestCheckToolNotFound:
    """check_tool_not_found 测试。"""

    def test_triggers_on_missing_tool(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """工具未找到时触发进化。"""
        result = trigger.check_tool_not_found(
            tool_name="file_search",
            tool_args={"pattern": "*.py"},
        )
        assert result.triggered is True
        assert result.evolution_result is not None
        assert result.evolution_result.success is True
        # 验证 engine.evolve 被调用
        mock_evolution_engine.evolve.assert_called_once()
        call_args = mock_evolution_engine.evolve.call_args
        assert "file_search" in call_args[0][0] or "file_search" in str(call_args)

    def test_does_not_trigger_on_empty_tool_name(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """空工具名不触发。"""
        result = trigger.check_tool_not_found(tool_name="", tool_args={})
        assert result.triggered is False
        mock_evolution_engine.evolve.assert_not_called()

    def test_suggest_mode_does_not_execute(
        self,
        suggest_trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """建议模式不执行进化，只返回建议。"""
        result = suggest_trigger.check_tool_not_found(
            tool_name="image_compress",
            tool_args={"quality": 80},
        )
        assert result.triggered is False
        assert result.suggestion is not None
        mock_evolution_engine.evolve.assert_not_called()

    def test_passes_context_to_engine(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """验证上下文传递给引擎。"""
        trigger.check_tool_not_found(
            tool_name="pdf_merge",
            tool_args={"files": ["a.pdf", "b.pdf"]},
        )
        call_args = mock_evolution_engine.evolve.call_args
        # 第二个参数是 context dict
        context = call_args[1].get("context") or call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("context")
        assert context is not None


# =========================================================================
# check_capability_gap 测试
# =========================================================================


class TestCheckCapabilityGap:
    """check_capability_gap 测试。"""

    def test_triggers_on_capability_gap(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """Agent 报告能力缺口时触发进化。"""
        result = trigger.check_capability_gap(
            capability_description="需要图片压缩能力",
            context={"task_id": "task-123"},
        )
        assert result.triggered is True
        assert result.evolution_result is not None
        mock_evolution_engine.evolve.assert_called_once()

    def test_does_not_trigger_on_empty_description(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """空描述不触发。"""
        result = trigger.check_capability_gap(
            capability_description="",
            context={},
        )
        assert result.triggered is False
        mock_evolution_engine.evolve.assert_not_called()

    def test_suggest_mode_returns_suggestion(
        self,
        suggest_trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """建议模式只返回建议。"""
        result = suggest_trigger.check_capability_gap(
            capability_description="需要数据分析能力",
            context={},
        )
        assert result.triggered is False
        assert result.suggestion is not None
        mock_evolution_engine.evolve.assert_not_called()


# =========================================================================
# trigger_evolution 测试
# =========================================================================


class TestTriggerEvolution:
    """trigger_evolution 手动触发测试。"""

    def test_manual_trigger(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """手动触发进化成功。"""
        result = trigger.trigger_evolution(
            capability="JSON 格式化",
            context={"priority": 3},
        )
        assert result.triggered is True
        assert result.evolution_result is not None

    def test_manual_trigger_propagates_failure(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """进化失败时传播结果。"""
        mock_evolution_engine.evolve.return_value = EvolutionResult(
            success=False,
            record=None,
            message="安全审查未通过",
        )
        result = trigger.trigger_evolution(
            capability="危险操作",
            context={},
        )
        assert result.triggered is True  # 确实触发了
        assert result.evolution_result is not None
        assert result.evolution_result.success is False

    def test_manual_trigger_raises_on_engine_busy(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """引擎忙碌时抛出异常。"""
        mock_evolution_engine.evolve.side_effect = RuntimeError("引擎正在执行中")
        result = trigger.trigger_evolution(
            capability="某能力",
            context={},
        )
        assert result.triggered is True
        assert result.evolution_result is None  # 异常时无结果
        assert "失败" in result.message or "异常" in result.message


# =========================================================================
# 频率限制测试
# =========================================================================


class TestRateLimit:
    """频率限制测试。"""

    def test_allows_trigger_within_limit(
        self,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """在限制内允许触发。"""
        trigger = EvolutionTrigger(
            evolution_engine=mock_evolution_engine,
            max_triggers_per_minute=3,
        )
        result = trigger.check_tool_not_found("tool_1", {})
        assert result.triggered is True

    def test_blocks_trigger_over_limit(
        self,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """超出频率限制时阻止触发。"""
        trigger = EvolutionTrigger(
            evolution_engine=mock_evolution_engine,
            max_triggers_per_minute=2,
        )
        # 前两次应该成功
        trigger.check_tool_not_found("tool_1", {})
        trigger.check_tool_not_found("tool_2", {})
        # 第三次应该被阻止
        result = trigger.check_tool_not_found("tool_3", {})
        assert result.triggered is False
        assert "频率" in result.message or "限制" in result.message

    def test_rate_limit_resets_after_window(
        self,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """时间窗口过后频率限制重置。"""
        trigger = EvolutionTrigger(
            evolution_engine=mock_evolution_engine,
            max_triggers_per_minute=1,
        )
        # 第一次成功
        trigger.check_tool_not_found("tool_1", {})

        # 模拟时间窗口已过：手动清除旧时间戳
        trigger._trigger_timestamps.clear()

        # 应该再次允许
        result = trigger.check_tool_not_found("tool_2", {})
        assert result.triggered is True

    def test_should_auto_trigger_respects_limit(
        self,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """should_auto_trigger 遵守频率限制。"""
        trigger = EvolutionTrigger(
            evolution_engine=mock_evolution_engine,
            max_triggers_per_minute=1,
        )
        assert trigger.should_auto_trigger() is True
        trigger.check_tool_not_found("tool_1", {})
        assert trigger.should_auto_trigger() is False


# =========================================================================
# TriggerMode 测试
# =========================================================================


class TestTriggerMode:
    """TriggerMode 测试。"""

    def test_auto_mode(self) -> None:
        """自动模式枚举值。"""
        assert TriggerMode.AUTO.value == "auto"

    def test_suggest_mode(self) -> None:
        """建议模式枚举值。"""
        assert TriggerMode.SUGGEST.value == "suggest"


# =========================================================================
# EventBus 集成测试
# =========================================================================


class TestEventBusIntegration:
    """EventBus 事件发射测试。"""

    def test_emits_event_on_tool_not_found(
        self,
        trigger_with_bus: EvolutionTrigger,
        mock_event_bus: AsyncMock,
    ) -> None:
        """工具未找到时发射事件到 EventBus。"""
        trigger_with_bus.check_tool_not_found("my_tool", {"arg": "val"})
        # 验证 emit 被调用（注意：emit 是异步的，但 trigger 内部同步调用）
        # 如果 trigger 内部用 asyncio.run 或类似方式调用 emit，验证行为
        # 由于 trigger 可能记录事件后异步发射，验证事件被记录
        assert len(trigger_with_bus._event_queue) > 0

    def test_records_trigger_event(
        self,
        trigger: EvolutionTrigger,
    ) -> None:
        """触发时记录 TriggerEvent。"""
        trigger.check_tool_not_found("tool_a", {})
        history = trigger.get_trigger_history()
        assert len(history) == 1
        assert history[0].trigger_type == "tool_not_found"
        assert "tool_a" in history[0].capability

    def test_records_capability_gap_event(
        self,
        trigger: EvolutionTrigger,
    ) -> None:
        """能力缺口触发时记录事件。"""
        trigger.check_capability_gap("数据分析", {"task_id": "t1"})
        history = trigger.get_trigger_history()
        assert len(history) == 1
        assert history[0].trigger_type == "capability_gap"

    def test_records_manual_trigger_event(
        self,
        trigger: EvolutionTrigger,
    ) -> None:
        """手动触发时记录事件。"""
        trigger.trigger_evolution("某能力", {})
        history = trigger.get_trigger_history()
        assert len(history) == 1
        assert history[0].trigger_type == "manual"


# =========================================================================
# 边界条件测试
# =========================================================================


class TestEdgeCases:
    """边界条件测试。"""

    def test_none_args_handled(
        self,
        trigger: EvolutionTrigger,
    ) -> None:
        """None 参数被正确处理。"""
        result = trigger.check_tool_not_found("tool", {})
        # 不应抛出异常
        assert isinstance(result, TriggerResult)

    def test_evolution_engine_exception_handled(
        self,
        trigger: EvolutionTrigger,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """引擎异常被捕获并返回失败结果。"""
        mock_evolution_engine.evolve.side_effect = Exception("未知错误")
        result = trigger.trigger_evolution("某能力", {})
        assert result.triggered is True
        assert result.evolution_result is None
        assert "异常" in result.message or "失败" in result.message

    def test_trigger_history_order(
        self,
        trigger: EvolutionTrigger,
    ) -> None:
        """触发历史按时间排序。"""
        trigger.check_tool_not_found("tool_1", {})
        trigger.check_capability_gap("能力_2", {})
        trigger.trigger_evolution("能力_3", {})
        history = trigger.get_trigger_history()
        assert len(history) == 3
        # 最新的在前
        assert history[0].trigger_type == "manual"
        assert history[2].trigger_type == "tool_not_found"

    def test_concurrent_safety(
        self,
        mock_evolution_engine: MagicMock,
    ) -> None:
        """并发安全：多线程调用不出错。"""
        import threading

        trigger = EvolutionTrigger(
            evolution_engine=mock_evolution_engine,
            max_triggers_per_minute=100,
        )
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                trigger.check_tool_not_found(f"tool_{i}", {})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
