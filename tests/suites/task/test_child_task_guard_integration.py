"""子任务守护集成测试。

模拟完整管道循环验证 ChildTaskGuard + TaskWorker 协作：
1. LLM 输出纯文本 + 有子任务 → 管道挂起
2. 子任务完成 → TaskWorker resume → 管道继续
3. LLM 有工具调用 → 不挂起，正常流转
4. 无子任务 → 不挂起，正常流转
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.engine import PipelineEngine
from pipeline.plugin import (
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    OutputResult,
    PluginContext,
    PluginResult,
)
from pipeline.registry import PluginRegistry
from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable
from pipeline.types import ErrorPolicy, RouteSignal
from plugins.output.child_task_guard import ChildTaskGuard


class PassthroughInputPlugin(IInputPlugin):
    """透传 Input 插件。"""
    error_policy = ErrorPolicy.SKIP

    @property
    def name(self) -> str:
        return "passthrough_input"

    @property
    def priority(self) -> int:
        return 0

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult(state_updates={})


class TextOnlyCorePlugin(ICorePlugin):
    """模拟 LLM 只输出纯文本（无工具调用）。"""
    error_policy = ErrorPolicy.ABORT
    fallback_state = {}

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = responses or ["我在等子任务完成", "子任务还没完吗", "好的收到结果了"]
        self._call_count = 0

    @property
    def name(self) -> str:
        return "llm_call"

    @property
    def priority(self) -> int:
        return 0

    async def execute(self, ctx: PluginContext) -> dict:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return {"raw_result": self._responses[idx], "raw_tool_calls": []}


class ToolCallCorePlugin(ICorePlugin):
    """模拟 LLM 输出工具调用。"""
    error_policy = ErrorPolicy.ABORT
    fallback_state = {}

    @property
    def name(self) -> str:
        return "llm_call"

    @property
    def priority(self) -> int:
        return 0

    async def execute(self, ctx: PluginContext) -> dict:
        return {
            "raw_result": "",
            "raw_tool_calls": [{"function": {"name": "task_manage", "arguments": "{}"}}],
        }


class EndAfterResumePlugin(IOutputPlugin):
    """Output 插件：resume 后第一轮就 end。"""
    error_policy = ErrorPolicy.SKIP

    def __init__(self) -> None:
        self._suspended_before = False

    @property
    def name(self) -> str:
        return "end_after_resume"

    @property
    def priority(self) -> int:
        return 50

    @property
    def route_signals(self) -> list[str]:
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        core_type = ctx.state.get("core_type", "")
        if core_type != "llm_call":
            return OutputResult()

        if ctx.state.get("raw_tool_calls"):
            return OutputResult(route_signal=RouteSignal(route_type="next_llm"))

        if ctx.state.get("raw_result"):
            return OutputResult(route_signal=RouteSignal(route_type="end", reason="done"))

        return OutputResult()


def _make_task_service_with_children(children_statuses: list[str]) -> MagicMock:
    """创建有指定状态子任务的 TaskService mock。"""
    svc = MagicMock()
    subtasks = []
    for s in children_statuses:
        st = MagicMock()
        st.status.value = s
        subtasks.append(st)
    svc.list_subtasks.return_value = subtasks
    return svc


def _make_timer_manager() -> MagicMock:
    """创建 TimerManager mock。"""
    mgr = MagicMock()
    mgr.reset_timer = AsyncMock(return_value=None)
    return mgr


def _build_engine(
    core_plugin: ICorePlugin,
    guard_config: dict | None = None,
    services: dict | None = None,
) -> PipelineEngine:
    """构建包含 ChildTaskGuard 的测试管道。"""
    input_table = InputRouteTable([
        InputRouteEntry(
            name="default", condition="True", target="core",
            plugins=["passthrough_input"], priority=0,
        ),
    ])
    output_table = OutputRouteTable([
        OutputRouteEntry(route_type="wait", condition="", priority=1),
        OutputRouteEntry(route_type="next_llm", condition="", priority=2),
        OutputRouteEntry(route_type="end", condition="", priority=3),
    ])

    registry = PluginRegistry()
    registry.register(PassthroughInputPlugin())
    registry.register_core("llm_call", core_plugin)
    registry.register(ChildTaskGuard(guard_config or {"priority": 28}))
    registry.register(EndAfterResumePlugin())

    return PipelineEngine(
        input_route_table=input_table,
        output_route_table=output_table,
        plugin_registry=registry,
        services=services or {},
    )


# ── 集成测试 ──


class TestGuardIntegration:
    """ChildTaskGuard 与管道引擎的集成测试。"""

    @pytest.mark.asyncio
    async def test_no_children_pipeline_ends_normally(self):
        """无子任务时管道正常结束，不挂起。"""
        task_svc = _make_task_service_with_children([])
        timer_mgr = _make_timer_manager()
        core = TextOnlyCorePlugin(["完成了"])
        services = {"task_service": task_svc, "timer_manager": timer_mgr}
        engine = _build_engine(core, services=services)

        result = await engine.run(
            user_input="测试",
            agent_config=None,
            task_id="task-001",
        )

        assert result.get("ended") is True
        assert not engine.is_suspended

    @pytest.mark.asyncio
    async def test_has_tool_calls_not_suspended(self):
        """有工具调用时不挂起。"""
        task_svc = _make_task_service_with_children(["running"])
        timer_mgr = _make_timer_manager()
        core = ToolCallCorePlugin()
        services = {"task_service": task_svc, "timer_manager": timer_mgr}
        engine = _build_engine(core, services=services)

        result = await engine.run(
            user_input="测试",
            agent_config=None,
            task_id="task-001",
        )

        assert result.get("ended") is True
        assert not engine.is_suspended

    @pytest.mark.asyncio
    async def test_active_children_suspends_pipeline(self):
        """有活跃子任务时管道挂起。"""
        task_svc = _make_task_service_with_children(["running"])
        timer_mgr = _make_timer_manager()
        core = TextOnlyCorePlugin(["等待中"])
        services = {"task_service": task_svc, "timer_manager": timer_mgr}
        engine = _build_engine(core, services=services)

        result = await engine.run(
            user_input="测试",
            agent_config=None,
            task_id="task-001",
        )

        assert engine.is_suspended
        assert result.get("iteration", 0) == 1

    @pytest.mark.asyncio
    async def test_resume_after_children_complete(self):
        """子任务完成后 resume 管道，管道继续运行到结束。"""
        task_svc_running = _make_task_service_with_children(["running"])
        timer_mgr = _make_timer_manager()

        call_count = 0

        class CountingCore(ICorePlugin):
            error_policy = ErrorPolicy.ABORT
            fallback_state = {}

            @property
            def name(self):
                return "llm_call"

            @property
            def priority(self):
                return 0

            async def execute(self, ctx):
                nonlocal call_count
                call_count += 1
                return {"raw_result": f"第{call_count}轮", "raw_tool_calls": []}

        core = CountingCore()
        services = {"task_service": task_svc_running, "timer_manager": timer_mgr}
        engine = _build_engine(core, services=services)

        await engine.run(
            user_input="测试",
            agent_config=None,
            task_id="task-001",
        )

        assert engine.is_suspended
        assert call_count == 1

        task_svc_running.list_subtasks.return_value = []

        resumed = await engine.resume()

        assert resumed.get("ended") is True
        assert not engine.is_suspended
        assert call_count == 2
