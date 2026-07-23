"""TrackPlugin 缓存命中异常检测算法测试。

钉死 _check_cache_anomaly 的单轮命中率语义：

背景：原实现把「累计未命中」（total_input - total_cached）和「末轮单轮 input」
（last_input）直接相减，再除以累计总量。这是量纲错配——
- total_uncached 是整个 pipeline 累计值
- last_input 是末轮单轮值
两者相减无物理意义。结果：只要整个 pipeline 累计未命中占比 >5%（哪怕累计命中率
高达 94.9%），末轮 summary（last_input 常为 0，因为 tool_execute 轮）必报异常。

正确语义：判断「本轮缓存有没有异常掉」，应基于本轮单轮量：
  本轮未命中 = last_input - last_cached
  本轮命中率 = last_cached / last_input   （last_input > 0 时）
当 last_input == 0（tool_execute 轮、或无 LLM 调用）时无法判定，应跳过，不报异常。

本测试覆盖：
- 单轮高命中率不报异常
- 单轮低命中率（< 阈值）报异常
- last_input == 0 时静默跳过（回归：原实现会误报）
- 累计未命中高、但单轮命中率正常时不报异常（回归：原实现的核心误报场景）
"""

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins.output.track.plugin import TrackPlugin  # noqa: E402

PIPELINE_ID = "pipeline_cache_001"


def _make_plugin() -> TrackPlugin:
    """构造 TrackPlugin（默认配置即可，_check_cache_anomaly 不依赖外部服务）。"""
    return TrackPlugin(config={})


def _captured_warnings(caplog: Any) -> list[str]:
    """提取捕获到的 WARNING 日志消息文本。"""
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_high_cache_hit_rate_no_warning(caplog: Any) -> None:
    """单轮高命中率（> 阈值）不应报异常。"""
    plugin = _make_plugin()
    usage = {
        "last_input_tokens": 10000,
        "last_cached_tokens": 9500,  # 命中 95%
        "total_input_tokens": 100000,
        "total_cached_tokens": 95000,
    }
    with caplog.at_level(logging.WARNING, logger="plugins.output.track.plugin"):
        plugin._check_cache_anomaly(usage, PIPELINE_ID)
    assert _captured_warnings(caplog) == []


def test_low_cache_hit_rate_warns(caplog: Any) -> None:
    """单轮命中率低于阈值时应报异常，并携带本轮单轮命中率。"""
    plugin = _make_plugin()
    usage = {
        "last_input_tokens": 10000,
        "last_cached_tokens": 3000,  # 命中 30% < 阈值
        "total_input_tokens": 100000,
        "total_cached_tokens": 95000,  # 累计命中率很高，但本轮掉了
    }
    with caplog.at_level(logging.WARNING, logger="plugins.output.track.plugin"):
        plugin._check_cache_anomaly(usage, PIPELINE_ID)
    warns = _captured_warnings(caplog)
    assert len(warns) == 1
    msg = warns[0]
    # 消息必须反映单轮语义，不能再用累计量误导
    assert "本轮" in msg
    assert "30.0%" in msg  # 本轮命中率


def test_zero_last_input_skipped(caplog: Any) -> None:
    """last_input == 0 时无法判定本轮命中率，应静默跳过，不报异常。

    回归：原实现对 last_input=0 计算 gap=|累计未命中 - 0|=累计未命中，
    再除以累计总量 → 必误报。这是最核心的误报场景。
    """
    plugin = _make_plugin()
    usage = {
        "last_input_tokens": 0,  # tool_execute 轮 / 无 LLM 调用
        "last_cached_tokens": 0,
        "total_input_tokens": 420041,
        "total_cached_tokens": 347776,  # 累计未命中 72265
    }
    with caplog.at_level(logging.WARNING, logger="plugins.output.track.plugin"):
        plugin._check_cache_anomaly(usage, PIPELINE_ID)
    assert _captured_warnings(caplog) == []


def test_high_cumulative_uncached_but_per_round_ok_no_warning(caplog: Any) -> None:
    """累计未命中占比高、但本轮命中率正常时不报异常。

    回归场景（取自生产日志 12:31:27）：
      总input=2458025, 总cached=2331456, 末轮input=60632, 末轮cached=60000
    累计命中率 94.9%（累计未命中 126569，占比 5.1%）。
    原实现：gap=|126569 - 60632|=65937，ratio=2.7% 刚好不报（边界抖动）；
    但若末轮 input=0（tool_execute 轮调用 summary）则误报 5.1%。
    新实现只看本轮：本轮命中率 = 60000/60632 ≈ 99.0%，不报。
    """
    plugin = _make_plugin()
    usage = {
        "last_input_tokens": 60632,
        "last_cached_tokens": 60000,
        "total_input_tokens": 2458025,
        "total_cached_tokens": 2331456,
    }
    with caplog.at_level(logging.WARNING, logger="plugins.output.track.plugin"):
        plugin._check_cache_anomaly(usage, PIPELINE_ID)
    assert _captured_warnings(caplog) == []


def test_zero_total_input_skipped(caplog: Any) -> None:
    """total_input == 0（pipeline 无任何 LLM 调用）时应跳过。"""
    plugin = _make_plugin()
    usage = {
        "last_input_tokens": 0,
        "last_cached_tokens": 0,
        "total_input_tokens": 0,
        "total_cached_tokens": 0,
    }
    with caplog.at_level(logging.WARNING, logger="plugins.output.track.plugin"):
        plugin._check_cache_anomaly(usage, PIPELINE_ID)
    assert _captured_warnings(caplog) == []
