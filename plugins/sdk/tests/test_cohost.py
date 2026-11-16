# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-test
"""CohostServer 生命周期扇出单测。

契约（广播语义）：生命周期通知分发给全部成员 handler；单个成员 handler
异常被隔离留痕，不中断其余成员的投递——合宿下一员生命周期失败不得饥饿
其余成员（否则排在故障成员之后的插件 on_load 接线全缺）。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import CohostServer
from agentos_plugin_sdk.exceptions.step import StepNotFoundError

pytestmark = pytest.mark.unit


def _plugin_with_on_load(
    plugin_id: str,
    received: list[str],
    *,
    raises: bool = False,
) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.on_load
    async def _on_load(_params: dict[str, Any]) -> None:
        if raises:
            raise RuntimeError(f"{plugin_id} on_load 故障")
        received.append(plugin_id)

    return plugin


async def test_fan_out_delivers_to_all_members() -> None:
    """正常路径：on_load 按成员注册序扇出到全部成员。"""
    received: list[str] = []
    server = CohostServer(
        {
            "a": _plugin_with_on_load("a", received),
            "b": _plugin_with_on_load("b", received),
        }
    )

    await server._server._handle_notification("notifications/on_load", {})

    assert received == ["a", "b"]


async def test_fan_out_isolates_member_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """单成员 on_load 抛错：其余成员仍收到通知，异常留痕不外抛。

    广播语义（可观察行为）：排在故障成员之后的成员 on_load 必须被执行。
    """
    received: list[str] = []
    server = CohostServer(
        {
            "healthy_first": _plugin_with_on_load("healthy_first", received),
            "broken": _plugin_with_on_load("broken", received, raises=True),
            "healthy_last": _plugin_with_on_load("healthy_last", received),
        }
    )

    with caplog.at_level(logging.ERROR, logger="agentos_plugin_sdk.cohost"):
        await server._server._handle_notification("notifications/on_load", {})

    assert received == ["healthy_first", "healthy_last"], "故障成员之后的成员 on_load 不得被饥饿"
    assert any("broken" in rec.message for rec in caplog.records), "成员 handler 异常必须留痕（含成员标识）"


# ── steps 聚合（commit 爆涨排查副产品：合宿路径漏传 steps 导致 todo.fold/
# todo.extract 出生起即 STEP_NOT_FOUND 422 次/日——单插件 run() 传 steps，
# 合宿构造漏传，两路径必须等价）───────────────────────────────────


def _plugin_with_step(plugin_id: str, step_name: str) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.step(step_name)
    async def _handler(state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"state_updates": {"done": step_name}}

    return plugin


def _make_server(members: dict[str, AgentOSPlugin]) -> CohostServer:
    return CohostServer(members)


@pytest.mark.asyncio
async def test_member_steps_aggregated_into_dispatch() -> None:
    """成员 @plugin.step 注册的步骤聚合进合宿分发表（回归：漏传 steps=
    合宿步骤分发恒 STEP_NOT_FOUND）。"""
    server = _make_server(
        {
            "pipeline_todo_track": _plugin_with_step("pipeline_todo_track", "todo.fold"),
            "other": _plugin_with_step("other", "other.step"),
        }
    )
    result = await server._server._dispatch_step("todo.fold", {})
    text = result.content[0].text
    assert '"done"' in text and "todo.fold" in text
    result2 = await server._server._dispatch_step("other.step", {})
    assert "other.step" in result2.content[0].text


def test_step_name_conflict_rejected() -> None:
    """跨成员同名步骤 → 拒绝构造（与 tools/resource 重名同语义）。"""
    with pytest.raises(ValueError, match="step name conflict: todo.fold"):
        _make_server(
            {
                "a": _plugin_with_step("a", "todo.fold"),
                "b": _plugin_with_step("b", "todo.fold"),
            }
        )


@pytest.mark.asyncio
async def test_swap_member_replaces_steps_in_place() -> None:
    """成员热换入：新成员 steps 原位生效（McpServer 持同一 dict 引用）。"""
    server = _make_server({"a": _plugin_with_step("a", "todo.fold")})
    server.swap_member("a", _plugin_with_step("a", "other.step"))
    result = await server._server._dispatch_step("other.step", {})
    assert "other.step" in result.content[0].text
    # 换出后旧步骤未注册：分发即 StepNotFoundError（B017 禁盲 Exception）
    with pytest.raises(StepNotFoundError):
        await server._server._dispatch_step("todo.fold", {})
