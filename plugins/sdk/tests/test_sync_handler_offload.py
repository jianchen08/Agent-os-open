# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""回归：同步工具 handler 不得冻住合宿宿主事件循环。

2026-11-11 装机事故机制：SDK 在事件循环里直接调用同步 handler
（``result = td.handler(**kwargs)``），任一成员插件同步阻塞超过 watchdog
30s 阈值即心跳停滞 → 整组 ``os._exit`` respawn——同宿无关成员（llm 服务）
在飞调用全部 ``response channel closed``。修复=同步 handler 下放线程池，
按插件粒度异步串行锁保持「单插件内同步调用排队」的既有语义。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import mcp_types
import pytest

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import CohostServer

pytestmark = pytest.mark.unit


def _served_text(served: Any) -> str:
    """取 tools/call 文本通道（本测试只产生 TextContent）。"""
    content = served.content[0]
    assert isinstance(content, mcp_types.TextContent)
    return content.text


def _blocking_plugin(plugin_id: str = "blocker", sleep_secs: float = 0.3) -> AgentOSPlugin:
    plugin = AgentOSPlugin(plugin_id)

    @plugin.tool(name="slow", schema={"type": "object"}, description="blocking sync probe")
    def slow() -> dict:
        time.sleep(sleep_secs)
        return {"ok": True}

    return plugin


async def test_sync_tool_handler_runs_off_event_loop() -> None:
    """同步 handler 阻塞期间事件循环必须持续推进（心跳不被冻住）。"""
    server = CohostServer({"blocker": _blocking_plugin()})
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    try:
        served = await server._server._handle_tools_call({"name": "blocker.slow", "arguments": {}})
    finally:
        tick_task.cancel()

    assert '"ok": true' in _served_text(served)
    assert ticks >= 6, f"事件循环被同步 handler 阻塞（期间仅 {ticks} 次心跳）"


async def test_sync_handlers_same_plugin_stay_serialized() -> None:
    """下放线程池后单插件内同步调用仍串行（不引入并发副作用）。"""
    active = 0
    peak = 0

    plugin = AgentOSPlugin("serial")

    @plugin.tool(name="slow", schema={"type": "object"}, description="serial probe")
    def slow() -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.1)
        active -= 1
        return {"ok": True}

    server = CohostServer({"serial": plugin})
    await asyncio.gather(
        *[server._server._handle_tools_call({"name": "serial.slow", "arguments": {}}) for _ in range(3)]
    )

    assert peak == 1


async def test_async_tool_handler_still_awaited() -> None:
    """协程 handler 仍直接 await（不过线程池）。"""
    plugin = AgentOSPlugin("async_owner")

    @plugin.tool(name="echo", schema={"type": "object"}, description="async probe")
    async def echo() -> dict:
        await asyncio.sleep(0)
        return {"async": True}

    server = CohostServer({"async_owner": plugin})
    served = await server._server._handle_tools_call({"name": "async_owner.echo", "arguments": {}})

    assert '"async": true' in _served_text(served)
