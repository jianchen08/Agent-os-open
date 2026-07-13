"""安全检查插件改造集成测试与端到端测试。

测试安全检查插件 (SecurityCheckPlugin) 和层级权限守卫插件 (LevelGuardPlugin)
与管道引擎 (PipelineEngine)、路由表 (InputRouteTable/OutputRouteTable) 的集成行为。

覆盖场景：
1. InputRouteTable 两步解析（resolve_plugins + resolve_target）
2. 引擎两步解析集成（input 插件更新 state 后再检查 target）
3. SecurityCheckPlugin 集成（block / pass / bypass / approval）
4. LevelGuardPlugin 集成（层级权限控制）
5. 完整管道端到端流程（安全工具执行、被拦截、审批、越权）

所有外部依赖使用 Mock，不依赖真实 LLM 或外部服务。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.plugin import (
    IInputPlugin,
    IOutputPlugin,
    OutputResult,
    PluginContext,
    PluginResult,
)
from pipeline.types import (
    ErrorPolicy,
    RouteSignal,
    StateKeys,
    create_initial_state,
)
from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable
from pipeline.registry import PluginRegistry


# ---------------------------------------------------------------------------
# Mock 插件定义（复用 test_engine.py 模式，继承正确接口）
# ---------------------------------------------------------------------------


class MockInputPlugin(IInputPlugin):
    """Mock 输入插件，返回预设的 state_updates。"""

    error_policy = ErrorPolicy.ABORT

    def __init__(
        self,
        name: str = "mock_input",
        priority: int = 50,
        state_updates: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self._state_updates = state_updates or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult(state_updates=self._state_updates)


class MockCorePlugin:
    """Mock Core 插件，返回预设的 state_updates。"""

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
    """Mock 输出插件，返回预设的 route_signal。"""

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
    services: dict[str, Any] | None = None,
) -> Any:
    """构建 PipelineEngine 实例，使用真实或 Mock 组件。"""
    from pipeline.engine import PipelineEngine

    if input_route_table is None:
        input_route_table = MagicMock()
        input_route_table.resolve_plugins = MagicMock(return_value=[])
        input_route_table.resolve_target = MagicMock(return_value=("core", None))

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
        services=services,
    )


def _make_security_check_plugin(
    rules: list[dict[str, Any]] | None = None,
    enabled: bool = True,
    workspace: str = "",
) -> Any:
    """构建 SecurityCheckPlugin 实例，使用直接传入的规则列表。

    Args:
        rules: 安全规则列表，None 则使用空规则
        enabled: 是否启用安全检查
        workspace: 允许的工作目录

    Returns:
        SecurityCheckPlugin 实例
    """
    from plugins.input.security_check import SecurityCheckPlugin

    config: dict[str, Any] = {
        "enabled": enabled,
        "workspace": workspace,
    }
    if rules is not None:
        config["rules"] = rules
    return SecurityCheckPlugin(config=config)


def _make_level_guard_plugin(
    enabled: bool = True,
    strict: bool = True,
) -> Any:
    """构建 LevelGuardPlugin 实例。

    Args:
        enabled: 是否启用权限守卫
        strict: 严格模式

    Returns:
        LevelGuardPlugin 实例
    """
    from plugins.input.level_guard import LevelGuardPlugin

    config: dict[str, Any] = {
        "enabled": enabled,
        "strict": strict,
    }
    return LevelGuardPlugin(config=config)


def _build_security_input_table() -> InputRouteTable:
    """构建包含安全拦截条件的标准 InputRouteTable。

    使用 state["security.decision"] 括号语法访问扁平 key，
    因为真实插件写入的是扁平 key（如 "security.decision"），
    条件解析器需要通过 state["key.with.dots"] 形式访问。

    Returns:
        配置了 security_blocked、level_blocked 和默认 tool_execute/llm_call 条目的路由表
    """
    return InputRouteTable([
        # 安全检查拦截条目
        InputRouteEntry(
            name="security_blocked",
            condition="state[\"security.decision\"].get('allowed') == False",
            target="end",
            plugins=[],
            priority=0,
            result="工具执行被安全检查拦截",
        ),
        # 层级权限拦截条目
        InputRouteEntry(
            name="level_blocked",
            condition="state[\"security.level_decision\"].get('allowed') == False",
            target="end",
            plugins=[],
            priority=0,
            result="工具执行被层级权限拦截",
        ),
        # 工具执行条目：core_type=tool_execute 时执行安全插件
        InputRouteEntry(
            name="tool_execute",
            condition="core_type == 'tool_execute'",
            target="core",
            plugins=["security_check", "level_guard"],
            priority=10,
        ),
        # LLM 调用条目：不带安全插件
        InputRouteEntry(
            name="llm_call",
            condition="core_type == 'llm_call'",
            target="core",
            plugins=[],
            priority=20,
        ),
    ])


def _build_engine_with_route(
    input_table: InputRouteTable,
    output_table: OutputRouteTable,
    registry: PluginRegistry,
    max_iterations: int = 100,
    services: dict[str, Any] | None = None,
) -> Any:
    """构建 PipelineEngine 实例，使用真实路由表和注册表。"""
    from pipeline.engine import PipelineEngine

    return PipelineEngine(
        input_route_table=input_table,
        output_route_table=output_table,
        plugin_registry=registry,
        max_iterations=max_iterations,
        services=services,
    )


# ---------------------------------------------------------------------------
# 一、InputRouteTable 两步解析测试
# ---------------------------------------------------------------------------


class TestInputRouteTwoStepResolve:
    """测试 InputRouteTable 的 resolve_plugins() 和 resolve_target() 两步解析。"""

    @pytest.mark.asyncio
    async def test_resolve_plugins_collects_all_matched(self) -> None:
        """resolve_plugins() 收集所有匹配条目的插件列表（可叠加）。

        多个条目同时匹配时，它们的插件列表应被合并去重。
        """
        table = InputRouteTable([
            InputRouteEntry(name="a", condition="core_type == 'tool_execute'", plugins=["p1", "p2"], priority=10),
            InputRouteEntry(name="b", condition="core_type == 'tool_execute'", plugins=["p2", "p3"], priority=20),
            InputRouteEntry(name="c", condition="core_type == 'llm_call'", plugins=["p4"], priority=30),
        ])
        state = {StateKeys.CORE_TYPE: "tool_execute"}
        plugins = table.resolve_plugins(state)
        # p2 去重，顺序保持
        assert plugins == ["p1", "p2", "p3"]

    @pytest.mark.asyncio
    async def test_resolve_plugins_excludes_unmatched(self) -> None:
        """不匹配的条目不贡献插件。"""
        table = InputRouteTable([
            InputRouteEntry(name="a", condition="core_type == 'tool_execute'", plugins=["p1"], priority=10),
            InputRouteEntry(name="b", condition="core_type == 'llm_call'", plugins=["p2"], priority=20),
        ])
        state = {StateKeys.CORE_TYPE: "llm_call"}
        plugins = table.resolve_plugins(state)
        assert plugins == ["p2"]

    @pytest.mark.asyncio
    async def test_resolve_target_returns_core_by_default(self) -> None:
        """无匹配时默认返回 ("core", None)。"""
        table = InputRouteTable([
            InputRouteEntry(name="a", condition="core_type == 'tool_execute'", target="end", plugins=[], priority=10),
        ])
        state = {StateKeys.CORE_TYPE: "llm_call"}
        target, entry = table.resolve_target(state)
        assert target == "core"
        assert entry is None

    @pytest.mark.asyncio
    async def test_resolve_target_returns_end_when_security_blocked(self) -> None:
        """security.decision.allowed=False 时返回 ("end", entry)。

        使用嵌套 state 结构测试 resolve_target 的点号路径解析能力。
        """
        table = InputRouteTable([
            InputRouteEntry(
                name="security_blocked",
                condition="security.decision.get('allowed') == False",
                target="end",
                plugins=[],
                priority=0,
            ),
        ])
        # 使用嵌套 state（点号路径解析需要嵌套结构）
        state = {"security": {"decision": {"allowed": False, "reason": "dangerous"}}}
        target, entry = table.resolve_target(state)
        assert target == "end"
        assert entry is not None
        assert entry.name == "security_blocked"

    @pytest.mark.asyncio
    async def test_resolve_target_returns_end_when_level_blocked(self) -> None:
        """security.level_decision.allowed=False 时返回 ("end", entry)。

        使用嵌套 state 结构测试。
        """
        table = InputRouteTable([
            InputRouteEntry(
                name="level_blocked",
                condition="security.level_decision.get('allowed') == False",
                target="end",
                plugins=[],
                priority=0,
            ),
        ])
        state = {"security": {"level_decision": {"allowed": False, "reason": "unauthorized"}}}
        target, entry = table.resolve_target(state)
        assert target == "end"
        assert entry is not None
        assert entry.name == "level_blocked"

    @pytest.mark.asyncio
    async def test_resolve_target_end_has_highest_priority(self) -> None:
        """end 条件优先级高于 core 条件。

        当同时有 end 和 core 的匹配条目时，end 应优先生效。
        """
        table = InputRouteTable([
            InputRouteEntry(name="normal", condition="True", target="core", plugins=[], priority=10),
            InputRouteEntry(
                name="security_blocked",
                condition="security.decision.get('allowed') == False",
                target="end",
                plugins=[],
                priority=0,
            ),
        ])
        state = {"security": {"decision": {"allowed": False, "reason": "blocked"}}}
        target, entry = table.resolve_target(state)
        assert target == "end"
        assert entry.name == "security_blocked"

    @pytest.mark.asyncio
    async def test_resolve_target_returns_matched_entry(self) -> None:
        """返回匹配的 InputRouteEntry 对象。"""
        entry_expected = InputRouteEntry(
            name="tool_execute",
            condition="core_type == 'tool_execute'",
            target="core",
            plugins=["security_check"],
            priority=10,
        )
        table = InputRouteTable([entry_expected])
        state = {StateKeys.CORE_TYPE: "tool_execute"}
        target, entry = table.resolve_target(state)
        assert target == "core"
        assert entry is entry_expected

    @pytest.mark.asyncio
    async def test_format_result_fills_template(self) -> None:
        """format_result() 正确填充模板中的点号路径。

        使用嵌套 state 结构，format_result 按点号分割逐层查找。
        """
        entry = InputRouteEntry(
            name="security_blocked",
            condition="True",
            target="end",
            plugins=[],
            priority=0,
            result="工具执行被安全检查拦截: {security.decision.reason}",
        )
        state = {
            "security": {
                "decision": {"allowed": False, "reason": "dangerous command"},
            }
        }
        result = entry.format_result(state)
        assert result == "工具执行被安全检查拦截: dangerous command"

        # 路径不存在时用空字符串替代
        state2 = {"security": {}}
        result2 = entry.format_result(state2)
        assert result2 == "工具执行被安全检查拦截: "


# ---------------------------------------------------------------------------
# 二、引擎两步解析集成测试
# ---------------------------------------------------------------------------


class TestEngineTwoStepResolve:
    """测试引擎的 input 插件执行后再解析 target 的两步解析集成。

    使用 MockInputPlugin（继承 IInputPlugin）模拟安全插件写入扁平 key，
    条件使用 state["key.with.dots"] 括号语法匹配。
    """

    @pytest.mark.asyncio
    async def test_engine_input_plugins_update_state_before_target_check(self) -> None:
        """input 插件更新 state 后再检查 target。

        使用 MockInputPlugin 模拟 security_check 写入 security.decision.allowed=False，
        验证引擎 target=end，不执行 core。
        """
        # MockInputPlugin 模拟安全检查拦截（使用扁平 key，与真实插件一致）
        security_mock = MockInputPlugin(
            name="security_check",
            state_updates={"security.decision": {"allowed": False, "reason": "dangerous"}},
        )
        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "should_not_run"})

        registry = PluginRegistry()
        registry.register(security_mock)
        registry.register_core("tool_execute", core_plugin)

        input_table = InputRouteTable([
            InputRouteEntry(
                name="tool_execute",
                condition="core_type == 'tool_execute'",
                target="core",
                plugins=["security_check"],
                priority=10,
            ),
            InputRouteEntry(
                name="security_blocked",
                condition="state[\"security.decision\"].get('allowed') == False",
                target="end",
                plugins=[],
                priority=0,
            ),
        ])
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(**{StateKeys.CORE_TYPE: "tool_execute"})
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        # core 不应执行
        assert result.get(StateKeys.RAW_RESULT) != "should_not_run"

    @pytest.mark.asyncio
    async def test_engine_target_end_writes_result_to_raw_result(self) -> None:
        """target=end 时将拦截原因写入 RAW_RESULT。

        配置 security_blocked 条目带 result 模板（纯字符串，不含变量引用），
        验证 state[RAW_RESULT] 包含拦截原因。
        """
        security_mock = MockInputPlugin(
            name="security_check",
            state_updates={"security.decision": {"allowed": False, "reason": "dangerous command"}},
        )

        registry = PluginRegistry()
        registry.register(security_mock)

        input_table = InputRouteTable([
            InputRouteEntry(
                name="tool_execute",
                condition="core_type == 'tool_execute'",
                target="core",
                plugins=["security_check"],
                priority=10,
            ),
            InputRouteEntry(
                name="security_blocked",
                condition="state[\"security.decision\"].get('allowed') == False",
                target="end",
                plugins=[],
                priority=0,
                result="工具执行被安全检查拦截",
            ),
        ])
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(**{StateKeys.CORE_TYPE: "tool_execute"})
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        assert "安全检查拦截" in result[StateKeys.RAW_RESULT]

    @pytest.mark.asyncio
    async def test_engine_target_end_no_result_template(self) -> None:
        """target=end 时无 result 模板，RAW_RESULT 不被覆盖。

        配置 security_blocked 条目不带 result，
        验证 state[RAW_RESULT] 保持原值。
        """
        security_mock = MockInputPlugin(
            name="security_check",
            state_updates={"security.decision": {"allowed": False, "reason": "blocked"}},
        )

        registry = PluginRegistry()
        registry.register(security_mock)

        input_table = InputRouteTable([
            InputRouteEntry(
                name="tool_execute",
                condition="core_type == 'tool_execute'",
                target="core",
                plugins=["security_check"],
                priority=10,
            ),
            InputRouteEntry(
                name="security_blocked",
                condition="state[\"security.decision\"].get('allowed') == False",
                target="end",
                plugins=[],
                priority=0,
                # 无 result 模板
            ),
        ])
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        initial = create_initial_state(**{StateKeys.CORE_TYPE: "tool_execute"})
        initial[StateKeys.RAW_RESULT] = "original_value"
        result = await engine.run(initial_state=initial)

        assert result[StateKeys.ENDED] is True
        # 无 result 模板时，RAW_RESULT 应保持原值
        assert result[StateKeys.RAW_RESULT] == "original_value"

    @pytest.mark.asyncio
    async def test_engine_core_executes_after_safe_input(self) -> None:
        """安全输入后正常执行 core。

        MockInputPlugin 写入 security.decision.allowed=True，
        验证 core 被执行。
        """
        security_mock = MockInputPlugin(
            name="security_check",
            state_updates={"security.decision": {"allowed": True, "reason": "safe"}},
        )
        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "core_executed"})

        registry = PluginRegistry()
        registry.register(security_mock)
        registry.register_core("tool_execute", core_plugin)

        input_table = InputRouteTable([
            InputRouteEntry(
                name="tool_execute",
                condition="core_type == 'tool_execute'",
                target="core",
                plugins=["security_check"],
                priority=10,
            ),
            InputRouteEntry(
                name="security_blocked",
                condition="state[\"security.decision\"].get('allowed') == False",
                target="end",
                plugins=[],
                priority=0,
            ),
        ])
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(**{StateKeys.CORE_TYPE: "tool_execute"})
        result = await engine.run(initial_state=state)

        assert result.get(StateKeys.RAW_RESULT) == "core_executed"

    @pytest.mark.asyncio
    async def test_engine_normal_llm_call_no_security_check(self) -> None:
        """LLM 调用时不执行安全检查插件。

        配置 llm_call 条件不带安全插件，
        验证安全插件不被执行，只有普通插件执行。
        """
        executed_plugins: list[str] = []

        class TrackedMockInputPlugin(IInputPlugin):
            """可追踪执行记录的 Mock 输入插件。"""

            error_policy = ErrorPolicy.ABORT

            def __init__(self, name: str, state_updates: dict[str, Any] | None = None) -> None:
                self._name = name
                self._priority = 50
                self._state_updates = state_updates or {}

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            async def execute(self, ctx: PluginContext) -> PluginResult:
                executed_plugins.append(self._name)
                return PluginResult(state_updates=self._state_updates)

        security_mock = TrackedMockInputPlugin(name="security_check", state_updates={})
        normal_mock = TrackedMockInputPlugin(name="normal_plugin", state_updates={})

        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "llm_response"})

        registry = PluginRegistry()
        registry.register(security_mock)
        registry.register(normal_mock)
        registry.register_core("llm_call", core_plugin)

        input_table = InputRouteTable([
            InputRouteEntry(
                name="llm_call",
                condition="core_type == 'llm_call'",
                target="core",
                plugins=["normal_plugin"],
                priority=20,
            ),
            InputRouteEntry(
                name="tool_execute",
                condition="core_type == 'tool_execute'",
                target="core",
                plugins=["security_check"],
                priority=10,
            ),
        ])
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state()
        result = await engine.run(initial_state=state)

        assert result.get(StateKeys.RAW_RESULT) == "llm_response"
        # security_check 不应被执行（llm_call 条目不包含它）
        assert "security_check" not in executed_plugins
        # normal_plugin 应被执行
        assert "normal_plugin" in executed_plugins


# ---------------------------------------------------------------------------
# 三、Security Check 插件集成测试
# ---------------------------------------------------------------------------


class TestSecurityCheckIntegration:
    """测试 SecurityCheckPlugin 与管道引擎的集成行为。

    使用 L2 agent_level（bash 在 L2 允许列表中），
    避免 LevelGuardPlugin 干扰安全检查测试。
    """

    @pytest.mark.asyncio
    async def test_block_rule_stops_execution(self) -> None:
        """block 规则阻止执行。

        使用 bash + "rm -rf /" 触发 block，
        验证 target=end，RAW_RESULT 包含拦截原因。
        """
        rules = [
            {
                "name": "dangerous_commands",
                "tools": ["bash"],
                "params": ["command"],
                "action": "block",
                "patterns": [
                    {"type": "keyword", "value": "rm -rf"},
                ],
            }
        ]
        plugin = _make_security_check_plugin(rules=rules)

        registry = PluginRegistry()
        registry.register(plugin)

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(
            **{
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.AGENT_LEVEL: "l2_subtask",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": {"command": "rm -rf /"}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        assert "安全检查拦截" in result[StateKeys.RAW_RESULT]

    @pytest.mark.asyncio
    async def test_safe_operation_passes_through(self) -> None:
        """安全操作正常放行。

        使用 bash + "ls" 安全命令，
        验证 target=core。
        """
        rules = [
            {
                "name": "dangerous_commands",
                "tools": ["bash"],
                "params": ["command"],
                "action": "block",
                "patterns": [
                    {"type": "keyword", "value": "rm -rf"},
                ],
            }
        ]
        plugin = _make_security_check_plugin(rules=rules)
        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "tool_result"})

        registry = PluginRegistry()
        registry.register(plugin)
        registry.register_core("tool_execute", core_plugin)

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(
            **{
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.AGENT_LEVEL: "l2_subtask",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": {"command": "ls"}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        # 安全命令应放行，core 执行
        assert result.get(StateKeys.RAW_RESULT) == "tool_result"

    @pytest.mark.asyncio
    async def test_non_tool_execute_bypasses_check(self) -> None:
        """非 tool_execute 时 SecurityCheckPlugin 内部跳过安全检查。

        core_type=llm_call 时，SecurityCheckPlugin.execute() 仍被调用，
        但内部判断 core_type != 'tool_execute' 后直接返回 allowed=True。
        """
        rules = [
            {
                "name": "dangerous_commands",
                "tools": ["bash"],
                "params": ["command"],
                "action": "block",
                "patterns": [
                    {"type": "keyword", "value": "rm -rf"},
                ],
            }
        ]
        plugin = _make_security_check_plugin(rules=rules)
        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "llm_result"})

        registry = PluginRegistry()
        registry.register(plugin)
        registry.register_core("llm_call", core_plugin)

        # llm_call 条目包含 security_check 插件，验证插件被注册和调用
        input_table = InputRouteTable([
            InputRouteEntry(
                name="llm_call",
                condition="core_type == 'llm_call'",
                target="core",
                plugins=["security_check"],
                priority=20,
            ),
            InputRouteEntry(
                name="tool_execute",
                condition="core_type == 'tool_execute'",
                target="core",
                plugins=["security_check"],
                priority=10,
            ),
        ])
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state()
        result = await engine.run(initial_state=state)

        # llm_call 时安全检查被调用但内部放行，core 正常执行
        assert result.get(StateKeys.RAW_RESULT) == "llm_result"
        # 验证 security_check 写入了 allowed=True
        decision = result.get("security.decision", {})
        assert decision.get("allowed") is True
        assert "not a tool execution" in decision.get("reason", "")

    @pytest.mark.asyncio
    async def test_needs_approval_without_service_blocks(self, monkeypatch) -> None:
        """engine 未注入服务时回退全局单例，用户拒绝则拦截。

        修复后契约：不再因 ctx._services 未注入而直接拒绝，而是回退全局单例
        正常发起审批。此处 mock 全局单例返回 denied，验证仍能拦截。
        """
        rules = [
            {
                "name": "sudo_commands",
                "tools": ["bash"],
                "params": ["command"],
                "action": "needs_approval",
                "patterns": [
                    {"type": "keyword", "value": "sudo"},
                ],
            }
        ]
        plugin = _make_security_check_plugin(rules=rules)

        registry = PluginRegistry()
        registry.register(plugin)
        # 不注入 engine 服务 → 回退全局单例

        # mock 全局单例返回 denied（模拟用户拒绝）
        mock_svc = AsyncMock()
        mock_svc.create_choice_request = AsyncMock(return_value="req-fallback")
        mock_svc.wait_for_choice = AsyncMock(
            return_value={"response_type": "denied"}
        )
        import human_interaction
        monkeypatch.setattr(
            human_interaction, "get_human_interaction_service", lambda: mock_svc,
        )

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(
            **{
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.AGENT_LEVEL: "l2_subtask",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": {"command": "sudo apt update"}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        assert "安全检查拦截" in result[StateKeys.RAW_RESULT]

    @pytest.mark.asyncio
    async def test_needs_approval_with_service_approved(self) -> None:
        """有审批服务时审批通过后放行。

        Mock human_interaction 服务，返回 approved，
        验证 allowed=True。
        """
        rules = [
            {
                "name": "sudo_commands",
                "tools": ["bash"],
                "params": ["command"],
                "action": "needs_approval",
                "patterns": [
                    {"type": "keyword", "value": "sudo"},
                ],
            }
        ]
        plugin = _make_security_check_plugin(rules=rules)
        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "sudo_result"})

        # Mock human_interaction 服务
        mock_interaction_svc = AsyncMock()
        mock_interaction_svc.create_choice_request = AsyncMock(return_value="request-001")
        mock_interaction_svc.wait_for_choice = AsyncMock(
            return_value={"response_type": "approved"}
        )

        registry = PluginRegistry()
        registry.register(plugin)
        registry.register_core("tool_execute", core_plugin)

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(
            input_table, output_table, registry,
            services={"human_interaction_service": mock_interaction_svc},
        )
        state = create_initial_state(
            **{
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.AGENT_LEVEL: "l2_subtask",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": {"command": "sudo apt update"}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        # 审批通过，core 应执行
        assert result.get(StateKeys.RAW_RESULT) == "sudo_result"
        # 验证审批服务被调用
        mock_interaction_svc.create_choice_request.assert_called_once()
        mock_interaction_svc.wait_for_choice.assert_called_once()

    @pytest.mark.asyncio
    async def test_needs_approval_with_service_denied(self) -> None:
        """有审批服务时审批拒绝后拦截。

        Mock human_interaction 服务，返回 denied，
        验证 allowed=False。
        """
        rules = [
            {
                "name": "sudo_commands",
                "tools": ["bash"],
                "params": ["command"],
                "action": "needs_approval",
                "patterns": [
                    {"type": "keyword", "value": "sudo"},
                ],
            }
        ]
        plugin = _make_security_check_plugin(rules=rules)

        # Mock human_interaction 服务
        mock_interaction_svc = AsyncMock()
        mock_interaction_svc.create_choice_request = AsyncMock(return_value="request-002")
        mock_interaction_svc.wait_for_choice = AsyncMock(
            return_value={"response_type": "denied"}
        )

        registry = PluginRegistry()
        registry.register(plugin)

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(
            input_table, output_table, registry,
            services={"human_interaction_service": mock_interaction_svc},
        )
        state = create_initial_state(
            **{
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.AGENT_LEVEL: "l2_subtask",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash", "args": {"command": "sudo rm -rf /"}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        assert "安全检查拦截" in result[StateKeys.RAW_RESULT]


# ---------------------------------------------------------------------------
# 四、Level Guard 集成测试
# ---------------------------------------------------------------------------


class TestLevelGuardIntegration:
    """测试 LevelGuardPlugin 与管道引擎的集成行为。

    使用 L1 agent_level 和无安全规则（空规则），专注测试层级权限。
    """

    @pytest.mark.asyncio
    async def test_level_guard_blocks_unauthorized_tool(self) -> None:
        """level_guard 阻止越权工具。

        agent_level=l1_main，尝试调用 write_file（L1 不允许），
        验证 target=end。
        """
        plugin = _make_level_guard_plugin()

        registry = PluginRegistry()
        registry.register(plugin)

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(
            **{
                StateKeys.AGENT_LEVEL: "l1_main",
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "write_file", "args": {}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        assert "层级权限拦截" in result[StateKeys.RAW_RESULT]

    @pytest.mark.asyncio
    async def test_level_guard_allows_authorized_tool(self) -> None:
        """level_guard 允许授权工具。

        agent_level=l1_main，调用 read_file（L1 允许），
        验证 target=core。
        """
        plugin = _make_level_guard_plugin()
        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "file_content"})

        registry = PluginRegistry()
        registry.register(plugin)
        registry.register_core("tool_execute", core_plugin)

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(
            **{
                StateKeys.AGENT_LEVEL: "l1_main",
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "read_file", "args": {}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        # L1 允许 read_file，core 应执行
        assert result.get(StateKeys.RAW_RESULT) == "file_content"

    @pytest.mark.asyncio
    async def test_level_guard_l3_allows_all(self) -> None:
        """L3 允许所有工具。

        agent_level=l3_atomic，调用任何工具，
        验证 target=core。
        """
        plugin = _make_level_guard_plugin()
        core_plugin = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "any_result"})

        registry = PluginRegistry()
        registry.register(plugin)
        registry.register_core("tool_execute", core_plugin)

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([])

        engine = _build_engine_with_route(input_table, output_table, registry)
        state = create_initial_state(
            **{
                StateKeys.AGENT_LEVEL: "l3_atomic",
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "write_file", "args": {}}
                ],
            }
        )
        result = await engine.run(initial_state=state)

        # L3 全部允许
        assert result.get(StateKeys.RAW_RESULT) == "any_result"


# ---------------------------------------------------------------------------
# 五、完整管道端到端测试
# ---------------------------------------------------------------------------


class TestSecurityGuardE2E:
    """完整管道端到端测试：安全检查 + 层级权限 + 引擎循环。"""

    @pytest.mark.asyncio
    async def test_e2e_safe_tool_execution_flow(self) -> None:
        """完整安全工具执行流程。

        构建完整管道：InputRouteTable + SecurityCheckPlugin + LevelGuardPlugin + Core + Output
        第一轮：LLM 调用 → next_tool 信号，输出插件设置安全工具调用
        第二轮：工具执行（安全命令 read_file）→ 安全检查通过 → core 执行 → end
        验证完整流程。
        """
        rules = [
            {
                "name": "path_traversal",
                "tools": ["*"],
                "params": ["path"],
                "action": "block",
                "patterns": [
                    {"type": "keyword", "value": "../"},
                ],
            }
        ]
        security_plugin = _make_security_check_plugin(rules=rules)
        level_plugin = _make_level_guard_plugin()

        llm_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "llm_thinking"})
        tool_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "file_content"})

        registry = PluginRegistry()
        registry.register(security_plugin)
        registry.register(level_plugin)
        registry.register_core("llm_call", llm_core)
        registry.register_core("tool_execute", tool_core)

        class RoundBasedOutputPlugin(IOutputPlugin):
            """根据调用次数产生不同路由信号的输出插件。"""

            error_policy = ErrorPolicy.SKIP
            _call_count = 0

            def __init__(self) -> None:
                self._name = "round_based_output"
                self._priority = 50

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            @property
            def route_signals(self) -> list[str]:
                return []

            async def execute(self, ctx: PluginContext) -> OutputResult:
                RoundBasedOutputPlugin._call_count += 1
                if RoundBasedOutputPlugin._call_count == 1:
                    # 第一轮：设置安全的工具调用并返回 next_tool
                    return OutputResult(
                        state_updates={
                            StateKeys.RAW_TOOL_CALLS: [
                                {"name": "read_file", "args": {"path": "/safe/path"}}
                            ]
                        },
                        route_signal=RouteSignal(
                            route_type="next_tool", target="tool_execute"
                        ),
                    )
                return OutputResult(
                    route_signal=RouteSignal(route_type="end", reason="done")
                )

        registry.register(RoundBasedOutputPlugin())

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([
            OutputRouteEntry(route_type="next_tool", condition="True", priority=1),
            OutputRouteEntry(route_type="end", condition="True", priority=2),
        ])

        engine = _build_engine_with_route(input_table, output_table, registry)
        RoundBasedOutputPlugin._call_count = 0
        state = create_initial_state(**{StateKeys.AGENT_LEVEL: "l1_main"})
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        # 两轮迭代
        assert result[StateKeys.ITERATION] == 2
        # 第二轮工具执行成功
        assert result.get(StateKeys.RAW_RESULT) == "file_content"

    @pytest.mark.asyncio
    async def test_e2e_blocked_tool_execution_flow(self) -> None:
        """完整被拦截工具执行流程。

        构建完整管道：
        第一轮：LLM 调用 → next_tool 信号，输出插件设置危险工具调用
        第二轮：工具执行（危险路径遍历）→ 安全检查拦截 → target=end
        验证 ended=True，RAW_RESULT 包含拦截原因。
        """
        rules = [
            {
                "name": "path_traversal",
                "tools": ["*"],
                "params": ["path"],
                "action": "block",
                "patterns": [
                    {"type": "keyword", "value": "../"},
                ],
            }
        ]
        security_plugin = _make_security_check_plugin(rules=rules)
        level_plugin = _make_level_guard_plugin()

        llm_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "llm_thinking"})
        tool_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "should_not_run"})

        registry = PluginRegistry()
        registry.register(security_plugin)
        registry.register(level_plugin)
        registry.register_core("llm_call", llm_core)
        registry.register_core("tool_execute", tool_core)

        class TwoRoundOutputPlugin(IOutputPlugin):
            """第一轮 next_tool（设置危险工具调用），第二轮 end。"""

            error_policy = ErrorPolicy.SKIP
            _call_count = 0

            def __init__(self) -> None:
                self._name = "two_round_output"
                self._priority = 50

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            @property
            def route_signals(self) -> list[str]:
                return []

            async def execute(self, ctx: PluginContext) -> OutputResult:
                TwoRoundOutputPlugin._call_count += 1
                if TwoRoundOutputPlugin._call_count == 1:
                    return OutputResult(
                        state_updates={
                            StateKeys.RAW_TOOL_CALLS: [
                                {"name": "read_file", "args": {"path": "../../../etc/passwd"}}
                            ]
                        },
                        route_signal=RouteSignal(
                            route_type="next_tool", target="tool_execute"
                        ),
                    )
                return OutputResult(
                    route_signal=RouteSignal(route_type="end", reason="done")
                )

        registry.register(TwoRoundOutputPlugin())

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([
            OutputRouteEntry(route_type="next_tool", condition="True", priority=1),
            OutputRouteEntry(route_type="end", condition="True", priority=2),
        ])

        engine = _build_engine_with_route(input_table, output_table, registry)
        TwoRoundOutputPlugin._call_count = 0
        state = create_initial_state(**{StateKeys.AGENT_LEVEL: "l1_main"})
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        assert "安全检查拦截" in result[StateKeys.RAW_RESULT]
        # core 不应执行（安全检查在 core 之前拦截）
        assert result.get(StateKeys.RAW_RESULT) != "should_not_run"

    @pytest.mark.asyncio
    async def test_e2e_approval_flow(self) -> None:
        """完整审批流程。

        构建完整管道，mock human_interaction 服务。
        第一轮：LLM 调用 → next_tool 信号，输出插件设置 sudo 命令
        第二轮：sudo 命令 → 审批通过 → core 执行 → end
        验证完整流程。
        """
        rules = [
            {
                "name": "sudo_commands",
                "tools": ["bash"],
                "params": ["command"],
                "action": "needs_approval",
                "patterns": [
                    {"type": "keyword", "value": "sudo"},
                ],
            }
        ]
        security_plugin = _make_security_check_plugin(rules=rules)
        level_plugin = _make_level_guard_plugin()

        llm_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "llm_response"})
        tool_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "sudo_done"})

        # Mock human_interaction 服务
        mock_interaction_svc = AsyncMock()
        mock_interaction_svc.create_choice_request = AsyncMock(return_value="req-001")
        mock_interaction_svc.wait_for_choice = AsyncMock(
            return_value={"response_type": "approved"}
        )

        registry = PluginRegistry()
        registry.register(security_plugin)
        registry.register(level_plugin)
        registry.register_core("llm_call", llm_core)
        registry.register_core("tool_execute", tool_core)

        class ApprovalRoundOutputPlugin(IOutputPlugin):
            """第一轮 next_tool（设置 sudo 命令），后续 end。"""

            error_policy = ErrorPolicy.SKIP
            _call_count = 0

            def __init__(self) -> None:
                self._name = "approval_round_output"
                self._priority = 50

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            @property
            def route_signals(self) -> list[str]:
                return []

            async def execute(self, ctx: PluginContext) -> OutputResult:
                ApprovalRoundOutputPlugin._call_count += 1
                if ApprovalRoundOutputPlugin._call_count == 1:
                    return OutputResult(
                        state_updates={
                            StateKeys.RAW_TOOL_CALLS: [
                                {"name": "bash", "args": {"command": "sudo apt update"}}
                            ]
                        },
                        route_signal=RouteSignal(
                            route_type="next_tool", target="tool_execute"
                        ),
                    )
                return OutputResult(
                    route_signal=RouteSignal(route_type="end", reason="done")
                )

        registry.register(ApprovalRoundOutputPlugin())

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([
            OutputRouteEntry(route_type="next_tool", condition="True", priority=1),
            OutputRouteEntry(route_type="end", condition="True", priority=2),
        ])

        engine = _build_engine_with_route(
            input_table, output_table, registry,
            services={"human_interaction_service": mock_interaction_svc},
        )
        ApprovalRoundOutputPlugin._call_count = 0
        # 使用 L2 agent_level（bash 在 L2 允许列表中）
        state = create_initial_state(**{StateKeys.AGENT_LEVEL: "l2_subtask"})
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        # 审批通过后 core 执行
        assert result.get(StateKeys.RAW_RESULT) == "sudo_done"
        # 验证审批服务被调用
        mock_interaction_svc.create_choice_request.assert_called_once()
        mock_interaction_svc.wait_for_choice.assert_called_once()

    @pytest.mark.asyncio
    async def test_e2e_level_guard_blocks_flow(self) -> None:
        """完整越权拦截流程。

        第一轮：LLM 调用 → next_tool 信号，输出插件设置 write_file 调用
        第二轮：agent_level=l1_main，尝试 write_file → level_guard 拦截
        验证 ended=True，RAW_RESULT 包含越权原因。
        """
        security_plugin = _make_security_check_plugin(rules=[])
        level_plugin = _make_level_guard_plugin()

        llm_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "llm_response"})
        tool_core = MockCorePlugin(state_updates={StateKeys.RAW_RESULT: "should_not_write"})

        registry = PluginRegistry()
        registry.register(security_plugin)
        registry.register(level_plugin)
        registry.register_core("llm_call", llm_core)
        registry.register_core("tool_execute", tool_core)

        class LevelGuardRoundOutputPlugin(IOutputPlugin):
            """第一轮 next_tool（设置 write_file 调用），后续 end。"""

            error_policy = ErrorPolicy.SKIP
            _call_count = 0

            def __init__(self) -> None:
                self._name = "level_guard_round_output"
                self._priority = 50

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            @property
            def route_signals(self) -> list[str]:
                return []

            async def execute(self, ctx: PluginContext) -> OutputResult:
                LevelGuardRoundOutputPlugin._call_count += 1
                if LevelGuardRoundOutputPlugin._call_count == 1:
                    return OutputResult(
                        state_updates={
                            StateKeys.RAW_TOOL_CALLS: [
                                {"name": "write_file", "args": {"path": "/tmp/test"}}
                            ]
                        },
                        route_signal=RouteSignal(
                            route_type="next_tool", target="tool_execute"
                        ),
                    )
                return OutputResult(
                    route_signal=RouteSignal(route_type="end", reason="done")
                )

        registry.register(LevelGuardRoundOutputPlugin())

        input_table = _build_security_input_table()
        output_table = OutputRouteTable([
            OutputRouteEntry(route_type="next_tool", condition="True", priority=1),
            OutputRouteEntry(route_type="end", condition="True", priority=2),
        ])

        engine = _build_engine_with_route(input_table, output_table, registry)
        LevelGuardRoundOutputPlugin._call_count = 0
        state = create_initial_state(**{StateKeys.AGENT_LEVEL: "l1_main"})
        result = await engine.run(initial_state=state)

        assert result[StateKeys.ENDED] is True
        assert "层级权限拦截" in result[StateKeys.RAW_RESULT]
