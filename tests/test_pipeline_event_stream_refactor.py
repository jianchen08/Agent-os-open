"""管道事件流重构测试 — Registry 与 MessageBus 单元/集成测试。

覆盖范围：
1. EngineRegistry 单元测试：register_pipeline、revive_pipeline、find_by_tag
2. MessageBus 单元测试：send_pipeline_message 边界条件
3. 集成测试：注册 + 发送完整流程
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.message_types import MessageType, PipelineMessage
from pipeline.message_bus import InjectResult, send_pipeline_message
from pipeline.registry import MAX_TAGS_PER_PIPELINE, PipelineEntry, get_engine_registry


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def _make_mock_engine(pipeline_id: str = "mock-pipeline") -> MagicMock:
    """创建模拟的 PipelineEngine 实例。

    Args:
        pipeline_id: 模拟引擎的管道 ID。

    Returns:
        配置好常用属性的 MagicMock。
    """
    engine = MagicMock()
    engine._pipeline_id = pipeline_id
    engine.is_running = False
    engine.is_suspended = False
    engine.inject_message = MagicMock()
    engine._pending_notifications = []
    engine._wake_event = None
    return engine


def _make_mock_plugin_registry() -> MagicMock:
    """创建模拟的 PluginRegistry 实例。

    Returns:
        配置好 fork() 方法的 MagicMock。
    """
    pr = MagicMock()
    pr.fork.return_value = pr
    return pr


# ---------------------------------------------------------------------------
# Fixture：每个测试前后清理 Registry
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前后清理 Registry，确保测试隔离。"""
    registry = get_engine_registry()
    registry._engines.clear()
    yield
    registry._engines.clear()


# ===========================================================================
# 单元测试：Registry
# ===========================================================================


class TestRegistry:
    """EngineRegistry 单元测试。"""

    def test_register_pipeline_creates_and_registers_engine(self):
        """验证 register_pipeline 创建引擎并注册到 Registry。

        准备 mock 的 input_route_table、output_route_table、plugin_registry，
        调用 register_pipeline 后断言引擎已注册且标签正确。
        """
        registry = get_engine_registry()
        mock_input = MagicMock(name="input_route_table")
        mock_output = MagicMock(name="output_route_table")
        mock_plugin = _make_mock_plugin_registry()

        mock_engine = MagicMock()
        mock_engine._pipeline_id = "test-123"

        with patch("pipeline.engine.PipelineEngine", return_value=mock_engine):
            entry = registry.register_pipeline(
                pipeline_id="test-123",
                tags={"mode": "interactive"},
                input_route_table=mock_input,
                output_route_table=mock_output,
                plugin_registry=mock_plugin,
            )

        assert registry.get("test-123") is not None
        assert entry is not None
        assert entry.engine is not None
        assert entry.tags == {"mode": "interactive"}

    def test_register_pipeline_reuses_existing(self):
        """验证重复注册同一 pipeline_id 时复用已有引擎。

        先注册 pipeline_id="test-456"，再注册同一 ID，
        断言两次返回相同的 entry。
        """
        registry = get_engine_registry()
        mock_engine = _make_mock_engine("test-456")
        first_entry = registry.register("test-456", mock_engine)

        second_entry = registry.register_pipeline(pipeline_id="test-456")

        assert second_entry is first_entry

    def test_register_pipeline_rejects_too_many_tags(self):
        """验证标签数量超过 MAX_TAGS_PER_PIPELINE 时抛出 ValueError。

        构造 MAX_TAGS_PER_PIPELINE + 1 个标签的 dict，
        断言 register_pipeline 抛出 ValueError。
        """
        registry = get_engine_registry()
        too_many_tags = {f"tag_{i}": f"value_{i}" for i in range(MAX_TAGS_PER_PIPELINE + 1)}

        with pytest.raises(ValueError):
            registry.register_pipeline(
                pipeline_id="too-many-tags",
                tags=too_many_tags,
            )

    def test_register_pipeline_returns_none_without_routes(self):
        """验证缺少路由表时返回 None。

        不传 input_route_table，断言返回 None。
        """
        registry = get_engine_registry()

        result = registry.register_pipeline(pipeline_id="no-routes")

        assert result is None

    def test_revive_pipeline_creates_engine(self):
        """验证 revive_pipeline 能从历史恢复引擎。

        Registry 中无 "revive-123"，调用 revive_pipeline 后
        断言引擎已注册。
        """
        registry = get_engine_registry()
        assert registry.get("revive-123") is None

        mock_input = MagicMock(name="input_route_table")
        mock_output = MagicMock(name="output_route_table")
        mock_plugin = _make_mock_plugin_registry()

        mock_engine = MagicMock()
        mock_engine._pipeline_id = "revive-123"

        with patch("pipeline.engine.PipelineEngine", return_value=mock_engine):
            entry = registry.revive_pipeline(
                "revive-123",
                input_route_table=mock_input,
                output_route_table=mock_output,
                plugin_registry=mock_plugin,
            )

        assert entry is not None
        assert registry.get("revive-123") is not None

    def test_revive_pipeline_returns_existing(self):
        """验证引擎已存在时 revive 直接返回已有条目。

        先注册 "revive-456"，再 revive 同一 ID，
        断言返回同一个 entry。
        """
        registry = get_engine_registry()
        mock_engine = _make_mock_engine("revive-456")
        existing_entry = registry.register("revive-456", mock_engine)

        revived_entry = registry.revive_pipeline("revive-456")

        assert revived_entry is existing_entry

    def test_find_by_tag(self):
        """验证标签查询功能。

        注册三个管道并打上不同标签组合，验证单标签和多标签查询结果。
        - find_by_tag("task_id", "task-1") 返回 2 个
        - find_by_tag("mode", "interactive") 返回 2 个
        - find_by_tag("task_id", "task-1", "mode", "interactive") 返回 1 个
        """
        registry = get_engine_registry()

        engine_a = _make_mock_engine("pipeline-a")
        engine_b = _make_mock_engine("pipeline-b")
        engine_c = _make_mock_engine("pipeline-c")

        registry.register("pipeline-a", engine_a, tags={"task_id": "task-1", "mode": "interactive"})
        registry.register("pipeline-b", engine_b, tags={"task_id": "task-1", "mode": "batch"})
        registry.register("pipeline-c", engine_c, tags={"task_id": "task-2", "mode": "interactive"})

        result_task1 = registry.find_by_tag("task_id", "task-1")
        assert len(result_task1) == 2

        result_interactive = registry.find_by_tag("mode", "interactive")
        assert len(result_interactive) == 2

        result_combined = registry.find_by_tag("task_id", "task-1", "mode", "interactive")
        assert len(result_combined) == 1


# ===========================================================================
# 单元测试：message_bus
# ===========================================================================


class TestMessageBus:
    """message_bus 单元测试。"""

    @pytest.mark.asyncio
    async def test_send_pipeline_message_returns_inject_result(self):
        """验证 send_pipeline_message 对未注册管道返回拒绝（I4：不建引擎）。

        mock _find_engine 返回 (None, "")，模拟引擎不存在场景，
        断言返回 InjectResult 且 success == False、method == "rejected"。
        （原测试 patch _try_revive_pipeline，revive 路径已删除。）
        """
        with patch("pipeline.message_bus._find_engine", return_value=(None, "")):
            result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="test", pipeline_id="nonexistent"))

        assert isinstance(result, InjectResult)
        assert result.success is False
        assert result.method == "rejected"

    @pytest.mark.asyncio
    async def test_send_pipeline_message_rejects_empty_pipeline_id(self):
        """验证空 pipeline_id 被拒绝。

        传入空字符串作为 pipeline_id，断言 result.success == False。
        """
        result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="test", pipeline_id=""))

        assert result.success is False


# ===========================================================================
# 集成测试：注册+发送流程
# ===========================================================================


class TestRegisterThenSend:
    """集成测试：注册 + 发送完整流程。"""

    @pytest.mark.asyncio
    async def test_register_then_send_message_flow(self):
        """验证完整的注册+发送流程。

        先通过 register 注册引擎，再 mock _find_engine 返回该引擎，
        调用 send_pipeline_message 发消息，
        断言 inject_message 被调用且 result.success == True。
        """
        registry = get_engine_registry()
        mock_engine = _make_mock_engine("flow-123")
        mock_engine.is_running = True
        mock_engine.is_suspended = False
        registry.register("flow-123", mock_engine)

        with patch(
            "pipeline.message_bus._find_engine",
            return_value=(mock_engine, "running"),
        ):
            result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="hello", pipeline_id="flow-123"))

        mock_engine.inject_message.assert_called_once()
        assert result.success is True


# ===========================================================================
# run_once 测试：send+等结束+读 state+可选 stop 包装
# ===========================================================================


class TestRunOnce:
    """run_once 同步执行拿结果的单元测试。

    run_once 不负责 register（持有者职责）。测试预先注册 mock entry。
    """

    @pytest.mark.asyncio
    async def test_run_once_returns_state_after_send(self):
        """已注册管道 → send 成功 → 等结束 → 返回 last_state → 默认 stop。"""
        from pipeline.message_bus import run_once

        mock_engine = MagicMock()
        mock_engine.pipeline_id = "run-once-1"
        mock_engine.last_state = {"raw_result": "done"}
        mock_engine.is_idle = True
        mock_engine.is_running = False
        mock_engine.is_suspended = False

        registry = get_engine_registry()
        registry._engines.clear()
        registry.register("run-once-1", mock_engine)
        try:
            with (
                patch("pipeline.message_bus.send_pipeline_message", new_callable=AsyncMock) as mock_send,
                patch("pipeline.message_bus.stop", new_callable=AsyncMock) as mock_stop,
            ):
                mock_send.return_value = InjectResult(success=True, method="start", pipeline_id="run-once-1")
                result, state = await run_once(
                    PipelineMessage(type=MessageType.CHAT, content="hi", pipeline_id="run-once-1"),
                )

            assert result.success is True
            assert state == {"raw_result": "done"}
            mock_send.assert_awaited_once()
            mock_stop.assert_awaited_once()  # cleanup=True 默认调 stop
        finally:
            registry._engines.clear()

    @pytest.mark.asyncio
    async def test_run_once_no_cleanup_skips_stop(self):
        """cleanup=False → 不调 stop（复用场景）。"""
        from pipeline.message_bus import run_once

        mock_engine = MagicMock()
        mock_engine.pipeline_id = "run-once-2"
        mock_engine.last_state = {"iteration": 5}
        mock_engine.is_idle = True
        mock_engine.is_running = False
        mock_engine.is_suspended = False

        registry = get_engine_registry()
        registry._engines.clear()
        registry.register("run-once-2", mock_engine)
        try:
            with (
                patch("pipeline.message_bus.send_pipeline_message", new_callable=AsyncMock) as mock_send,
                patch("pipeline.message_bus.stop", new_callable=AsyncMock) as mock_stop,
            ):
                mock_send.return_value = InjectResult(success=True, method="start", pipeline_id="run-once-2")
                result, state = await run_once(
                    PipelineMessage(type=MessageType.CHAT, content="hi", pipeline_id="run-once-2"),
                    cleanup=False,
                )

            assert result.success is True
            assert state == {"iteration": 5}
            mock_stop.assert_not_awaited()  # cleanup=False 不调 stop
        finally:
            registry._engines.clear()

    @pytest.mark.asyncio
    async def test_run_once_send_rejected_returns_empty_state(self):
        """send 被拒绝（如未注册）→ 返回 (rejected, 空字典)。"""
        from pipeline.message_bus import run_once

        registry = get_engine_registry()
        registry._engines.clear()
        try:
            # 不注册任何引擎，send 会拒绝
            with patch("pipeline.message_bus.send_pipeline_message", new_callable=AsyncMock) as mock_send:
                mock_send.return_value = InjectResult(
                    success=False, error="管道未注册", method="rejected", pipeline_id="ghost-1",
                )
                result, state = await run_once(
                    PipelineMessage(type=MessageType.CHAT, content="hi", pipeline_id="ghost-1"),
                )

            assert result.success is False
            assert result.method == "rejected"
            assert state == {}
        finally:
            registry._engines.clear()

    @pytest.mark.asyncio
    async def test_send_to_unregistered_pipeline_fails(self):
        """验证发消息给未注册管道失败（I4：send 不建引擎，直接拒绝）。

        不注册引擎，直接 send_pipeline_message，
        断言 result.success == False、method == "rejected"。
        （原测试 patch _try_revive_pipeline，revive 路径已删除。）
        """
        with patch("pipeline.message_bus._find_engine", return_value=(None, "")):
            result = await send_pipeline_message(PipelineMessage(type=MessageType.CHAT, content="hello", pipeline_id="unregistered-pipeline"))

        assert result.success is False
        assert result.method == "rejected"
