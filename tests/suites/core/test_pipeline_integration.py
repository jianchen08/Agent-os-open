"""Integration test for the pipeline framework."""
import asyncio
from pipeline.types import RouteSignal, StateKeys, ErrorPolicy, create_initial_state
from pipeline.plugin import IInputPlugin, ICorePlugin, IOutputPlugin, PluginContext, PluginResult, OutputResult
from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable
from pipeline.chain import PluginChain
from pipeline.engine import PipelineEngine
from pipeline.registry import PluginRegistry


class MockInputPlugin(IInputPlugin):
    """Mock input plugin for testing."""
    error_policy = ErrorPolicy.ABORT

    @property
    def name(self) -> str:
        return "mock_input"

    @property
    def priority(self) -> int:
        return 0

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult(state_updates={"input_processed": True})


class MockCorePlugin(ICorePlugin):
    """Mock core plugin that simulates LLM call."""
    error_policy = ErrorPolicy.ABORT
    fallback_state = {"raw_result": "fallback"}

    @property
    def name(self) -> str:
        return "llm_call"

    @property
    def priority(self) -> int:
        return 0

    async def execute(self, ctx: PluginContext) -> dict:
        iteration = ctx.state.get("iteration", 1)
        if iteration >= 3:
            return {"raw_result": "Final answer", "task_complete": True}
        return {"raw_result": f"LLM response iter {iteration}", "task_complete": False}


class MockToolCorePlugin(ICorePlugin):
    """Mock core plugin that simulates tool execution."""
    error_policy = ErrorPolicy.ABORT
    fallback_state = {}

    @property
    def name(self) -> str:
        return "tool_execute"

    @property
    def priority(self) -> int:
        return 0

    async def execute(self, ctx: PluginContext) -> dict:
        return {"raw_result": "Tool result", "task_complete": False}


class MockOutputPlugin(IOutputPlugin):
    """Mock output plugin that generates route signals."""
    error_policy = ErrorPolicy.ABORT

    @property
    def name(self) -> str:
        return "mock_output"

    @property
    def priority(self) -> int:
        return 0

    @property
    def route_signals(self) -> list[str]:
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        task_complete = ctx.state.get("task_complete", False)
        if task_complete:
            signal = RouteSignal(route_type="end", reason="task completed")
            return OutputResult(route_signal=signal)
        # next_llm：标记有新输入，避免 apply_route 把 text-only 输出降级为 wait 挂起
        signal = RouteSignal(route_type="next_llm", reason="continue conversation")
        return OutputResult(route_signal=signal, state_updates={"_has_new_llm_input": True})


async def test_engine():
    """Test the full pipeline engine loop."""
    input_table = InputRouteTable([
        InputRouteEntry(name="default", condition="", target="core", plugins=["mock_input"], priority=0),
    ])
    output_table = OutputRouteTable([
        OutputRouteEntry(route_type="next_llm", condition="", priority=0),
        OutputRouteEntry(route_type="end", condition="", priority=1),
    ])

    registry = PluginRegistry()
    registry.register(MockInputPlugin())
    registry.register_core("llm_call", MockCorePlugin())
    registry.register_core("tool_execute", MockToolCorePlugin())
    registry.register(MockOutputPlugin())

    engine = PipelineEngine(input_table, output_table, registry)

    initial = create_initial_state(session_id="test-session")
    result = await engine.run(initial)

    print(f"Iterations: {result[StateKeys.ITERATION]}")
    print(f"Ended: {result[StateKeys.ENDED]}")
    print(f"Last core_type: {result[StateKeys.CORE_TYPE]}")
    print(f"Input processed: {result.get('input_processed', False)}")

    assert result["ended"] is True, "Pipeline should have ended"
    assert result[StateKeys.ITERATION] == 3, f"Expected 3 iterations, got {result[StateKeys.ITERATION]}"
    print("Engine integration test PASSED!")


async def test_error_policy_retry():
    """Test RETRY error policy in PluginChain."""

    class FailingPlugin(IInputPlugin):
        """Plugin that fails then succeeds."""
        error_policy = ErrorPolicy.RETRY
        _attempt = 0

        @property
        def name(self) -> str:
            return "failing_plugin"

        @property
        def priority(self) -> int:
            return 0

        async def execute(self, ctx: PluginContext) -> PluginResult:
            self._attempt += 1
            if self._attempt < 2:
                raise RuntimeError("simulated failure")
            return PluginResult(state_updates={"recovered": True})

    plugin = FailingPlugin()
    chain = PluginChain([plugin])
    ctx = PluginContext(state={}, config={})
    results = await chain.execute(ctx)
    assert results[0].state_updates.get("recovered") is True, "RETRY should recover"
    print("RETRY error policy test PASSED!")


async def test_error_policy_fallback():
    """Test FALLBACK error policy in PluginChain."""

    class FallbackPlugin(IInputPlugin):
        """Plugin that always fails with FALLBACK policy."""
        error_policy = ErrorPolicy.FALLBACK
        fallback_state = {"fallback_value": 42}

        @property
        def name(self) -> str:
            return "fallback_plugin"

        @property
        def priority(self) -> int:
            return 0

        async def execute(self, ctx: PluginContext) -> PluginResult:
            raise RuntimeError("always fails")

    plugin = FallbackPlugin()
    chain = PluginChain([plugin])
    ctx = PluginContext(state={}, config={})
    results = await chain.execute(ctx)
    assert results[0].state_updates.get("fallback_value") == 42, "FALLBACK should use fallback_state"
    print("FALLBACK error policy test PASSED!")


async def main():
    """Run all integration tests."""
    await test_engine()
    await test_error_policy_retry()
    await test_error_policy_fallback()
    print("\n=== ALL INTEGRATION TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
