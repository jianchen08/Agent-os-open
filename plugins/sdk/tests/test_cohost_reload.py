# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""CohostServer 成员热交换（reload_member）单测。

契约（合宿成员粒度热重载）：
- ``swap_member``：成员实例热替换后，工具/资源/生命周期三张聚合表原位生效
  （新成员工具可达、旧成员工具消失），新成员接入共享 KernelChannel 并重放
  initialize 注入；
- ``reload_handler`` 注入即注册 ``agentos/reload_member`` 请求（带应答）：
  成功返回重载摘要；未知成员/缺 plugin_id 报协议错误；旧成员先收定向
  on_unload 再换入新实例。
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import CohostServer

pytestmark = pytest.mark.unit


def _plugin_with_tool(plugin_id: str, tool_name: str, marker: str) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.tool(name=tool_name, schema={"type": "object"}, description="reload probe")
    async def probe() -> dict:
        return {"marker": marker}

    return plugin


def _plugin_with_unload(plugin_id: str, received: list[str]) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.on_unload
    async def _on_unload(_params: dict[str, Any]) -> None:
        received.append(plugin_id)

    return plugin


# ── swap_member：聚合表原位热替换 ─────────────────────────


async def test_swap_member_tools_go_live_immediately() -> None:
    """换入成员的工具立即可达（新 handler 应答），旧工具从表中消失。"""
    old_plugin = _plugin_with_tool("a", "probe", "old")
    server = CohostServer({"a": old_plugin, "b": _plugin_with_tool("b", "b_echo", "b")})

    new_plugin = _plugin_with_tool("a", "probe", "new")
    server.swap_member("a", new_plugin)

    assert "a.probe" in server.tool_names and "b.b_echo" in server.tool_names
    result = await server._server._handle_tools_call({"name": "a.probe", "arguments": {}})
    assert '"marker": "new"' in result.content[0].text


async def test_swap_member_reinjects_channel_and_initialize() -> None:
    """换入成员接入共享 KernelChannel，并重放最近一次 initialize 注入参数。"""
    server = CohostServer({"a": _plugin_with_tool("a", "probe", "v1")})
    init_params = {"capabilities": {"x": 1}, "config": {"k": "v"}}
    server._fan_out_initialize(init_params)

    received: dict[str, Any] = {}
    new_plugin = AgentOSPlugin("a")

    orig_init = new_plugin._on_initialize

    def _spy(params: dict[str, Any]) -> None:
        received.update(params)
        orig_init(params)

    new_plugin._on_initialize = _spy  # type: ignore[method-assign]
    server.swap_member("a", new_plugin)

    assert new_plugin._kernel_channel is server._channel, "换入成员必须共享反向调用通道"
    assert received.get("config") == {"k": "v"}, "initialize 注入参数必须重放"


async def test_swap_member_unknown_id_raises() -> None:
    """未知成员 id：ValueError，不得静默吞掉（内核将走 force_unload 回退）。"""
    server = CohostServer({"a": _plugin_with_tool("a", "probe", "v1")})
    with pytest.raises(ValueError):
        server.swap_member("ghost", _plugin_with_tool("ghost", "g", "v"))


async def test_swap_member_updates_lifecycle_handlers() -> None:
    """换入成员的生命周期 handler 重新聚合：新成员可收 on_unload，旧成员退出。"""
    unloaded: list[str] = []
    server = CohostServer({"a": _plugin_with_tool("a", "probe", "v1")})
    server.swap_member("a", _plugin_with_unload("a", unloaded))

    with contextlib.suppress(Exception):
        await server._server._handle_notification("notifications/on_unload", {})
    assert unloaded == ["a"], "换入成员的 on_unload 必须已被扇出登记"


def _plugin_with_load(plugin_id: str, loaded: list[str]) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.on_load
    async def _on_load(_params: dict[str, Any]) -> None:
        loaded.append(plugin_id)

    return plugin


async def test_swap_member_dispatches_on_load_to_new_instance() -> None:
    """热换入后新实例必须收到 on_load——否则其初始化状态为空。

    实况（2026-09-15）：cost_control 在成员热换入后 ``_budget_manager`` 恒为
    None，/ext/cost_control/* 全 502「service not initialized (sidecar warming
    up)」持续约两小时，直到整组重建才恢复；进程一直存活（on_load 从未重跑）。
    首载路径由内核 spawn 后广播 on_load，热换入无 spawn、内核不发该通知，
    须由本层补齐。
    """
    loaded: list[str] = []

    def loader(_plugin_id: str) -> AgentOSPlugin:
        return _plugin_with_load("a", loaded)

    server = CohostServer({"a": _plugin_with_load("a", loaded)}, reload_handler=loader)
    loaded.clear()  # 首载由内核广播；这里只观察热重载后的派发

    handler = _sdk_handler(server)
    result = await handler(_FakeCtx({"plugin_id": "a"}), None)

    assert result["reloaded"] is True
    assert loaded == ["a"], "热换入的新实例必须收到 on_load，否则初始化状态为空"


async def test_reload_member_load_failure_is_isolated() -> None:
    """新成员 on_load 抛错：换入本身仍成功（工具表已生效），异常就地隔离。"""
    attempts: list[str] = []
    plugin = AgentOSPlugin("a")

    @plugin.on_load
    async def _boom(_params: dict[str, Any]) -> None:
        attempts.append("raised")
        raise RuntimeError("init failed")

    @plugin.tool(name="probe", schema={"type": "object"}, description="probe")
    async def probe() -> dict:
        return {"marker": "v2"}

    server = CohostServer({"a": _plugin_with_tool("a", "probe", "v1")}, reload_handler=lambda _pid: plugin)
    handler = _sdk_handler(server)
    result = await handler(_FakeCtx({"plugin_id": "a"}), None)

    assert result["reloaded"] is True, "单个成员初始化失败不得使换入失败"
    assert attempts == ["raised"], "on_load 必须真的被调用过（异常已隔离）"
    # 工具表已按新实例生效（换入不被初始化失败回滚）
    assert "a.probe" in server.tool_names


# ── reload_handler 注入：agentos/reload_member 请求 ────────


def _sdk_handler(server: CohostServer):
    entry = server._server._sdk.get_request_handler("agentos/reload_member")
    assert entry is not None, "注入 reload_handler 后必须注册 reload 请求"
    return entry.handler


class _FakeCtx:
    def __init__(self, params: dict[str, Any] | None) -> None:
        self.params = params


async def test_reload_request_invokes_handler_and_swaps() -> None:
    """reload 请求：取回新实例 → 定向 on_unload 旧成员 → 换入 → 返回摘要。"""
    calls: list[str] = []
    unloaded: list[str] = []

    async def fake_loader(plugin_id: str) -> AgentOSPlugin:
        calls.append(plugin_id)
        return _plugin_with_tool(plugin_id, "probe", "reloaded")

    server = CohostServer(
        {"a": _plugin_with_unload("a", unloaded), "b": _plugin_with_tool("b", "b_echo", "b")},
        reload_handler=fake_loader,
    )
    handler = _sdk_handler(server)

    result = await handler(_FakeCtx({"plugin_id": "a"}), None)

    assert calls == ["a"]
    assert unloaded == ["a"], "旧成员必须先收定向 on_unload"
    assert result["reloaded"] is True and result["plugin_id"] == "a"
    probe = await server._server._handle_tools_call({"name": "a.probe", "arguments": {}})
    assert '"marker": "reloaded"' in probe.content[0].text


async def test_reload_request_unknown_member_raises() -> None:
    """未知成员：处理器抛错（协议错误应答），内核据此回退 force_unload。"""
    async def fake_loader(plugin_id: str) -> AgentOSPlugin:
        raise AssertionError("未知成员不得触发加载")

    server = CohostServer(
        {"a": _plugin_with_tool("a", "probe", "v1")}, reload_handler=fake_loader
    )
    handler = _sdk_handler(server)
    with pytest.raises(ValueError):
        await handler(_FakeCtx({"plugin_id": "ghost"}), None)


async def test_reload_request_missing_plugin_id_raises() -> None:
    """缺 plugin_id：处理器抛错（fail-closed，不猜目标）。"""
    async def fake_loader(plugin_id: str) -> AgentOSPlugin:
        raise AssertionError("缺参不得触发加载")

    server = CohostServer(
        {"a": _plugin_with_tool("a", "probe", "v1")}, reload_handler=fake_loader
    )
    handler = _sdk_handler(server)
    with pytest.raises(ValueError):
        await handler(_FakeCtx({}), None)


async def test_no_reload_handler_no_request_registered() -> None:
    """未注入 reload_handler（独占 server.py 复用 McpServer 形态）：不注册 reload 请求。"""
    server = CohostServer({"a": _plugin_with_tool("a", "probe", "v1")})
    assert server._server._sdk.get_request_handler("agentos/reload_member") is None
