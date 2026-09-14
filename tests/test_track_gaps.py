# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""track 插件剩余分支补测（缺行清零批）。

锁定以下行为契约（对应 plugin.py 缺行）：

1. **插件元数据**：name 恒为 "track"；priority 默认 15、配置值原样生效；
   route_signals 为空表——track 不参与路由（纯统计侧写）。
2. **enabled=False 全关**：不产任何 state 更新（token/耗时两路都关），
   也不触发指标上报与前端推送（观察出口零调用）。
3. **耗时锚点容错**：``run_started_at`` 不可解析（脏值）→ 记 warning 并回退
   实例构造时刻（同进程生存期内有效），不抛异常、不阻断统计；
   合法 RFC3339 值仍按墙钟差计算——两种输入可区分（脏值不产 0 之外的假值域）。
   ``Z`` 后缀（UTC 标记）按 +00:00 归一后解析成功——不落入容错分支。
4. **服务桥接为 None 的降级**：metrics / frontend 服务已注册但值为 None
   （桥接未就绪）时静默返回——区别于服务未注册（KeyError）的早退路径，
   两者都不产异常、不阻断统计主流程。
5. **推送失败不阻断**：frontend.emit 抛异常被吞并记 debug 日志——统计
   主流程照常完成（execute 返回完整 state_updates）。

**不可达/难以直达的行（已全部覆盖，说明覆盖方式）**：
- 113-114（``ValueError`` 容错）以 ``run_started_at`` 传入非法字符串直达；
- 222-224（emit 异常吞没）以抛异常的 frontend 替身直达。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("output", "track")
from pipeline.plugin import PluginContext  # noqa: E402
from plugin import TrackPlugin  # noqa: E402


class _FakeService:
    """服务替身：记录调用并可选地让 emit 抛错。"""

    def __init__(self, *, emit_error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._emit_error = emit_error

    async def record(self, name: str, value: float, metric_type: str, labels: dict | None = None,
                     unit: str | None = None, help_text: str | None = None) -> None:
        self.calls.append(("record", name, value, metric_type))

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
        "llm_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cached_tokens": 40},
    }
    state.update(over)
    return state


def _run(plugin: TrackPlugin, ctx: PluginContext) -> dict[str, Any]:
    return asyncio.run(plugin.execute(ctx)).state_updates


# ═══════════════════════════════════════════════════════════
# 1. 插件元数据与全关开关
# ═══════════════════════════════════════════════════════════


class TestPluginMetadata:
    def test_name_is_stable(self) -> None:
        assert TrackPlugin().name == "track"

    @pytest.mark.parametrize("configured", [1, 30, 99])
    def test_priority_configurable(self, configured: int) -> None:
        assert TrackPlugin({"priority": configured}).priority == configured

    def test_priority_defaults_to_15(self) -> None:
        assert TrackPlugin().priority == 15

    def test_route_signals_empty(self) -> None:
        """track 不声明任何路由信号（纯统计侧写，不参与路由决策）。"""
        assert TrackPlugin().route_signals == []


class TestDisabledSwitch:
    def test_disabled_produces_no_updates(self) -> None:
        """enabled=False → 空 updates（token 与耗时两路都不写）。"""
        updates = _run(TrackPlugin({"enabled": False}), _ctx(_llm_state()))
        assert updates == {}

    def test_disabled_does_not_touch_service_exits(self) -> None:
        """全关时指标/前端出口零调用（观察出口不可达）。"""
        metrics = _FakeService()
        frontend = _FakeService()
        updates = _run(
            TrackPlugin({"enabled": False}),
            _ctx(_llm_state(), {"metrics": metrics, "frontend": frontend}),
        )
        assert updates == {}
        assert metrics.calls == []
        assert frontend.calls == []

    def test_enabled_writes_both_stat_blocks(self) -> None:
        """对照：默认启用时 token 与耗时两块都产出。"""
        updates = _run(TrackPlugin(), _ctx(_llm_state()))
        assert updates["track.total_tokens"] == 120
        assert updates["track.execution_stats"]["iteration"] == 2
        assert updates["track.execution_stats"]["core_type"] == "llm_call"


# ═══════════════════════════════════════════════════════════
# 2. 耗时锚点容错
# ═══════════════════════════════════════════════════════════


class TestElapsedAnchorFallback:
    """run_started_at 锚点：合法 RFC3339 走墙钟差，脏值回退实例锚点记 warning。"""

    def test_valid_iso_anchor_measures_wall_clock(self) -> None:
        """合法锚点（30 秒前）→ 耗时 ≈30s（墙钟差，非实例构造时刻）。"""
        started = datetime.now(UTC) - timedelta(seconds=30)
        state = _llm_state(run_started_at=started.isoformat())
        stats = _run(TrackPlugin(), _ctx(state))["track.execution_stats"]
        assert 25.0 <= stats["elapsed_total"] <= 35.0

    def test_z_suffixed_anchor_parses(self) -> None:
        """Z 后缀（引擎 RFC3339 UTC）归一后解析成功，不落容错分支。"""
        started = datetime.now(UTC) - timedelta(seconds=10)
        raw = started.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        stats = _run(TrackPlugin(), _ctx(_llm_state(run_started_at=raw)))["track.execution_stats"]
        assert stats["elapsed_total"] >= 5.0

    @pytest.mark.parametrize("junk", ["not-a-timestamp", "2026-99-99T99:99:99", "12345x"])
    def test_unparsable_anchor_falls_back_with_warning(
        self, junk: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """脏锚点：回退实例构造时刻，记 warning，统计仍完整产出。"""
        plugin = TrackPlugin()
        with caplog.at_level(logging.WARNING, logger="plugin"):
            updates = _run(plugin, _ctx(_llm_state(run_started_at=junk)))
        assert "run_started_at 不可解析" in caplog.text
        assert any(str(junk) in r.getMessage() or junk in str(r.args) for r in caplog.records)
        stats = updates["track.execution_stats"]
        assert stats["elapsed_total"] >= 0.0
        assert stats["iteration"] == 2
        assert stats["elapsed_total"] < 60.0, "回退值是进程内短时差，不得是墙钟纪元差"

    def test_missing_anchor_falls_back_silently(self, caplog: pytest.LogCaptureFixture) -> None:
        """对照：键缺失直接回退实例锚点，不产生 warning（区别于脏值）。"""
        with caplog.at_level(logging.WARNING, logger="plugin"):
            stats = _run(TrackPlugin(), _ctx(_llm_state()))["track.execution_stats"]
        assert "run_started_at 不可解析" not in caplog.text
        assert stats["elapsed_total"] >= 0.0

    def test_elapsed_monotonic_with_anchor_age(self) -> None:
        """性质：锚点越早 → 耗时越长（墙钟差单调）。"""
        plugin = TrackPlugin()
        recent = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        older = (datetime.now(UTC) - timedelta(seconds=50)).isoformat()
        recent_elapsed = _run(plugin, _ctx(_llm_state(run_started_at=recent)))["track.execution_stats"]["elapsed_total"]
        older_elapsed = _run(plugin, _ctx(_llm_state(run_started_at=older)))["track.execution_stats"]["elapsed_total"]
        assert older_elapsed > recent_elapsed

    def test_track_time_disabled_skips_stats(self) -> None:
        """track_execution_time=False：耗时块不产出（token 块不受影响）。"""
        updates = _run(TrackPlugin({"track_execution_time": False}), _ctx(_llm_state()))
        assert "track.execution_stats" not in updates
        assert updates["track.total_tokens"] == 120


# ═══════════════════════════════════════════════════════════
# 3. 服务桥接为 None 的降级
# ═══════════════════════════════════════════════════════════


class TestNoneServiceDegradation:
    """服务已注册但值为 None（桥未就绪）→ 静默降级，统计主流程照常。"""

    def test_metrics_none_skips_reporting(self) -> None:
        """metrics=None 不抛异常，stats 仍完整。"""
        updates = _run(TrackPlugin(), _ctx(_llm_state(), {"metrics": None}))
        assert updates["track.total_tokens"] == 120

    def test_metrics_present_records_while_none_does_not(self) -> None:
        """对照：metrics 在场时 record 有调用；None 时零调用（可区分）。"""
        present = _FakeService()
        _run(TrackPlugin(), _ctx(_llm_state(), {"metrics": present}))
        assert {call[1] for call in present.calls} == {
            "total_tokens", "cached_tokens", "missed_tokens", "cache_ratio",
        }

        absent = _FakeService()
        _run(TrackPlugin(), _ctx(_llm_state(), {"metrics": None}))
        assert absent.calls == []

    def test_frontend_none_skips_push(self) -> None:
        """frontend=None 不抛异常，stats 仍完整。"""
        updates = _run(TrackPlugin(), _ctx(_llm_state(), {"frontend": None}))
        assert updates["track.execution_stats"]["iteration"] == 2

    def test_frontend_present_pushes_cost_update(self) -> None:
        """对照：frontend 在场时推送一次 cost_update。"""
        frontend = _FakeService()
        _run(TrackPlugin(), _ctx(_llm_state(), {"frontend": frontend}))
        assert [call[1] for call in frontend.calls] == ["cost_update"]


# ═══════════════════════════════════════════════════════════
# 4. 推送失败不阻断统计主流程
# ═══════════════════════════════════════════════════════════


class TestPushFailureIsolation:
    """frontend.emit 抛异常：吞并记 debug，统计主流程不受影响。"""

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("ws closed"), ConnectionError("socket gone"), ValueError("bad payload")],
    )
    def test_emit_failure_does_not_break_stats(
        self, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        frontend = _FakeService(emit_error=error)
        with caplog.at_level(logging.DEBUG, logger="plugin"):
            updates = _run(TrackPlugin(), _ctx(_llm_state(), {"frontend": frontend}))
        assert updates["track.total_tokens"] == 120
        assert updates["track.execution_stats"]["iteration"] == 2
        assert "cost_update 推送失败" in caplog.text

    def test_emit_attempted_before_failure(self) -> None:
        """副作用可观察：emit 确实被调用过（失败发生在调用内，而非提前跳过）。"""
        frontend = _FakeService(emit_error=RuntimeError("boom"))
        _run(TrackPlugin(), _ctx(_llm_state(), {"frontend": frontend}))
        assert [call[1] for call in frontend.calls] == ["cost_update"]

    def test_metrics_failure_still_propagates(self) -> None:
        """对照：metrics 上报失败不在吞没范围（只吞 frontend 推送）→ 异常上抛。"""
        metrics = _FakeService(emit_error=RuntimeError("metrics down"))
        async def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("metrics down")

        metrics.record = _boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="metrics down"):
            _run(TrackPlugin(), _ctx(_llm_state(), {"metrics": metrics}))
