"""同步阻塞调用下放线程池（async 工具 handler 的唯一合法出口）。

纪律（ADR 2026-11-16-async-blocking-gate.md，真源
docs/working/fs_tools同族阻塞_同类审计与修复方案_20261116.md）：
async 工具 handler 内一切同步文件/进程 I/O 必经此口——SDK
``_invoke_callable`` 只对**同步函数**整体下放线程池，async-def-with-sync-body
是盲区：直接在事件循环线程执行同步阻塞会冻住合宿宿主心跳（watchdog 30s
判停滞 → 整组 ``os._exit`` respawn，同宿无关成员在飞调用全部
``response channel closed``）。

暴露接口：
- run_blocking(fn, *args, **kwargs)：``asyncio.to_thread`` 薄包装，
  语义同源（contextvars 复制调用方上下文、异常原样传播、返回值直通）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

__all__ = ["run_blocking"]

_P = ParamSpec("_P")
_R = TypeVar("_R")


async def run_blocking(fn: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs) -> _R:
    """把同步阻塞调用下放线程池执行，不冻结事件循环。

    纪律一句话：async 工具 handler 内一切同步文件/进程 I/O 必经此口。

    Args:
        fn: 同步函数（文件 I/O / 进程调用 / CPU 重活）。
        *args: 位置参数，原样转发。
        **kwargs: 关键字参数，原样转发。

    Returns:
        ``fn(*args, **kwargs)`` 的返回值，直通不改写。
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
