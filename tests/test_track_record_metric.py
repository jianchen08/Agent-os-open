# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""TrackPlugin record_metric 上报契约测试（G1，监控设计 §三 通道2）。

钉死 track 把每轮 token 业务指标经 metrics capability 上报内核聚合器：
- llm_call 轮上报四个指标：total_tokens/cached_tokens/missed_tokens（counter）
  与 cache_ratio（gauge），labels 携带 model/provider（取自 state）
- missed = input - cached（未命中缓存重新计费的输入）
- tool_execute 轮 llm_usage 是上一轮残留，不重复上报（与 cost_update 同语义）
- metrics 服务未注入（旧内核 / 单测环境）静默跳过，不抛异常
- 上报失败不阻断统计主流程（fire-and-forget）

[来源: docs/working/重要设计/监控与成本统一方案.md §三.2 指标目录]
"""

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tests._pipeline_plugin_path import add_plugin_dir  # noqa: E402

add_plugin_dir("output", "track")
from pipeline.plugin import PluginContext  # noqa: E402
from plugin import TrackPlugin  # noqa: E402

PIPELINE_ID = "pipeline_metrics_001"


class _FakeMetrics:
    """假 metrics reporter：记录 record(name, value, metric_type, labels, ...) 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def record(self, name: str, value: float, metric_type: str, labels: dict | None = None,
                     unit: str | None = None, help_text: str | None = None) -> None:
        self.calls.append((name, value, metric_type, labels))


def _make_ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    """构造带可选 services 的 PluginContext。"""
    return PluginContext(state=state, config={}, _services=services or {})


def _base_state(**overrides: Any) -> dict[str, Any]:
    """llm_call 轮标准 state（带模型/供应商标签）。"""
    state = {
        "core_type": "llm_call",
        "session_id": "session_001",
        "pipeline_id": PIPELINE_ID,
        "llm_model": "deepseek-v4",
        "llm_provider": "deepseek",
        "llm_usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "cached_tokens": 1000,
        },
    }
    state.update(overrides)
    return state


def _metrics_map(metrics: _FakeMetrics) -> dict[str, tuple[Any, ...]]:
    out: dict[str, tuple[Any, ...]] = {}
    for name, value, metric_type, labels in metrics.calls:
        out[name] = (value, metric_type, labels)
    return out


@pytest.mark.asyncio
async def test_report_metrics_llm_call_round() -> None:
    """llm_call 轮：四个指标各上报一次，labels 含 model/provider。"""
    plugin = TrackPlugin()
    metrics = _FakeMetrics()
    ctx = _make_ctx(_base_state(), {"metrics": metrics})

    usage = plugin._collect_token_usage(ctx)
    await plugin._try_report_metrics(ctx, usage)

    by_name = _metrics_map(metrics)
    assert set(by_name) == {"total_tokens", "cached_tokens", "missed_tokens", "cache_ratio"}
    # 单轮值 = 本轮 llm_usage（同源同量：与 cost_update 同一批数字）
    assert by_name["total_tokens"][0] == 1500
    assert by_name["cached_tokens"][0] == 1000
    assert by_name["missed_tokens"][0] == 200  # input(1200) - cached(1000)
    # counter 语义：单调累加；cache_ratio 是 gauge（覆盖）
    assert by_name["total_tokens"][1] == "counter"
    assert by_name["cache_ratio"][1] == "gauge"
    assert by_name["cache_ratio"][0] == pytest.approx(1000 / 1200)
    # labels：model/provider 透传
    for name in ("total_tokens", "cached_tokens", "missed_tokens", "cache_ratio"):
        assert by_name[name][2] == {"model": "deepseek-v4", "provider": "deepseek"}


@pytest.mark.asyncio
async def test_report_metrics_skips_tool_round() -> None:
    """tool_execute 轮：llm_usage 是上一轮残留，不上报。"""
    plugin = TrackPlugin()
    metrics = _FakeMetrics()
    state = _base_state(
        core_type="tool_execute",
        llm_usage={"input_tokens": 500, "output_tokens": 10, "total_tokens": 510, "cached_tokens": 0},
    )
    ctx = _make_ctx(state, {"metrics": metrics})

    usage = plugin._collect_token_usage(ctx)
    await plugin._try_report_metrics(ctx, usage)

    assert metrics.calls == []


@pytest.mark.asyncio
async def test_report_metrics_no_service_silent() -> None:
    """metrics 服务未注入：静默跳过（不抛、不写 state）。"""
    plugin = TrackPlugin()
    state = _base_state()
    snapshot = dict(state)
    ctx = _make_ctx(state, {})

    usage = plugin._collect_token_usage(ctx)
    assert await plugin._try_report_metrics(ctx, usage) is None
    # 跳过不得有任何 state 副作用
    assert ctx.state == snapshot


@pytest.mark.asyncio
async def test_report_metrics_no_model_labels() -> None:
    """state 无 model/provider：labels 为空 dict，上报不失败。"""
    plugin = TrackPlugin()
    metrics = _FakeMetrics()
    state = _base_state()
    state.pop("llm_model")
    state.pop("llm_provider")
    ctx = _make_ctx(state, {"metrics": metrics})

    usage = plugin._collect_token_usage(ctx)
    await plugin._try_report_metrics(ctx, usage)

    by_name = _metrics_map(metrics)
    assert by_name["total_tokens"][2] == {}
