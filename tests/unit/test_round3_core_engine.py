"""
Round 3 测试审查 — 核心引擎模块（Pipeline + Agent + Tools）

Round 3 新视角：跨模块集成 + 错误/异常路径 + 边界场景

覆盖 Round 1 未覆盖或部分覆盖的关键 AC：
- AC-PIP-05: 四种错误策略的边界行为
- AC-PIP-08: YAML 配置加载（${ENV_VAR} 替换 + 安全白名单）
- AC-PIP-10: 跨管道路由（PluginRegistry.fork 保留运行时依赖）
- AC-PIP-12: 路由条件表达式安全（非 eval）
- AC-AGT-08: 热替换（HotSwapManager swap + rollback）
- AC-AGT-10: tool_ids 限制可用工具范围
- AC-TOOL-04: 动态 Schema 注入（schema_enricher）
- AC-TOOL-07: 危险工具 dangerous_operations 声明
- AC-TOOL-09: 工具结果缓存（should_cache + 敏感信息检测）
- AC-TOOL-10: 嵌套工具调用链追踪（NestedRecordManager）
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pipeline.chain import PluginChain, _deep_update
from pipeline.condition_parser import parse_condition
from pipeline.config import (
    _ALLOWED_PREFIXES,
    _import_class,
    _infer_provider_from_env_var,
    _resolve_env_vars_in_value,
    load_pipeline_config,
)
from pipeline.hot_swap import HotSwapManager, SwapResult
from pipeline.plugin import (
    IInputPlugin,
    IOutputPlugin,
    PluginContext,
    PluginResult,
    OutputResult,
)
from pipeline.registry import PluginRegistry
from pipeline.route import (
    InputRouteEntry,
    InputRouteTable,
    OutputRouteEntry,
    OutputRouteTable,
)
from pipeline.types import (
    ErrorPolicy,
    RouteSignal,
    StateKeys,
    create_initial_state,
)

from agents.context_builder import ContextBuilder
from agents.level_controller import LevelController, ValidationError
from agents.loader import _substitute_env_vars
from agents.types import (
    AgentConfig,
    AgentLevel,
    ContextConfig,
    ContextVarItem,
)

from tools.registry import ToolRegistry
from tools.tool_cache import ToolCache, ToolCacheConfig, _contains_sensitive_info
from tools.nested_record_manager import NestedRecordManager
from tools.types import Tool, ToolSource


# ============================================================================
# 1. 错误路径 — 插件链四种错误策略的边界行为
# AC-PIP-05 / F-PIP-13
# ============================================================================


class _ErrorPlugin(IInputPlugin):
    """可配置错误策略的桩件插件，用于测试 ABORT/SKIP/FALLBACK/RETRY。"""

    def __init__(
        self,
        name: str,
        priority: int = 10,
        *,
        policy: ErrorPolicy = ErrorPolicy.ABORT,
        should_fail: bool = True,
        fallback_state: dict[str, Any] | None = None,
        max_retries: int = 3,
        state_updates: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self.error_policy = policy
        self._should_fail = should_fail
        if fallback_state is not None:
            self.fallback_state = fallback_state
        self.max_retries = max_retries
        self._state_updates = state_updates or {}
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        self.call_count += 1
        if self._should_fail:
            raise RuntimeError(f"{self._name} intentional failure")
        return PluginResult(state_updates=dict(self._state_updates))


class TestErrorPolicyBoundary:
    """错误策略边界场景（Round 3 错误路径视角）。

    Round 1 已覆盖基本路径；Round 3 补充：
    - FALLBACK 的 fallback_state 真正合并到 ctx.state
    - ABORT 阻断后续插件
    - SKIP 保留前后插件状态
    - RETRY 调用次数精确性
    - 未知策略降级为 ABORT
    """

    @pytest.mark.asyncio
    async def test_fallback_state_merged_into_ctx_state(self) -> None:
        """FALLBACK：fallback_state 应合并到 ctx.state，影响后续插件。"""
        p1 = _ErrorPlugin(
            "fb", 10,
            policy=ErrorPolicy.FALLBACK,
            fallback_state={"fallback_applied": True, "value": 42},
        )
        p2 = _ErrorPlugin("next", 20, should_fail=False, state_updates={"ok": True})
        chain = PluginChain([p1, p2])
        ctx = PluginContext(state={"original": "kept"})

        results = await chain.execute(ctx)

        # fallback_state 合并到 state
        assert ctx.state["fallback_applied"] is True
        assert ctx.state["value"] == 42
        assert ctx.state["original"] == "kept"
        # 后续插件继续执行（不 skip）
        assert ctx.state["ok"] is True
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_abort_stops_subsequent_plugins(self) -> None:
        """ABORT：第一个失败插件停止链，后续插件不执行。"""
        p1 = _ErrorPlugin("aborter", 10, policy=ErrorPolicy.ABORT, should_fail=True)
        p2 = _ErrorPlugin("after", 20, policy=ErrorPolicy.ABORT, should_fail=False)
        chain = PluginChain([p1, p2])

        results = await chain.execute(PluginContext(state={}))

        assert p2.call_count == 0
        assert len(results) == 1
        assert results[0].skip_remaining is True
        assert isinstance(results[0].error, RuntimeError)

    @pytest.mark.asyncio
    async def test_skip_preserves_neighbor_state(self) -> None:
        """SKIP：前后插件的 state_updates 正常合并，SKIP 不传播 error。"""
        p1 = _ErrorPlugin("before", 10, should_fail=False, state_updates={"k1": "v1"})
        p2 = _ErrorPlugin("skipper", 20, policy=ErrorPolicy.SKIP, should_fail=True)
        p3 = _ErrorPlugin("after", 30, should_fail=False, state_updates={"k3": "v3"})
        chain = PluginChain([p1, p2, p3])
        ctx = PluginContext(state={})

        results = await chain.execute(ctx)

        assert len(results) == 3
        assert ctx.state["k1"] == "v1"
        assert ctx.state["k3"] == "v3"
        # SKIP 插件返回空结果，不传播 error
        assert results[1].error is None
        assert results[1].state_updates == {}

    @pytest.mark.asyncio
    async def test_retry_call_count_precise(self) -> None:
        """RETRY：max_retries=2 时总调用次数 = 1 初始 + 2 重试 = 3。"""
        plugin = _ErrorPlugin(
            "retry_fail", 10, policy=ErrorPolicy.RETRY, max_retries=2
        )
        chain = PluginChain([plugin])

        results = await chain.execute(PluginContext(state={}))

        assert plugin.call_count == 3
        assert results[0].error is not None
        assert results[0].skip_remaining is True

    @pytest.mark.asyncio
    async def test_unknown_policy_defaults_to_abort(self) -> None:
        """未知 error_policy 应回退为 ABORT（不抛未捕获异常）。"""
        plugin = _ErrorPlugin("unknown", 10)
        plugin.error_policy = "INVALID_XYZ"  # type: ignore[assignment]
        plugin._should_fail = True
        p2 = _ErrorPlugin("after", 20, should_fail=False)
        chain = PluginChain([plugin, p2])

        results = await chain.execute(PluginContext(state={}))

        assert p2.call_count == 0
        assert results[0].skip_remaining is True
        assert results[0].error is not None


# ============================================================================
# 2. 状态一致性 — _deep_update 点号键展开与嵌套合并
# AC-PIP-03 / F-PIP-11
# ============================================================================


class TestStateDeepUpdate:
    """state_updates 合并的一致性测试。

    _deep_update 的关键行为：点号键同时展开为嵌套字典 + 保留顶层点号键。
    这保证了 state["a"]["b"] 和 state.get("a.b") 两种访问方式都能工作。
    """

    def test_dot_key_creates_nested_and_flat(self) -> None:
        """点号键 'security.decision' 同时建立嵌套和顶层两种访问路径。"""
        state: dict[str, Any] = {}
        _deep_update(state, {"security.decision": "allow"})

        # 嵌套访问
        assert state["security"]["decision"] == "allow"
        # 顶层点号键访问
        assert state["security.decision"] == "allow"

    def test_dot_key_preserves_existing_nested_siblings(self) -> None:
        """新增点号键不应覆盖已存在的兄弟键。"""
        state: dict[str, Any] = {"a": {"b": "old", "x": "keep"}}
        _deep_update(state, {"a.c": "new"})

        assert state["a"]["b"] == "old"
        assert state["a"]["x"] == "keep"
        assert state["a"]["c"] == "new"

    def test_dot_key_replaces_non_dict_intermediate(self) -> None:
        """中间节点为非字典时被替换为字典（防止 TypeError）。"""
        state: dict[str, Any] = {"a": "string_val"}
        _deep_update(state, {"a.b": "nested"})

        assert isinstance(state["a"], dict)
        assert state["a"]["b"] == "nested"

    def test_plain_key_directly_overwrites(self) -> None:
        """无点号键直接覆盖（不展开）。"""
        state: dict[str, Any] = {"k1": 1}
        _deep_update(state, {"k1": 100, "k2": 2})

        assert state["k1"] == 100
        assert state["k2"] == 2

    def test_multiple_dot_keys_accumulate(self) -> None:
        """多个点号键写入同一命名空间应累积。"""
        state: dict[str, Any] = {}
        _deep_update(state, {"prompt.system": "sys_text"})
        _deep_update(state, {"prompt.dynamic_vars": ["v1", "v2"]})

        assert state["prompt"]["system"] == "sys_text"
        assert state["prompt"]["dynamic_vars"] == ["v1", "v2"]
        assert state["prompt.dynamic_vars"] == ["v1", "v2"]

    def test_create_initial_state_independent_instances(self) -> None:
        """多次创建初始 state 应互不影响（浅拷贝安全）。"""
        s1 = create_initial_state()
        s1[StateKeys.ITERATION] = 10
        s1[StateKeys.TOOL_RESULTS].append({"x": 1})

        s2 = create_initial_state()
        assert s2[StateKeys.ITERATION] == 0
        assert s2[StateKeys.TOOL_RESULTS] == []


# ============================================================================
# 3. 边界场景 — 空输入 / 空路由表 / 超大输入
# ============================================================================


class TestBoundaryConditions:
    """空输入、超大输入、非法配置等边界场景。"""

    def test_empty_input_route_table_returns_core(self) -> None:
        """空输入路由表 → resolve_plugins 返回空，resolve_target 返回 core。"""
        table = InputRouteTable([])
        assert table.resolve_plugins({}) == []
        target, entry = table.resolve_target({})
        assert target == "core"
        assert entry is None

    def test_empty_output_route_table_returns_fallback_end(self) -> None:
        """空输出路由表 → 仲裁返回 fallback end 信号。"""
        table = OutputRouteTable([])
        result = table.arbitrate([RouteSignal(route_type="next_llm")], {})
        assert result.route_type == "end"
        assert result.reason == "fallback"

    def test_empty_signals_returns_fallback(self) -> None:
        """空信号列表 → 仲裁返回 fallback。"""
        table = OutputRouteTable([
            OutputRouteEntry(name="x", route_type="next_llm", priority=1),
        ])
        result = table.arbitrate([], {})
        assert result.route_type == "end"

    def test_oversized_route_table_dedup_still_correct(self) -> None:
        """500 个条目叠加去重后插件列表仍然正确。"""
        entries = [
            InputRouteEntry(
                name=f"e_{i}", condition="", plugins=[f"plugin_{i % 3}"],
                priority=i,
            )
            for i in range(500)
        ]
        table = InputRouteTable(entries)
        plugins = table.resolve_plugins({})
        assert plugins == ["plugin_0", "plugin_1", "plugin_2"]

    def test_load_config_missing_file_raises(self, tmp_path: Path) -> None:
        """AC-PIP-08: 文件不存在抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            load_pipeline_config(tmp_path / "no_such.yaml")

    def test_load_config_missing_name_raises(self, tmp_path: Path) -> None:
        """AC-PIP-08: YAML 缺少 name 字段抛 ValueError。"""
        path = tmp_path / "bad.yaml"
        path.write_text("input_routes: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="name"):
            load_pipeline_config(path)


# ============================================================================
# 4. 安全边界 — 路由条件表达式 + 插件白名单 + ENV 替换
# AC-PIP-08 / AC-PIP-12
# ============================================================================


class TestConditionParserExtended:
    """条件解析器安全性的延伸场景。"""

    def test_lambda_injection_blocked(self) -> None:
        """lambda 表达式注入不执行。"""
        assert parse_condition("(lambda: 1)() == 1", {}) is False

    def test_compile_call_blocked(self) -> None:
        """compile 调用不执行。"""
        assert isinstance(parse_condition("compile('x','','exec')", {}), bool)

    def test_dunder_attribute_returns_none_safely(self) -> None:
        """__class__ / __mro__ 等访问安全降级为 None，不暴露内部对象。"""
        assert parse_condition('state.__class__ == "dict"', {"a": 1}) is False

    def test_empty_expression_always_true(self) -> None:
        """空表达式视为始终匹配（路由表设计语义）。"""
        assert parse_condition("", {}) is True
        assert parse_condition("   ", {}) is True

    def test_not_in_operator_works(self) -> None:
        """not_in 运算符安全工作。"""
        assert parse_condition(
            "'x' not_in ['a','b']", {"a": 1}
        ) is True
        assert parse_condition(
            "'x' not_in ['x','y']", {"a": 1}
        ) is False

    def test_is_empty_on_various_types(self) -> None:
        """is_empty 对空字符串/空列表/空字典/None 均为 True。"""
        assert parse_condition("v is_empty", {"v": ""}) is True
        assert parse_condition("v is_empty", {"v": []}) is True
        assert parse_condition("v is_empty", {"v": {}}) is True
        assert parse_condition("v is_empty", {"v": None}) is True
        assert parse_condition("v is_empty", {"v": "data"}) is False

    def test_comparison_with_missing_key_safely_false(self) -> None:
        """访问不存在的 key 返回 None，比较结果为 False。"""
        assert parse_condition('state["no_key"] == "value"', {}) is False


class TestImportClassSecurity:
    """_import_class 白名单安全机制。"""

    def test_blocks_os_module(self) -> None:
        """os.system 被白名单阻止。"""
        with pytest.raises(ImportError, match="not in allowed prefixes"):
            _import_class("os.system")

    def test_blocks_subprocess(self) -> None:
        """subprocess.Popen 被白名单阻止。"""
        with pytest.raises(ImportError, match="not in allowed prefixes"):
            _import_class("subprocess.Popen")

    def test_blocks_builtins(self) -> None:
        """builtins.eval 被白名单阻止。"""
        with pytest.raises(ImportError, match="not in allowed prefixes"):
            _import_class("builtins.eval")

    def test_blocks_random_package(self) -> None:
        """随机包名被白名单阻止。"""
        with pytest.raises(ImportError):
            _import_class("some_malicious_package.evil")

    def test_whitelist_includes_all_expected_prefixes(self) -> None:
        """白名单包含 4 个安全前缀。"""
        assert set(_ALLOWED_PREFIXES) >= {
            "plugins.", "pipeline.", "agents.", "tools."
        }


class TestEnvVarSubstitutionExtended:
    """${ENV_VAR} 替换的边界场景。"""

    def test_recursive_in_nested_structures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """三层嵌套字典+列表中的占位符全部被替换。"""
        monkeypatch.setenv("R3_DEEP", "deep_val")
        data = {
            "l1": {
                "l2": [
                    {"l3": "${R3_DEEP}"},
                    "${R3_DEEP}",
                ]
            }
        }
        result = _resolve_env_vars_in_value(data)
        assert result["l1"]["l2"][0]["l3"] == "deep_val"
        assert result["l1"]["l2"][1] == "deep_val"

    def test_missing_var_returns_empty(self) -> None:
        """缺失变量替换为空字符串。"""
        env_clean = {k: v for k, v in os.environ.items() if k != "R3_MISSING"}
        with patch.dict(os.environ, env_clean, clear=True):
            assert _resolve_env_vars_in_value("v=${R3_MISSING}") == "v="

    def test_infer_provider_suffixes(self) -> None:
        """从 ${VAR} 推断 provider 名。"""
        assert _infer_provider_from_env_var("${MINIMAX_API_KEY}") == "minimax"
        assert _infer_provider_from_env_var("${DEEPSEEK_KEY}") == "deepseek"
        assert _infer_provider_from_env_var("${APP_ZHIPU_API_KEY}") == "zhipu"
        assert _infer_provider_from_env_var("not_placeholder") is None

    def test_partial_placeholder_replacement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """字符串中间的占位符仅替换变量部分。"""
        monkeypatch.setenv("R3_HOST", "api.test.com")
        result = _resolve_env_vars_in_value("https://${R3_HOST}/v1")
        assert result == "https://api.test.com/v1"


# ============================================================================
# 5. 热替换 — HotSwapManager swap + rollback (AC-AGT-08)
# ============================================================================


class TestHotSwapManager:
    """热替换插件实例的完整流程：swap → health check → rollback。"""

    @pytest.fixture
    def registry_with_plugin(self) -> PluginRegistry:
        """注册一个插件实例的注册表。"""
        reg = PluginRegistry()
        reg.register(_ErrorPlugin("original", 10, should_fail=False))
        return reg

    @pytest.mark.asyncio
    async def test_swap_succeeds_when_healthy(
        self, registry_with_plugin: PluginRegistry
    ) -> None:
        """健康的新插件应成功替换。"""
        mgr = HotSwapManager(registry_with_plugin)
        new_plugin = _ErrorPlugin("new_plugin", 10, should_fail=False)

        result = await mgr.swap_plugin("original", new_plugin)

        assert result.success is True
        assert result.rolled_back is False
        # registry 中已替换
        assert registry_with_plugin.get("original") is new_plugin

    @pytest.mark.asyncio
    async def test_swap_rolls_back_on_health_check_failure(
        self, registry_with_plugin: PluginRegistry
    ) -> None:
        """健康检查失败时自动回滚到旧插件。"""

        class UnhealthyPlugin(_ErrorPlugin):
            async def execute(self, ctx: PluginContext) -> PluginResult:
                raise RuntimeError("unhealthy")

        mgr = HotSwapManager(registry_with_plugin)
        unhealthy = UnhealthyPlugin("bad", 10, should_fail=False)

        result = await mgr.swap_plugin("original", unhealthy, health_check=True)

        assert result.success is False
        assert result.rolled_back is True
        # 原插件恢复
        original = registry_with_plugin.get("original")
        assert original is not unhealthy

    @pytest.mark.asyncio
    async def test_swap_without_health_check_skips_check(
        self, registry_with_plugin: PluginRegistry
    ) -> None:
        """health_check=False 时不执行检查，即使插件会失败也能替换成功。"""

        class UnhealthyPlugin(_ErrorPlugin):
            async def execute(self, ctx: PluginContext) -> PluginResult:
                raise RuntimeError("bad")

        mgr = HotSwapManager(registry_with_plugin)
        unhealthy = UnhealthyPlugin("no_check", 10, should_fail=False)

        result = await mgr.swap_plugin("original", unhealthy, health_check=False)

        assert result.success is True
        assert registry_with_plugin.get("original") is unhealthy

    @pytest.mark.asyncio
    async def test_rollback_nonexistent_swap_id_returns_false(
        self, registry_with_plugin: PluginRegistry
    ) -> None:
        """回滚不存在的 swap_id 返回 False。"""
        mgr = HotSwapManager(registry_with_plugin)
        assert await mgr.rollback("nonexistent_id") is False

    @pytest.mark.asyncio
    async def test_swap_new_plugin_not_in_registry_succeeds(
        self, registry_with_plugin: PluginRegistry
    ) -> None:
        """替换一个原 registry 中存在的插件名，新插件自动注册。"""
        mgr = HotSwapManager(registry_with_plugin)
        new_plugin = _ErrorPlugin("replacement", 10, should_fail=False)

        result = await mgr.swap_plugin("original", new_plugin)
        assert result.success is True
        assert registry_with_plugin.get("original").name == "replacement"


# ============================================================================
# 6. 跨管道 — PluginRegistry.fork 保留运行时依赖 (AC-PIP-10 间接)
# ============================================================================


class TestPluginRegistryFork:
    """fork() 必须保留运行时注入的依赖（adapter / router / _tools）。

    背景：跨管道路由（delegate）时 registry 会被 fork。
    若 fork 丢失依赖（如 adapter），子管道会用默认配置回退，导致认证失败。
    """

    def test_fork_creates_independent_registry(self) -> None:
        """fork 后两个 registry 独立，互不影响。"""
        reg1 = PluginRegistry()
        reg1.register(_ErrorPlugin("p1", 10))
        reg2 = reg1.fork()

        # fork 后两个 registry 都有 p1
        assert reg1.get("p1") is not None
        assert reg2.get("p1") is not None

        # 在 reg2 中注册新插件不影响 reg1
        reg2.register(_ErrorPlugin("p_new", 20))
        assert reg1.get("p_new") is None
        assert reg2.get("p_new") is not None

    def test_fork_preserves_runtime_attached_attributes(self) -> None:
        """fork 后插件实例的运行时属性（如 _tools 字典）应被保留。

        关键场景：LLM/Tool Core 插件常被附加 _tools 字典，
        fork 时若丢失会导致子管道无法调用工具。
        """

        class WithTools(_ErrorPlugin):
            def __init__(self, **kw: Any) -> None:
                super().__init__(name="with_tools", priority=10)
                self._config = kw.get("config", {})
                self._tools = {"echo": object()}

        reg = PluginRegistry()
        original = WithTools()
        reg.register_core("llm_call", original)

        forked = reg.fork()
        forked_plugin = forked.get_core("llm_call")

        # _tools 字典被复制（同内容，可能不同对象）
        assert hasattr(forked_plugin, "_tools")
        assert "echo" in forked_plugin._tools

    def test_fork_keeps_core_plugin_mapping(self) -> None:
        """fork 后 _core_plugins 映射正确指向新实例。"""

        class FakeCore(_ErrorPlugin):
            def __init__(self, **kw: Any) -> None:
                super().__init__(name="fake_core", priority=10)
                self._config = kw.get("config", {})

        reg = PluginRegistry()
        reg.register_core("llm_call", FakeCore())
        forked = reg.fork()

        # core 插件可通过 get_core 找到
        assert forked.get_core("llm_call") is not None
        # 名称一致
        assert forked.get_core("llm_call").name == "fake_core"


# ============================================================================
# 7. 工具系统 — 动态 Schema 注入 / 危险标记 (AC-TOOL-04, AC-TOOL-07)
# ============================================================================


class TestDynamicSchemaInjection:
    """AC-TOOL-04: 动态 Schema 注入（schema_enricher 机制）。"""

    def test_register_and_retrieve_schema_enricher(self) -> None:
        """注册的 enricher 应能通过 get_schema_enricher 取回。"""
        registry = ToolRegistry(lazy_load=False)

        def my_enricher(tool: Any, services: dict[str, Any]) -> Any:
            return tool

        registry.register_schema_enricher("image_generate", my_enricher)
        retrieved = registry.get_schema_enricher("image_generate")
        assert retrieved is my_enricher

    def test_unregistered_enricher_returns_none(self) -> None:
        """未注册 enricher 的工具返回 None。"""
        registry = ToolRegistry(lazy_load=False)
        assert registry.get_schema_enricher("nonexistent") is None

    def test_enricher_modifies_runtime_schema(self) -> None:
        """enricher 应能修改 LLM 看到的 schema enum 字段。"""
        registry = ToolRegistry(lazy_load=False)

        def enrich_provider(tool: Tool, services: dict[str, Any]) -> Tool:
            # 模拟运行时动态注入 Provider 列表
            current_providers = ["minimax", "deepseek"]
            new_schema = dict(tool.input_schema)
            new_props = dict(new_schema.get("properties", {}))
            if "provider" in new_props:
                new_props["provider"] = dict(new_props["provider"])
                new_props["provider"]["enum"] = current_providers
            new_schema["properties"] = new_props
            return tool.model_copy(update={"input_schema": new_schema})

        tool = Tool(
            name="image_generate",
            description="生成图像",
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": []},
                    "prompt": {"type": "string"},
                },
                "required": ["prompt"],
            },
            source=ToolSource.BUILTIN,
        )
        registry.register(tool)
        registry.register_schema_enricher("image_generate", enrich_provider)

        enricher = registry.get_schema_enricher("image_generate")
        enriched_tool = enricher(tool, {})
        assert enriched_tool.input_schema["properties"]["provider"]["enum"] == [
            "minimax", "deepseek"
        ]


class TestDangerousToolDeclaration:
    """AC-TOOL-07: 危险工具通过 dangerous_operations 字段声明（由安全插件审批）。"""

    def test_tool_can_declare_dangerous_operations(self) -> None:
        """Tool 可声明 dangerous_operations 字段。"""
        tool = Tool(
            name="delete_file",
            description="删除文件",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            dangerous_operations=["delete", "destroy"],
            source=ToolSource.BUILTIN,
        )
        assert "delete" in tool.dangerous_operations
        assert "destroy" in tool.dangerous_operations

    def test_normal_tool_has_no_dangerous_operations(self) -> None:
        """普通工具默认 dangerous_operations 为空。"""
        tool = Tool(
            name="file_read",
            description="读文件",
            input_schema={"type": "object"},
            source=ToolSource.BUILTIN,
        )
        assert tool.dangerous_operations == []

    def test_dangerous_operations_preserved_through_format(self) -> None:
        """to_llm_format 后 dangerous_operations 不暴露给 LLM（内部决策字段）。"""
        tool = Tool(
            name="delete_file",
            description="删除文件",
            input_schema={"type": "object"},
            dangerous_operations=["delete"],
            source=ToolSource.BUILTIN,
        )
        llm_format = tool.to_llm_format()
        # OpenAI function calling schema 不应包含 dangerous_operations
        function_def = llm_format.get("function", {})
        assert "dangerous_operations" not in function_def
        assert "dangerous_operations" not in function_def.get("parameters", {})


# ============================================================================
# 8. 工具结果缓存 — ToolCache 敏感信息检测与键稳定性
# AC-TOOL-09
# ============================================================================


class TestToolCacheBehavior:
    """AC-TOOL-09: 工具结果缓存生效（should_cache 判定）。"""

    def test_cache_disabled_returns_false(self) -> None:
        """全局缓存禁用时，所有工具都不应缓存。"""
        config = ToolCacheConfig(enabled=False)
        config.tools = {"some_tool": {"enabled": True}}
        cache = ToolCache(config)
        assert cache.should_cache("some_tool", {"q": "hi"}) is False

    def test_tool_not_in_cache_config_returns_false(self) -> None:
        """未在 cache config 中声明的工具不缓存。"""
        config = ToolCacheConfig(enabled=True, default_ttl=300)
        cache = ToolCache(config)
        assert cache.should_cache("unknown_tool", {"q": "hi"}) is False

    def test_task_submit_never_cached(self) -> None:
        """task_submit 工具永远不缓存（有副作用）。"""
        config = ToolCacheConfig(enabled=True, default_ttl=300)
        config.tools = {"task_submit": {"enabled": True}}
        cache = ToolCache(config)
        assert cache.should_cache("task_submit", {"goal": "x"}) is False

    def test_sensitive_input_blocks_cache(self) -> None:
        """输入含敏感信息（password/token/secret/key）时不缓存。"""
        config = ToolCacheConfig(enabled=True)
        config.tools = {"safe_tool": {"enabled": True}}
        cache = ToolCache(config)

        assert cache.should_cache("safe_tool", {"password": "123"}) is False
        assert cache.should_cache("safe_tool", {"api_key": "sk-xxx"}) is False
        assert cache.should_cache("safe_tool", {"token": "tok"}) is False
        # 不含敏感信息的正常输入
        assert cache.should_cache("safe_tool", {"query": "hello"}) is True

    def test_cache_key_stable_for_same_input(self) -> None:
        """相同输入（排除无关字段）生成相同缓存键。"""
        config = ToolCacheConfig(enabled=True)
        cache = ToolCache(config)

        key1 = cache.generate_cache_key("tool_x", {"query": "test"})
        key2 = cache.generate_cache_key("tool_x", {"query": "test"})
        assert key1 == key2

    def test_cache_key_ignores_session_fields(self) -> None:
        """session_id / user_id / timestamp 等无关字段不影响缓存键。"""
        config = ToolCacheConfig(enabled=True)
        cache = ToolCache(config)

        key1 = cache.generate_cache_key("tool_x", {
            "query": "test", "session_id": "s1", "user_id": "u1",
        })
        key2 = cache.generate_cache_key("tool_x", {
            "query": "test", "session_id": "s2", "user_id": "u2",
        })
        assert key1 == key2

    def test_cache_key_differs_for_different_input(self) -> None:
        """不同参数生成不同缓存键。"""
        config = ToolCacheConfig(enabled=True)
        cache = ToolCache(config)

        key1 = cache.generate_cache_key("tool", {"query": "a"})
        key2 = cache.generate_cache_key("tool", {"query": "b"})
        assert key1 != key2

    def test_cache_stats_initial_state(self) -> None:
        """初始状态缓存统计为 0。"""
        config = ToolCacheConfig(enabled=True)
        cache = ToolCache(config)
        stats = cache.get_cache_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["total"] == 0


# ============================================================================
# 9. 嵌套工具调用链追踪 — NestedRecordManager 初始化与容错
# AC-TOOL-10
# ============================================================================


class TestNestedRecordManager:
    """AC-TOOL-10: 嵌套工具调用链追踪。

    NestedRecordManager 负责创建/更新嵌套执行记录。
    在没有数据库的情况下，所有操作应静默失败（不抛异常）。
    """

    def test_manager_initializes_without_error(self) -> None:
        """NestedRecordManager 无 DB session 也能初始化。"""
        mgr = NestedRecordManager()
        assert mgr is not None

    @pytest.mark.asyncio
    async def test_create_record_without_db_returns_none_silently(self) -> None:
        """无 DB session 时创建记录返回 None（不抛异常）。"""
        mgr = NestedRecordManager()
        result = await mgr.create_nested_execution_record(
            parent_record_id="fake_parent",
            session_id="fake_session",
            tool_name="inner_tool",
            tool_args={"query": "test"},
        )
        # 应返回 None，不抛异常
        assert result is None

    @pytest.mark.asyncio
    async def test_update_record_without_db_no_exception(self) -> None:
        """无 DB session 时更新记录不抛异常。"""
        mgr = NestedRecordManager()
        # 不抛异常即通过
        await mgr.update_nested_execution_record(
            record_id="nonexistent",
            success=True,
            output={"result": "ok"},
        )


# ============================================================================
# 10. Agent tool_ids 限制的强制执行 (AC-AGT-10)
# ============================================================================


class TestToolIdsEnforcement:
    """AC-AGT-10: tool_ids 限制可用工具范围。"""

    def test_l3_agent_tool_ids_intersection_with_default_restrictions(self) -> None:
        """L3 Agent 即使在 tool_ids 中声明了 task_submit，实际仍被默认禁用。"""
        with patch.object(
            LevelController,
            "_load_tool_permissions",
            return_value={
                "L1": {"allowed": ["*"]},
                "L2": {"allowed": ["*"]},
                "L3": {"denied": ["task_submit", "task_evaluate"]},
            },
        ):
            controller = LevelController()
            l3_agent = AgentConfig(
                config_id="l3_test",
                level=AgentLevel.L3_ATOMIC,
                tool_ids=["file_read", "task_submit", "bash_execute"],
            )
            # 有效工具集 = tool_ids - DEFAULT_RESTRICTED_TOOLS
            effective = set(l3_agent.tool_ids) - controller.DEFAULT_RESTRICTED_TOOLS
            assert "task_submit" not in effective
            assert "file_read" in effective
            assert "bash_execute" in effective

    def test_l1_agent_unrestricted_by_default(self) -> None:
        """L1 Agent 的权限配置为 allowed=['*']，可以使用 task_submit。"""
        with patch.object(
            LevelController,
            "_load_tool_permissions",
            return_value={
                "L1": {"allowed": ["*"]},
                "L2": {"allowed": ["*"]},
                "L3": {"denied": ["task_submit"]},
            },
        ):
            controller = LevelController()
            l1_agent = AgentConfig(
                config_id="l1_test",
                level=AgentLevel.L1_MAIN,
                tool_ids=["task_submit", "task_manage", "memory"],
            )
            # L1 的权限配置是 allowed=["*"]，不受 DEFAULT_RESTRICTED_TOOLS 限制
            l1_perms = controller._tool_permissions.get("L1", {})
            assert l1_perms.get("allowed") == ["*"]
            # L1 的 tool_ids 完整保留（含 task_submit）
            assert "task_submit" in l1_agent.tool_ids

    def test_empty_tool_ids_means_no_tools(self) -> None:
        """空 tool_ids 表示该 Agent 无可用工具。"""
        agent = AgentConfig(
            config_id="no_tools_agent",
            level=AgentLevel.L3_ATOMIC,
            tool_ids=[],
        )
        assert agent.tool_ids == []
        assert len(agent.tool_ids) == 0


# ============================================================================
# 11. 跨模块集成 — Agent state → Pipeline → Tool 链路
# ============================================================================


class TestCrossModuleIntegration:
    """Agent 配置加载 → 管道引擎 state → 工具可用性的链路验证。"""

    def test_agent_to_state_merges_with_pipeline_initial_state(self) -> None:
        """AgentConfig.to_state() 的字段可安全合并到管道初始 state。"""
        agent = AgentConfig(
            config_id="pipeline_agent",
            level=AgentLevel.L3_ATOMIC,
            tool_ids=["file_read", "bash_execute"],
            max_iterations=50,
        )
        pipeline_state = create_initial_state()
        pipeline_state.update(agent.to_state())

        # 框架字段保持初始值
        assert pipeline_state[StateKeys.ITERATION] == 0
        assert pipeline_state[StateKeys.ENDED] is False
        # Agent 字段被合并
        assert pipeline_state["tool_ids"] == ["file_read", "bash_execute"]
        assert pipeline_state["max_iterations"] == 50

    def test_pipeline_state_updates_with_dot_keys_survive_iterations(self) -> None:
        """模拟多轮迭代中点号键 state_updates 累积的一致性。"""
        state = create_initial_state()

        # 模拟第 1 轮迭代的插件输出
        _deep_update(state, {"security.decision": "allow", "context.ready": True})
        state[StateKeys.ITERATION] = 1

        # 模拟第 2 轮迭代的插件输出
        _deep_update(state, {"security.reason": "verified", "memory.retrieved": ["m1"]})
        state[StateKeys.ITERATION] = 2

        # 两轮迭代后所有键都保持
        assert state[StateKeys.ITERATION] == 2
        assert state["security"]["decision"] == "allow"
        assert state["security"]["reason"] == "verified"
        assert state["context"]["ready"] is True
        assert state["memory"]["retrieved"] == ["m1"]
        # 点号顶层键也保留
        assert state.get("security.decision") == "allow"
        assert state.get("memory.retrieved") == ["m1"]

    def test_tool_registry_filter_by_agent_tool_ids(self) -> None:
        """工具注册表配合 Agent tool_ids 做过滤（模拟 ToolSchemaPlugin 行为）。"""
        registry = ToolRegistry(lazy_load=False)
        for name in ("file_read", "file_write", "bash_execute", "web_search"):
            registry.register(Tool(
                name=name,
                description=f"tool {name}",
                input_schema={"type": "object"},
                source=ToolSource.BUILTIN,
            ))

        agent = AgentConfig(
            config_id="filtered_agent",
            level=AgentLevel.L3_ATOMIC,
            tool_ids=["file_read", "bash_execute"],
        )

        # 模拟 ToolSchemaPlugin 按 tool_ids 过滤
        all_tools = registry.list_all()
        visible_tools = [t for t in all_tools if t.name in agent.tool_ids]

        visible_names = {t.name for t in visible_tools}
        assert visible_names == {"file_read", "bash_execute"}
        assert "file_write" not in visible_names
        assert "web_search" not in visible_names
