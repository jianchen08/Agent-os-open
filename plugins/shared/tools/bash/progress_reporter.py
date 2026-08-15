"""Bash 工具执行中进度节流器（task_observability 任务 2）。

解决「工具执行黑盒」：bash 跑 30 秒用户干等。stdout 增量经
process_manager._read_output → ProgressReporter.report（本类，阈值节流）→
server.py 的 queue 消费者 → frontend.emit("tool_progress") → 内核 → 前端。

节流阈值（对齐任务书「每 1KB 或每 2 秒推一次」）：
- min_chars：缓冲字符数达到即推（UTF-8 下 1 字符 ≥1 字节，1024 字符 ≈ 1KB）
- min_interval_s：距上次推送超过该间隔时，下次 report 即推（低频输出也保底可见）

时间函数可注入（time_fn），便于测试用假时钟驱动，不依赖真实 sleep。
"""

from __future__ import annotations

import time
from collections.abc import Callable

# 缓冲字符数阈值（≈1KB）
_DEFAULT_MIN_CHARS = 1024

# 时间阈值（秒）
_DEFAULT_MIN_INTERVAL_S = 2.0

# 单次 delta 字符上限（防超大块刷屏；超限保留尾部）
_DEFAULT_MAX_DELTA_CHARS = 4096


class ProgressReporter:
    """stdout 增量进度节流器。

    report() 由输出读取方调用（同一事件循环的任务，序列化保证）；
    达到阈值时回调 on_flush(delta, bytes_total)，由调用方组装
    tool_progress 事件推送前端。

    Args:
        on_flush: 推送回调 (delta 文本, 累计字节数)。
        min_chars: 缓冲字符阈值（默认 1024）。
        min_interval_s: 时间阈值秒（默认 2.0）。
        max_delta_chars: 单次 delta 字符上限（默认 4096）。
        time_fn: 单调时钟（默认 time.monotonic，测试可注入）。
    """

    def __init__(
        self,
        on_flush: Callable[[str, int], None],
        min_chars: int = _DEFAULT_MIN_CHARS,
        min_interval_s: float = _DEFAULT_MIN_INTERVAL_S,
        max_delta_chars: int = _DEFAULT_MAX_DELTA_CHARS,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_flush = on_flush
        self._min_chars = min_chars
        self._min_interval_s = min_interval_s
        self._max_delta_chars = max_delta_chars
        self._time_fn = time_fn

        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._bytes_total = 0
        self._last_flush = self._time_fn()
        self._closed = False

    @property
    def bytes_total(self) -> int:
        """累计输出的字节数。"""
        return self._bytes_total

    def report(self, text: str) -> None:
        """上报一段输出增量（达阈值即触发 flush）。"""
        if self._closed or not text:
            return
        self._bytes_total += len(text.encode("utf-8", errors="replace"))
        self._buffer.append(text)
        self._buffer_chars += len(text)

        now = self._time_fn()
        if self._buffer_chars >= self._min_chars or (now - self._last_flush) >= self._min_interval_s:
            self._flush(now)

    def close(self) -> None:
        """冲刷残留缓冲并关闭（后续 report 静默丢弃）。"""
        if self._closed:
            return
        self._closed = True
        self._flush(self._time_fn())

    def _flush(self, now: float) -> None:
        """推送缓冲内容（空缓冲直接更新锚点返回）。"""
        if not self._buffer:
            self._last_flush = now
            return
        delta = "".join(self._buffer)
        if len(delta) > self._max_delta_chars:
            # 超限保留尾部（最新输出最有信息量），头部标注截断
            delta = "...(前文已截断)...\n" + delta[-self._max_delta_chars:]
        self._buffer = []
        self._buffer_chars = 0
        self._last_flush = now
        self._on_flush(delta, self._bytes_total)
