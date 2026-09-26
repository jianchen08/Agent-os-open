# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""CohostServer 成员粒度卸载（unload_member）单测。

契约（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）：
- ``agentos/unload_member`` 请求：定向 on_unload → 成员表摘除 → 工具/资源/
  生命周期三张聚合表原位重聚合（被卸成员工具立即不可达，其余成员不受影响）；
- 最后成员拒绝成员级卸载（协议错误，空成员集破坏合宿进程不变量）——内核
  对最后成员走整组回收；
- unload_handler（宿主侧模块缓存摘除）被调用且异常就地隔离：服务面摘除是
  Correctness 面，缓存残留只是内存面，不得让卸载本身失败；
- 未知成员 / 缺 plugin_id 报协议错误，宿主状态不动。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import UNLOAD_MEMBER_METHOD, CohostServer

pytestmark = pytest.mark.unit


def _plugin_with_tool(plugin_id: str, tool_name: str, marker: str) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.tool(name=tool_name, schema={"type": "object"}, description="unload probe")
    async def probe() -> dict:
        return {"marker": marker}

    return plugin


def _plugin_with_unload(plugin_id: str, received: list[str]) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.on_unload
    async def _on_unload(_params: dict[str, Any]) -> None:
        received.append(plugin_id)

    return plugin


class _FakeCtx:
    def __init__(self, params: dict[str, Any] | None) -> None:
        self.params = params


def _unload_handler(server: CohostServer):
    entry = server._server._sdk.get_request_handler(UNLOAD_MEMBER_METHOD)
    assert entry is not None, "unload_member 请求必须默认注册"
    return entry.handler


async def test_unload_member_removes_tools_other_members_intact() -> None:
    """被卸成员工具立即不可达，其余成员工具与调用面原样。"""
    server = CohostServer(
        {
            "a": _plugin_with_tool("a", "a_echo", "a"),
            "b": _plugin_with_tool("b", "b_echo", "b"),
        }
    )
    result = await _unload_handler(server)(_FakeCtx({"plugin_id": "a"}), None)

    assert result["unloaded"] is True and result["plugin_id"] == "a"
    assert "a.a_echo" not in server.tool_names, "被卸成员工具必须立即退出服务面"
    assert "b.b_echo" in server.tool_names
    served = await server._server._handle_tools_call({"name": "b.b_echo", "arguments": {}})
    assert '"marker": "b"' in served.content[0].text


async def test_unload_member_dispatches_on_unload_to_target_only() -> None:
    """定向 on_unload 只投递被卸成员（广播扇出不误伤其余成员）。"""
    unloaded: list[str] = []
    server = CohostServer(
        {
            "a": _plugin_with_unload("a", unloaded),
            "b": _plugin_with_unload("b", unloaded),
            "c": _plugin_with_tool("c", "c_echo", "c"),
        }
    )
    await _unload_handler(server)(_FakeCtx({"plugin_id": "a"}), None)

    assert unloaded == ["a"], f"只允许被卸成员收到 on_unload: {unloaded}"


async def test_unload_last_member_refused() -> None:
    """最后成员拒绝成员级卸载（协议错误），成员仍在服务面。"""
    server = CohostServer({"solo": _plugin_with_tool("solo", "s_echo", "s")})
    with pytest.raises(ValueError, match="last member"):
        await _unload_handler(server)(_FakeCtx({"plugin_id": "solo"}), None)
    assert "solo.s_echo" in server.tool_names, "拒绝后宿主状态必须原样"


async def test_unload_member_unknown_and_missing_id_are_protocol_errors() -> None:
    """未知成员 / 缺 plugin_id：协议错误，宿主状态不动。"""
    server = CohostServer(
        {
            "a": _plugin_with_tool("a", "a_echo", "a"),
            "b": _plugin_with_tool("b", "b_echo", "b"),
        }
    )
    handler = _unload_handler(server)
    with pytest.raises(ValueError, match="unknown member"):
        await handler(_FakeCtx({"plugin_id": "ghost"}), None)
    with pytest.raises(ValueError, match="missing plugin_id"):
        await handler(_FakeCtx({}), None)
    assert set(server.tool_names) == {"a.a_echo", "b.b_echo"}


async def test_unload_handler_called_and_exception_isolated() -> None:
    """unload_handler 收到 plugin_id；其异常被隔离，卸载照常成功。"""
    calls: list[str] = []

    def dropping(plugin_id: str) -> None:
        calls.append(plugin_id)
        if plugin_id == "boom":
            raise RuntimeError("cache drop failed")

    server = CohostServer(
        {
            "a": _plugin_with_tool("a", "a_echo", "a"),
            "boom": _plugin_with_tool("boom", "x_echo", "x"),
        },
        unload_handler=dropping,
    )
    result = await _unload_handler(server)(_FakeCtx({"plugin_id": "boom"}), None)

    assert calls == ["boom"], "模块缓存摘除回调必须被调用"
    assert result["unloaded"] is True, "缓存摘除失败不得使卸载失败（服务面已摘除）"
    assert "boom.x_echo" not in server.tool_names


async def test_unload_then_broadcast_reaches_remaining_members_only() -> None:
    """卸载后生命周期广播按剩余成员扇出（聚合表重建后不含被卸成员）。"""
    unloaded: list[str] = []
    server = CohostServer(
        {
            "a": _plugin_with_tool("a", "a_echo", "a"),
            "b": _plugin_with_unload("b", unloaded),
        }
    )
    await _unload_handler(server)(_FakeCtx({"plugin_id": "a"}), None)

    await server._server._handle_notification("notifications/on_unload", {})
    assert unloaded == ["b"], "广播只应达剩余成员 b"
