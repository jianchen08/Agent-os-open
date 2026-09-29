# @feature: FP-0.2.CFG 插件配置热感知 | @ci: python-test
"""on_config_changed 配置变更感知钩子测试（2026-09-28 配置读写单源化批次 B）。

行为契约（断输入→输出/副作用，不钉实现）：
- initialize 握手配置即对比基线：首个同配置调用不触发钩子
- 配置视图真变（命名空间内容差异）→ 钩子按注册顺序触发，收到剔除瞬态键
  后的视图，且 get_config() 已刷新为最新完整 config
- 仅瞬态键差异（inputs/_step_method/_pipe_hook，内核步骤 config 合并产物）
  不触发——瞬态键逐调用不同，不属于配置面
- 非 dict config（旧内核不携带）静默忽略；async handler 支持；
  handler 异常不外抛（旁路增强不阻断主流程调用）
- McpServer 装配：on_call_config 回调在工具分发前收到本次调用 config
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.server import CALL_CONFIG_TRANSIENT_KEYS, McpServer
from agentos_plugin_sdk.types import ToolDef

pytestmark = pytest.mark.unit


def _make_plugin() -> AgentOSPlugin:
    plugin = AgentOSPlugin("cfg_hook_under_test")
    plugin._on_initialize({"capabilities": {}, "config": {"llm": {"defaults": {"chat": "m1"}}}})
    return plugin


class TestConfigChangeHook:
    def test_initialize_config_is_baseline_first_same_call_no_fire(self) -> None:
        plugin = _make_plugin()
        fired: list[dict[str, Any]] = []
        plugin.on_config_changed(fired.append)

        same = {"llm": {"defaults": {"chat": "m1"}}, "inputs": {"x": 1}}
        import asyncio

        asyncio.run(plugin._apply_call_config(same))

        assert fired == []
        assert plugin.get_config() == same

    def test_changed_namespace_fires_with_stripped_view(self) -> None:
        plugin = _make_plugin()
        fired: list[dict[str, Any]] = []
        plugin.on_config_changed(fired.append)

        import asyncio

        changed = {"llm": {"defaults": {"chat": "m2"}}, "inputs": {"y": 2}}
        asyncio.run(plugin._apply_call_config(changed))

        assert len(fired) == 1
        assert fired[0] == {"llm": {"defaults": {"chat": "m2"}}}

    def test_transient_only_change_does_not_fire(self) -> None:
        plugin = _make_plugin()
        fired: list[dict[str, Any]] = []
        plugin.on_config_changed(fired.append)

        import asyncio

        transient_only = {
            "llm": {"defaults": {"chat": "m1"}},
            "inputs": {"n": 3},
            "_step_method": "execute",
        }
        asyncio.run(plugin._apply_call_config(transient_only))

        assert fired == []

    def test_unchanged_second_call_does_not_fire_again(self) -> None:
        plugin = _make_plugin()
        fired: list[dict[str, Any]] = []
        plugin.on_config_changed(fired.append)

        import asyncio

        changed = {"llm": {"defaults": {"chat": "m2"}}}
        asyncio.run(plugin._apply_call_config(changed))
        asyncio.run(plugin._apply_call_config(dict(changed)))

        assert len(fired) == 1

    def test_non_dict_config_ignored(self) -> None:
        plugin = _make_plugin()
        fired: list[dict[str, Any]] = []
        plugin.on_config_changed(fired.append)

        import asyncio

        asyncio.run(plugin._apply_call_config(None))
        asyncio.run(plugin._apply_call_config("nope"))

        assert fired == []

    def test_async_handler_and_exception_isolation(self) -> None:
        plugin = _make_plugin()
        seen: list[dict[str, Any]] = []

        def _boom(_config: dict[str, Any]) -> None:
            raise RuntimeError("handler bug")

        async def _async_collect(config: dict[str, Any]) -> None:
            seen.append(config)

        plugin.on_config_changed(_boom)
        plugin.on_config_changed(_async_collect)

        import asyncio

        # _boom 抛异常不阻断后续 handler，也不向调用方外抛
        asyncio.run(plugin._apply_call_config({"llm": {"defaults": {"chat": "m3"}}}))

        assert seen == [{"llm": {"defaults": {"chat": "m3"}}}]

    def test_transient_keys_frozenset_covers_invoker_merge_contract(self) -> None:
        """瞬态键集合钉住 invoker merge_injected_with_step_config 合并契约。"""
        assert CALL_CONFIG_TRANSIENT_KEYS == frozenset({"inputs", "_step_method", "_pipe_hook"})


class TestMcpServerWiring:
    @pytest.mark.asyncio
    async def test_handle_tools_call_invokes_on_call_config_before_dispatch(self) -> None:
        """工具分发前收到本次调用 config 与工具名（含瞬态键原文，剔除归 plugin 侧）。

        接缝签名 (config, tool_name)：独占形态忽略工具名；合宿形态据此路由
        属主成员（CohostServer._route_call_config）。
        """
        seen: list[tuple[Any, str]] = []

        async def _spy(config: Any, tool_name: str | None = None) -> None:
            seen.append((config, tool_name))

        tool = ToolDef(
            name="echo",
            schema={"type": "object", "properties": {}},
            handler=lambda: {"ok": True},
        )
        server = McpServer(
            tools={"echo": tool},
            resources={},
            lifecycle_handlers={},
            on_call_config=_spy,
        )

        config = {"llm": {"defaults": {"chat": "m1"}}, "inputs": {"x": 1}}
        await server._handle_tools_call({"name": "echo", "arguments": {"config": config}})

        assert seen == [(config, "echo")]

    @pytest.mark.asyncio
    async def test_no_on_call_config_is_noop(self) -> None:
        """未装配回调（旧构造）不报错——存量用法零感知。"""
        tool = ToolDef(
            name="echo",
            schema={"type": "object", "properties": {}},
            handler=lambda: {"ok": True},
        )
        server = McpServer(tools={"echo": tool}, resources={}, lifecycle_handlers={})
        result = await server._handle_tools_call({"name": "echo", "arguments": {}})
        assert result is not None
