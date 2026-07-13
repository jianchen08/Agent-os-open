"""PipelineEngine while 循环逻辑单元测试。

测试管道引擎的核心循环：输入路由 → Input 插件链 → Core → Output 插件链 → 输出路由，
覆盖单次迭代、多轮 NEXT_LLM、输入路由直接结束、以及最大迭代数限制等场景。

所有外部依赖（路由表、插件注册表、插件实例等）使用 Mock，
不依赖真实 LLM 或外部服务。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext, PluginResult
from pipeline.types import (
    ErrorPolicy,
    RouteSignal,
    StateKeys,
    create_initial_state,
)


# ---------------------------------------------------------------------------
# Mock 插件定义
# ---------------------------------------------------------------------------


class MockInputPlugin:
    """Mock 输入插件，用于 PipelineEngine 测试。"""

    def __init__(
        self,
        name: str = "mock_input",
        priority: int = 50,
        state_updates: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self._state_updates = state_updates or {}
        self.error_policy = ErrorPolicy.ABORT

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult(state_updates=self._state_updates)


class MockCorePlugin:
    """Mock Core 插件，用于 PipelineEngine 测试。"""

    def __init__(
        self,
        name: str = "mock_core",
        priority: int = 0,
        state_updates: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self._state_updates = state_updates or {}
        self.error_policy = ErrorPolicy.ABORT

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:
        return self._state_updates


class MockOutputPlugin(IOutputPlugin):
    """Mock 输出插件，继承 IOutputPlugin 以便注册表正确识别。

    用于 PipelineEngine 测试。
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(
        self,
        name: str = "mock_output",
        priority: int = 50,
        state_updates: dict[str, Any] | None = None,
        route_signal: RouteSignal | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self._state_updates = state_updates or {}
        self._route_signal = route_signal

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    @property
    def route_signals(self) -> list[str]:
        # 所有 Output 插件均会被执行，route_signals 仅作声明用途
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        return OutputResult(
            state_updates=self._state_updates,
            route_signal=self._route_signal,
        )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _build_engine(
    input_route_table: Any = None,
    output_route_table: Any = None,
    plugin_registry: Any = None,
    max_iterations: int = 100,
) -> Any:
    """构建 PipelineEngine 实例，使用真实或 Mock 组件。"""
    from pipeline.engine import PipelineEngine

    if input_route_table is None:
        input_route_table = MagicMock()
        input_route_table.resolve = MagicMock(return_value=([], "core"))

    if output_route_table is None:
        output_route_table = MagicMock()
        output_route_table.arbitrate = MagicMock(
            return_value=RouteSignal(route_type="end", reason="fallback")
        )

    if plugin_registry is None:
        plugin_registry = MagicMock()
        plugin_registry.get_core = MagicMock(return_value=None)
        plugin_registry.get_output_plugins = MagicMock(return_value=[])
        plugin_registry.get = MagicMock(return_value=None)

    return PipelineEngine(
        input_route_table=input_route_table,
        output_route_table=output_route_table,
        plugin_registry=plugin_registry,
        max_iterations=max_iterations,
    )


# ---------------------------------------------------------------------------
# 测试类
# ---------------------------------------------------------------------------


class TestPipelineEngine:
    """PipelineEngine while 循环逻辑测试。"""

    async def test_engine_single_iteration(self) -> None:
        """单次迭代后 END。

        流程:
          输入路由返回 ([], "core")
          Core 返回 {"raw_result": "hello"}
          Output 返回 OutputResult(route_signal=RouteSignal("end", reason="test"))
          输出路由仲裁为 end
          验证: state["ended"] == True, state["iteration"] == 1
        """
        # 使用真实的路由表和注册表
        from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable
        from pipeline.registry import PluginRegistry

        core_plugin = MockCorePlugin(
            state_updates={StateKeys.RAW_RESULT: "hello"}
        )
        output_plugin = MockOutputPlugin(
            route_signal=RouteSignal(route_type="end", reason="test"),
        )

        registry = PluginRegistry()
        registry.register_core("llm_call", core_plugin)
        registry.register(output_plugin)

        input_table = InputRouteTable([
            InputRouteEntry(name="default", condition="True", target="core", plugins=[], priority=10),
        ])
        output_table = OutputRouteTable([
            OutputRouteEntry(route_type="end", condition="True", priority=1),
        ])

        engine = _build_engine(
            input_route_table=input_table,
            output_route_table=output_table,
            plugin_registry=registry,
        )
        initial_state = create_initial_state()
        result = await engine.run(initial_state)

        assert result[StateKeys.ENDED] is True
        assert result[StateKeys.ITERATION] == 1

    async def test_engine_next_llm_then_end(self) -> None:
        """NEXT_LLM 信号后下一轮继续，第二轮 END。

        流程:
          第一轮: 输出路由返回 next_llm → core_type 设为 llm_call → 继续
          第二轮: 输出路由返回 end → ended=True
          验证: iteration == 2
        """
        from pipeline.route import InputRouteEntry, InputRouteTable
        from pipeline.registry import PluginRegistry

        core_plugin = MockCorePlugin(
            state_updates={StateKeys.RAW_RESULT: "response"}
        )
        output_plugin = MockOutputPlugin(
            route_signal=RouteSignal(route_type="next_llm", reason="continue"),
        )

        registry = PluginRegistry()
        registry.register_core("llm_call", core_plugin)
        # 也需要为 next_llm 路由后注册 tool_execute core（即使不会真正使用）
        registry.register_core("tool_execute", core_plugin)
        registry.register(output_plugin)

        # 第一轮 next_llm，第二轮 end
        arbitrate_calls = 0

        class AlternatingOutputTable:
            def __init__(self):
                self.entries = []

            def arbitrate(self, signals, state):
                nonlocal arbitrate_calls
                arbitrate_calls += 1
                if arbitrate_calls == 1:
                    # 标记有新输入，避免 text-only 的 next_llm 被降级为 wait 挂起
                    state["_has_new_llm_input"] = True
                    # 找到 next_llm 信号
                    for s in signals:
                        if s.route_type == "next_llm":
                            return RouteSignal(route_type="next_llm", reason="continue", target="llm_call")
                    return RouteSignal(route_type="next_llm", reason="continue", target="llm_call")
                return RouteSignal(route_type="end", reason="done")

        input_table = InputRouteTable([
            InputRouteEntry(name="default", condition="True", target="core", plugins=[], priority=10),
        ])

        engine = _build_engine(
            input_route_table=input_table,
            output_route_table=AlternatingOutputTable(),
            plugin_registry=registry,
        )
        initial_state = create_initial_state()
        result = await engine.run(initial_state)

        assert result[StateKeys.ENDED] is True
        assert result[StateKeys.ITERATION] == 2

    async def test_engine_input_route_end(self) -> None:
        """输入路由返回 target="end" 时直接结束。

        流程:
          输入路由返回 target="end"
          验证: 直接结束，不执行 Core 和 Output
        """
        from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteTable
        from pipeline.registry import PluginRegistry

        core_plugin = MockCorePlugin(
            state_updates={StateKeys.RAW_RESULT: "should_not_see"}
        )

        registry = PluginRegistry()
        registry.register_core("llm_call", core_plugin)

        # 输入路由直接返回 end
        input_table = InputRouteTable([
            InputRouteEntry(
                name="stop",
                condition="should_stop == True",
                target="end",
                plugins=[],
                priority=1,
            ),
        ])
        output_table = OutputRouteTable([])

        engine = _build_engine(
            input_route_table=input_table,
            output_route_table=output_table,
            plugin_registry=registry,
        )
        initial_state = create_initial_state(**{StateKeys.SHOULD_STOP: True})
        result = await engine.run(initial_state)

        # 应直接结束
        assert result[StateKeys.ENDED] is True
        # Core 不应执行，raw_result 不应为 "should_not_see"
        assert result.get(StateKeys.RAW_RESULT) != "should_not_see"

    async def test_engine_max_iterations(self) -> None:
        """max_iterations 参数防止无限循环。

        流程:
          设置 max_iterations=10
          输出路由始终返回 next_llm（模拟持续对话）
          循环超过 10 次后强制结束
          验证: ended == True
        """
        from pipeline.route import InputRouteEntry, InputRouteTable
        from pipeline.registry import PluginRegistry

        core_plugin = MockCorePlugin(
            state_updates={StateKeys.RAW_RESULT: "looping"}
        )
        output_plugin = MockOutputPlugin(
            route_signal=RouteSignal(route_type="next_llm", reason="keep_going"),
        )

        registry = PluginRegistry()
        registry.register_core("llm_call", core_plugin)
        registry.register_core("tool_execute", core_plugin)
        registry.register(output_plugin)

        # 输出路由始终返回 next_llm
        class AlwaysNextLLMTable:
            def __init__(self):
                self.entries = []

            def arbitrate(self, signals, state):
                # 标记有新输入，避免 apply_route 把 text-only 的 next_llm
                # 误降级为 wait（生产中由 input 插件注入；测试模拟持续对话场景）
                state["_has_new_llm_input"] = True
                return RouteSignal(route_type="next_llm", reason="keep_going", target="llm_call")

        input_table = InputRouteTable([
            InputRouteEntry(name="default", condition="True", target="core", plugins=[], priority=10),
        ])

        engine = _build_engine(
            input_route_table=input_table,
            output_route_table=AlwaysNextLLMTable(),
            plugin_registry=registry,
            max_iterations=10,
        )
        initial_state = create_initial_state()
        result = await engine.run(initial_state)

        # 强制结束
        assert result[StateKeys.ENDED] is True
        assert result[StateKeys.ITERATION] >= 10

    async def test_engine_state_preserves_across_iterations(self) -> None:
        """跨迭代状态保持。

        流程:
          initial_state 带有 custom_key="first"
          两轮迭代后 custom_key 应仍在 state 中
        """
        from pipeline.route import InputRouteEntry, InputRouteTable
        from pipeline.registry import PluginRegistry

        core_plugin = MockCorePlugin(
            state_updates={StateKeys.RAW_RESULT: "data"}
        )
        output_plugin = MockOutputPlugin(
            route_signal=RouteSignal(route_type="next_llm", reason="continue"),
        )

        registry = PluginRegistry()
        registry.register_core("llm_call", core_plugin)
        registry.register_core("tool_execute", core_plugin)
        registry.register(output_plugin)

        arbitrate_calls = 0

        class TwoRoundTable:
            def __init__(self):
                self.entries = []

            def arbitrate(self, signals, state):
                nonlocal arbitrate_calls
                arbitrate_calls += 1
                if arbitrate_calls == 1:
                    # 标记有新输入，避免 text-only 的 next_llm 被降级为 wait 挂起
                    state["_has_new_llm_input"] = True
                    return RouteSignal(route_type="next_llm", reason="continue", target="llm_call")
                return RouteSignal(route_type="end", reason="done")

        input_table = InputRouteTable([
            InputRouteEntry(name="default", condition="True", target="core", plugins=[], priority=10),
        ])

        engine = _build_engine(
            input_route_table=input_table,
            output_route_table=TwoRoundTable(),
            plugin_registry=registry,
        )
        initial_state = create_initial_state(custom_key="first")
        result = await engine.run(initial_state)

        # 跨迭代状态应保持
        assert result.get("custom_key") == "first"
        assert result[StateKeys.ITERATION] == 2

    async def test_engine_apply_route_next_tool(self) -> None:
        """输出路由 next_tool 信号正确设置 core_type。

        流程:
          第一轮: 输出路由返回 next_tool → core_type 设为 tool_execute
          第二轮: 输出路由返回 end
        """
        from pipeline.route import InputRouteEntry, InputRouteTable
        from pipeline.registry import PluginRegistry

        core_plugin = MockCorePlugin(
            state_updates={StateKeys.RAW_RESULT: "tool_result"}
        )
        output_plugin = MockOutputPlugin(
            route_signal=RouteSignal(
                route_type="next_tool",
                reason="has_tool_calls",
            ),
        )

        registry = PluginRegistry()
        registry.register_core("llm_call", core_plugin)
        registry.register_core("tool_execute", core_plugin)
        registry.register(output_plugin)

        arbitrate_calls = 0

        class ToolThenEndTable:
            def __init__(self):
                self.entries = []

            def arbitrate(self, signals, state):
                nonlocal arbitrate_calls
                arbitrate_calls += 1
                if arbitrate_calls == 1:
                    return RouteSignal(
                        route_type="next_tool",
                        reason="has_tool_calls",
                        target="tool_execute",
                    )
                return RouteSignal(route_type="end", reason="done")

        input_table = InputRouteTable([
            InputRouteEntry(name="default", condition="True", target="core", plugins=[], priority=10),
        ])

        engine = _build_engine(
            input_route_table=input_table,
            output_route_table=ToolThenEndTable(),
            plugin_registry=registry,
        )
        initial_state = create_initial_state()
        result = await engine.run(initial_state)

        assert result[StateKeys.ENDED] is True
        assert result[StateKeys.ITERATION] == 2
        # 第一轮 next_tool 后 core_type 应被设为 tool_execute
        assert result[StateKeys.CORE_TYPE] == "tool_execute"


class TestSuspendAndWaitPendingNotifications:
    """测试 _suspend_and_wait 消费竞态通知队列。"""

    @pytest.mark.asyncio
    async def test_pending_notifications_consumed_on_suspend(self):
        """管道挂起时自动消费挂起期间入队的通知。"""
        import asyncio

        from pipeline.engine import PipelineEngine

        services: dict[str, Any] = {"__test__": True}
        engine = PipelineEngine(
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            plugin_registry=MagicMock(),
            services=services,
        )
        pipeline_id = "test-pipe-123"

        engine._suspended_state = {
            StateKeys.PIPELINE_ID: pipeline_id,
            "user_input": "原始输入",
            "submitted_task_ids": ["child-a", "child-b"],
        }

        state = {
            StateKeys.PIPELINE_ID: pipeline_id,
            "submitted_task_ids": ["child-a", "child-b"],
        }

        # 挂起后异步注入通知：inject_message 入 _inject_queue 并 set wake_event，
        # _suspend_and_wait 唤醒后消息留在队列，由主循环 consume_pending_notifications
        # 统一处理（注入 state + 推送）。
        async def _inject_after_suspend():
            await asyncio.sleep(0.05)
            engine.inject_message("[系统通知] 子任务 'A' failed")
            engine.inject_message("[系统通知] 子任务 'B' completed")

        asyncio.create_task(_inject_after_suspend())
        resumed = await engine._suspend_and_wait(state)

        # 新架构：消息留在队列等 consume 处理，_suspend_and_wait 不消费队列
        assert resumed is True
        assert len(engine._inject_queue) == 2
        assert "子任务 'A'" in engine._inject_queue[0][0]
        assert "子任务 'B'" in engine._inject_queue[1][0]

    @pytest.mark.asyncio
    async def test_no_pending_notifications_normal_suspend(self):
        """无 pending notifications 时正常挂起（手动唤醒）。"""
        import asyncio

        from pipeline.engine import PipelineEngine

        services: dict[str, Any] = {"__test__": True}
        engine = PipelineEngine(
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            plugin_registry=MagicMock(),
            services=services,
        )
        pipeline_id = "test-pipe-456"
        engine._suspended_state = {
            StateKeys.PIPELINE_ID: pipeline_id,
            "user_input": "原始输入",
        }

        state = {StateKeys.PIPELINE_ID: pipeline_id}

        async def _wake_after_delay():
            await asyncio.sleep(0.05)
            engine.wake()

        asyncio.create_task(_wake_after_delay())
        await engine._suspend_and_wait(state)


class TestEnginePublicInterface:
    """测试引擎重构后的公开属性和接口。"""

    def test_pipeline_id_read_write(self):
        """pipeline_id 属性支持读写。"""
        engine = _build_engine()
        original_id = engine.pipeline_id
        assert isinstance(original_id, str)
        assert len(original_id) > 0

        engine.pipeline_id = "custom-id-123"
        assert engine.pipeline_id == "custom-id-123"

        engine.pipeline_id = "another-id-456"
        assert engine.pipeline_id == "another-id-456"

    def test_services_property_readonly(self):
        """services 属性返回构造时传入的服务字典。"""
        svc = {"db": "mock_db", "cache": "mock_cache"}
        from pipeline.engine import PipelineEngine

        engine = PipelineEngine(
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            plugin_registry=MagicMock(),
            services=svc,
        )
        assert engine.services is svc
        assert engine.services["db"] == "mock_db"

    def test_services_default_empty_dict(self):
        """未传 services 时返回空字典。"""
        engine = _build_engine()
        assert isinstance(engine.services, dict)
        assert len(engine.services) == 0

    def test_consecutive_core_errors_read_write(self):
        """consecutive_core_errors 属性支持读写。"""
        engine = _build_engine()
        assert engine.consecutive_core_errors == 0

        engine.consecutive_core_errors = 3
        assert engine.consecutive_core_errors == 3

        engine.consecutive_core_errors = 0
        assert engine.consecutive_core_errors == 0

    def test_max_consecutive_core_errors_readonly(self):
        """max_consecutive_core_errors 属性只读。"""
        engine = _build_engine()
        assert isinstance(engine.max_consecutive_core_errors, int)
        assert engine.max_consecutive_core_errors > 0

    def test_is_running_initially_false(self):
        """引擎初始状态 is_running 为 False。"""
        engine = _build_engine()
        assert engine.is_running is False

    def test_is_suspended_initially_false(self):
        """引擎初始状态 is_suspended 为 False。"""
        engine = _build_engine()
        assert engine.is_suspended is False

    @pytest.mark.asyncio
    async def test_suspend_and_wait_saves_state(self):
        """suspend_and_wait 内部自动保存 state 快照到 _suspended_state。"""
        import asyncio

        from pipeline.engine import PipelineEngine

        engine = PipelineEngine(
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            plugin_registry=MagicMock(),
            services={"__test__": True},
        )

        state = {
            StateKeys.PIPELINE_ID: "test-save-state",
            "user_input": "hello",
            "submitted_task_ids": [],
        }

        async def _wake_after_delay():
            await asyncio.sleep(0.05)
            engine.wake()

        asyncio.create_task(_wake_after_delay())
        result = await engine.suspend_and_wait(state)

        assert engine.is_suspended is False

    @pytest.mark.asyncio
    async def test_resume_from_state(self):
        """resume_from_state 从外部状态恢复管道执行。"""
        from pipeline.engine import PipelineEngine

        engine = PipelineEngine(
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            plugin_registry=MagicMock(),
            services={"__test__": True},
        )

        saved_state = create_initial_state()
        saved_state[StateKeys.ITERATION] = 5
        saved_state[StateKeys.CORE_TYPE] = "llm_call"
        saved_state["user_input"] = "resumed input"
        saved_state["submitted_task_ids"] = []

        mock_route_table = MagicMock()
        mock_route_table.arbitrate = MagicMock(
            return_value=RouteSignal(route_type="end", reason="test_resume")
        )
        engine.output_route_table = mock_route_table

        result = await engine.resume_from_state(saved_state)

        assert result[StateKeys.ENDED] is True

    def test_wake_no_error_when_not_suspended(self):
        """wake() 在引擎未挂起时不报错。"""
        engine = _build_engine()
        engine.wake()

    def test_inject_message_when_not_suspended(self):
        """inject_message() 在引擎未挂起时入队到 _inject_queue。"""
        engine = _build_engine()
        engine.inject_message("test message", source="system")
        assert len(engine._inject_queue) == 1
        assert engine._inject_queue[0] == ("test message", "system")

    def test_consume_pending_notifications(self):
        """drain_inject_queue 原子消费并清空通知队列。"""
        engine = _build_engine()
        engine._inject_queue = [("msg1", "user"), ("msg2", "system"), ("msg3", "user")]
        result = engine.drain_inject_queue()
        assert result == [("msg1", "user"), ("msg2", "system"), ("msg3", "user")]
        assert engine._inject_queue == []

    def test_consume_pending_notifications_empty(self):
        """无通知时返回空列表。"""
        engine = _build_engine()
        result = engine.drain_inject_queue()
        assert result == []
