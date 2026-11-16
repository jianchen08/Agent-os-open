# @feature: FP-0.2.一 反向调用租户透传 | @vision: V4 多租户 | @ci: python-test
"""SDK 反向调用身份透传测试（刀1，装机版缺陷清偿计划 §一刀1）。

内核调用插件时经 invocation `_identity` 下发执行上下文身份（tenant_id/
session_id，原头=内核）；SDK 在调用边界捕获该身份（contextvar，per-invocation
隔离），插件发起 capability 反向调用时自动回带 `_tenant_id`/`_session_id`
——内核桥入口（PluginScopedRouter）据此建立租户 scope，读侧不再恒落 default。

覆盖：
- invocation 携带 `_identity` → 该次调用内 capability 反调 params 带租户键
- 无 `_identity`（旧内核/无身份路径）→ params 不带租户键（回退语义）
- 连续两次不同租户调用 → 各自正确，无跨调用泄漏（contextvar 隔离）
- 插件手传 `_tenant_id` → SDK 以内核下发身份覆盖（身份不可由插件指派）
- 畸形 `_identity`（非 dict）→ 按缺席处理
- `_identity` 被 SDK 消费，不泄漏给 handler 参数
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos_plugin_sdk.capability import (
    bind_reverse_call_identity,
    current_reverse_call_tenant_id,
    reset_reverse_call_identity,
)
from agentos_plugin_sdk.plugin import AgentOSPlugin
from agentos_plugin_sdk.server import McpServer


def make_probe_plugin() -> tuple[AgentOSPlugin, AsyncMock]:
    """构造「反调探针」插件：handler 内发起一次 pipeline-state.get 反调。

    返回 (plugin, send_request mock)——mock 打在 KernelChannel 层，真实
    `_make_call_fn._call`（身份注入点）照常执行，mock 记录最终出站参数。
    """
    plugin = AgentOSPlugin("probe")
    plugin._on_initialize({"capabilities": {"pipeline-state": {}}, "config": {}})
    send_request = AsyncMock(return_value={"tenant_id": "default", "session_id": ""})
    plugin._kernel_channel.send_request = send_request

    @plugin.tool(name="probe", schema={"type": "object"})
    async def probe(state: dict, config: dict) -> dict:
        await plugin.get_capability("pipeline-state").call("get", {"key": "task.id"})
        return {"ok": True}

    return plugin, send_request


async def run_probe(plugin: AgentOSPlugin, arguments_extra: dict[str, Any]) -> dict[str, Any]:
    server = McpServer(plugin._tools, {}, {})
    result = await server._handle_tools_call(
        {
            "name": "probe",
            "arguments": {"state": {}, "config": {}, **arguments_extra},
        }
    )
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_identity_in_invocation_flows_to_reverse_call_params() -> None:
    """invocation `_identity` → 该次调用内的反调 params 带租户键。"""
    plugin, call_fn = make_probe_plugin()
    out = await run_probe(
        plugin,
        {"_identity": {"tenant_id": "maintenance", "session_id": "sess-9"}},
    )
    assert out == {"ok": True}
    params = call_fn.call_args.args[1]
    assert params["_tenant_id"] == "maintenance", "反调必须回带内核下发的租户"
    assert params["_session_id"] == "sess-9"
    assert params["key"] == "task.id", "业务参数原样保留"


@pytest.mark.asyncio
async def test_user_identity_flows_to_reverse_call_params() -> None:
    """刀2：`_identity.user_id` → 反调回带 `_user_id`（派发面注入身份真值）。

    三组有区分度输入：有效字符串回带；null/非字符串（畸形/旧内核缺席形态）
    按缺席处理不回带；无 `_identity` 整体干净。
    """
    plugin, call_fn = make_probe_plugin()
    await run_probe(
        plugin,
        {
            "_identity": {
                "tenant_id": "t",
                "session_id": "s",
                "user_id": "u-real",
            }
        },
    )
    params = call_fn.call_args.args[1]
    assert params["_user_id"] == "u-real", "user 维必须随反调回带"

    await run_probe(
        plugin,
        {"_identity": {"tenant_id": "t", "session_id": "s", "user_id": None}},
    )
    params_null = call_fn.call_args_list[1].args[1]
    assert "_user_id" not in params_null, "null user（旧内核形态）按缺席"

    await run_probe(
        plugin,
        {"_identity": {"tenant_id": "t", "session_id": "s", "user_id": 42}},
    )
    params_bad = call_fn.call_args_list[2].args[1]
    assert "_user_id" not in params_bad, "非字符串 user（畸形）按缺席"

    await run_probe(plugin, {})
    params_clean = call_fn.call_args_list[3].args[1]
    assert "_user_id" not in params_clean, "无身份不伪造 user 键"


@pytest.mark.asyncio
async def test_no_identity_keeps_params_clean() -> None:
    """无 `_identity`（旧内核）→ 反调 params 不带租户键（回退语义，不伪造）。"""
    plugin, call_fn = make_probe_plugin()
    await run_probe(plugin, {})
    params = call_fn.call_args.args[1]
    assert "_tenant_id" not in params
    assert "_session_id" not in params


@pytest.mark.asyncio
async def test_consecutive_invocations_no_cross_leak() -> None:
    """连续两次不同租户调用 → 各自正确（contextvar per-invocation 隔离）。"""
    plugin, call_fn = make_probe_plugin()

    await run_probe(plugin, {"_identity": {"tenant_id": "tenant_a", "session_id": "s-a"}})
    params_a = call_fn.call_args_list[0].args[1]
    assert params_a["_tenant_id"] == "tenant_a"

    await run_probe(plugin, {"_identity": {"tenant_id": "tenant_b", "session_id": "s-b"}})
    params_b = call_fn.call_args_list[1].args[1]
    assert params_b["_tenant_id"] == "tenant_b", "第二次调用必须用第二次身份"

    # 无身份第三次调用：不得残留第二次的身份（调用边界必须复位）
    await run_probe(plugin, {})
    params_c = call_fn.call_args_list[2].args[1]
    assert "_tenant_id" not in params_c, "身份随调用边界复位，不得跨调用残留"


@pytest.mark.asyncio
async def test_plugin_supplied_tenant_is_overridden() -> None:
    """插件手传 `_tenant_id` → SDK 以内核下发身份覆盖（身份原头不可指派）。"""
    plugin, send_request = make_probe_plugin()

    # handler 直接在 params 里带伪造租户
    plugin.register_tool(
        "forger",
        {"type": "object"},
        _make_forger(plugin, {"_tenant_id": "victim", "_session_id": "forged"}),
    )
    server = McpServer(plugin._tools, {}, {})
    await server._handle_tools_call(
        {
            "name": "forger",
            "arguments": {
                "state": {},
                "config": {},
                "_identity": {"tenant_id": "owner", "session_id": "s-owner"},
            },
        }
    )
    params = send_request.call_args.args[1]
    assert params["_tenant_id"] == "owner", "SDK 反调身份以内核下发为准"
    assert params["_session_id"] == "s-owner"


def _make_forger(plugin: AgentOSPlugin, extra: dict[str, Any]):
    async def forger(state: dict, config: dict) -> dict:
        await plugin.get_capability("pipeline-state").call("get", {"key": "task.id", **extra})
        return {"ok": True}

    return forger


@pytest.mark.asyncio
async def test_malformed_identity_treated_as_absent() -> None:
    """畸形 `_identity`（非 dict）→ 按缺席处理（不建身份、不报错）。"""
    plugin, call_fn = make_probe_plugin()
    out = await run_probe(plugin, {"_identity": "maintenance"})
    assert out == {"ok": True}
    params = call_fn.call_args.args[1]
    assert "_tenant_id" not in params


@pytest.mark.asyncio
async def test_identity_stripped_from_handler_arguments() -> None:
    """`_identity` 被 SDK 消费，不泄漏给 handler 参数（保留键契约）。"""
    plugin = AgentOSPlugin("probe2")
    plugin._on_initialize({"capabilities": {}, "config": {}})
    received: dict[str, Any] = {}

    def handler(state: dict, config: dict, **kwargs) -> dict:
        received.update(kwargs)
        received["state"] = state
        return {"ok": True}

    plugin.register_tool("probe2", {"type": "object"}, handler)
    server = McpServer(plugin._tools, {}, {})
    result = await server._handle_tools_call(
        {
            "name": "probe2",
            "arguments": {
                "state": {"x": 1},
                "config": {},
                "_identity": {"tenant_id": "maintenance", "session_id": "s"},
            },
        }
    )
    assert json.loads(result.content[0].text) == {"ok": True}
    assert "_identity" not in received, "身份保留键不得进入 handler 参数"
    assert received["state"] == {"x": 1}


class TestDirectCallTenantAccessor:
    """current_reverse_call_tenant_id：直调面读访问器（刀1b 直调延伸）。

    进程内直调（shared 平铺模块 import）不走反调出站回带，取租户方在本
    进程内读同一 contextvar——访问器是tasks 存储层「显式形参 > contextvar >
    default」取序的中段。
    """

    def test_bound_identity_yields_tenant(self) -> None:
        token = bind_reverse_call_identity({"tenant_id": "maintenance", "session_id": "s-m"})
        try:
            assert current_reverse_call_tenant_id() == "maintenance"
        finally:
            reset_reverse_call_identity(token)
        assert current_reverse_call_tenant_id() is None, "复位后不得残留（跨调用隔离）"

    @pytest.mark.parametrize("identity", [None, {}, "maintenance", {"tenant_id": ""}, {"tenant_id": 7}])
    def test_absent_or_malformed_yields_none(self, identity: Any) -> None:
        token = bind_reverse_call_identity(identity)
        try:
            assert current_reverse_call_tenant_id() is None
        finally:
            reset_reverse_call_identity(token)
