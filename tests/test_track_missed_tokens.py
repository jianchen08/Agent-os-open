# @feature: FP-0.2.可观测性 可观测性 | @ci: python-coverage
"""TrackPlugin token 用量收集的 cache 可观测字段测试（task_observability 1b）。

钉死 _collect_token_usage 的 missed_tokens / cache 命中率语义：
- last_missed_tokens = 本轮 input - 本轮 cached（独立字段输出，不再只藏在差值里）
- total_missed_tokens = 累计 input - 累计 cached
- last_cache_hit_ratio / total_cache_hit_ratio = cached / input（input == 0 时为 0.0）
- tool_execute 轮（llm_usage 残留）不累加，单轮字段全 0
- llm_core 未上报 cached_tokens 时按 0 处理（missed = input）
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("output", "track")
from pipeline.plugin import PluginContext  # noqa: E402
from plugin import TrackPlugin  # noqa: E402


def _make_plugin() -> TrackPlugin:
    return TrackPlugin(config={})


def _ctx(state: dict) -> PluginContext:
    return PluginContext(state=state, config={})


def test_llm_call_round_missed_and_ratio_fields() -> None:
    """llm_call 轮：missed = input - cached，ratio = cached / input。"""
    plugin = _make_plugin()
    ctx = _ctx({
        "core_type": "llm_call",
        "llm_usage": {"input_tokens": 10000, "output_tokens": 500, "cached_tokens": 8000},
        "track.llm_usage": {
            "total_input_tokens": 20000,
            "total_output_tokens": 1000,
            "total_cached_tokens": 12000,
        },
    })
    usage = plugin._collect_token_usage(ctx)
    assert usage["last_missed_tokens"] == 2000
    assert usage["total_missed_tokens"] == 10000  # (20000+10000) - (12000+8000)
    assert usage["last_cache_hit_ratio"] == 0.8
    assert usage["total_cache_hit_ratio"] == 20000 / 30000
    # 既有累计/单轮字段不被破坏
    assert usage["total_input_tokens"] == 30000
    assert usage["total_output_tokens"] == 1500
    assert usage["total_cached_tokens"] == 20000
    assert usage["last_input_tokens"] == 10000


def test_tool_execute_round_zero_single_fields() -> None:
    """tool_execute 轮：llm_usage 是上一轮残留，单轮 missed/ratio 全 0，累计保留。"""
    plugin = _make_plugin()
    ctx = _ctx({
        "core_type": "tool_execute",
        "llm_usage": {"input_tokens": 9999, "output_tokens": 1, "cached_tokens": 9999},
        "track.llm_usage": {
            "total_input_tokens": 10000,
            "total_output_tokens": 100,
            "total_cached_tokens": 9000,
        },
    })
    usage = plugin._collect_token_usage(ctx)
    assert usage["last_missed_tokens"] == 0
    assert usage["last_cache_hit_ratio"] == 0.0
    # 累计 missed/ratio 基于既有累计值重算（10000 - 9000）
    assert usage["total_missed_tokens"] == 1000
    assert usage["total_cache_hit_ratio"] == 0.9


def test_missing_cached_tokens_treated_as_zero() -> None:
    """llm_core 未上报 cached_tokens（如未开 cache）→ cached 按 0，missed = input。"""
    plugin = _make_plugin()
    ctx = _ctx({
        "core_type": "llm_call",
        "llm_usage": {"input_tokens": 5000, "output_tokens": 100},
    })
    usage = plugin._collect_token_usage(ctx)
    assert usage["last_cached_tokens"] == 0
    assert usage["last_missed_tokens"] == 5000
    assert usage["last_cache_hit_ratio"] == 0.0


def test_zero_total_input_ratio_is_zero() -> None:
    """pipeline 无任何 LLM 调用 → total ratio 0.0（不除零）。"""
    plugin = _make_plugin()
    ctx = _ctx({"core_type": "llm_call", "llm_usage": {}})
    usage = plugin._collect_token_usage(ctx)
    assert usage["total_missed_tokens"] == 0
    assert usage["total_cache_hit_ratio"] == 0.0
