# @feature: FP-0.2.三 宿主接入（双宿 parity 契约） | @ci: python-coverage
"""双宿形态 parity 契约——单插件 run() 与合宿 CohostServer 的分发面等价性。

事故背景（2026-10-10）：CohostServer 构造 McpServer 漏传 steps，合宿路径步骤
分发恒 STEP_NOT_FOUND（todo.fold/extract 出生起 422 次/日，静默两天）。单插件
路径一直正确——两路径行为分叉没有任何测试拦截。本契约钉死：**同一插件经两
种宿主形态暴露后，steps/tools/resources 三面的分发结果必须等价**；此后任何
一条路径新增/漏传聚合面，此测试即红。

[来源: plugins/sdk/src/agentos_plugin_sdk/cohost.py / plugin.py]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SDK_TESTS_DIR = Path(__file__).resolve().parent


def _load_cohost_module() -> Any:
    mod_name = "hosting_parity_cohost_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _SDK_TESTS_DIR / ".." / "src" / "agentos_plugin_sdk" / "cohost.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _build_plugin(plugin_id: str) -> Any:
    """带 steps/tools/resources 三面的成员插件（真实装饰器注册）。"""
    from agentos_plugin_sdk import AgentOSPlugin

    plugin = AgentOSPlugin(plugin_id)

    @plugin.step(f"{plugin_id}.fold")
    async def _fold(state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"state_updates": {"fold": plugin_id}}

    @plugin.tool(
        name="probe",
        schema={"type": "object", "properties": {}},
        description="parity probe tool",
    )
    async def _probe() -> dict[str, Any]:
        return {"ok": True, "who": plugin_id}

    return plugin


def _single_path_server(plugin: Any) -> Any:
    """路径 A：单插件形态——plugin.run() 的 McpServer 装配（同参数子集）。"""
    from agentos_plugin_sdk.server import McpServer

    return McpServer(
        tools=plugin._tools,
        resources=plugin._resources,
        lifecycle_handlers=plugin._lifecycle_handlers,
        on_initialize=plugin._on_initialize,
        kernel_channel=plugin._kernel_channel,
        steps=plugin.steps,
        on_call_config=plugin._apply_call_config,
    )


def _cohost_path_server(plugin: Any) -> Any:
    """路径 B：合宿形态——CohostServer 聚合装配。"""
    cohost = _load_cohost_module()
    return cohost.CohostServer({plugin_id_of(plugin): plugin})


def plugin_id_of(plugin: Any) -> str:
    return plugin.name


class TestHostingParity:
    def test_steps_dispatch_equivalent_on_both_paths(self) -> None:
        """steps：同一 step_name 两路径都能分发到成员 handler（事故回归锚）。"""
        plugin = _build_plugin("parity_a")
        single = _single_path_server(plugin)
        cohost = _cohost_path_server(plugin)
        name = "parity_a.fold"

        for label, server in (("single", single), ("cohost", cohost._server)):
            handler = server._steps.get(name)
            assert handler is not None, f"{label} 路径步骤分发表缺 {name}"

    def test_steps_declared_equals_dispatchable(self) -> None:
        """声明面（get_declared_steps）与两路径分发面一致——声明未注册即红。"""
        plugin = _build_plugin("parity_b")
        declared = plugin.get_declared_steps()
        single = _single_path_server(plugin)
        cohost = _cohost_path_server(plugin)
        for server in (single, cohost._server):
            for name in declared:
                assert name in server._steps, f"声明 {name} 未进分发表"

    def test_tools_namespaced_equivalent(self) -> None:
        """tools：单路径裸名、合宿路径 {plugin_id}. 前缀——handler 必须同一对象。"""
        plugin = _build_plugin("parity_c")
        single = _single_path_server(plugin)
        cohost = _cohost_path_server(plugin)
        assert "probe" in single._tools
        assert "parity_c.probe" in cohost._server._tools
        assert cohost._server._tools["parity_c.probe"].handler is single._tools["probe"].handler, (
            "两路径的 tool handler 必须同一对象（同一注册来源）"
        )
