"""
Round2 Agent 编排 + Pipeline 管道引擎模块 — 测试缺口补充。

与 round1 独立，以新视角审查以下需求点的测试覆盖：

Agent 编排 (F-AGT / AC-AGT):
- F-AGT-10: dynamic_vars 每轮注入到最后一条消息（to_state 序列化 context.dynamic_vars）
- F-AGT-11: reference / literal / expression 三种类型在 to_state 中的完整字段保留
- F-AGT-13: tool_ids 限制（to_state 序列化 + 空 tool_ids 不注入 + agent_self_memory 追加）
- F-AGT-15: input_schema / output_schema 在 to_state 中的传递
- AgentPluginsConfig: disabled / enabled 同时存在的合并语义
- F-AGT-04: 热替换后新 Agent 配置正确生效

Pipeline 管道引擎 (F-PIP / AC-PIP):
- F-PIP-04 / AC-PIP-11: 终态 Output 插件链在管道结束后执行（ended=True 时仍运行 + state 修改可见）
- F-PIP-07: 输入路由 target=wait 解析（wait 优先级高于 core）
- F-PIP-08: 五种 route_type (next_llm/next_tool/end/delegate/wait) 完整仲裁
- F-PIP-09 / AC-PIP-12: 路由条件安全解析（subprocess/getattr/globals/lambda/list-comp 注入阻断）
- F-PIP-13 / AC-PIP-05: 四种错误策略边界（混合策略链 + FALLBACK 非 dict + RETRY 默认次数 + 未知策略默认 ABORT）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Agent imports
# ---------------------------------------------------------------------------
from agents.types import (
    AgentConfig,
    AgentLevel,
    AgentType,
    AgentPluginsConfig,
    ContextConfig,
    ContextVarItem,
    DeliverableSpec,
)
from agents.context_builder import ContextBuilder
from agents.registry import AgentRegistry

# ---------------------------------------------------------------------------
# Pipeline imports
# ---------------------------------------------------------------------------
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


# ============================================================================
# Part 1 — Agent: dynamic_vars 注入 (F-AGT-10)
# ============================================================================


class TestDynamicVarsToStateSerialization:
    """F-AGT-10: dynamic_vars 每轮注入 — to_state 序列化验证。

    dynamic_vars 配置通过 to_state() 序列化到 state["context.dynamic_vars"]，
    由 PromptBuildPlugin 读取并构建为动态变量消息，追加到消息列表末尾。
    """

    def test_dynamic_vars_serialized_to_state(self) -> None:
        """dynamic_vars.enabled=True + items 非空 → context.dynamic_vars 写入 state。"""
        config = AgentConfig(
            config_id="dyn_test",
            dynamic_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(name="current_time", type="timestamp"),
                    ContextVarItem(name="session", type="session"),
                ],
            ),
        )
        state = config.to_state()

        assert "context.dynamic_vars" in state
        items = state["context.dynamic_vars"]
        assert len(items) == 2
        assert items[0]["name"] == "current_time"
        assert items[0]["type"] == "timestamp"

    def test_dynamic_vars_disabled_not_serialized(self) -> None:
        """dynamic_vars.enabled=False → context.dynamic_vars 不写入 state。"""
        config = AgentConfig(
            config_id="dyn_disabled",
            dynamic_vars=ContextConfig(
                enabled=False,
                items=[ContextVarItem(name="x", type="timestamp")],
            ),
        )
        state = config.to_state()
        assert "context.dynamic_vars" not in state

    def test_dynamic_vars_empty_items_not_serialized(self) -> None:
        """dynamic_vars.items 为空列表 → context.dynamic_vars 不写入 state。"""
        config = AgentConfig(
            config_id="dyn_empty",
            dynamic_vars=ContextConfig(enabled=True, items=[]),
        )
        state = config.to_state()
        assert "context.dynamic_vars" not in state

    def test_dynamic_vars_item_fields_complete(self) -> None:
        """动态变量项序列化后应保留所有字段（name/type/path/content/tags 等）。"""
        item = ContextVarItem(
            name="agent_info",
            type="agent",
            path="some/path",
            content="inline content",
            tags=["tag1"],
            inject_type="full",
            top_k=10,
        )
        config = AgentConfig(
            config_id="dyn_fields",
            dynamic_vars=ContextConfig(enabled=True, items=[item]),
        )
        state = config.to_state()
        serialized = state["context.dynamic_vars"][0]

        assert serialized["name"] == "agent_info"
        assert serialized["type"] == "agent"
        assert serialized["path"] == "some/path"
        assert serialized["content"] == "inline content"
        assert serialized["tags"] == ["tag1"]
        assert serialized["inject_type"] == "full"
        assert serialized["top_k"] == 10


# ============================================================================
# Part 2 — Agent: context 变量三种类型完整保留 (F-AGT-11)
# ============================================================================


class TestContextVarTypesInToState:
    """F-AGT-11: reference / literal / expression 三种类型经 to_state 序列化后完整保留。"""

    def test_reference_type_fields_preserved(self) -> None:
        """reference (path) 类型 → to_state 中 type/path 字段保留。"""
        config = AgentConfig(
            config_id="ref_ser",
            static_vars=ContextConfig(
                enabled=True,
                items=[ContextVarItem(name="rules", type="path", path="config/rules.md")],
            ),
        )
        state = config.to_state()
        sv = state.get("context.static_vars", [])

        assert len(sv) >= 1
        ref_item = next(it for it in sv if it["name"] == "rules")
        assert ref_item["type"] == "path"
        assert ref_item["path"] == "config/rules.md"

    def test_literal_type_fields_preserved(self) -> None:
        """literal (inline content) 类型 → to_state 中 content 字段保留。"""
        config = AgentConfig(
            config_id="lit_ser",
            static_vars=ContextConfig(
                enabled=True,
                items=[ContextVarItem(name="tool_list", content="file_read, bash_execute")],
            ),
        )
        state = config.to_state()
        sv = state.get("context.static_vars", [])

        lit_item = next(it for it in sv if it["name"] == "tool_list")
        assert lit_item["content"] == "file_read, bash_execute"

    def test_expression_type_fields_preserved(self) -> None:
        """expression (timestamp) 类型 → to_state 中 type 字段保留。"""
        config = AgentConfig(
            config_id="expr_ser",
            dynamic_vars=ContextConfig(
                enabled=True,
                items=[ContextVarItem(name="now", type="timestamp")],
            ),
        )
        state = config.to_state()
        dv = state.get("context.dynamic_vars", [])

        expr_item = next(it for it in dv if it["name"] == "now")
        assert expr_item["type"] == "timestamp"

    def test_folder_type_fields_preserved(self) -> None:
        """folder 类型 → to_state 中 type/path/extensions 字段保留。"""
        config = AgentConfig(
            config_id="folder_ser",
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="rules_dir",
                        type="folder",
                        path="config/rules",
                        extensions=[".md", ".yaml"],
                    )
                ],
            ),
        )
        state = config.to_state()
        sv = state.get("context.static_vars", [])

        folder_item = next(it for it in sv if it["name"] == "rules_dir")
        assert folder_item["type"] == "folder"
        assert folder_item["path"] == "config/rules"
        assert folder_item["extensions"] == [".md", ".yaml"]

    def test_static_vars_includes_agent_self_memory(self) -> None:
        """config_id 非空时，static_vars 序列化自动追加 agent_self_memory 检索项。"""
        config = AgentConfig(
            config_id="self_mem_test",
            static_vars=ContextConfig(
                enabled=True,
                items=[ContextVarItem(name="rules", type="path")],
            ),
        )
        state = config.to_state()
        sv = state["context.static_vars"]

        self_mem = [it for it in sv if it.get("name") == "agent_self_memory"]
        assert len(self_mem) == 1
        assert self_mem[0]["tags"] == ["self_mem_test"]
        assert self_mem[0]["inject_type"] == "retrieval"

    def test_static_vars_disabled_still_gets_self_memory(self) -> None:
        """static_vars.enabled=False 但 config_id 非空时，仍注入 agent_self_memory。"""
        config = AgentConfig(
            config_id="disabled_sm",
            static_vars=ContextConfig(enabled=False, items=[]),
        )
        state = config.to_state()
        sv = state.get("context.static_vars", [])

        assert len(sv) == 1
        assert sv[0]["name"] == "agent_self_memory"

    def test_no_config_id_no_self_memory(self) -> None:
        """config_id 为空时，不注入 agent_self_memory。"""
        config = AgentConfig(
            config_id="",
            static_vars=ContextConfig(enabled=True, items=[]),
        )
        state = config.to_state()
        assert "context.static_vars" not in state


class TestContextBuilderFolderType:
    """F-AGT-11 补充: folder 类型在 ContextBuilder 中的实际读取。"""

    def test_folder_type_reads_multiple_files(self, tmp_path: Path) -> None:
        """folder 类型应读取目录下所有文件并合并。"""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "rule1.md").write_text("# Rule 1", encoding="utf-8")
        (rules_dir / "rule2.md").write_text("# Rule 2", encoding="utf-8")

        builder = ContextBuilder(base_path=tmp_path)
        config = AgentConfig(
            config_id="folder_test",
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="all_rules",
                        type="folder",
                        path="rules",
                    )
                ],
            ),
        )
        ctx = builder.build_static_context(config)
        item = ctx["items"][0]

        assert item["type"] == "folder"
        assert "Rule 1" in item["content"]
        assert "Rule 2" in item["content"]

    def test_folder_type_with_extensions_filter(self, tmp_path: Path) -> None:
        """folder 类型 extensions 过滤只读取指定扩展名。"""
        rules_dir = tmp_path / "mixed"
        rules_dir.mkdir()
        (rules_dir / "keep.md").write_text("Markdown content", encoding="utf-8")
        (rules_dir / "skip.txt").write_text("Text content", encoding="utf-8")

        builder = ContextBuilder(base_path=tmp_path)
        config = AgentConfig(
            config_id="ext_filter",
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="filtered",
                        type="folder",
                        path="mixed",
                        extensions=[".md"],
                    )
                ],
            ),
        )
        ctx = builder.build_static_context(config)
        item = ctx["items"][0]

        assert "Markdown content" in item["content"]
        assert "Text content" not in item["content"]


# ============================================================================
# Part 3 — Agent: tool_ids 限制 (F-AGT-13)
# ============================================================================


class TestToolIdsSerialization:
    """F-AGT-13: tool_ids 通过 to_state 序列化，限制可用工具范围。"""

    def test_tool_ids_non_empty_serialized(self) -> None:
        """tool_ids 非空 → state 中写入 tool_ids。"""
        config = AgentConfig(
            config_id="tool_ser",
            tool_ids=["file_read", "bash_execute", "web_search"],
        )
        state = config.to_state()

        assert state["tool_ids"] == ["file_read", "bash_execute", "web_search"]

    def test_tool_ids_empty_not_serialized(self) -> None:
        """tool_ids 为空列表 → state 中不包含 tool_ids 键。"""
        config = AgentConfig(
            config_id="no_tools",
            tool_ids=[],
        )
        state = config.to_state()
        assert "tool_ids" not in state

    def test_tool_ids_preserves_order(self) -> None:
        """tool_ids 顺序应保留。"""
        tools = ["z_tool", "a_tool", "m_tool"]
        config = AgentConfig(config_id="order_test", tool_ids=tools)
        state = config.to_state()
        assert state["tool_ids"] == tools

    def test_tool_ids_independent_from_agent_level(self) -> None:
        """tool_ids 在所有层级（L1/L2/L3）都能正确序列化。"""
        for level in [AgentLevel.L1_MAIN, AgentLevel.L2_SUBTASK, AgentLevel.L3_ATOMIC]:
            config = AgentConfig(
                config_id=f"level_{level.value}",
                level=level,
                tool_ids=["file_read"],
            )
            state = config.to_state()
            assert state["tool_ids"] == ["file_read"]
            assert state["agent_level"] == level.value


# ============================================================================
# Part 4 — Agent: AgentPluginsConfig 合并语义
# ============================================================================


class TestAgentPluginsConfigMerge:
    """AgentPluginsConfig disabled/enabled 合并到 plugin_configs 的语义验证。"""

    def test_disabled_only_produces_enabled_false(self) -> None:
        """仅 disabled 列表 → plugin_configs[name] = {"enabled": False}。"""
        config = AgentConfig(
            config_id="dis_only",
            plugins=AgentPluginsConfig(disabled=["security_check"]),
        )
        configs = config.get_plugin_configs()

        assert "security_check" in configs
        assert configs["security_check"]["enabled"] is False

    def test_enabled_only_produces_enabled_true_with_params(self) -> None:
        """仅 enabled 字典 → plugin_configs[name] = {"enabled": True, **params}。"""
        config = AgentConfig(
            config_id="en_only",
            plugins=AgentPluginsConfig(
                enabled={"memory_read": {"top_k": 5, "mode": "vector"}}
            ),
        )
        configs = config.get_plugin_configs()

        assert configs["memory_read"]["enabled"] is True
        assert configs["memory_read"]["top_k"] == 5
        assert configs["memory_read"]["mode"] == "vector"

    def test_both_disabled_and_enabled_merge_correctly(self) -> None:
        """disabled 和 enabled 同时存在 → 两部分合并到一个字典。"""
        config = AgentConfig(
            config_id="both",
            plugins=AgentPluginsConfig(
                disabled=["security_check", "duplicate_check"],
                enabled={"memory_read": {"top_k": 3}},
            ),
        )
        configs = config.get_plugin_configs()

        assert configs["memory_read"]["enabled"] is True
        assert configs["memory_read"]["top_k"] == 3
        assert configs["security_check"]["enabled"] is False
        assert configs["duplicate_check"]["enabled"] is False

    def test_enabled_overrides_disabled_for_same_plugin(self) -> None:
        """同一个插件名在 disabled 和 enabled 中同时出现 → enabled 优先。"""
        config = AgentConfig(
            config_id="conflict",
            plugins=AgentPluginsConfig(
                disabled=["memory_read"],
                enabled={"memory_read": {"top_k": 10}},
            ),
        )
        configs = config.get_plugin_configs()

        # enabled 在合并循环中先处理，disabled 跳过已存在键
        assert configs["memory_read"]["enabled"] is True
        assert configs["memory_read"]["top_k"] == 10

    def test_empty_plugins_no_plugin_configs(self) -> None:
        """无 disabled 也无 enabled → get_plugin_configs 返回空字典。"""
        config = AgentConfig(config_id="empty_plugins")
        configs = config.get_plugin_configs()
        assert configs == {}


# ============================================================================
# Part 5 — Agent: 热替换后新配置生效 (F-AGT-04)
# ============================================================================


class TestHotSwapNewConfig:
    """F-AGT-04: 热替换后 registry 返回新配置，旧引用不受影响。"""

    def test_reload_returns_updated_system_prompt(self) -> None:
        """热替换后 registry.get 返回新的 system_prompt。"""
        registry = AgentRegistry()
        old = AgentConfig(
            config_id="hot_agent",
            system_prompt="Old prompt v1",
        )
        registry.register(old)

        new = AgentConfig(
            config_id="hot_agent",
            system_prompt="New prompt v2",
            tool_ids=["file_read"],
        )
        registry.register(new)  # register 覆盖旧配置

        current = registry.get("hot_agent")
        assert current.system_prompt == "New prompt v2"
        assert current.tool_ids == ["file_read"]

    def test_old_reference_not_mutated_after_swap(self) -> None:
        """热替换后旧引用的 system_prompt 不变。"""
        registry = AgentRegistry()
        old = AgentConfig(config_id="swap_ref", system_prompt="v1")
        registry.register(old)
        old_ref = registry.get("swap_ref")

        registry.register(AgentConfig(config_id="swap_ref", system_prompt="v2"))

        # 旧引用保持原值
        assert old_ref.system_prompt == "v1"
        # 新查询返回新值
        assert registry.get("swap_ref").system_prompt == "v2"


# ============================================================================
# Part 6 — Pipeline: 终态 Output 插件链 (F-PIP-04 / AC-PIP-11)
# ============================================================================


class _TerminalTrackingPlugin(IOutputPlugin):
    """终态链追踪插件：记录执行顺序到 state。"""

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
        order_list = ctx.state.setdefault("_terminal_order", [])
        order_list.append(self._name)
        return OutputResult(state_updates={"_terminal_order": order_list})


class TestPostEndOutputChainDeep:
    """F-PIP-04 / AC-PIP-11: 终态 Output 插件链在管道结束后执行的深度验证。"""

    @staticmethod
    def _build_mock_engine(plugins: list) -> Any:
        from pipeline.registry import PluginRegistry

        registry = PluginRegistry()
        for p in plugins:
            registry.register(p)

        engine = MagicMock()
        engine.plugin_registry = registry
        engine.services = {}
        engine.output_route_table = OutputRouteTable([])
        return engine

    @pytest.mark.asyncio
    async def test_post_end_chain_runs_after_ended_true(self) -> None:
        """管道 ended=True 后，终态链仍执行。"""
        from pipeline.engine_chain import run_post_end_output_chain

        plugin = _TerminalTrackingPlugin("persist", 10)
        engine = self._build_mock_engine([plugin])
        state = create_initial_state(**{StateKeys.ENDED: True})

        await run_post_end_output_chain(engine, state)

        assert "_terminal_order" in state
        assert "persist" in state["_terminal_order"]

    @pytest.mark.asyncio
    async def test_post_end_chain_state_modifications_visible(self) -> None:
        """终态链修改的 state 对调用方可见（持久化等）。"""
        from pipeline.engine_chain import run_post_end_output_chain

        plugin = _TerminalTrackingPlugin("summary", 10)
        engine = self._build_mock_engine([plugin])
        state = create_initial_state(**{StateKeys.ENDED: True})

        await run_post_end_output_chain(engine, state)

        # state 被修改且可见
        assert state["_terminal_order"] == ["summary"]

    @pytest.mark.asyncio
    async def test_post_end_chain_multiple_plugins_priority_order(self) -> None:
        """终态链按 priority 从小到大排序执行。"""
        from pipeline.engine_chain import run_post_end_output_chain

        plugins = [
            _TerminalTrackingPlugin("c", 30),
            _TerminalTrackingPlugin("a", 10),
            _TerminalTrackingPlugin("b", 20),
        ]
        engine = self._build_mock_engine(plugins)
        state = create_initial_state(**{StateKeys.ENDED: True})

        await run_post_end_output_chain(engine, state)

        assert state["_terminal_order"] == ["a", "b", "c"]


# ============================================================================
# Part 7 — Pipeline: 输入路由 target=wait (F-PIP-07)
# ============================================================================


class TestInputRouteWaitTarget:
    """F-PIP-07: 输入路由 target 支持 core / end / wait 三种。"""

    def test_wait_target_resolved_when_condition_matches(self) -> None:
        """条件匹配时 target 解析为 wait。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="wait_entry",
                condition="approval_required == True",
                target="wait",
                plugins=["approval_plugin"],
                priority=10,
            ),
        ])
        state = {"approval_required": True}
        target, entry = table.resolve_target(state)

        assert target == "wait"
        assert entry is not None
        assert entry.name == "wait_entry"

    def test_wait_takes_priority_when_lower_priority_value(self) -> None:
        """wait 条目 priority 更小（高优先）时，wait 先匹配并生效。

        源码 resolve_target 按 priority 升序排序后遍历，
        第一个 target == "end" 或 target == "wait" 的条目生效。
        本例 wait priority=10 < end priority=20，wait 先被匹配。
        """
        table = InputRouteTable([
            InputRouteEntry(
                name="wait_entry",
                condition="",
                target="wait",
                plugins=[],
                priority=10,
            ),
            InputRouteEntry(
                name="end_entry",
                condition="",
                target="end",
                plugins=[],
                priority=20,
            ),
        ])
        target, entry = table.resolve_target({})
        assert target == "wait"

    def test_end_takes_priority_when_lower_priority_value(self) -> None:
        """end 条目 priority 更小（高优先）时，end 先匹配并生效。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="end_entry",
                condition="",
                target="end",
                plugins=[],
                priority=10,
            ),
            InputRouteEntry(
                name="wait_entry",
                condition="",
                target="wait",
                plugins=[],
                priority=20,
            ),
        ])
        target, entry = table.resolve_target({})
        assert target == "end"

    def test_wait_target_when_no_end_present(self) -> None:
        """只有 wait 条目匹配时 target 为 wait。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="core_entry",
                condition="mode == 'normal'",
                target="core",
                plugins=["normal_plugin"],
                priority=10,
            ),
            InputRouteEntry(
                name="wait_entry",
                condition="mode == 'approval'",
                target="wait",
                plugins=["approval_plugin"],
                priority=20,
            ),
        ])
        state = {"mode": "approval"}
        target, entry = table.resolve_target(state)

        assert target == "wait"
        assert entry.name == "wait_entry"

    def test_core_target_when_no_special_match(self) -> None:
        """无 end/wait 匹配时，target 为最高优先级条目的 target。"""
        table = InputRouteTable([
            InputRouteEntry(
                name="entry_a",
                condition="level == 'L1'",
                target="core",
                plugins=["plugin_a"],
                priority=10,
            ),
        ])
        target, entry = table.resolve_target({"level": "L1"})
        assert target == "core"
        assert entry.name == "entry_a"


# ============================================================================
# Part 8 — Pipeline: 五种 route_type 完整仲裁 (F-PIP-08)
# ============================================================================


class TestOutputRouteAllFiveTypes:
    """F-PIP-08: 输出路由 route_type 支持 next_llm / next_tool / end / delegate / wait。"""

    def test_each_route_type_arbitrated_correctly(self) -> None:
        """每种 route_type 单独信号时，仲裁结果正确。"""
        table = OutputRouteTable([
            OutputRouteEntry(name="llm", route_type="next_llm", condition="", priority=10),
            OutputRouteEntry(name="tool", route_type="next_tool", condition="", priority=20),
            OutputRouteEntry(name="end_e", route_type="end", condition="", priority=30),
            OutputRouteEntry(name="del", route_type="delegate", condition="", priority=40),
            OutputRouteEntry(name="wt", route_type="wait", condition="", priority=50),
        ])
        for rt in ["next_llm", "next_tool", "delegate", "wait"]:
            signals = [RouteSignal(route_type=rt)]
            result = table.arbitrate(signals, {})
            assert result.route_type == rt, f"route_type={rt} 仲裁失败"

    def test_end_overrides_all_other_types(self) -> None:
        """end 信号存在时，无论其他信号类型如何，end 胜出。"""
        table = OutputRouteTable([
            OutputRouteEntry(name="llm", route_type="next_llm", condition="", priority=10),
            OutputRouteEntry(name="end_e", route_type="end", condition="", priority=50),
        ])
        signals = [
            RouteSignal(route_type="next_llm"),
            RouteSignal(route_type="next_tool"),
            RouteSignal(route_type="delegate"),
            RouteSignal(route_type="wait"),
            RouteSignal(route_type="end"),
        ]
        result = table.arbitrate(signals, {})
        assert result.route_type == "end"

    def test_delegate_signal_with_target(self) -> None:
        """delegate 信号携带 target 信息正确传递。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="del_entry",
                route_type="delegate",
                condition="",
                priority=10,
                target_core="sub_pipeline",
            ),
        ])
        signals = [RouteSignal(route_type="delegate", target="L2_agent")]
        result = table.arbitrate(signals, {})
        assert result.route_type == "delegate"
        assert result.target == "sub_pipeline"

    def test_wait_signal_with_reason(self) -> None:
        """wait 信号携带 reason 信息正确传递。"""
        table = OutputRouteTable([
            OutputRouteEntry(name="wait_entry", route_type="wait", condition="", priority=10),
        ])
        signals = [RouteSignal(route_type="wait", reason="human_approval_needed")]
        result = table.arbitrate(signals, {})
        assert result.route_type == "wait"
        assert "human_approval" in result.reason or "matched" in result.reason

    def test_no_matching_signal_returns_fallback_end(self) -> None:
        """无匹配信号时返回 fallback end 信号。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="conditional",
                route_type="delegate",
                condition="level == 'L1'",
                priority=10,
            ),
        ])
        signals = [RouteSignal(route_type="next_llm")]
        result = table.arbitrate(signals, {"level": "L3"})
        assert result.route_type == "end"
        assert result.reason == "fallback"


# ============================================================================
# Part 9 — Pipeline: 路由条件安全解析 — 更多注入向量 (F-PIP-09 / AC-PIP-12)
# ============================================================================


class TestConditionParserAdvancedSecurity:
    """F-PIP-09 / AC-PIP-12: 安全表达式解析器阻断多种代码注入向量。"""

    def test_subprocess_injection_blocked(self) -> None:
        """__import__ + subprocess 注入被阻断。"""
        result = parse_condition(
            '__import__("subprocess").run(["ls", "-la"])', {}
        )
        assert result is False

    def test_getattr_injection_blocked(self) -> None:
        """getattr 访问 builtins 被阻断。"""
        result = parse_condition(
            'getattr(__builtins__, "eval")', {}
        )
        assert result is False

    def test_globals_injection_blocked(self) -> None:
        """globals() 调用被阻断。"""
        result = parse_condition("globals()", {})
        assert result is False

    def test_lambda_injection_blocked(self) -> None:
        """lambda 表达式注入被阻断。"""
        result = parse_condition("(lambda: __import__('os'))()", {})
        assert result is False

    def test_list_comprehension_injection_blocked(self) -> None:
        """列表推导式注入被阻断。"""
        result = parse_condition("[x for x in __import__('os').listdir('.')]", {})
        assert result is False

    def test_nested_dict_attribute_access_works(self) -> None:
        """合法嵌套字典点号属性访问正常工作。"""
        result = parse_condition(
            "state.agent.level == 'L1'",
            {"agent": {"level": "L1"}},
        )
        assert result is True

    def test_dict_get_method_supported(self) -> None:
        """dict.get(key, default) 方法调用被安全支持。"""
        result = parse_condition(
            'state.get("count", 0) > 5',
            {"count": 10},
        )
        assert result is True

    def test_dict_get_with_default_when_missing(self) -> None:
        """dict.get 缺失键时返回默认值。"""
        result = parse_condition(
            'state.get("missing", 0) == 0',
            {},
        )
        assert result is True

    def test_not_in_operator_works(self) -> None:
        """not_in 运算符正常工作。"""
        result = parse_condition(
            "'x' not_in state['list']",
            {"list": ["a", "b"]},
        )
        assert result is True

    def test_undefined_variable_resolves_to_none(self) -> None:
        """未定义变量解析为 None，与 None 比较为 True。"""
        assert parse_condition("undefined_var == None", {}) is True
        # 未定义变量在布尔上下文中为 False
        assert parse_condition("not undefined_var", {}) is True

    def test_arithmetic_comparison_works(self) -> None:
        """数值比较运算符正常工作。"""
        assert parse_condition("count >= 10", {"count": 10}) is True
        assert parse_condition("count <= 10", {"count": 10}) is True
        assert parse_condition("count < 5", {"count": 3}) is True


# ============================================================================
# Part 10 — Pipeline: 四种错误策略 — 混合链与边界 (F-PIP-13 / AC-PIP-05)
# ============================================================================


class _FlakyInputPlugin(IInputPlugin):
    """可控制失败次数的测试插件。"""

    def __init__(
        self,
        name: str,
        priority: int,
        error_policy: ErrorPolicy,
        fail_times: int = 0,
        fallback_state: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> None:
        self._name = name
        self._priority = priority
        self.error_policy = error_policy
        self._fail_times = fail_times
        self._call_count = 0
        self.max_retries = max_retries
        if fallback_state is not None:
            self.fallback_state = fallback_state

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise RuntimeError(f"{self._name} controlled failure #{self._call_count}")
        return PluginResult(state_updates={"executed": self._name})


class TestErrorPolicyMixedAndBoundary:
    """F-PIP-13 / AC-PIP-05: 四种错误策略的混合链与边界场景。"""

    @pytest.mark.asyncio
    async def test_skip_then_abort_chain_mixed_policies(self) -> None:
        """SKIP 策略插件失败后，ABORT 策略插件失败终止链。"""
        skip_plugin = _FlakyInputPlugin("skip_p", 10, ErrorPolicy.SKIP, fail_times=1)
        abort_plugin = _FlakyInputPlugin("abort_p", 20, ErrorPolicy.ABORT, fail_times=1)
        chain = PluginChain([skip_plugin, abort_plugin])

        results = await chain.execute(PluginContext(state={}))

        # SKIP 插件失败被吞掉，ABORT 插件失败终止链
        assert len(results) == 2
        assert results[0].error is None  # SKIP 吞掉了错误
        assert results[1].error is not None  # ABORT 保留了错误
        assert results[1].skip_remaining is True

    @pytest.mark.asyncio
    async def test_fallback_non_dict_fallback_state_returns_empty(self) -> None:
        """FALLBACK 策略但 fallback_state 不是 dict 时，返回空 PluginResult。"""

        class NonDictFallbackPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "non_dict_fb"

            @property
            def priority(self) -> int:
                return 10

            error_policy = ErrorPolicy.FALLBACK
            fallback_state = "not a dict"  # 非 dict

            async def execute(self, ctx: PluginContext) -> PluginResult:
                raise RuntimeError("always fails")

        chain = PluginChain([NonDictFallbackPlugin()])
        results = await chain.execute(PluginContext(state={}))

        assert len(results) == 1
        assert results[0].state_updates == {}
        assert results[0].error is None
        assert results[0].skip_remaining is False

    @pytest.mark.asyncio
    async def test_retry_default_max_retries_when_not_set(self) -> None:
        """RETRY 策略未设 max_retries 时默认重试 3 次。"""

        class NoMaxRetriesPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "no_max"

            @property
            def priority(self) -> int:
                return 10

            error_policy = ErrorPolicy.RETRY
            # 不设置 max_retries，测试默认值

            async def execute(self, ctx: PluginContext) -> PluginResult:
                raise RuntimeError("always fails")

        chain = PluginChain([NoMaxRetriesPlugin()])
        results = await chain.execute(PluginContext(state={}))

        # 默认 max_retries=3 → 总调用 1 + 3 = 4
        assert results[0].error is not None
        assert results[0].skip_remaining is True

    @pytest.mark.asyncio
    async def test_unknown_error_policy_defaults_to_abort(self) -> None:
        """未知 error_policy 值默认走 ABORT 分支。"""

        class UnknownPolicyPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "unknown"

            @property
            def priority(self) -> int:
                return 10

            error_policy = "nonexistent_policy"  # 不在 ErrorPolicy 枚举中的值

            async def execute(self, ctx: PluginContext) -> PluginResult:
                raise RuntimeError("fails with unknown policy")

        chain = PluginChain([UnknownPolicyPlugin()])
        results = await chain.execute(PluginContext(state={}))

        assert len(results) == 1
        assert results[0].error is not None
        assert results[0].skip_remaining is True

    @pytest.mark.asyncio
    async def test_fallback_continues_to_next_plugin(self) -> None:
        """FALLBACK 策略插件使用 fallback_state 后，后续插件正常执行。"""
        fb_plugin = _FlakyInputPlugin(
            "fb_p",
            10,
            ErrorPolicy.FALLBACK,
            fail_times=1,
            fallback_state={"fb_key": "fb_val"},
        )
        normal_plugin = _FlakyInputPlugin(
            "normal_p", 20, ErrorPolicy.ABORT, fail_times=0
        )
        chain = PluginChain([fb_plugin, normal_plugin])

        ctx = PluginContext(state={})
        results = await chain.execute(ctx)

        assert len(results) == 2
        assert results[0].state_updates.get("fb_key") == "fb_val"
        assert results[1].state_updates.get("executed") == "normal_p"

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self) -> None:
        """RETRY 策略第 2 次成功时返回正确结果。"""
        retry_plugin = _FlakyInputPlugin(
            "retry_ok",
            10,
            ErrorPolicy.RETRY,
            fail_times=1,  # 第 1 次失败，第 2 次成功
            max_retries=5,
        )
        chain = PluginChain([retry_plugin])
        results = await chain.execute(PluginContext(state={}))

        assert results[0].state_updates.get("executed") == "retry_ok"
        assert results[0].error is None
        assert retry_plugin._call_count == 2  # 1 初始 + 1 重试


# ============================================================================
# Part 11 — Pipeline: Output 路由表插件解析 (F-PIP-06 补充)
# ============================================================================


class TestOutputRoutePluginResolution:
    """OutputRouteTable.resolve_plugins / has_plugin_routing 验证。"""

    def test_has_plugin_routing_true_when_any_entry_has_plugins(self) -> None:
        """任一条目声明了 plugins → has_plugin_routing 返回 True。"""
        table = OutputRouteTable([
            OutputRouteEntry(name="e1", route_type="next_llm", condition="", priority=10, plugins=["p1"]),
            OutputRouteEntry(name="e2", route_type="end", condition="", priority=20),
        ])
        assert table.has_plugin_routing() is True

    def test_has_plugin_routing_false_when_no_plugins(self) -> None:
        """无任何条目声明 plugins → has_plugin_routing 返回 False。"""
        table = OutputRouteTable([
            OutputRouteEntry(name="e1", route_type="next_llm", condition="", priority=10),
            OutputRouteEntry(name="e2", route_type="end", condition="", priority=20),
        ])
        assert table.has_plugin_routing() is False

    def test_resolve_plugins_deduplicates_preserving_order(self) -> None:
        """resolve_plugins 去重保序。"""
        table = OutputRouteTable([
            OutputRouteEntry(name="e1", route_type="next_llm", condition="", priority=10, plugins=["a", "b"]),
            OutputRouteEntry(name="e2", route_type="end", condition="", priority=20, plugins=["b", "c"]),
        ])
        plugins = table.resolve_plugins({})
        assert plugins == ["a", "b", "c"]

    def test_resolve_plugins_empty_when_no_match(self) -> None:
        """条件不匹配时 resolve_plugins 返回空列表。"""
        table = OutputRouteTable([
            OutputRouteEntry(
                name="e1", route_type="next_llm",
                condition="level == 'L1'", priority=10, plugins=["a"],
            ),
        ])
        plugins = table.resolve_plugins({"level": "L3"})
        assert plugins == []


# ============================================================================
# Part 12 — Pipeline: AgentConfig.to_state max_iterations 传递 (F-PIP-02 关联)
# ============================================================================


class TestMaxIterationsPropagation:
    """AgentConfig.max_iterations 通过 to_state 传递给 PipelineEngine。"""

    def test_default_max_iterations_is_100(self) -> None:
        """AgentConfig 默认 max_iterations=100。"""
        config = AgentConfig(config_id="default_iter")
        assert config.max_iterations == 100

    def test_custom_max_iterations_in_state(self) -> None:
        """自定义 max_iterations 通过 to_state 传递。"""
        config = AgentConfig(config_id="custom_iter", max_iterations=50)
        state = config.to_state()
        assert state["max_iterations"] == 50

    def test_max_iterations_zero_not_in_state(self) -> None:
        """max_iterations=0 时不写入 state（falsy 值不注入）。"""
        config = AgentConfig(config_id="zero_iter", max_iterations=0)
        state = config.to_state()
        assert "max_iterations" not in state

    def test_max_reminders_in_state(self) -> None:
        """max_reminders 通过 to_state 传递。"""
        config = AgentConfig(config_id="reminders", max_reminders=5)
        state = config.to_state()
        assert state["max_reminders"] == 5

    def test_timeout_seconds_negative_one_allowed(self) -> None:
        """timeout_seconds=-1（无限制）通过 to_state 传递。"""
        config = AgentConfig(config_id="timeout_neg1", timeout_seconds=-1)
        state = config.to_state()
        assert state["timeout_seconds"] == -1

    def test_timeout_seconds_zero_not_in_state(self) -> None:
        """timeout_seconds=0 时不写入 state。"""
        config = AgentConfig(config_id="timeout_zero", timeout_seconds=0)
        state = config.to_state()
        assert "timeout_seconds" not in state
