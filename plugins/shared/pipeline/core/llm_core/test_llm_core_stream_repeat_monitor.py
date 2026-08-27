# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core _stream_repeat_monitor 流式重复检测监控器测试。

契约：包装原始 on_chunk 回调，转发 chunk 的同时用滑动窗口检测内容重复；
相似度超阈值连续 trigger 次 → 返回 "stop" 信号（由 adapter 截断流式输出）。

覆盖分支：
- 非 text chunk / 空内容 → 只转发不检测；
- 累积字符数未达 interval → 不检测；
- 窗口内容过短（≤20 字符）→ 跳过相似度计算；
- 相似度超阈值连续触发 → "stop"；未超阈值 → 计数清零；
- 缓冲超 window*5 → 裁剪保留最近 window*2；
- original 回调为 None → 跳过转发。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit

from _stream_repeat_monitor import StreamRepetitionMonitor  # noqa: E402


def _text_chunk(content: str) -> dict[str, Any]:
    return {"type": "text", "content": content}


class TestStreamRepetitionMonitor:
    def test_non_text_chunk_forwarded_without_check(self) -> None:
        """非 text chunk → 转发给 original，不进入检测（返回 None）。"""
        seen: list[dict[str, Any]] = []
        mon = StreamRepetitionMonitor(seen.append)
        assert mon({"type": "tool_call", "content": "x"}) is None
        assert mon({"type": "thinking", "content": "plan"}) is None
        assert seen == [
            {"type": "tool_call", "content": "x"},
            {"type": "thinking", "content": "plan"},
        ]

    def test_empty_content_forwarded_without_check(self) -> None:
        """空内容 chunk → 转发但不检测。"""
        seen: list[dict[str, Any]] = []
        mon = StreamRepetitionMonitor(seen.append)
        assert mon({"type": "text", "content": ""}) is None
        assert seen == [{"type": "text", "content": ""}]

    def test_original_none_skips_forward(self) -> None:
        """original 为 None → 跳过转发，检测逻辑照常。"""
        mon = StreamRepetitionMonitor(None)
        assert mon(_text_chunk("a" * 10)) is None

    def test_below_interval_no_check(self) -> None:
        """累积字符数未达 interval → 不检测（返回 None）。"""
        mon = StreamRepetitionMonitor(None, window=30, interval=60, similarity=0.9, trigger=3)
        assert mon(_text_chunk("a" * 30)) is None  # 30 < 60
        assert mon(_text_chunk("a" * 20)) is None  # 50 < 60

    def test_short_window_content_skips_similarity(self) -> None:
        """窗口内容 ≤20 字符 → 跳过相似度计算（不误触发）。"""
        mon = StreamRepetitionMonitor(None, window=10, interval=20, similarity=0.9, trigger=3)
        # recent/prev 各 10 字符，len > 20 守卫不满足 → 不计数
        assert mon(_text_chunk("a" * 20)) is None
        assert mon(_text_chunk("a" * 20)) is None
        assert mon(_text_chunk("a" * 20)) is None  # 即使重复也不触发

    def test_repetition_triggers_stop_after_three(self) -> None:
        """相似度超阈值连续 3 次 → 返回 "stop" 信号。"""
        mon = StreamRepetitionMonitor(None, window=30, interval=60, similarity=0.9, trigger=3)
        assert mon(_text_chunk("a" * 60)) is None  # 第 1 次命中
        assert mon(_text_chunk("a" * 60)) is None  # 第 2 次命中
        assert mon(_text_chunk("a" * 60)) == "stop"  # 第 3 次触发

    def test_similarity_below_threshold_resets_counter(self) -> None:
        """相似度未超阈值 → 计数清零（一次低相似度打断连续触发）。"""
        mon = StreamRepetitionMonitor(None, window=30, interval=60, similarity=0.9, trigger=3)
        assert mon(_text_chunk("a" * 60)) is None  # 命中 1 次
        # 半 b 半 a：recent(a*30) vs prev(b*30) 相似度低 → 计数清零
        assert mon(_text_chunk("b" * 30 + "a" * 30)) is None
        assert mon(_text_chunk("a" * 60)) is None  # 重新计数第 1 次
        assert mon(_text_chunk("a" * 60)) is None  # 第 2 次
        assert mon(_text_chunk("a" * 60)) == "stop"  # 第 3 次触发

    def test_buffer_trimmed_beyond_five_windows(self) -> None:
        """缓冲超 window*5 → 裁剪保留最近 window*2（内存有界）。"""
        # trigger 调高避免提前 stop 短路（stop 在裁剪前 return）
        mon = StreamRepetitionMonitor(None, window=30, interval=60, similarity=0.9, trigger=10)
        for _ in range(3):
            mon(_text_chunk("a" * 60))
        assert len(mon._buf) == 60  # noqa: SLF001 裁剪后保留 window*2

    def test_forward_order_preserved(self) -> None:
        """original 收到与输入一致的 chunk 序列（转发不丢不改）。"""
        seen: list[dict[str, Any]] = []
        mon = StreamRepetitionMonitor(seen.append, window=30, interval=60, similarity=0.9, trigger=3)
        chunks = [_text_chunk("a" * 60), _text_chunk("b" * 60), {"type": "text", "content": "c"}]
        for c in chunks:
            mon(c)
        assert seen == chunks
