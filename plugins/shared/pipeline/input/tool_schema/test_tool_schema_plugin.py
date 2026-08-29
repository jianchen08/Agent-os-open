# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""tool_schema 工具 Schema 注入插件 + MCP server.py 适配层测试。

覆盖行为面：
- 配置接口：name/priority、enabled、tool_ids、include_tools_description_in_prompt
- 禁用时返回空 tool_schemas
- 无 tool_registry 服务（KeyError）：
    - state 无 tool_schemas → {"tool_schemas": []}
    - state 有内核注入 tool_schemas + 有 tool_ids → 按 agent 工具面过滤（保留 spill_retrieve）
    - 过滤后工具面漂移（tool_ids 引用注册表不存在工具）→ warning 留痕，结果不覆盖为全量
    - state 有 tool_schemas 但无 tool_ids → warning 留痕（配置断链），返回 {} 不覆盖
- tool_registry 服务可用：
    - 按 active_tool_ids 精确取工具（含动态工具合并、动态预加载）
    - 取不到的工具 warning 留痕
    - 无 active_tool_ids → list_all
    - 空工具 → {"tool_schemas": []}
    - schema 丰富器成功/失败降级（to_llm_format 兜底）
    - include_tools_description_in_prompt=true 生成人类可读描述（多工具、数量断言）
- 层级解析 _resolve_agent_level：数字串/前缀 L/非法值/空
- 服务收集 _get_services：部分服务缺失不中断
- execute 入口：state_updates 透传
- server.py：get_instance 懒构建缓存、on_load 预热、on_unload 清缓存、execute 工具
  契约（PluginResult → dict 序列化、route_signal/skip_remaining、dict 直通）

外部依赖（工具注册表）用轻量替身；内部逻辑（过滤/枚举合并/描述拼接）全部真实实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from agentos_plugin_sdk.tool_types import Tool, ToolSource

pytestmark = pytest.mark.unit

# 复制 server.py 的 sys.path 机制：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

# 全车道共跑时裸名 `plugin` 会被先收集目录的同名模块劫持，按 _THIS_DIR 显式路径加载
# （与 prompt_build/test_workflow_and_server.py 的 _load_plugin_module 同范式）。
def _load_plugin() -> Any:
    mod_name = "tool_schema_plugin_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, Path(_THIS_DIR) / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_PLUGIN_MOD = _load_plugin()
ToolSchemaPlugin: Any = _PLUGIN_MOD.ToolSchemaPlugin


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=dict(state or {}), _services=dict(services or {}))


def _make_tool(name: str, description: str = "desc") -> Tool:
    """构造真实 Tool（to_llm_format 走真实实现）。"""
    return Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        source=ToolSource.CODE,
    )


def _make_registry(tools: list[Tool], enrichers: dict[str, Any] | None = None) -> Any:
    """轻量工具注册表替身：真实 list_all/get/get_schema_enricher/get_dynamic_tool_names。"""

    class _Registry:
        def __init__(self) -> None:
            self._by_name = {t.name: t for t in tools}
            self._enrichers = enrichers or {}
            self._dynamic: set[str] = set()

        def list_all(self) -> list[Tool]:
            return list(self._by_name.values())

        def get(self, tool_id: str) -> Tool:
            return self._by_name[tool_id]

        def get_schema_enricher(self, name: str) -> Any:
            return self._enrichers.get(name)

        def get_dynamic_tool_names(self) -> set[str]:
            return self._dynamic

    return _Registry()


# ═══════════════════════════════════════════════════════════
# 配置接口
# ═══════════════════════════════════════════════════════════


class TestConfigInterface:
    def test_name_and_priority(self) -> None:
        """name 固定；priority 从 config 读取，默认 50。"""
        assert ToolSchemaPlugin(config={}).name == "tool_schema"
        assert ToolSchemaPlugin(config={}).priority == 50
        assert ToolSchemaPlugin(config={"priority": 33}).priority == 33

    def test_config_flag_defaults(self) -> None:
        """默认 enabled=True、tool_ids=[]、include_tools_description_in_prompt=False。"""
        p = ToolSchemaPlugin(config={})
        assert p._enabled is True
        assert p._tool_ids == []
        assert p._include_desc is False

    def test_config_flags_from_config(self) -> None:
        """配置显式覆盖三个开关。"""
        p = ToolSchemaPlugin(
            config={
                "enabled": False,
                "tool_ids": ["a", "b"],
                "include_tools_description_in_prompt": True,
            }
        )
        assert p._enabled is False
        assert p._tool_ids == ["a", "b"]
        assert p._include_desc is True

    def test_execute_disabled_returns_empty_schemas(self) -> None:
        """禁用时 execute 返回空 tool_schemas，不触碰注册表。"""
        p = ToolSchemaPlugin(config={"enabled": False})
        ctx = _make_ctx(state={"tool_ids": ["x"]}, services={"tool_registry": _make_registry([])})
        result = _run(p.execute(ctx))
        assert result.state_updates == {"tool_schemas": []}

    def test_execute_result_is_plugin_result(self) -> None:
        """execute 返回值是 PluginResult（行为契约）。"""
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={}, services={"tool_registry": _make_registry([])})
        result = _run(p.execute(ctx))
        assert isinstance(result, _PLUGIN_MOD.PluginResult)
        assert isinstance(result.state_updates, dict)


# ═══════════════════════════════════════════════════════════
# 无 tool_registry 服务（KeyError）路径
# ═══════════════════════════════════════════════════════════


class TestNoRegistryService:
    def test_no_registry_no_schemas_returns_empty(self) -> None:
        """无服务且 state 无 tool_schemas → 空列表，不抛异常。"""
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={})
        result = _run(p.execute(ctx))
        assert result.state_updates == {"tool_schemas": []}

    def test_no_registry_keeps_injected_schemas(self, caplog: Any) -> None:
        """无服务且内核已注入 tool_schemas（无 tool_ids）→ {} 不覆盖，warning 留痕。"""
        p = ToolSchemaPlugin(config={})
        injected = [{"function": {"name": "alpha"}}, "not-a-dict"]
        ctx = _make_ctx(state={"tool_schemas": injected})
        with caplog.at_level(logging.WARNING):
            result = _run(p.execute(ctx))
        assert result.state_updates == {}  # 保留内核注入，不覆盖

    def test_no_registry_filters_injected_by_tool_ids(self) -> None:
        """无服务 + 内核注入 tool_schemas + tool_ids → 按工具面过滤并保留 spill_retrieve。"""
        p = ToolSchemaPlugin(config={})
        injected = [
            {"function": {"name": "alpha"}},
            {"function": {"name": "beta"}},
            {"function": {"name": "spill_retrieve"}},
            "not-a-dict",  # 非 dict 行被跳过
        ]
        ctx = _make_ctx(state={"tool_schemas": injected, "tool_ids": ["alpha"]})
        result = _run(p.execute(ctx))
        names = [(s.get("function") or {}).get("name") for s in result.state_updates["tool_schemas"]]
        assert names == ["alpha", "spill_retrieve"]

    def test_no_registry_full_wanted_set_returns_all(self) -> None:
        """tool_ids 全在注入面内 → 过滤为恒等 → {} 不覆盖（保留内核注入原样）。"""
        p = ToolSchemaPlugin(config={})
        injected = [
            {"function": {"name": "alpha"}},
            {"function": {"name": "spill_retrieve"}},
        ]
        ctx = _make_ctx(state={"tool_schemas": injected, "tool_ids": ["alpha", "spill_retrieve"]})
        result = _run(p.execute(ctx))
        assert result.state_updates == {}  # 过滤未收窄，无覆盖动作

    def test_no_registry_drift_warns_and_returns_empty(self, caplog: Any) -> None:
        """wanted 引用了注入面不存在的工具 → 工具面漂移 warning 留痕。"""
        p = ToolSchemaPlugin(config={})
        injected = [{"function": {"name": "alpha"}}]
        ctx = _make_ctx(state={"tool_schemas": injected, "tool_ids": ["ghost"]})
        with caplog.at_level(logging.WARNING):
            result = _run(p.execute(ctx))
        assert result.state_updates["tool_schemas"] == []
        assert any("工具面漂移" in r.getMessage() and "ghost" in r.getMessage() for r in caplog.records)

    def test_no_registry_missing_tool_ids_warns(self, caplog: Any) -> None:
        """内核已注入但 agent 未声明 tool_ids → 断链 warning，返回 {} 不覆盖。"""
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={"tool_schemas": [{"function": {"name": "alpha"}}]})
        with caplog.at_level(logging.WARNING):
            result = _run(p.execute(ctx))
        assert result.state_updates == {}
        assert any("未声明 tool_ids" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# tool_registry 服务可用
# ═══════════════════════════════════════════════════════════


class TestRegistryService:
    def test_list_all_when_no_tool_ids(self) -> None:
        """无 tool_ids → 全量注入，schema 为 OpenAI function 格式。"""
        registry = _make_registry([_make_tool("alpha"), _make_tool("beta")])
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        schemas = result.state_updates["tool_schemas"]
        assert len(schemas) == 2
        names = sorted(s["function"]["name"] for s in schemas)
        assert names == ["alpha", "beta"]
        assert all(s["type"] == "function" for s in schemas)
        assert all("parameters" in s["function"] for s in schemas)

    def test_get_by_tool_ids_preferred_over_config(self) -> None:
        """state.tool_ids 优先于插件配置 tool_ids。"""
        registry = _make_registry([_make_tool("alpha"), _make_tool("beta")])
        p = ToolSchemaPlugin(config={"tool_ids": ["beta"]})
        ctx = _make_ctx(state={"tool_ids": ["alpha"]}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        names = [s["function"]["name"] for s in result.state_updates["tool_schemas"]]
        assert names == ["alpha"]

    def test_config_tool_ids_fallback(self) -> None:
        """state 无 tool_ids → 降级用插件配置 tool_ids。"""
        registry = _make_registry([_make_tool("alpha"), _make_tool("beta")])
        p = ToolSchemaPlugin(config={"tool_ids": ["beta"]})
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        names = [s["function"]["name"] for s in result.state_updates["tool_schemas"]]
        assert names == ["beta"]

    def test_unknown_tool_id_warns_and_skips(self, caplog: Any) -> None:
        """tool_id 注册表查不到 → warning 留痕，其余工具照常注入。"""
        registry = _make_registry([_make_tool("alpha")])
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={"tool_ids": ["ghost", "alpha"]}, services={"tool_registry": registry})
        with caplog.at_level(logging.WARNING):
            result = _run(p.execute(ctx))
        names = [s["function"]["name"] for s in result.state_updates["tool_schemas"]]
        assert names == ["alpha"]
        assert any("Tool not found: ghost" in r.getMessage() for r in caplog.records)

    def test_dynamic_tool_names_merged(self) -> None:
        """动态工具名与 active_tool_ids 取并集。"""
        registry = _make_registry([_make_tool("alpha"), _make_tool("dyn")])
        registry._dynamic = {"dyn"}  # type: ignore[attr-defined]
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={"tool_ids": ["alpha"]}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        names = sorted(s["function"]["name"] for s in result.state_updates["tool_schemas"])
        assert names == ["alpha", "dyn"]

    def test_empty_tools_returns_empty_schemas(self) -> None:
        """注册表为空 → {"tool_schemas": []}。"""
        registry = _make_registry([])
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        assert result.state_updates == {"tool_schemas": []}

    def test_dynamic_preload_loader_none(self, monkeypatch: Any) -> None:
        """tools.loader 存在但返回 None → 跳过预加载，正常注入。"""
        registry = _make_registry([_make_tool("alpha")])
        tools_pkg = types.ModuleType("tools")
        loader_mod = types.ModuleType("tools.loader")
        loader_mod.get_dynamic_tool_loader = lambda: None
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
        monkeypatch.setitem(sys.modules, "tools.loader", loader_mod)
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={"tool_ids": ["alpha"]}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        names = [s["function"]["name"] for s in result.state_updates["tool_schemas"]]
        assert names == ["alpha"]

    def test_dynamic_preload_loader_ensures_loaded(self, monkeypatch: Any) -> None:
        """tools.loader 存在且返回 loader → ensure_loaded_sync 收到 active 工具面。"""
        registry = _make_registry([_make_tool("alpha")])
        tools_pkg = types.ModuleType("tools")
        loader_mod = types.ModuleType("tools.loader")
        loaded: list[list[str]] = []

        class _FakeLoader:
            def ensure_loaded_sync(self, ids: list[str]) -> None:
                loaded.append(list(ids))

        loader_mod.get_dynamic_tool_loader = lambda: _FakeLoader()
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
        monkeypatch.setitem(sys.modules, "tools.loader", loader_mod)
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={"tool_ids": ["alpha"]}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        assert loaded == [["alpha"]]
        assert result.state_updates["tool_schemas"][0]["function"]["name"] == "alpha"

    def test_enricher_success_used(self) -> None:
        """schema 丰富器存在且成功 → 丰富后的 to_llm_format 生效。"""
        base_tool = _make_tool("alpha")

        def enricher(tool: Tool, services: dict[str, Any]) -> Tool:
            assert services["tool_registry"] is not None  # services 字典已装配
            return tool.model_copy(update={"description": "ENRICHED"})

        registry = _make_registry([base_tool], enrichers={"alpha": enricher})
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        desc = result.state_updates["tool_schemas"][0]["function"]["description"]
        assert "ENRICHED" in desc
        assert "【适用场景】" not in desc

    def test_enricher_failure_falls_back_to_plain(self, caplog: Any) -> None:
        """丰富器抛异常 → 降级用原始 to_llm_format，不中断。"""
        base_tool = _make_tool("alpha")

        def bad_enricher(tool: Tool, services: dict[str, Any]) -> Tool:
            raise RuntimeError("boom")

        registry = _make_registry([base_tool], enrichers={"alpha": bad_enricher})
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        with caplog.at_level(logging.DEBUG):
            result = _run(p.execute(ctx))
        name = result.state_updates["tool_schemas"][0]["function"]["name"]
        assert name == "alpha"
        assert any("Schema enrichment failed" in r.getMessage() for r in caplog.records)

    def test_include_desc_in_prompt(self) -> None:
        """开关开启 → 生成人类可读工具描述，覆盖全部工具且每行格式固定。"""
        registry = _make_registry([_make_tool("alpha", "A desc"), _make_tool("beta", "B desc")])
        p = ToolSchemaPlugin(config={"include_tools_description_in_prompt": True})
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        desc = result.state_updates["prompt.tool_descriptions"]
        assert desc.startswith("## 可用工具\n")
        lines = desc.split("\n")[1:]
        assert len(lines) == 2  # 性质断言：工具数量 = 描述条目数
        assert all(line.startswith("- ") for line in lines)
        assert "- alpha: A desc" in lines
        assert "- beta: B desc" in lines
        # 同时 tool_schemas 始终写入
        assert len(result.state_updates["tool_schemas"]) == 2

    def test_no_desc_when_flag_off(self) -> None:
        """开关关闭 → 不写 prompt.tool_descriptions。"""
        registry = _make_registry([_make_tool("alpha")])
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        result = _run(p.execute(ctx))
        assert "prompt.tool_descriptions" not in result.state_updates

    def test_agent_level_resolution(self) -> None:
        """_resolve_agent_level：数字串/前缀 L/非法值/空 → None。"""
        p = ToolSchemaPlugin(config={})
        assert p._resolve_agent_level(_make_ctx(state={"agent_level": "2"})) == 2
        assert p._resolve_agent_level(_make_ctx(state={"agent_level": "L3"})) == 3
        assert p._resolve_agent_level(_make_ctx(state={"agent_level": "l1"})) == 1
        assert p._resolve_agent_level(_make_ctx(state={"agent_level": "abc"})) is None
        assert p._resolve_agent_level(_make_ctx(state={})) is None
        assert p._resolve_agent_level(_make_ctx(state={"agent_level": 4})) == 4

    def test_get_services_skips_missing(self) -> None:
        """服务收集：存在的收录，缺失的跳过，不抛异常。"""
        registry = _make_registry([])
        p = ToolSchemaPlugin(config={})
        ctx = _make_ctx(
            state={},
            services={"tool_registry": registry, "memory_store": object()},
        )
        services = p._get_services(ctx)
        assert set(services) == {"tool_registry", "memory_store"}


# ═══════════════════════════════════════════════════════════
# server.py MCP 适配层
# ═══════════════════════════════════════════════════════════


def _load_server() -> Any:
    """显式路径加载 server.py；逐出裸名 plugin 防劫持。"""
    sys.modules.pop("plugin", None)
    mod_name = "tool_schema_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, Path(_THIS_DIR) / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestServerAdapter:
    def test_get_instance_returns_plugin(self) -> None:
        """get_instance 懒构建 ToolSchemaPlugin（config 缺省为空）。"""
        server = _load_server()
        inst = server.get_instance()
        assert isinstance(inst, server.ToolSchemaPlugin)
        assert inst.name == "tool_schema"
        assert inst is server.get_instance()  # 缓存单例

    def test_on_load_preheats_and_on_unload_clears(self) -> None:
        """on_load 预热单例；on_unload 清缓存后可重建。"""
        server = _load_server()
        _run(server._on_load({}))
        inst = server.get_instance()
        assert isinstance(inst, server.ToolSchemaPlugin)
        _run(server._on_unload({}))
        rebuilt = server.get_instance()
        assert isinstance(rebuilt, server.ToolSchemaPlugin)

    def test_execute_tool_returns_state_updates(self) -> None:
        """execute 工具：无服务时返回空 tool_schemas（PluginResult 序列化契约）。"""
        server = _load_server()
        data = _run(server.execute({"pipeline_id": "p-1"}))
        assert data["state_updates"] == {"tool_schemas": []}
        assert "route_signal" not in data
        assert "skip_remaining" not in data

    def test_execute_tool_dict_result_passthrough(self, monkeypatch: Any) -> None:
        """插件返回 dict 时 execute 直接透传。"""
        server = _load_server()

        class _DictPlugin:
            async def execute(self, ctx: Any) -> dict[str, Any]:
                return {"tool_schemas": [{"function": {"name": "x"}}]}

        monkeypatch.setattr(server, "get_instance", lambda: _DictPlugin())
        data = _run(server.execute({"pipeline_id": "p-1"}))
        assert data == {"tool_schemas": [{"function": {"name": "x"}}]}

    def test_execute_tool_serializes_skip(self, monkeypatch: Any) -> None:
        """PluginResult 的 state_updates/skip_remaining 序列化进返回 dict。"""
        from pipeline.plugin import PluginResult

        server = _load_server()

        class _StubPlugin:
            async def execute(self, ctx: Any) -> PluginResult:
                return PluginResult(
                    state_updates={"tool_schemas": []},
                    skip_remaining=True,
                )

        monkeypatch.setattr(server, "get_instance", lambda: _StubPlugin())
        data = _run(server.execute({"pipeline_id": "p-1"}))
        assert data["state_updates"] == {"tool_schemas": []}
        assert "route_signal" not in data
        assert data["skip_remaining"] is True

    def test_execute_tool_plain_result(self, monkeypatch: Any) -> None:
        """PluginResult 无 skip_remaining → 不写入对应键。"""
        from pipeline.plugin import PluginResult

        server = _load_server()

        class _StubPlugin:
            async def execute(self, ctx: Any) -> PluginResult:
                return PluginResult(state_updates={"tool_schemas": []})

        monkeypatch.setattr(server, "get_instance", lambda: _StubPlugin())
        data = _run(server.execute({"pipeline_id": "p-1"}))
        assert "route_signal" not in data
        assert "skip_remaining" not in data

    def test_tool_registered_with_schema(self) -> None:
        """工具名与入参 schema 契约（state 必填、config 可选）。"""
        server = _load_server()
        tool_def = server.plugin._tools.get("tool_schema.execute")
        assert tool_def is not None
        assert "state" in tool_def.schema["required"]
        assert "config" in tool_def.schema["properties"]

    def test_plugin_name_registered(self) -> None:
        """server 注册的插件名固定。"""
        server = _load_server()
        assert server.plugin.name == "tool_schema_pipeline"
