# @feature: FP-0.2.一 插件协议 | @ci: python-coverage
"""run_blocking：同步阻塞下放线程池（行为等价 + 事件循环不冻结断言）。

断行为（不钉实现）：
- 返回值直通、参数原样转发（行为与直接调用等价）；
- 执行不在事件循环线程（worker 线程身份 + 心跳 tick 两面）；
- 异常原样传播出 await（不吞、不改类型）。
配方承接 plugins/sdk/tests/test_sync_handler_offload.py 的工作线程断言式。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from agentos_plugin_sdk import run_blocking

pytestmark = pytest.mark.unit


async def test_returns_value_and_forwards_args() -> None:
    """返回值直通、位置/关键字参数原样转发（与直接调用行为等价）。"""

    def combine(a: int, b: int, *, sep: str = "-") -> str:
        return f"{a}{sep}{b}"

    assert await run_blocking(combine, 1, 2) == "1-2"
    assert await run_blocking(combine, 1, 2, sep="+") == "1+2"


async def test_runs_off_event_loop_with_heartbeat_alive() -> None:
    """0.3s 同步阻塞期间：worker 线程执行 + 事件循环持续打点（0.02s 心跳 ≥ 6）。"""
    seen: dict[str, threading.Thread | None] = {"thread": None}
    ticks = 0

    def slow() -> str:
        seen["thread"] = threading.current_thread()
        time.sleep(0.3)
        return "done"

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    try:
        result = await run_blocking(slow)
    finally:
        tick_task.cancel()

    assert result == "done"
    assert seen["thread"] is not None
    assert seen["thread"] is not threading.main_thread(), "run_blocking 仍在事件循环线程执行"
    assert ticks >= 6, f"事件循环被阻塞（期间仅 {ticks} 次心跳）"


async def test_exception_propagates_unchanged() -> None:
    """同步体异常原样传播出 await（不吞、不改类型）。"""

    class BoomError(RuntimeError):
        pass

    def explode() -> None:
        raise BoomError("sync exploded")

    with pytest.raises(BoomError, match="sync exploded"):
        await run_blocking(explode)
