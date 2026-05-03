"""管道并发控制 — 限制同时运行的管道数。

LLM 并发由 llm.key_pool.KeyPool 管理（按 key 级限流），
这里只管管道层的全局并发上限。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class ConcurrencyController:
    """管道并发控制器。

    用 asyncio.Semaphore 限制同时活跃的管道数。
    """

    def __init__(self, max_concurrent: int = 5) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
        return self._semaphore

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[None, None]:
        """获取一个管道执行槽位。"""
        sem = self._get_semaphore()
        try:
            await sem.acquire()
        except RuntimeError as e:
            if "bound to a different event loop" in str(e).lower():
                self._semaphore = None
                sem = self._get_semaphore()
                await sem.acquire()
            else:
                raise
        try:
            yield
        finally:
            sem.release()

    @property
    def available(self) -> int:
        """当前可用槽位数。"""
        sem = self._semaphore
        return sem._value if sem is not None else self._max_concurrent

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent
