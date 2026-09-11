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

    assert received == ["healthy_first", "healthy_last"], (
        "故障成员之后的成员 on_load 不得被饥饿"
    )
    assert any("broken" in rec.message for rec in caplog.records), (
        "成员 handler 异常必须留痕（含成员标识）"
    )
