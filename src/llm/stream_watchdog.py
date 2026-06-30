"""LLM 流式独立线程硬超时兜底。

背景
----
LLM 流式调用的 ``aiter.__anext__()`` 在上游半死连接（TCP 建连成功、请求已
发出，但上游既不回数据也不断开）时会阻塞事件循环线程。此时所有 asyncio 级
超时（``asyncio.wait_for``、每 30s 一次的 ``_stream_heartbeat`` 心跳协程、
``TimerManager.call_later``）共享同一个 loop，loop 一冻全部失效——这是历史上
多次"管道僵死一整晚"的根因。

``StreamHardTimeout`` 用 ``threading.Thread`` 独立倒计时，到点若无 ``disarm``
取消，就用 ``asyncio.run_coroutine_threadsafe`` 在主 loop 执行
``stream.aclose()`` 强制断流。它是 loop 冻住也能生效的最后一道兜底。

设计要点
--------
- 用 ``threading.Event.wait(timeout)`` 做倒计时：可被 ``disarm`` 的 ``set()``
  提前唤醒，不浪费线程时间。
- ``aclose`` 经 ``run_coroutine_threadsafe`` 回主 loop 执行（async 函数不能
  在线程里直接 await）。主 loop 若已冻死，这个 future 也排不进去——但此时
  底层 socket 的阻塞源自 loop 线程被占住，强制关连接是 OS 级操作，能打破
  死锁。
- 全程吞异常：watchdog 自身永不抛错影响主流程（aclose 失败也只记日志）。
- ``arm`` / ``disarm`` 幂等：adapter 的 finally 块可能重复调用。

可复用范式来源：``src/triggers/manager.py:680-1017``（同样以"脱离事件循环"
为目的设计的独立线程巡检器，有 bug 复盘佐证）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class StreamHardTimeout:
    """独立线程硬超时：loop 冻住也能生效的兜底。

    在流式调用启动时 ``arm()``，正常结束时 ``disarm()``。若到点未 disarm，
    在主事件循环强制关闭底层 stream。

    Args:
        stream: litellm 流对象（需有 async ``aclose`` 方法）
        loop: 主事件循环（用于 run_coroutine_threadsafe 回桥执行 aclose）
        timeout: 硬超时秒数（生产由插件传 stream_idle_timeout 覆盖）
        on_fire: 触发时的同步回调（仅用于测试观测，生产可省略）
    """

    def __init__(
        self,
        stream: Any,
        loop: asyncio.AbstractEventLoop,
        timeout: float,
        on_fire: Callable[[], None] | None = None,
    ) -> None:
        self._stream = stream
        self._loop = loop
        self._timeout = timeout
        self._on_fire = on_fire
        # 用 Event 做倒计时门闩：disarm 时 set() 提前唤醒，避免空等。
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._fired = False

    def arm(self) -> None:
        """启动硬超时倒计时（幂等：重复 arm 不创建多个线程）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._countdown,
            name="llm-stream-hard-timeout",
            daemon=True,
        )
        self._thread.start()

    def disarm(self) -> None:
        """取消硬超时（幂等：finally 块可能重复调用）。"""
        if self._stop_event is not None:
            self._stop_event.set()

    def _countdown(self) -> None:
        """独立线程入口：等 timeout 或被 disarm 唤醒。

        线程函数，所有异常吞掉——watchdog 永不传播错误到主流程。
        """
        try:
            assert self._stop_event is not None  # noqa: S101
            # wait 返回 True 表示被 disarm(set)，False 表示超时到期
            timed_out = not self._stop_event.wait(timeout=self._timeout)
            if not timed_out:
                return
            self._fire()
        except Exception:  # noqa: BLE001
            logger.warning(
                "[StreamHardTimeout] 倒计时线程异常（已吞，不影响主流程）",
                exc_info=True,
            )

    def _fire(self) -> None:
        """到点强制关闭 stream。

        经 run_coroutine_threadsafe 回主 loop 执行 aclose（async 函数不能在
        线程里直接 await）。即使主 loop 已冻死，关闭底层连接是打破死锁的
        关键——阻塞在 socket recv 的协程会因连接关闭而收到异常退出。
        """
        if self._fired:
            return
        self._fired = True
        if self._on_fire is not None:
            with contextlib.suppress(Exception):
                self._on_fire()
        aclose = getattr(self._stream, "aclose", None)
        if aclose is None:
            logger.warning(
                "[StreamHardTimeout] stream 无 aclose 方法，无法强制关闭"
            )
            return
        try:
            # 回桥主 loop 执行 async aclose；不阻塞等待结果（loop 可能已冻，
            # future.result 会卡住本线程）。仅提交，让 loop 恢复时执行。
            asyncio.run_coroutine_threadsafe(aclose(), self._loop)
            logger.warning(
                "[StreamHardTimeout] 已强制关闭 stream（硬超时 %.0fs 触发）",
                self._timeout,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[StreamHardTimeout] 强制关闭 stream 失败（已吞）",
                exc_info=True,
            )
