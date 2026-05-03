"""trigger_types 模块详细测试。

补充 test_trigger.py 中未覆盖的 trigger_types 细节：
- TriggerEvent.to_dict() 序列化
- TriggerResult.not_triggered() 静态工厂方法
- TriggerResult.suggest() 静态工厂方法
- make_timestamp() 时间戳生成
- trigger_types 与 types 的兼容性
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from evolution.trigger_types import TriggerEvent, TriggerResult, make_timestamp
from evolution.types import EvolutionResult


# =========================================================================
# TriggerEvent.to_dict 测试
# =========================================================================


class TestTriggerEventToDict:
    """TriggerEvent.to_dict() 序列化测试。"""

    def test_to_dict_contains_all_fields(self) -> None:
        """to_dict 包含所有字段。"""
        event = TriggerEvent(
            trigger_type="tool_not_found",
            capability="文件搜索",
            context={"tool_name": "file_search"},
            timestamp="2024-01-01T00:00:00+00:00",
            metadata={"source": "agent"},
        )
        d = event.to_dict()

        assert "trigger_id" in d
        assert "trigger_type" in d
        assert "capability" in d
        assert "context" in d
        assert "timestamp" in d
        assert "metadata" in d

    def test_to_dict_values_match(self) -> None:
        """to_dict 返回的值与原始字段一致。"""
        event = TriggerEvent(
            trigger_type="capability_gap",
            capability="数据分析",
            context={"task_id": "t1"},
            timestamp="2024-06-15T12:00:00+00:00",
        )
        d = event.to_dict()

        assert d["trigger_type"] == "capability_gap"
        assert d["capability"] == "数据分析"
        assert d["context"] == {"task_id": "t1"}
        assert d["timestamp"] == "2024-06-15T12:00:00+00:00"
        assert d["metadata"] == {}
        assert d["trigger_id"] == event.trigger_id

    def test_to_dict_with_custom_metadata(self) -> None:
        """to_dict 包含自定义元数据。"""
        event = TriggerEvent(
            trigger_type="manual",
            capability="某能力",
            context={},
            timestamp="2024-01-01T00:00:00+00:00",
            metadata={"priority": "high", "agent_id": "agent_001"},
        )
        d = event.to_dict()
        assert d["metadata"]["priority"] == "high"
        assert d["metadata"]["agent_id"] == "agent_001"

    def test_to_dict_returns_new_dict(self) -> None:
        """to_dict 返回的是新字典，修改不影响原始对象。"""
        event = TriggerEvent(
            trigger_type="test",
            capability="测试",
            context={"key": "value"},
            timestamp="2024-01-01T00:00:00+00:00",
        )
        d = event.to_dict()
        d["capability"] = "被修改了"
        assert event.capability == "测试"


# =========================================================================
# TriggerResult 静态工厂方法测试
# =========================================================================


class TestTriggerResultFactories:
    """TriggerResult 静态工厂方法测试。"""

    def test_not_triggered_factory(self) -> None:
        """not_triggered() 创建正确的未触发结果。"""
        result = TriggerResult.not_triggered("频率限制")

        assert result.triggered is False
        assert result.evolution_result is None
        assert result.message == "频率限制"
        assert result.suggestion is None

    def test_not_triggered_factory_various_messages(self) -> None:
        """not_triggered() 可接受不同消息。"""
        r1 = TriggerResult.not_triggered("工具名为空，跳过触发")
        r2 = TriggerResult.not_triggered("触发频率已达上限")

        assert r1.message == "工具名为空，跳过触发"
        assert r2.message == "触发频率已达上限"
        assert r1.triggered is False
        assert r2.triggered is False

    def test_suggest_factory(self) -> None:
        """suggest() 创建正确的建议模式结果。"""
        result = TriggerResult.suggest(
            message="检测到能力缺口",
            suggestion="建议进化以获取该能力",
        )

        assert result.triggered is False
        assert result.evolution_result is None
        assert result.message == "检测到能力缺口"
        assert result.suggestion == "建议进化以获取该能力"

    def test_suggest_factory_with_empty_values(self) -> None:
        """suggest() 可接受空字符串。"""
        result = TriggerResult.suggest(message="", suggestion="")

        assert result.message == ""
        assert result.suggestion == ""


# =========================================================================
# make_timestamp 测试
# =========================================================================


class TestMakeTimestamp:
    """make_timestamp() 时间戳生成测试。"""

    def test_returns_string(self) -> None:
        """返回字符串类型。"""
        ts = make_timestamp()
        assert isinstance(ts, str)

    def test_valid_iso_format(self) -> None:
        """返回有效的 ISO 8601 格式。"""
        ts = make_timestamp()
        # 应该能被 datetime 解析
        parsed = datetime.fromisoformat(ts)
        assert parsed is not None

    def test_timestamp_is_utc(self) -> None:
        """时间戳使用 UTC 时区。"""
        ts = make_timestamp()
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None

    def test_timestamp_is_recent(self) -> None:
        """时间戳接近当前时间。"""
        before = datetime.now(timezone.utc)
        ts = make_timestamp()
        after = datetime.now(timezone.utc)

        parsed = datetime.fromisoformat(ts)
        assert before <= parsed <= after

    def test_unique_timestamps(self) -> None:
        """连续调用生成不同的时间戳（微妙级差异可能相同，但大概率不同）。"""
        ts_list = [make_timestamp() for _ in range(100)]
        # 至少应该有多个不同的值
        assert len(set(ts_list)) > 1


# =========================================================================
# TriggerEvent 自动生成 ID 测试
# =========================================================================


class TestTriggerEventAutoId:
    """TriggerEvent 自动生成 ID 测试。"""

    def test_auto_generated_id_format(self) -> None:
        """自动生成的 ID 以 'trig_' 开头。"""
        event = TriggerEvent(
            trigger_type="test",
            capability="测试",
            context={},
            timestamp=make_timestamp(),
        )
        assert event.trigger_id.startswith("trig_")

    def test_auto_generated_id_unique(self) -> None:
        """不同事件的 ID 唯一。"""
        events = [
            TriggerEvent(
                trigger_type="test",
                capability=f"能力_{i}",
                context={},
                timestamp=make_timestamp(),
            )
            for i in range(50)
        ]
        ids = [e.trigger_id for e in events]
        assert len(set(ids)) == 50

    def test_custom_id_not_overridden(self) -> None:
        """显式传入的 ID 不被覆盖。"""
        event = TriggerEvent(
            trigger_type="test",
            capability="测试",
            context={},
            timestamp=make_timestamp(),
            trigger_id="custom_id_123",
        )
        assert event.trigger_id == "custom_id_123"


# =========================================================================
# trigger_types 与 types 兼容性测试
# =========================================================================


class TestTriggerTypesCompatibility:
    """trigger_types 与 types 模块的兼容性测试。"""

    def test_trigger_result_accepts_evolution_result(self) -> None:
        """TriggerResult.evolution_result 接受 EvolutionResult 类型。"""
        evo_result = EvolutionResult(
            success=True,
            loaded_plugin_name="test_plugin",
            message="进化成功",
        )
        result = TriggerResult(
            triggered=True,
            evolution_result=evo_result,
            message="已触发",
        )
        assert result.evolution_result is evo_result
        assert result.evolution_result.success is True
        assert result.evolution_result.loaded_plugin_name == "test_plugin"

    def test_trigger_result_accepts_none_evolution_result(self) -> None:
        """TriggerResult.evolution_result 接受 None。"""
        result = TriggerResult(
            triggered=False,
            evolution_result=None,
            message="未触发",
        )
        assert result.evolution_result is None

    def test_trigger_result_with_failed_evolution(self) -> None:
        """TriggerResult 可包含失败的 EvolutionResult。"""
        evo_result = EvolutionResult(
            success=False,
            message="安全审查未通过",
        )
        result = TriggerResult(
            triggered=True,
            evolution_result=evo_result,
            message="进化触发失败",
        )
        assert result.triggered is True
        assert result.evolution_result.success is False
