# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""tool_schema 工具 Schema 注入插件 + MCP server.py 适配层测试。

插件行为（capability 通道型）：读 state["tool_ids"]（context_build 供给），
经 tool-surface capability 拉取过滤后的工具面写入 state。

覆盖行为面：
- 配置接口：name/priority、enabled 默认值
- 禁用时返回空工具面，不发起 capability 调用
- state 无 tool_ids（缺失/非列表）→ 空工具面 + 断链 warning，不发起调用
- 显式空 tool_ids（声明零工具）→ 照常转发空白名单，无断链告警
- caller 正常返回 → schemas/contracts 原样写入 state（短方法名 + tool_ids 转发）
- caller 未注入（通道未接线）/ 调用异常 → 空工具面 + error 留痕（fail-closed）
- 工具面漂移（wanted 引用返回面不存在的工具）→ warning 留痕，schema 仍按内核
  过滤结果写入（不静默缩面、不覆盖为全量）
- execute 入口：state_updates 透传
- server.py：get_instance 懒构建缓存、on_load 注入 caller + 预热、on_unload 清
  caller + 缓存、execute 工具契约（PluginResult → dict 序列化、dict 直通）

外部依赖（内核 tool-surface capability）用轻量替身；过滤转发逻辑全部真实实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

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


def _make_ctx(state: dict[str, Any] | None = None) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=dict(state or {}))


class _FakeCaller:
    """tool-surface capability 轻量替身：记录调用并回放脚本化结果（外部依赖替身）。"""

    def __init__(self, result: dict[str, Any] | Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result if result is not None else {"schemas": [], "contracts": {}}

    async def __call__(self, method: str, params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.fixture(autouse=True)
def _reset_capability_caller() -> Any:
    """每个用例前后清 caller，防模块级状态跨用例污染。"""
    _PLUGIN_MOD.set_capability_caller(None)
    yield
    _PLUGIN_MOD.set_capability_caller(None)


# ═══════════════════════════════════════════════════════════
# 配置接口
# ═══════════════════════════════════════════════════════════


class TestConfigInterface:
    def test_name_and_priority(self) -> None:
        """name 固定；priority 从 config 读取，默认 50。"""
        assert ToolSchemaPlugin(config={}).name == "tool_schema"
        assert ToolSchemaPlugin(config={}).priority == 50
        assert ToolSchemaPlugin(config={"priority": 33}).priority == 33

    def test_enabled_default_true(self) -> None:
        """默认 enabled=True。"""
        assert ToolSchemaPlugin(config={})._enabled is True

    def test_execute_disabled_returns_empty_without_call(self) -> None:
        """禁用时返回空工具面，不发起 capability 调用。"""
        caller = _FakeCaller()
        _PLUGIN_MOD.set_capability_caller(caller)
        p = ToolSchemaPlugin(config={"enabled": False})
        result = _run(p.execute(_make_ctx(state={"tool_ids": ["x"]})))
        assert result.state_updates == {"tool_schemas": [], "tool_output_contracts": {}}
        assert caller.calls == []

    def test_execute_result_is_plugin_result(self) -> None:
        """execute 返回值是 PluginResult（行为契约）。"""
        p = ToolSchemaPlugin(config={})
        result = _run(p.execute(_make_ctx(state={"tool_ids": []})))
        assert isinstance(result, _PLUGIN_MOD.PluginResult)
        assert isinstance(result.state_updates, dict)


# ═══════════════════════════════════════════════════════════
# 白名单解析（配置断链 fail-closed）
# ═══════════════════════════════════════════════════════════


class TestWantedResolution:
    def test_missing_tool_ids_yields_empty_surface(self, caplog: Any) -> None:
        """state 无 tool_ids = 配置断链 → 空工具面 + warning，不发起调用（K10 不兜底全量）。"""
        caller = _FakeCaller()
        _PLUGIN_MOD.set_capability_caller(caller)
        p = ToolSchemaPlugin(config={})
        with caplog.at_level(logging.WARNING):
            result = _run(p.execute(_make_ctx(state={})))
        assert result.state_updates == {"tool_schemas": [], "tool_output_contracts": {}}
        assert caller.calls == []
        assert any("tool_ids" in r.getMessage() for r in caplog.records)

    def test_non_list_tool_ids_yields_empty_surface(self, caplog: Any) -> None:
        """tool_ids 非列表（脏数据）→ 同断链处理：空面 + warning。"""
        _PLUGIN_MOD.set_capability_caller(_FakeCaller())
        p = ToolSchemaPlugin(config={})
        with caplog.at_level(logging.WARNING):
            result = _run(p.execute(_make_ctx(state={"tool_ids": "alpha"})))
        assert result.state_updates["tool_schemas"] == []
        assert any("tool_ids" in r.getMessage() for r in caplog.records)

    def test_explicit_empty_tool_ids_forwards_empty_whitelist(self) -> None:
        """显式空表 = agent 声明零工具：照常转发空白名单（与断链区分），无告警。"""
        caller = _FakeCaller(result={"schemas": [{"function": {"name": "spill_retrieve"}}],
                                     "contracts": {}})
        _PLUGIN_MOD.set_capability_caller(caller)
        p = ToolSchemaPlugin(config={})
        result = _run(p.execute(_make_ctx(state={"tool_ids": []})))
        assert caller.calls == [("schemas", {"tool_ids": []})]
        names = [s["function"]["name"] for s in result.state_updates["tool_schemas"]]
        assert names == ["spill_retrieve"]

    def test_missing_caller_yields_empty_surface(self, caplog: Any) -> None:
        """caller 未注入（通道未接线）→ 空工具面 + error 留痕，不炸管道。"""
        p = ToolSchemaPlugin(config={})
        with caplog.at_level(logging.ERROR):
            result = _run(p.execute(_make_ctx(state={"tool_ids": ["alpha"]})))
        assert result.state_updates == {"tool_schemas": [], "tool_output_contracts": {}}
        assert any("caller 未注入" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# capability 调用与结果落 state
# ═══════════════════════════════════════════════════════════


class TestCapabilityFetch:
    def test_schemas_and_contracts_passthrough(self) -> None:
        """caller 正常返回 → schemas/contracts 原样写入 state_updates。"""
        schemas = [
            {"type": "function", "function": {"name": "alpha", "description": "d",
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "spill_retrieve", "description": "s",
                                              "parameters": {"type": "object"}}},
        ]
        contracts = {"alpha": {"schema": {"type": "object"}, "render": None}}
        caller = _FakeCaller(result={"schemas": schemas, "contracts": contracts})
        _PLUGIN_MOD.set_capability_caller(caller)
        p = ToolSchemaPlugin(config={})
        result = _run(p.execute(_make_ctx(state={"tool_ids": ["alpha", "spill_retrieve"]}))
                      )
        assert result.state_updates["tool_schemas"] == schemas
        assert result.state_updates["tool_output_contracts"] == contracts

    def test_caller_receives_short_method_and_tool_ids(self) -> None:
        """转发契约：短方法名 "schemas" + {"tool_ids": [...]} 参数包。"""
        caller = _FakeCaller()
        _PLUGIN_MOD.set_capability_caller(caller)
        p = ToolSchemaPlugin(config={})
        _run(p.execute(_make_ctx(state={"tool_ids": ["alpha", "beta"]})))
        assert caller.calls == [("schemas", {"tool_ids": ["alpha", "beta"]})]

    def test_capability_failure_yields_empty_surface(self, caplog: Any) -> None:
        """capability 调用异常（内核未注入 registry 等）→ 空工具面 + error 留痕。"""
        _PLUGIN_MOD.set_capability_caller(_FakeCaller(result=RuntimeError("boom")))
        p = ToolSchemaPlugin(config={})
        with caplog.at_level(logging.ERROR):
            result = _run(p.execute(_make_ctx(state={"tool_ids": ["alpha"]})))
        assert result.state_updates == {"tool_schemas": [], "tool_output_contracts": {}}
        assert any("tool-surface.schemas 调用失败" in r.getMessage() for r in caplog.records)

    def test_drift_warns_but_keeps_kernel_filtered_surface(self, caplog: Any) -> None:
        """wanted 引用了返回面不存在的工具 → 漂移 warning；schema 仍按内核过滤结果写入。"""
        caller = _FakeCaller(result={
            "schemas": [{"function": {"name": "alpha"}}],
            "contracts": {},
        })
        _PLUGIN_MOD.set_capability_caller(caller)
        p = ToolSchemaPlugin(config={})
        with caplog.at_level(logging.WARNING):
            result = _run(p.execute(_make_ctx(state={"tool_ids": ["alpha", "ghost"]})))
        assert result.state_updates["tool_schemas"] == [{"function": {"name": "alpha"}}]
        assert any("工具面漂移" in r.getMessage() and "ghost" in r.getMessage()
                   for r in caplog.records)

    def test_no_drift_warning_when_whitelist_satisfied(self, caplog: Any) -> None:
        """wanted 全部命中返回面 → 无漂移告警（性质断言：告警只由缺口触发）。"""
        caller = _FakeCaller(result={
            "schemas": [{"function": {"name": "alpha"}}, {"function": {"name": "beta"}}],
            "contracts": {},
        })
        _PLUGIN_MOD.set_capability_caller(caller)
        p = ToolSchemaPlugin(config={})
        with caplog.at_level(logging.WARNING):
            _run(p.execute(_make_ctx(state={"tool_ids": ["alpha", "beta"]})))
        assert not any("工具面漂移" in r.getMessage() for r in caplog.records)


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

    def test_on_load_injects_caller_and_on_unload_clears(self) -> None:
        """on_load 注入 tool-surface caller + 预热单例；on_unload 清 caller 与缓存。"""
        server = _load_server()
        plugin_mod = sys.modules["plugin"]
        assert plugin_mod._capability_caller is None
        _run(server._on_load({}))
        assert plugin_mod._capability_caller is not None
        assert isinstance(server.get_instance(), server.ToolSchemaPlugin)
        _run(server._on_unload({}))
        assert plugin_mod._capability_caller is None
        rebuilt = server.get_instance()
        assert isinstance(rebuilt, server.ToolSchemaPlugin)

    def test_wired_caller_routes_via_tool_surface_handle(self) -> None:
        """on_load 注入的 caller 经 plugin.get_capability("tool-surface").call 转发；
        测试环境无内核注入（KeyError）→ execute 落 fail-closed 空面而非炸管道。"""
        server = _load_server()
        _run(server._on_load({}))
        data = _run(server.execute({"pipeline_id": "p-1", "tool_ids": ["alpha"]}))
        assert data["state_updates"]["tool_schemas"] == []
        assert data["state_updates"]["tool_output_contracts"] == {}

    def test_execute_tool_returns_state_updates(self) -> None:
        """execute 工具：state 无 tool_ids → 空工具面（PluginResult 序列化契约）。"""
        server = _load_server()
        data = _run(server.execute({"pipeline_id": "p-1"}))
        assert data["state_updates"] == {"tool_schemas": [], "tool_output_contracts": {}}
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
