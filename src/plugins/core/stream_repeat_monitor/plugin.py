"""流式输出重复检测监控器。

包装 on_chunk 回调，在流式输出过程中实时检测内容重复。
当检测到重复时返回 "stop" 信号，由 adapter 截断流式输出。

检测策略：滑动窗口，比较最近 N 字符与前 N 字符的相似度。
相似度超过阈值连续 M 次触发，返回 "stop" 信号。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)


class StreamRepetitionMonitor:
    """流式输出重复检测监控器。

    包装原始 on_chunk 回调，转发 chunk 的同时监控内容重复。

    Attributes:
        _original: 原始 on_chunk 回调
        _window: 比较窗口大小（字符数）
        _interval: 检查间隔（累积字符数）
        _similarity: 相似度阈值
        _trigger: 连续触发次数
    """

    def __init__(
        self,
        original: Callable[[dict[str, Any]], Any] | None,
        *,
        window: int = 100,
        interval: int = 200,
        similarity: float = 0.9,
        trigger: int = 3,
    ) -> None:
        self._original = original
        self._window = window
        self._interval = interval
        self._similarity = similarity
        self._trigger = trigger
        self._buf = ""
        self._chars_since_check = 0
        self._repeat_count = 0

    def __call__(self, chunk: dict[str, Any]) -> str | None:
        """处理流式 chunk，检测重复。

        先转发给原始回调，再检查文本内容是否重复。

        Args:
            chunk: 流式 chunk 数据 {"type": ..., "content": ...}

        Returns:
            "stop" 表示检测到重复，None 表示正常
        """
        content = chunk.get("content", "")
        chunk_type = chunk.get("type", "")

        if self._original:
            self._original(chunk)

        if chunk_type != "text" or not content:
            return None

        self._buf += content
        self._chars_since_check += len(content)

        if self._chars_since_check >= self._interval and len(self._buf) >= self._window * 2:
            recent = self._buf[-self._window :]
            prev = self._buf[-self._window * 2 : -self._window]

            if len(recent) > 20 and len(prev) > 20:
                sim = SequenceMatcher(None, recent, prev).ratio()
                if sim > self._similarity:
                    self._repeat_count += 1
                    if self._repeat_count >= self._trigger:
                        logger.warning(
                            "[StreamRepetitionMonitor] 检测到流式重复 (sim=%.2f, count=%d), 发送 stop 信号",
                            sim,
                            self._repeat_count,
                        )
                        return "stop"
                else:
                    self._repeat_count = 0

            self._chars_since_check = 0
            if len(self._buf) > self._window * 5:
                self._buf = self._buf[-self._window * 2 :]

        return None
