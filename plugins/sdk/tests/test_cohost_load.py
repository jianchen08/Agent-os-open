# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci:python-test
"""CohostServer 成员加回（agentos/load_member）单测。

契约（ADR 2026-10-28 D5，用户裁定「事件的推送应该唤醒插件」）：

- 宿主保留**出生成员名册**（spawn 时的 ``--members``）；名册内的成员被摘除后
  可经 ``agentos/load_member`` 加回宿主进程；
- 加回幂等：成员已在场 → ``added=False``（内核在每次域事件前都会调用）；
- 加回后三张聚合表（工具/资源/生命周期处理器）与 steps **原位重建**——判据是
  「随后的 ``notifications/domain_event`` 能到达该成员」（事故现场：成员被摘
  后宿主扇出没有它的 handler，域事件静默丢失）；
- 名册外 id / 无加载回调 / 缺 plugin_id → 协议错误（内核据此告警，不得静默）。

与 ``agentos/reload_member`` 的分界：reload 是「换码」（要求在成员表里），
load 是「加回」（接受名册内已被摘除的成员）。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import CohostServer

pytestmark = pytest.mark.unit


class _FakeCtx:
    def __init__(self, params: dict[str, Any] | None) -> None:
        self.params = params


def _request_handler(server: CohostServer, method: str):
    entry = server._server._sdk.get_request_handler(method)
    assert entry is not None, f"必须注册 {method} 请求"
    return entry.handler


def _plugin_with_tool(plugin_id: str, tool_name: str, marker: str) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.tool(name=tool_name, schema={"type": "object"}, description="load probe")
    async def probe() -> dict:
        return {"marker": marker}

    return plugin


def _plugin_with_domain_events(plugin_id: str, received: list[str]) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.on_domain_event
    async def _on_domain_event(params: dict[str, Any]) -> None:
        received.append(str(params.get("event") or ""))

    return plugin


async def _drop_member(server: CohostServer, plugin_id: str) -> None:
    """借宿主侧 unload 请求摘除成员（真实摘除路径，不手改私有表）。"""
    handler = _request_handler(server, "agentos/unload_member")
    await handler(_FakeCtx({"plugin_id": plugin_id}), None)


# ── 加回：幂等 / 聚合表重建 / 域事件可达 ───────────────────


async def test_load_member_adds_back_and_domain_event_reaches_it() -> None:
    """被摘成员加回后，域事件能到达它（D5 的核心判据）。"""
    received: list[str] = []
    server = CohostServer(
        {
            "t": _plugin_with_domain_events("t", received),
            "other": _plugin_with_tool("other", "echo", "x"),
        },
        reload_handler=lambda pid: _plugin_with_domain_events(pid, received),
    )
    await _drop_member(server, "t")
    # 摘除后宿主扇出没有该成员的 handler —— 事故形态
    assert "t" not in server._members

    handler = _request_handler(server, "agentos/load_member")
    result = await handler(_FakeCtx({"plugin_id": "t"}), None)
    assert result["loaded"] is True
    assert result["added"] is True, "被摘成员必须被真正加回（不是空应答）"
    assert "t" in server._members

    # 判据：域事件扇出真的到得了它
    fan_out = server._lifecycle_handlers["domain_event"]
    await fan_out({"event": "run.completed"})
    assert received == ["run.completed"]


async def test_load_member_is_idempotent_when_present() -> None:
    """成员已在场 → added=False（内核每次域事件前调用，不能重载/报错）。"""
    server = CohostServer(
        {
            "t": _plugin_with_tool("t", "probe", "v1"),
            "b": _plugin_with_tool("b", "b_echo", "b"),
        },
        reload_handler=lambda pid: _plugin_with_tool(pid, "probe", "v2"),
    )
    handler = _request_handler(server, "agentos/load_member")
    result = await handler(_FakeCtx({"plugin_id": "t"}), None)
    assert result == {"loaded": True, "added": False, "plugin_id": "t"}
    # 未换码：工具表保持原实例
    assert server._members["t"]._tools  # 实例仍在（非重载路径）


async def test_load_member_accepts_id_outside_birth_members() -> None:
    """出生成员之外的 id 也照常装载：成员集权威在内核分配表（ADR 2026-10-29）。

    场景 = 新插件惰性装箱进已存活宿主：内核按分配表发 load_member，宿主不设
    准入白名单，直接载入并重聚合（否则只能整组重启进程）。
    """
    server = CohostServer(
        {"a": _plugin_with_tool("a", "probe", "v1")},
        reload_handler=lambda pid: _plugin_with_tool(pid, f"{pid}_probe", "v2"),
    )
    handler = _request_handler(server, "agentos/load_member")
    result = await handler(_FakeCtx({"plugin_id": "newcomer"}), None)
    assert result == {"loaded": True, "added": True, "plugin_id": "newcomer"}
    assert "newcomer" in server._members
    # 聚合表已重算：新成员的工具以自身命名空间出现在协议面
    assert "newcomer.newcomer_probe" in server._tools


async def test_load_member_without_reload_handler_rejects() -> None:
    """无加载回调（宿主未注入）→ 协议错误，不静默返回成功。"""
    server = CohostServer(
        {"a": _plugin_with_tool("a", "probe", "v1")},
    )
    handler = _request_handler(server, "agentos/load_member")
    with pytest.raises(ValueError, match="no reload handler"):
        await handler(_FakeCtx({"plugin_id": "b"}), None)


async def test_load_member_missing_plugin_id_rejects() -> None:
    """缺 plugin_id → 协议错误。"""
    server = CohostServer(
        {"a": _plugin_with_tool("a", "probe", "v1")},
        reload_handler=lambda pid: _plugin_with_tool(pid, "probe", "v2"),
    )
    handler = _request_handler(server, "agentos/load_member")
    with pytest.raises(ValueError, match="missing plugin_id"):
        await handler(_FakeCtx({}), None)


async def test_swap_member_accepts_new_member_id() -> None:
    """swap_member 不做成员资格预检：权威在内核分配表，宿主只执行装载。"""
    server = CohostServer(
        {"a": _plugin_with_tool("a", "probe", "v1")},
    )
    server.swap_member("fresh", _plugin_with_tool("fresh", "fresh_probe", "v1"))
    assert "fresh" in server._members
    assert "fresh.fresh_probe" in server._tools
