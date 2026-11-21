"""事件循环活性观测仪（async handler 同步阻塞复现的共用测试基建）。

范式来源：``isolation_guard/test_async_handler_loop_liveness.py``——插件 SDK
``_invoke_callable`` 只对同步 handler 做 ``asyncio.to_thread`` 下放，async
handler 直接在合宿宿主事件循环里执行；单次 ≥30s 同步阻塞即触发宿主 watchdog
``os._exit(1)``（整组连坐崩溃，非"变慢"）。

口径：心跳协程按固定 interval 打点，测量**最大 tick 间隔**（含末段 tick→收尾
时刻——阻塞期间停摆的心跳在收尾时无法补点，只比 tick 与 tick 的间隔会把整段
停滞漏判成 0）。假身阻塞量级 0.2-0.3s 必破 ``MAX_TICK_GAP``，修复后（阻塞段
在线程池）间隔趋近 interval。

用法（车道需把 ``plugins/shared`` 置于 ``sys.path``——``_libs`` 命名空间包）::

    from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker

    async with LoopTicker() as ticker:
        result = await handler_under_test(...)
    assert ticker.max_gap < MAX_TICK_GAP
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType

#: 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
#: 「事件循环未被饿死」的机械判据——假身阻塞量级 0.2-0.3s 必破此界。
MAX_TICK_GAP = 0.1


class LoopTicker:
    """心跳协程：记录 tick 时刻与末段收尾时刻，暴露最大间隔。

    退出时把收尾时刻计入序列：被测段以同步阻塞结束时，最后一次 tick 到收尾
    之间的空窗就是停滞本身。
    """

    def __init__(self, interval: float = 0.01) -> None:
        self._interval = interval
        self._ticks: list[float] = []
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._end: float = 0.0

    async def __aenter__(self) -> LoopTicker:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        # 预热：让至少两 tick 落在被测调用之前（阻塞段前有基线 tick）
        await asyncio.sleep(self._interval * 3)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        assert self._stop is not None
        assert self._task is not None
        self._end = time.monotonic()
        self._stop.set()
        await self._task

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            self._ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    @property
    def max_gap(self) -> float:
        """相邻时刻（tick…tick，末段 tick→收尾）的最大间隔秒数。"""
        points = [*self._ticks, self._end]
        return max(
            (b - a for a, b in zip(points, points[1:], strict=False)), default=0.0
        )
