# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""track 统计并入 llm_core 后的契约测试（原 tests/test_track_*.py 的合并归口）。

背景（ADR 2026-09-15-track-merged-into-llm-core）：原 ``pipeline/output/track``
是 post 步骤插件，读 ``state["llm_usage"]`` 累加 token；而该键正是 llm_core
每轮写出的。并入后同一 execute 内完成累加与写回，state 写面一次成型。

本文件锁定**跨插件公开契约不变**（消费面：前端 cost_update / 内核 metrics /
context_window_guard 公式 / monitoring 与 cost_control 读 ``track.*``）：

1. ``track.llm_usage`` / ``track.total_tokens`` 的累计与单轮字段语义
   （``track.execution_stats`` 已随并入退役，见 ADR）；
2. ``cost_update`` 推送 payload 形状（路由键 + 单轮值 + cumulative 块）；
3. metrics 四指标（total/cached/missed/cache_ratio）与 labels；
4. cache 命中率异常告警（含 logger 层级名与单轮语义）；
5. ``TrackStats`` 在 llm_core 侧的执行入口（每轮一次、含 tool 轮与中断轮）。

契约面用 ``TrackStats`` 直测（与 llm_core 解耦，聚焦语义）；llm_core 接线
由 ``test_llm_core_track_wiring.py`` 钉死。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _SHARED_DIR / "system" / "llm"

for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

# track_stats 与 llm_core 同目录平铺导入（plugin.py 同款），需目录在 path 上。
if str(_LLM_CORE_DIR) in sys.path:
    sys.path.remove(str(_LLM_CORE_DIR))
sys.path.insert(0, str(_LLM_CORE_DIR))

from pipeline.plugin import PluginContext  # noqa: E402
from track_stats import TrackStats  # noqa: E402


def _usage_of(state: dict[str, Any]) -> dict[str, Any]:
    """测试便捷：把 state["llm_usage"] 当作「本轮用量」传入（真实面由 llm_core
    把本轮 API 返回的 usage 直接传进来——core 阶段 state 里还是上一轮的值）。"""
    return state.get("llm_usage") or {}


_DEFAULT = object()


def _sync(stats: TrackStats, ctx: PluginContext, usage: Any = _DEFAULT) -> dict[str, Any]:
    """同步执行统计。

    Args:
        usage: 本轮用量；缺省取 ``state["llm_usage"]``（等价"本轮有用量"）。
            传 ``{}`` 表示本轮无用量（API 未回 usage / 无 LLM 调用轮）。
    """
    current = (_usage_of(ctx.state) if usage is _DEFAULT else usage)
    return asyncio.run(stats.run(ctx, current))



class _FakeService:
    """服务替身：记录调用并可选让 emit 抛错。"""

    def __init__(self, *, emit_error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._emit_error = emit_error

    async def record(
        self,
        name: str,
        value: float,
        metric_type: str,
        labels: dict | None = None,
        unit: str | None = None,
        help_text: str | None = None,
    ) -> None:
        self.calls.append(("record", name, value, metric_type, labels))

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.calls.append(("emit", event, payload))
        if self._emit_error is not None:
            raise self._emit_error


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state, config={}, _services=services or {})


def _llm_state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "core_type": "llm_call",
        "iteration": 2,
        "session_id": "s1",
        "pipeline_id": "p1",
        "llm_usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 40,
        },
    }
    state.update(over)
    return state


def _warnings(caplog: Any) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ═══════════════════════════════════════════════════════════
# 1. 累计与单轮语义（原 test_track_missed_tokens.py 契约）
# ═══════════════════════════════════════════════════════════


class TestTokenAccumulation:
    def test_llm_round_writes_token_block(self) -> None:
        """llm_call 轮：token 块产出，累计 = 既有 + 本轮。"""
        updates = _sync(TrackStats(), _ctx(_llm_state()))
        assert updates["track.total_tokens"] == 120
        usage = updates["track.llm_usage"]
        assert usage["total_input_tokens"] == 100
        assert usage["total_output_tokens"] == 20
        assert usage["total_cached_tokens"] == 40

    def test_accumulates_across_rounds(self) -> None:
        """跨轮累计：前轮已有累计值时相加，不是覆盖。"""
        state = _llm_state(
            **{
                "track.llm_usage": {
                    "total_input_tokens": 20000,
                    "total_output_tokens": 1000,
                    "total_cached_tokens": 12000,
                }
            }
        )
        state["llm_usage"] = {
            "input_tokens": 10000,
            "output_tokens": 500,
            "cached_tokens": 8000,
        }
        usage = _sync(TrackStats(), _ctx(state))["track.llm_usage"]
        assert usage["total_input_tokens"] == 30000
        assert usage["total_output_tokens"] == 1500
        assert usage["total_cached_tokens"] == 20000
        # missed / ratio 派生字段
        assert usage["last_missed_tokens"] == 2000
        assert usage["total_missed_tokens"] == 10000
        assert usage["last_cache_hit_ratio"] == pytest.approx(0.8)
        assert usage["total_cache_hit_ratio"] == pytest.approx(20000 / 30000)

    def test_missing_cached_tokens_treated_as_zero(self) -> None:
        """上游未回 cached_tokens（未开 cache）→ cached 按 0，missed = input。"""
        state = _llm_state(llm_usage={"input_tokens": 5000, "output_tokens": 100})
        usage = _sync(TrackStats(), _ctx(state))["track.llm_usage"]
        assert usage["last_cached_tokens"] == 0
        assert usage["last_missed_tokens"] == 5000
        assert usage["last_cache_hit_ratio"] == 0.0

    def test_round_without_usage_inherits_last_llm_values(self) -> None:
        """本轮无用量（API 未回 usage）：不累加，total_* 与 last_* 整块继承上一轮。

        「本轮」语义 = 最近一次有数据的 LLM 轮（前端输入框用量/浮窗按此显示当前
        上下文占用）。并入后只有 llm_core 轮会调本模块，故无用量轮不写 state
        也天然保持原值——这里钉死"显式传入空 usage 时不得清零/不得编造"。
        """
        state = {
            "llm_usage": {"input_tokens": 9999, "output_tokens": 1, "cached_tokens": 9999},
            "track.llm_usage": {
                "total_input_tokens": 10000,
                "total_output_tokens": 100,
                "total_cached_tokens": 9000,
                "last_input_tokens": 8000,
                "last_output_tokens": 200,
                "last_cached_tokens": 7200,
                "last_missed_tokens": 800,
                "last_cache_hit_ratio": 0.9,
            },
        }
        usage = _sync(TrackStats(), _ctx(state), {})["track.llm_usage"]
        assert usage["total_input_tokens"] == 10000
        assert usage["total_missed_tokens"] == 1000
        assert usage["last_input_tokens"] == 8000
        assert usage["last_output_tokens"] == 200
        assert usage["last_missed_tokens"] == 800
        assert usage["last_cache_hit_ratio"] == 0.9

    def test_first_round_without_usage_keeps_zeros(self) -> None:
        """首轮即无用量且无历史累计 → 全 0，不编造数据。"""
        state = {"llm_usage": {"input_tokens": 9999, "output_tokens": 1}}
        usage = _sync(TrackStats(), _ctx(state), {})["track.llm_usage"]
        assert usage["last_input_tokens"] == 0
        assert usage["last_cache_hit_ratio"] == 0.0
        assert usage["total_input_tokens"] == 0

    def test_zero_total_input_ratio_is_zero(self) -> None:
        """无任何 LLM 调用 → total ratio 0.0（不除零）。"""
        usage = _sync(TrackStats(), _ctx({"core_type": "llm_call", "llm_usage": {}}))[
            "track.llm_usage"
        ]
        assert usage["total_missed_tokens"] == 0
        assert usage["total_cache_hit_ratio"] == 0.0


# ═══════════════════════════════════════════════════════════
# 2. 开关与配置
# ═══════════════════════════════════════════════════════════


class TestSwitches:
    def test_disabled_produces_no_updates(self) -> None:
        """enabled=False → 两路都关，且观察出口零调用。"""
        metrics = _FakeService()
        frontend = _FakeService()
        updates = _sync(TrackStats({"enabled": False}), 
            _ctx(_llm_state(), {"metrics": metrics, "frontend": frontend})
        )
        assert updates == {}
        assert metrics.calls == []
        assert frontend.calls == []

    def test_tokens_disabled_skips_token_and_exits(self) -> None:
        """track_token_usage=False → token 块与两个观察出口都不动，耗时块保留。"""
        metrics = _FakeService()
        frontend = _FakeService()
        updates = _sync(TrackStats({"track_token_usage": False}), 
            _ctx(_llm_state(), {"metrics": metrics, "frontend": frontend})
        )
        assert updates == {}, "token 追踪关 + 耗时块已退役 → 无任何产出"


# ═══════════════════════════════════════════════════════════
# 3. cost_update 推送契约（原 test_track_cost_update_event.py）
# ═══════════════════════════════════════════════════════════


class TestCostUpdatePush:
    def _state(self, **over: Any) -> dict[str, Any]:
        state = {
            "core_type": "llm_call",
            "session_id": "session_001",
            "pipeline_id": "pipeline_cost_001",
            "message_id": "msg_001",
            "llm_usage": {
                "input_tokens": 1200,
                "output_tokens": 300,
                "total_tokens": 1500,
                "cached_tokens": 1000,
            },
            "track.llm_usage": {
                "total_input_tokens": 3200,
                "total_output_tokens": 700,
                "total_tokens": 3900,
                "total_cached_tokens": 2500,
                "total_missed_tokens": 700,
                "total_cache_hit_ratio": 2500 / 3200,
                "last_input_tokens": 1200,
                "last_output_tokens": 300,
                "last_cached_tokens": 1000,
                "last_missed_tokens": 200,
                "last_cache_hit_ratio": 1000 / 1200,
            },
        }
        state.update(over)
        return state

    def test_payload_routing_keys_and_single_round(self) -> None:
        """payload 含路由键（内核/前端硬门控）+ 本轮单轮值。"""
        frontend = _FakeService()
        stats = TrackStats()
        state = self._state()
        ctx = _ctx(state, {"frontend": frontend})
        usage = stats.collect(ctx, _usage_of(state))
        asyncio.run(stats.push_cost_update(ctx, usage, _usage_of(state)))
        data = frontend.calls[0][2]
        assert data["thread_id"] == "session_001"
        assert data["pipeline_id"] == "pipeline_cost_001"
        assert data["message_id"] == "msg_001"
        assert data["input_tokens"] == 1200
        assert data["output_tokens"] == 300
        assert data["cached_tokens"] == 1000
        assert data["total_tokens"] == 1500
        assert data["missed_tokens"] == 200
        assert data["cache_hit_ratio"] == pytest.approx(1000 / 1200)

    def test_payload_carries_cumulative_block(self) -> None:
        """cumulative.* = 整个管道累计消耗（前端统计区分桶加总）。"""
        frontend = _FakeService()
        stats = TrackStats()
        state = self._state()
        ctx = _ctx(state, {"frontend": frontend})
        usage = stats.collect(ctx, _usage_of(state))
        asyncio.run(stats.push_cost_update(ctx, usage, _usage_of(state)))
        cum = frontend.calls[0][2]["cumulative"]
        assert cum["total_input"] == 4400  # 3200 + 1200
        assert cum["total_output"] == 1000
        assert cum["total_cached"] == 3500
        assert cum["missed"] == 900
        assert cum["total_tokens"] == 5400
        assert cum["cache_hit_ratio"] == pytest.approx(3500 / 4400)

    def test_not_pushed_without_usage(self) -> None:
        """本轮无用量不推送（避免用继承值覆盖前端显示）。"""
        frontend = _FakeService()
        stats = TrackStats()
        state = self._state()
        ctx = _ctx(state, {"frontend": frontend})
        usage = stats.collect(ctx, {})
        asyncio.run(stats.push_cost_update(ctx, usage, {}))
        assert frontend.calls == []

    def test_silent_without_frontend_service(self) -> None:
        """frontend 未注入（旧内核/单测）静默跳过，不外泄异常。"""
        stats = TrackStats()
        state = self._state()
        ctx = _ctx(state)
        with pytest.raises(KeyError):
            ctx.get_service("frontend")
        usage = stats.collect(ctx, _usage_of(state))
        asyncio.run(stats.push_cost_update(ctx, usage, _usage_of(state)))  # 不抛

    def test_not_accumulated_across_rounds(self) -> None:
        """每轮推送的是本轮单轮值，不是跨轮累计（回归守护）。"""
        frontend = _FakeService()
        stats = TrackStats()
        state = self._state(
            llm_usage={"input_tokens": 1800, "output_tokens": 100, "total_tokens": 1900}
        )
        ctx = _ctx(state, {"frontend": frontend})
        usage = stats.collect(ctx, _usage_of(state))
        asyncio.run(stats.push_cost_update(ctx, usage, _usage_of(state)))
        assert frontend.calls[0][2]["total_tokens"] == 1900

    def test_emit_failure_does_not_break_stats(self, caplog: pytest.LogCaptureFixture) -> None:
        """推送失败被吞并记 debug，统计主流程照常完成。"""
        frontend = _FakeService(emit_error=RuntimeError("ws closed"))
        stats = TrackStats()
        state = self._state()
        ctx = _ctx(state, {"frontend": frontend})
        with caplog.at_level(logging.DEBUG, logger="track_stats"):
            updates = _sync(stats, ctx)
        assert "cost_update 推送失败" in caplog.text
        assert updates["track.total_tokens"] == 5400  # 累计 3900 + 本轮 1500
        assert [c[1] for c in frontend.calls] == ["cost_update"]


# ═══════════════════════════════════════════════════════════
# 4. metrics 上报契约（原 test_track_record_metric.py）
# ═══════════════════════════════════════════════════════════


class TestMetricsReport:
    def _state(self, **over: Any) -> dict[str, Any]:
        state = {
            "core_type": "llm_call",
            "session_id": "session_001",
            "pipeline_id": "pipeline_metrics_001",
            "llm_model": "deepseek-v4",
            "llm_provider": "deepseek",
            "llm_usage": {
                "input_tokens": 1200,
                "output_tokens": 300,
                "total_tokens": 1500,
                "cached_tokens": 1000,
            },
        }
        state.update(over)
        return state

    def _records(self, metrics: _FakeService) -> dict[str, tuple[Any, ...]]:
        return {c[1]: (c[2], c[3], c[4]) for c in metrics.calls}

    def test_four_metrics_with_labels(self) -> None:
        """llm_call 轮四个指标各一次，labels 带 model/provider。"""
        metrics = _FakeService()
        stats = TrackStats()
        state = self._state()
        ctx = _ctx(state, {"metrics": metrics})
        usage = stats.collect(ctx, _usage_of(state))
        asyncio.run(stats.report_metrics(ctx, usage, _usage_of(state)))
        by_name = self._records(metrics)
        assert set(by_name) == {"total_tokens", "cached_tokens", "missed_tokens", "cache_ratio"}
        assert by_name["total_tokens"][0] == 1500
        assert by_name["cached_tokens"][0] == 1000
        assert by_name["missed_tokens"][0] == 200
        assert by_name["total_tokens"][1] == "counter"
        assert by_name["cache_ratio"][1] == "gauge"
        assert by_name["cache_ratio"][0] == pytest.approx(1000 / 1200)
        for name in by_name:
            assert by_name[name][2] == {"model": "deepseek-v4", "provider": "deepseek"}

    def test_skips_without_usage(self) -> None:
        """本轮无用量不上报（避免把继承值当本轮量重复计入累计）。"""
        metrics = _FakeService()
        stats = TrackStats()
        state = self._state()
        ctx = _ctx(state, {"metrics": metrics})
        usage = stats.collect(ctx, {})
        asyncio.run(stats.report_metrics(ctx, usage, {}))
        assert metrics.calls == []

    def test_silent_without_service(self) -> None:
        """metrics 未注入 → 静默跳过，无 state 副作用。"""
        stats = TrackStats()
        state = self._state()
        snapshot = dict(state)
        ctx = _ctx(state)
        usage = stats.collect(ctx, _usage_of(state))
        assert asyncio.run(stats.report_metrics(ctx, usage, _usage_of(state))) is None
        assert ctx.state == snapshot

    def test_no_model_labels_yields_empty_dict(self) -> None:
        """无 model/provider → labels 空 dict，上报不失败。"""
        metrics = _FakeService()
        stats = TrackStats()
        state = self._state()
        state.pop("llm_model")
        state.pop("llm_provider")
        ctx = _ctx(state, {"metrics": metrics})
        usage = stats.collect(ctx, _usage_of(state))
        asyncio.run(stats.report_metrics(ctx, usage, _usage_of(state)))
        assert self._records(metrics)["total_tokens"][2] == {}


# ═══════════════════════════════════════════════════════════
# 5. cache 命中率异常告警（原 test_track_cache_anomaly.py）
# ═══════════════════════════════════════════════════════════


class TestCacheAnomaly:
    _LOGGER = "plugins.output.track.plugin"

    def _check(self, usage: dict[str, Any], **cfg: Any) -> None:
        TrackStats(cfg).check_cache_anomaly(usage, "pipeline_cache_001")

    def test_high_hit_rate_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            self._check({"last_input_tokens": 10000, "last_cached_tokens": 9500})
        assert _warnings(caplog) == []

    def test_low_hit_rate_warns_with_per_round_ratio(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """低命中率告警，消息携带本轮（非累计）命中率。"""
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            self._check(
                {
                    "last_input_tokens": 10000,
                    "last_cached_tokens": 3000,
                    "total_input_tokens": 100000,
                    "total_cached_tokens": 95000,
                }
            )
        warns = _warnings(caplog)
        assert len(warns) == 1
        assert "本轮" in warns[0]
        assert "30.0%" in warns[0]

    def test_zero_last_input_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        """last_input=0 无法判定（tool 轮）→ 静默跳过，不得拿累计量误报。"""
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            self._check(
                {
                    "last_input_tokens": 0,
                    "last_cached_tokens": 0,
                    "total_input_tokens": 420041,
                    "total_cached_tokens": 347776,
                }
            )
        assert _warnings(caplog) == []

    def test_high_cumulative_uncached_but_per_round_ok(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """累计未命中占比高但本轮命中率正常 → 不报（累计量不得触发）。"""
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            self._check(
                {
                    "last_input_tokens": 60632,
                    "last_cached_tokens": 60000,
                    "total_input_tokens": 2458025,
                    "total_cached_tokens": 2331456,
                }
            )
        assert _warnings(caplog) == []

    def test_threshold_configurable(self, caplog: pytest.LogCaptureFixture) -> None:
        """阈值可配：命中率 50% 时默认（0.9）报、阈值降到 0.4 不报。"""
        usage = {"last_input_tokens": 10000, "last_cached_tokens": 5000}
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            self._check(usage)
        assert len(_warnings(caplog)) == 1

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            self._check(usage, cache_hit_warn_threshold=0.4)
        assert _warnings(caplog) == []
