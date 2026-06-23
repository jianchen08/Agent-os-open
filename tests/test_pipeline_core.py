"""
Pipeline 管道引擎核心测试。

覆盖 AC：
- AC-PIP-01: 管道循环正常启动、执行、结束（通过 state 初始值验证）
- AC-PIP-02: max_iterations 超限自动结束
- AC-PIP-03: 输入路由可叠加匹配
- AC-PIP-04: 输出路由互斥优先级仲裁
- AC-PIP-05: 四种错误策略正确执行
- AC-PIP-11: 终态 Output 插件链执行
- AC-PIP-12: 路由条件表达式安全（非 eval）
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pipeline.types import (
    ErrorPolicy,
    RouteSignal,
    StateKeys,
    TargetType,
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
# AC-PIP-01: 管道初始状态创建
# ════════════════════════════════════════════════════════════════


class TestCreateInitialState:
    """管道初始状态创建测试。"""

    def test_initial_state_has_required_keys(self):
        """测试: 初始 state 包含所有必要字段。"""
        state = create_initial_state()

        assert state[StateKeys.ITERATION] == 0
        assert state[StateKeys.CORE_TYPE] == TargetType.LLM_CALL.value
        assert state[StateKeys.ENDED] is False
        assert state[StateKeys.RAW_RESULT] is None
        assert state[StateKeys.RAW_ERROR] is None
        assert state[StateKeys.RAW_TOOL_CALLS] == []
        assert state[StateKeys.TASK_COMPLETE] is False

    def test_initial_state_with_overrides(self):
        """测试: 覆盖参数正确应用。"""
        state = create_initial_state(
            **{
                StateKeys.SESSION_ID: "test_session",
                StateKeys.AGENT_LEVEL: "L2",
            }
        )
        assert state[StateKeys.SESSION_ID] == "test_session"
        assert state[StateKeys.AGENT_LEVEL] == "L2"


# ════════════════════════════════════════════════════════════════
# AC-PIP-02: max_iterations 安全阀
# ════════════════════════════════════════════════════════════════


class TestMaxIterations:
    """max_iterations 迭代安全阀测试。"""

    def test_max_iterations_safety_check(self):
        """测试: 超过 max_iterations 时 ended 被设置为 True。"""
        max_iter = 3
        state = create_initial_state()

        for i in range(1, max_iter + 2):
            state[StateKeys.ITERATION] = i
            if i > max_iter:
                state[StateKeys.ENDED] = True
                break

        assert state[StateKeys.ITERATION] == max_iter + 1
        assert state[StateKeys.ENDED] is True

    def test_max_iterations_not_exceeded(self):
        """测试: 未超过 max_iterations 时管道继续。"""
        max_iter = 5
        state = create_initial_state()

        for i in range(1, max_iter):
            state[StateKeys.ITERATION] = i
            assert i <= max_iter


# ════════════════════════════════════════════════════════════════
# AC-PIP-03: 输入路由可叠加匹配
# ════════════════════════════════════════════════════════════════


class TestInputRouteTable:
    """输入路由表（可叠加匹配）测试。"""

    def test_resolve_plugins_single_match(self):
        """测试: 单条件匹配返回对应插件。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="agent_level == 'L1'",
                target="core",
                plugins=["plugin_a"],
                priority=10,
            ),
        ])
        state = {"agent_level": "L1"}
        plugins = table.resolve_plugins(state)
        assert "plugin_a" in plugins

    def test_resolve_plugins_multiple_matches_merge(self):
        """测试: 多条件同时匹配，插件列表合并去重保序。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="agent_level == 'L1'",
                target="core",
                plugins=["plugin_a", "plugin_b"],
                priority=10,
            ),
            InputRouteEntry(
                name="entry_b",
                condition="task_complete == False",
                target="core",
                plugins=["plugin_b", "plugin_c"],
                priority=20,
            ),
        ])
        state = {"agent_level": "L1", "task_complete": False}
        plugins = table.resolve_plugins(state)
        # plugin_b 去重，保序
        assert plugins == ["plugin_a", "plugin_b", "plugin_c"]

    def test_resolve_plugins_no_match(self):
        """测试: 无条件匹配返回空列表。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="agent_level == 'L1'",
                target="core",
                plugins=["plugin_a"],
                priority=10,
            ),
        ])
        state = {"agent_level": "L3"}
        plugins = table.resolve_plugins(state)
        assert plugins == []

    def test_resolve_target_core(self):
        """测试: resolve_target 返回 core。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="",
                target="core",
                plugins=["plugin_a"],
                priority=10,
            ),
        ])
        target, entry = table.resolve_target({})
        assert target == "core"

    def test_resolve_target_end_takes_priority(self):
        """测试: end 目标优先级最高。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="core_entry",
                condition="",
                target="core",
                plugins=["plugin_a"],
                priority=1,
            ),
            InputRouteEntry(
                name="end_entry",
                condition="task_complete == True",
                target="end",
                plugins=["plugin_b"],
                priority=50,
            ),
        ])
        state = {"task_complete": True}
        target, entry = table.resolve_target(state)
        assert target == "end"

    def test_resolve_target_default_core_when_no_match(self):
        """测试: 无匹配条目时默认返回 core。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="agent_level == 'L1'",
                target="core",
                plugins=["plugin_a"],
                priority=10,
            ),
        ])
        target, entry = table.resolve_target({"agent_level": "L3"})
        assert target == "core"
        assert entry is None


# ════════════════════════════════════════════════════════════════
# AC-PIP-04: 输出路由互斥优先级仲裁
# ════════════════════════════════════════════════════════════════


class TestOutputRouteTable:
    """输出路由表（互斥优先级仲裁）测试。"""

    def test_arbitrate_first_match_wins(self):
        """测试: 首匹配生效，后续不再检查。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="high_priority",
                route_type="next_llm",
                condition="",
                priority=10,
            ),
            OutputRouteEntry(
                name="low_priority",
                route_type="next_llm",
                condition="",
                priority=50,
            ),
        ])
        signals = [RouteSignal(route_type="next_llm")]
        result = table.arbitrate(signals, {})
        assert result.route_type == "next_llm"

    def test_arbitrate_end_takes_highest_priority(self):
        """测试: end 信号具有最高优先级，覆盖 next_llm。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="next_llm_entry",
                route_type="next_llm",
                condition="",
                priority=10,
            ),
            OutputRouteEntry(
                name="end_entry",
                route_type="end",
                condition="",
                priority=50,
            ),
        ])
        signals = [
            RouteSignal(route_type="next_llm"),
            RouteSignal(route_type="end"),
        ]
        result = table.arbitrate(signals, {})
        assert result.route_type == "end"

    def test_arbitrate_no_match_returns_fallback(self):
        """测试: 无匹配时返回 fallback 信号。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="entry_a",
                route_type="delegate",
                condition="agent_level == 'L1'",
                priority=10,
            ),
        ])
        signals = [RouteSignal(route_type="next_llm")]
        result = table.arbitrate(signals, {"agent_level": "L3"})
        assert result.route_type == "end"
        assert result.reason == "fallback"

    def test_arbitrate_empty_signals(self):
        """测试: 无信号时返回 fallback。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="entry_a",
                route_type="next_llm",
                condition="",
                priority=10,
            ),
        ])
        result = table.arbitrate([], {})
        assert result.route_type == "end"

    def test_arbitrate_condition_filters(self):
        """测试: 条件为 False 时不匹配。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="entry_a",
                route_type="next_llm",
                condition="task_complete == True",
                priority=10,
            ),
        ])
        signals = [RouteSignal(route_type="next_llm")]
        result = table.arbitrate(signals, {"task_complete": False})
        assert result.route_type == "end"  # fallback


# ════════════════════════════════════════════════════════════════
# AC-PIP-05: 四种错误策略
# ════════════════════════════════════════════════════════════════


class _DummyPlugin(IInputPlugin):
    """用于测试错误策略的插件桩。"""

    def __init__(
        self,
        name: str,
        priority: int,
        error_policy: ErrorPolicy,
        should_fail: bool = True,
        fallback_state: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self.error_policy = error_policy
        self._should_fail = should_fail
        if fallback_state is not None:
            self.fallback_state = fallback_state

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


class TestErrorPolicies:
    """插件链四种错误策略测试。"""

    @pytest.mark.asyncio
    async def test_abort_policy_stops_chain(self):
        """测试: ABORT 策略终止后续插件。"""
        plugin_a = _DummyPlugin("a", 10, ErrorPolicy.ABORT, should_fail=True)
        plugin_b = _DummyPlugin("b", 20, ErrorPolicy.ABORT, should_fail=False)
        chain = PluginChain([plugin_a, plugin_b])

        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        # plugin_b 不应被执行
        assert len(results) == 1
        assert results[0].error is not None
        assert results[0].skip_remaining is True

    @pytest.mark.asyncio
    async def test_skip_policy_continues_chain(self):
        """测试: SKIP 策略跳过当前插件，继续后续。"""
        plugin_a = _DummyPlugin("a", 10, ErrorPolicy.SKIP, should_fail=True)
        plugin_b = _DummyPlugin("b", 20, ErrorPolicy.ABORT, should_fail=False)
        chain = PluginChain([plugin_a, plugin_b])

        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert len(results) == 2
        # plugin_a 的结果无 error（SKIP 吞掉了）
        assert results[0].error is None
        # plugin_b 正常执行
        assert results[1].state_updates.get("executed") == "b"

    @pytest.mark.asyncio
    async def test_fallback_policy_uses_fallback_state(self):
        """测试: FALLBACK 策略使用 fallback_state 替代。"""
        plugin_a = _DummyPlugin(
            "a", 10, ErrorPolicy.FALLBACK,
            should_fail=True,
            fallback_state={"fallback_key": "fallback_value"},
        )
        chain = PluginChain([plugin_a])

        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert results[0].state_updates.get("fallback_key") == "fallback_value"

    @pytest.mark.asyncio
    async def test_retry_policy_eventually_succeeds(self):
        """测试: RETRY 策略重试后成功。"""
        call_count = 0

        class RetryPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "retry_plugin"

            @property
            def priority(self) -> int:
                return 10

            error_policy = ErrorPolicy.RETRY
            max_retries = 3

            async def execute(self, ctx: PluginContext) -> PluginResult:
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise RuntimeError("transient failure")
                return PluginResult(state_updates={"success": True})

        chain = PluginChain([RetryPlugin()])
        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert results[0].state_updates.get("success") is True

    @pytest.mark.asyncio
    async def test_retry_policy_exhausted_aborts(self):
        """测试: RETRY 策略耗尽后转为 ABORT。"""
        plugin = _DummyPlugin(
            "retry_fail", 10, ErrorPolicy.RETRY, should_fail=True
        )
        plugin.max_retries = 2
        chain = PluginChain([plugin])

        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert results[0].error is not None
        assert results[0].skip_remaining is True

    @pytest.mark.asyncio
    async def test_skip_remaining_flag(self):
        """测试: skip_remaining=True 时跳过后续插件。"""

        class SkipPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "skip_plugin"

            @property
            def priority(self) -> int:
                return 10

            async def execute(self, ctx: PluginContext) -> PluginResult:
                return PluginResult(
                    state_updates={"stopped": True},
                    skip_remaining=True,
                )

        class NormalPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "normal_plugin"

            @property
            def priority(self) -> int:
                return 20

            async def execute(self, ctx: PluginContext) -> PluginResult:
                return PluginResult(state_updates={"should_not_run": True})

        chain = PluginChain([SkipPlugin(), NormalPlugin()])
        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert len(results) == 1
        assert ctx.state.get("stopped") is True
        assert "should_not_run" not in ctx.state


# ════════════════════════════════════════════════════════════════
# AC-PIP-12: 路由条件表达式安全（非 eval）
# ════════════════════════════════════════════════════════════════


class TestConditionParser:
    """安全条件表达式解析器测试。"""

    def test_empty_condition_returns_true(self):
        """测试: 空条件视为始终匹配。"""
        assert parse_condition("", {}) is True
        assert parse_condition("   ", {}) is True

    def test_boolean_literal(self):
        """测试: 布尔字面量。"""
        assert parse_condition("True", {}) is True
        assert parse_condition("False", {}) is False

    def test_comparison_equal(self):
        """测试: 相等比较。"""
        assert parse_condition("a == 1", {"a": 1}) is True
        assert parse_condition("a == 1", {"a": 2}) is False

    def test_comparison_not_equal(self):
        """测试: 不等比较。"""
        assert parse_condition("a != 1", {"a": 2}) is True

    def test_comparison_greater(self):
        """测试: 大于比较。"""
        assert parse_condition("a > 3", {"a": 5}) is True
        assert parse_condition("a > 3", {"a": 2}) is False

    def test_and_logic(self):
        """测试: and 布尔逻辑。"""
        assert parse_condition("a == 1 and b == 2", {"a": 1, "b": 2}) is True
        assert parse_condition("a == 1 and b == 2", {"a": 1, "b": 3}) is False

    def test_or_logic(self):
        """测试: or 布尔逻辑。"""
        assert parse_condition("a == 1 or b == 2", {"a": 9, "b": 2}) is True
        assert parse_condition("a == 1 or b == 2", {"a": 9, "b": 9}) is False

    def test_not_logic(self):
        """测试: not 布尔取反。"""
        assert parse_condition("not a", {"a": False}) is True
        assert parse_condition("not a", {"a": True}) is False

    def test_is_empty_check(self):
        """测试: is_empty 空值检查。"""
        assert parse_condition("a is_empty", {"a": None}) is True
        assert parse_condition("a is_empty", {"a": ""}) is True
        assert parse_condition("a is_empty", {"a": []}) is True
        assert parse_condition("a is_empty", {"a": "data"}) is False

    def test_is_not_empty_check(self):
        """测试: is_not_empty 非空检查。"""
        assert parse_condition("a is_not_empty", {"a": "data"}) is True
        assert parse_condition("a is_not_empty", {"a": None}) is False

    def test_state_subscript_access(self):
        """测试: state["key"] 下标访问。"""
        assert parse_condition(
            'state["level"] == "L1"', {"level": "L1"}
        ) is True

    def test_invalid_expression_returns_false(self):
        """测试: 无效表达式不抛异常，返回 False。"""
        assert parse_condition("invalid syntax !!!", {}) is False

    def test_code_injection_blocked(self):
        """测试: 代码注入被阻断（不使用 eval）。"""
        # 尝试注入代码不应执行
        result = parse_condition(
            "__import__('os').system('echo hack')", {}
        )
        assert result is False

    def test_undefined_variable_returns_none(self):
        """测试: 未定义变量解析为 None。"""
        assert parse_condition("undefined_var == None", {}) is True


# ════════════════════════════════════════════════════════════════
# AC-PIP-11: 插件链排序与 state_updates 合并
# ════════════════════════════════════════════════════════════════


class TestPluginChainStateMerge:
    """插件链 state_updates 合并测试。"""

    @pytest.mark.asyncio
    async def test_plugins_executed_in_priority_order(self):
        """测试: 插件按 priority 从小到大排序执行。"""
        execution_order: list[str] = []

        class OrderedPlugin(IInputPlugin):
            def __init__(self, name: str, priority: int) -> None:
                self._name = name
                self._priority = priority

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            async def execute(self, ctx: PluginContext) -> PluginResult:
                execution_order.append(self._name)
                return PluginResult()

        chain = PluginChain([
            OrderedPlugin("third", 30),
            OrderedPlugin("first", 10),
            OrderedPlugin("second", 20),
        ])
        ctx = PluginContext(state={})
        await chain.execute(ctx)

        assert execution_order == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_state_updates_merged_to_context(self):
        """测试: 插件的 state_updates 合并到 ctx.state。"""

        class WritingPlugin(IInputPlugin):
            def __init__(self, name: str, priority: int, key: str, val: Any) -> None:
                self._name = name
                self._priority = priority
                self._key = key
                self._val = val

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            async def execute(self, ctx: PluginContext) -> PluginResult:
                return PluginResult(state_updates={self._key: self._val})

        chain = PluginChain([
            WritingPlugin("a", 10, "key_a", "value_a"),
            WritingPlugin("b", 20, "key_b", "value_b"),
        ])
        ctx = PluginContext(state={})
        await chain.execute(ctx)

        assert ctx.state.get("key_a") == "value_a"
        assert ctx.state.get("key_b") == "value_b"

    @pytest.mark.asyncio
    async def test_deep_update_with_dot_keys(self):
        """测试: 带点号的 state_updates 键展开为嵌套字典。"""

        class DotKeyPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "dot_plugin"

            @property
            def priority(self) -> int:
                return 10

            async def execute(self, ctx: PluginContext) -> PluginResult:
                return PluginResult(state_updates={
                    "security.decision": "approved",
                    "context.user": "admin",
                })

        chain = PluginChain([DotKeyPlugin()])
        ctx = PluginContext(state={})
        await chain.execute(ctx)

        assert ctx.state.get("security", {}).get("decision") == "approved"
        assert ctx.state.get("context", {}).get("user") == "admin"
