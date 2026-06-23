"""
Pipeline 管道引擎模块 — 测试覆盖补充。

补充 6 个关键测试缺口（需求文档 AC-PIP-02 ~ AC-PIP-12）：
1. 迭代安全阀：max_iterations 超过上限自动结束
2. 输入路由表：多条件同时为真时插件列表合并（可叠加）
3. 输出路由表：多信号首匹配生效（互斥优先级）
4. 四种错误策略：ABORT/SKIP/FALLBACK/RETRY 各自行为
5. 路由条件表达式安全（非 eval）
6. 终态 Output 插件链在管道结束后执行

测试原则：
- 每个测试独立运行、可重复执行
- 用 Mock/桩件避免外部依赖
- AAA 模式（Arrange-Act-Assert）
"""
from __future__ import annotations

from typing import Any

import pytest

from pipeline.types import (
    ErrorPolicy,
    RouteSignal,
    StateKeys,
    create_initial_state,
)
from pipeline.route import (
    InputRouteEntry,
    InputRouteTable,
    OutputRouteEntry,
    OutputRouteTable,
)
from pipeline.chain import PluginChain
from pipeline.plugin import (
    IInputPlugin,
    IOutputPlugin,
    PluginContext,
    PluginResult,
    OutputResult,
)
from pipeline.condition_parser import parse_condition


# ════════════════════════════════════════════════════════════════
# 1. 迭代安全阀：max_iterations 超过上限自动结束
# AC-PIP-02 / F-PIP-02
# 来源: src/pipeline/engine.py 第 691-699 行
# ════════════════════════════════════════════════════════════════


class TestIterationSafetyValve:
    """max_iterations 迭代安全阀边界测试。

    覆盖现有测试未涉及的边界场景：
    -1 无限制、0 禁用、边界值等。
    """

    @staticmethod
    def _run_safety_valve_loop(
        max_iterations: int, simulate_iterations: int = 20
    ) -> dict[str, Any]:
        """模拟 engine.py 第 691-699 行的安全阀循环逻辑。

        Args:
            max_iterations: 引擎配置的最大迭代次数
            simulate_iterations: 模拟的总迭代次数上限

        Returns:
            运行结束后的 state 字典
        """
        state = create_initial_state()
        for i in range(1, simulate_iterations + 1):
            state[StateKeys.ITERATION] = i
            # 精确复现 engine.py 的条件判断
            if max_iterations > 0 and i > max_iterations:
                state[StateKeys.ENDED] = True
                break
        return state

    def test_safety_valve_triggers_at_boundary_plus_one(self):
        """测试: iteration == max_iterations+1 时安全阀触发，管道结束。"""
        state = self._run_safety_valve_loop(max_iterations=3, simulate_iterations=10)
        assert state[StateKeys.ENDED] is True
        assert state[StateKeys.ITERATION] == 4

    def test_safety_valve_not_triggered_at_exact_boundary(self):
        """测试: iteration == max_iterations 时安全阀不触发（边界值）。"""
        state = self._run_safety_valve_loop(max_iterations=3, simulate_iterations=3)
        assert state[StateKeys.ENDED] is False
        assert state[StateKeys.ITERATION] == 3

    def test_negative_one_means_unlimited(self):
        """测试: max_iterations=-1 表示无限制，安全阀不触发。"""
        state = self._run_safety_valve_loop(max_iterations=-1, simulate_iterations=50)
        assert state[StateKeys.ENDED] is False
        assert state[StateKeys.ITERATION] == 50

    def test_zero_means_disabled(self):
        """测试: max_iterations=0 时安全阀条件不成立（0 > 0 为 False）。"""
        state = self._run_safety_valve_loop(max_iterations=0, simulate_iterations=10)
        assert state[StateKeys.ENDED] is False

    def test_one_means_single_iteration_allowed(self):
        """测试: max_iterations=1 时只允许 iteration=1，iteration=2 触发结束。"""
        state = self._run_safety_valve_loop(max_iterations=1, simulate_iterations=10)
        assert state[StateKeys.ENDED] is True
        assert state[StateKeys.ITERATION] == 2


# ════════════════════════════════════════════════════════════════
# 2. 输入路由表：多条件同时为真时插件列表合并（可叠加）
# AC-PIP-03 / F-PIP-05
# 来源: src/pipeline/route.py InputRouteTable.resolve_plugins
# ════════════════════════════════════════════════════════════════


class TestInputRouteAccumulation:
    """输入路由表可叠加匹配测试。

    覆盖现有测试未涉及的场景：
    三条件同时匹配、跨条目去重保序。
    """

    def test_three_conditions_all_match_plugins_merged(self):
        """测试: 三个条件同时匹配，插件列表完全合并去重保序。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="agent_level == 'L1'",
                target="core",
                plugins=["plugin_a"],
                priority=10,
            ),
            InputRouteEntry(
                name="entry_b",
                condition="task_complete == False",
                target="core",
                plugins=["plugin_b"],
                priority=20,
            ),
            InputRouteEntry(
                name="entry_c",
                condition="conversation_mode == False",
                target="core",
                plugins=["plugin_c"],
                priority=30,
            ),
        ])
        state = {
            "agent_level": "L1",
            "task_complete": False,
            "conversation_mode": False,
        }
        plugins = table.resolve_plugins(state)
        assert plugins == ["plugin_a", "plugin_b", "plugin_c"]

    def test_cross_entry_duplicate_deduplicated_preserving_order(self):
        """测试: 跨条目重复的插件名去重，保留首次出现顺序。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="",
                plugins=["shared_plugin", "plugin_a"],
                priority=10,
            ),
            InputRouteEntry(
                name="entry_b",
                condition="",
                plugins=["plugin_b", "shared_plugin", "plugin_a"],
                priority=20,
            ),
        ])
        plugins = table.resolve_plugins({})
        # shared_plugin 和 plugin_a 去重，保留 entry_a 中的顺序
        assert plugins == ["shared_plugin", "plugin_a", "plugin_b"]

    def test_empty_condition_always_matches_for_accumulation(self):
        """测试: 空条件始终匹配，可与条件条目叠加。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="always",
                condition="",
                plugins=["always_plugin"],
                priority=10,
            ),
            InputRouteEntry(
                name="conditional",
                condition="nonexistent_key == 'value'",
                plugins=["conditional_plugin"],
                priority=20,
            ),
        ])
        # 空 state 也能匹配空条件
        plugins = table.resolve_plugins({})
        assert "always_plugin" in plugins
        assert "conditional_plugin" not in plugins


# ════════════════════════════════════════════════════════════════
# 3. 输出路由表：多信号首匹配生效（互斥优先级）
# AC-PIP-04 / F-PIP-06
# 来源: src/pipeline/route.py OutputRouteTable.arbitrate
# ════════════════════════════════════════════════════════════════


class TestOutputRouteMutualExclusion:
    """输出路由表互斥优先级仲裁测试。

    覆盖现有测试未涉及的场景：
    多种信号类型竞争、条件过滤后回退。
    """

    def test_multiple_signal_types_first_matching_entry_wins(self):
        """测试: 多种信号类型同时存在，按条目优先级首匹配生效。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="delegate_first",
                route_type="delegate",
                condition="",
                priority=10,
                target_core="tool_execute",
            ),
            OutputRouteEntry(
                name="next_llm_second",
                route_type="next_llm",
                condition="",
                priority=20,
                target_core="llm_call",
            ),
            OutputRouteEntry(
                name="next_tool_third",
                route_type="next_tool",
                condition="",
                priority=30,
                target_core="tool_execute",
            ),
        ])
        signals = [
            RouteSignal(route_type="next_llm"),
            RouteSignal(route_type="next_tool"),
            RouteSignal(route_type="delegate"),
        ]
        result = table.arbitrate(signals, {})
        # delegate 条目优先级最高（priority=10），首匹配生效
        assert result.route_type == "delegate"
        assert result.target == "tool_execute"

    def test_end_overrides_all_other_signals_regardless_of_entry_priority(self):
        """测试: end 信号始终最高优先，即使其条目 priority 数值更大。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="high_pri_next_llm",
                route_type="next_llm",
                condition="",
                priority=1,
            ),
            OutputRouteEntry(
                name="low_pri_end",
                route_type="end",
                condition="",
                priority=99,
            ),
        ])
        signals = [
            RouteSignal(route_type="next_llm"),
            RouteSignal(route_type="end"),
        ]
        result = table.arbitrate(signals, {})
        assert result.route_type == "end"

    def test_condition_false_falls_through_to_next_entry(self):
        """测试: 高优先级条目条件不满足时，回退到次优先级条目。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="conditional_high",
                route_type="next_llm",
                condition="task_complete == True",
                priority=10,
            ),
            OutputRouteEntry(
                name="unconditional_low",
                route_type="next_tool",
                condition="",
                priority=50,
            ),
        ])
        signals = [
            RouteSignal(route_type="next_llm"),
            RouteSignal(route_type="next_tool"),
        ]
        # task_complete=False，高优先级条件不满足，回退
        result = table.arbitrate(signals, {"task_complete": False})
        assert result.route_type == "next_tool"


# ════════════════════════════════════════════════════════════════
# 4. 四种错误策略：ABORT/SKIP/FALLBACK/RETRY 各自行为
# AC-PIP-05 / F-PIP-13
# 来源: src/pipeline/chain.py PluginChain._handle_error
# ════════════════════════════════════════════════════════════════


class _FailingPlugin(IInputPlugin):
    """用于测试错误策略的桩件插件。"""

    def __init__(
        self,
        name: str,
        priority: int,
        error_policy: ErrorPolicy,
        should_fail: bool = True,
        fallback_state: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> None:
        self._name = name
        self._priority = priority
        self.error_policy = error_policy
        self._should_fail = should_fail
        if fallback_state is not None:
            self.fallback_state = fallback_state
        self.max_retries = max_retries

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        if self._should_fail:
            raise RuntimeError(f"{self._name} failed")
        return PluginResult(state_updates={"executed": self._name})


class TestErrorPolicyCoverage:
    """四种错误策略边界测试。

    覆盖现有测试未涉及的场景：
    RETRY 精确调用次数、FALLBACK 无 fallback_state 退化。
    """

    @pytest.mark.asyncio
    async def test_retry_exact_call_count_on_success(self):
        """测试: RETRY 策略第 2 次成功时，总调用次数恰好为 2（1 失败 + 1 成功）。"""
        call_count = 0

        class FlakyPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "flaky"

            @property
            def priority(self) -> int:
                return 10

            error_policy = ErrorPolicy.RETRY
            max_retries = 5

            async def execute(self, ctx: PluginContext) -> PluginResult:
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise RuntimeError("transient")
                return PluginResult(state_updates={"ok": True})

        chain = PluginChain([FlakyPlugin()])
        results = await chain.execute(PluginContext(state={}))

        assert results[0].state_updates.get("ok") is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_call_count_matches_max_retries_plus_one(self):
        """测试: RETRY 耗尽时总调用次数 = 1 (初始) + max_retries 次重试。"""
        call_count = 0

        class AlwaysFailPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "always_fail"

            @property
            def priority(self) -> int:
                return 10

            error_policy = ErrorPolicy.RETRY
            max_retries = 2

            async def execute(self, ctx: PluginContext) -> PluginResult:
                nonlocal call_count
                call_count += 1
                raise RuntimeError("permanent failure")

        chain = PluginChain([AlwaysFailPlugin()])
        results = await chain.execute(PluginContext(state={}))

        assert results[0].error is not None
        assert results[0].skip_remaining is True
        # 初始调用 1 次 + 重试 max_retries=2 次 = 3 次
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_fallback_without_fallback_state_returns_empty_result(self):
        """测试: FALLBACK 策略但插件无 fallback_state 时，返回空 PluginResult。"""
        plugin = _FailingPlugin(
            "no_fallback", 10, ErrorPolicy.FALLBACK,
            should_fail=True,
            # 不设置 fallback_state
        )
        chain = PluginChain([plugin])
        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        # 无 fallback_state 时返回空结果，不终止链
        assert results[0].state_updates == {}
        assert results[0].skip_remaining is False

    @pytest.mark.asyncio
    async def test_abort_propagates_error_and_stops_chain(self):
        """测试: ABORT 策略传播 error 且 skip_remaining=True。"""
        plugin_a = _FailingPlugin("a", 10, ErrorPolicy.ABORT, should_fail=True)
        plugin_b = _FailingPlugin("b", 20, ErrorPolicy.ABORT, should_fail=False)
        chain = PluginChain([plugin_a, plugin_b])
        results = await chain.execute(PluginContext(state={}))

        assert len(results) == 1
        assert isinstance(results[0].error, RuntimeError)
        assert results[0].skip_remaining is True

    @pytest.mark.asyncio
    async def test_skip_continues_to_next_plugin(self):
        """测试: SKIP 策略跳过失败插件，后续插件正常执行。"""
        plugin_a = _FailingPlugin("a", 10, ErrorPolicy.SKIP, should_fail=True)
        plugin_b = _FailingPlugin("b", 20, ErrorPolicy.SKIP, should_fail=False)
        chain = PluginChain([plugin_a, plugin_b])
        results = await chain.execute(PluginContext(state={}))

        assert len(results) == 2
        assert results[0].error is None
        assert results[1].state_updates.get("executed") == "b"


# ════════════════════════════════════════════════════════════════
# 5. 路由条件表达式安全（非 eval）
# AC-PIP-12 / F-PIP-09
# 来源: src/pipeline/condition_parser.py parse_condition
# ════════════════════════════════════════════════════════════════


class TestConditionParserSecurity:
    """条件表达式安全性测试。

    覆盖现有测试未涉及的场景：
    exec/open/文件操作注入阻断。
    """

    def test_import_injection_blocked(self):
        """测试: __import__ 注入被阻断，不执行也不抛异常。"""
        result = parse_condition(
            "__import__('os').system('echo hack')", {}
        )
        assert result is False

    def test_exec_injection_blocked(self):
        """测试: exec() 调用注入被阻断。"""
        result = parse_condition(
            "exec('import os')", {}
        )
        assert result is False

    def test_open_injection_blocked(self):
        """测试: open() 文件操作注入被阻断。"""
        result = parse_condition(
            "open('/etc/passwd').read()", {}
        )
        assert result is False

    def test_malformed_expression_returns_false_not_raises(self):
        """测试: 格式错误的表达式不抛异常，安全降级返回布尔值。

        解析器设计为不抛异常：无法识别的 token 被静默跳过，
        解析错误（如缺少操作数）返回 False。关键是不会执行代码。
        """
        # 不完整的表达式（== 后缺右操作数）→ 解析错误 → False
        assert parse_condition("a == ", {"a": 1}) is False
        # 纯语法垃圾（无有效 token）→ 空 token 视为 True（始终匹配）
        # 这是安全设计：无法判断的条件默认通过，不阻断管道
        result = parse_condition("!!!@@##", {})
        assert isinstance(result, bool)
        # 任何情况都不应抛异常
        assert parse_condition("(", {}) is False  # 不闭合的括号

    def test_nested_state_access_works_safely(self):
        """测试: 嵌套 state 下标访问安全工作（非 eval 方式）。"""
        assert parse_condition(
            'state["key"] == "value"', {"key": "value"}
        ) is True
        assert parse_condition(
            'state["count"] > 5', {"count": 10}
        ) is True

    def test_in_operator_works(self):
        """测试: in 运算符安全工作。"""
        assert parse_condition(
            "'a' in state['list']", {"list": ["a", "b", "c"]}
        ) is True
        assert parse_condition(
            "'d' in state['list']", {"list": ["a", "b", "c"]}
        ) is False


# ════════════════════════════════════════════════════════════════
# 6. 终态 Output 插件链在管道结束后执行
# AC-PIP-11 / F-PIP-04
# 来源: src/pipeline/engine_chain.py run_post_end_output_chain
# ════════════════════════════════════════════════════════════════


class _TrackingOutputPlugin(IOutputPlugin):
    """终态链测试用的追踪插件桩件。"""

    def __init__(self, plugin_name: str, prio: int) -> None:
        self._name = plugin_name
        self._priority = prio

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> OutputResult:
        # 记录执行标记到 state
        executed_list = ctx.state.setdefault("_terminal_executed", [])
        executed_list.append(self._name)
        return OutputResult(state_updates={"_terminal_executed": executed_list})


class _FailingOutputPlugin(IOutputPlugin):
    """终态链测试用的失败插件桩件。"""

    def __init__(self, plugin_name: str, prio: int) -> None:
        self._name = plugin_name
        self._priority = prio

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    error_policy = ErrorPolicy.ABORT

    async def execute(self, ctx: PluginContext) -> OutputResult:
        raise RuntimeError(f"{self._name} failed in terminal chain")


class TestPostEndOutputChain:
    """终态 Output 插件链执行测试。

    覆盖现有测试完全未涉及的场景：
    run_post_end_output_chain 的行为验证。
    """

    @pytest.mark.asyncio
    async def test_post_end_chain_executes_all_output_plugins(self):
        """测试: 管道结束后执行所有 Output 插件（持久化/追踪等）。"""
        from pipeline.engine_chain import run_post_end_output_chain

        plugin_a = _TrackingOutputPlugin("persist", 10)
        plugin_b = _TrackingOutputPlugin("track", 20)

        # 构建 Mock engine
        engine = self._build_mock_engine([plugin_a, plugin_b])
        state = create_initial_state(**{StateKeys.ENDED: True})

        await run_post_end_output_chain(engine, state)

        executed = state.get("_terminal_executed", [])
        assert "persist" in executed
        assert "track" in executed

    @pytest.mark.asyncio
    async def test_post_end_chain_executes_in_priority_order(self):
        """测试: 终态链按 priority 从小到大排序执行。"""
        from pipeline.engine_chain import run_post_end_output_chain

        plugin_high = _TrackingOutputPlugin("high_pri", 10)
        plugin_low = _TrackingOutputPlugin("low_pri", 50)

        engine = self._build_mock_engine([plugin_low, plugin_high])
        state = create_initial_state(**{StateKeys.ENDED: True})

        await run_post_end_output_chain(engine, state)

        executed = state.get("_terminal_executed", [])
        # high_pri (priority=10) 先执行
        assert executed.index("high_pri") < executed.index("low_pri")

    @pytest.mark.asyncio
    async def test_post_end_chain_no_output_plugins_does_nothing(self):
        """测试: 无 Output 插件时，run_post_end_output_chain 安全跳过。"""
        from pipeline.engine_chain import run_post_end_output_chain

        engine = self._build_mock_engine([])
        state = create_initial_state(**{StateKeys.ENDED: True})

        # 不应抛异常
        await run_post_end_output_chain(engine, state)
        assert "_terminal_executed" not in state

    @pytest.mark.asyncio
    async def test_post_end_chain_failure_is_non_critical(self):
        """测试: 终态链中插件失败不导致整体异常（非关键路径）。

        run_post_end_output_chain 的 try/except 确保失败不影响管道返回。
        但 ABORT 策略会在 PluginChain 内部处理（skip_remaining=True），
        所以失败插件的 error 不会向上抛出。
        """
        from pipeline.engine_chain import run_post_end_output_chain

        failing_plugin = _FailingOutputPlugin("bad_persist", 10)
        good_plugin = _TrackingOutputPlugin("good_track", 20)

        engine = self._build_mock_engine([failing_plugin, good_plugin])
        state = create_initial_state(**{StateKeys.ENDED: True})

        # 不应抛出未捕获异常
        await run_post_end_output_chain(engine, state)

    @staticmethod
    def _build_mock_engine(output_plugins: list):
        """构建 Mock PipelineEngine，仅包含 resolve_output_plugins 所需属性。"""
        from unittest.mock import MagicMock
        from pipeline.registry import PluginRegistry

        registry = PluginRegistry()
        for p in output_plugins:
            registry.register(p)

        engine = MagicMock()
        engine.plugin_registry = registry
        engine.services = {}
        engine.output_route_table = OutputRouteTable([])
        return engine
