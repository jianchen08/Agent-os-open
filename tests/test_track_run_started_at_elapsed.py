# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""TrackPlugin 耗时锚点测试（控制状态键契约 ADR 2026-08-30）。

钉死 elapsed 的锚点语义：

- 锚点 = 引擎每 run 覆盖写的 ``run_started_at``（RFC3339 墙钟）——
  elapsed 表达「本次 run 跑了多久」；
- 键缺失（旧内核 / 单测环境）回退实例构造时刻（monotonic 域）；
- **同实例跨管道不得串钟**：sidecar 实例被多管道复用，前一管道的
  run_started_at/锚点不得污染后一管道的 elapsed。

背景（2026-08-29 实测）：旧实现 `_start_time` 只在实例构造时取一次，
elapsed_total 实为宿主进程 uptime——6 分 42 秒的 run 被判
"elapsed cap reached: 8282s >= 3600s"（三管道四读数反推锚点 ±0.3s 同锚）。
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("output", "track")
from plugin import TrackPlugin  # noqa: E402


def _ctx(state: dict[str, Any]) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


def _execute(plugin: TrackPlugin, state: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(plugin.execute(_ctx(state)))
    return result.state_updates


def _rfc3339(seconds_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat()


def test_elapsed_from_run_started_at_is_run_scoped() -> None:
    """run_started_at 10 分钟前 → elapsed_total ≈ 600s（而非宿主 uptime）。"""
    plugin = TrackPlugin(config={})
    state = {"core_type": "llm_call", "run_started_at": _rfc3339(600.0)}
    stats = _execute(plugin, state)["track.execution_stats"]
    assert 590.0 <= stats["elapsed_total"] <= 615.0, (
        "elapsed 应锚定 run 起始墙钟（600s±余量），实际 %r" % stats["elapsed_total"]
    )


def test_elapsed_does_not_bleed_across_pipelines_same_instance() -> None:
    """同实例跨管道：后一管道新 run_started_at → elapsed 归零级，不继承前值。"""
    plugin = TrackPlugin(config={})
    pipe_a = {"pipeline_id": "pipe-a", "core_type": "llm_call",
              "run_started_at": _rfc3339(7200.0)}
    stats_a = _execute(plugin, pipe_a)["track.execution_stats"]
    assert stats_a["elapsed_total"] >= 7000.0

    pipe_b = {"pipeline_id": "pipe-b", "core_type": "llm_call",
              "run_started_at": _rfc3339(2.0)}
    stats_b = _execute(plugin, pipe_b)["track.execution_stats"]
    assert stats_b["elapsed_total"] < 30.0, (
        "跨管道不得继承前管道 elapsed（sidecar 复用串钟即本 bug），实际 %r"
        % stats_b["elapsed_total"]
    )


def test_elapsed_falls_back_to_instance_anchor_when_key_missing() -> None:
    """键缺失（旧内核/单测）→ 回退实例锚点：首次调用 elapsed 近零。"""
    plugin = TrackPlugin(config={})
    state = {"core_type": "llm_call"}
    stats = _execute(plugin, state)["track.execution_stats"]
    assert 0.0 <= stats["elapsed_total"] < 30.0


def test_elapsed_never_negative_on_future_timestamp() -> None:
    """性质断言：异常未来时间戳不得产生负耗时（max(0) 钳制）。"""
    plugin = TrackPlugin(config={})
    state = {"core_type": "llm_call", "run_started_at": _rfc3339(-120.0)}
    stats = _execute(plugin, state)["track.execution_stats"]
    assert stats["elapsed_total"] == 0.0


def test_iteration_guard_keeps_per_iteration_definition() -> None:
    """iteration=0 时 elapsed_per_iteration 仍为 elapsed/1（不除零、不发散）。"""
    plugin = TrackPlugin(config={})
    state = {"core_type": "llm_call", "run_started_at": _rfc3339(10.0), "iteration": 0}
    stats = _execute(plugin, state)["track.execution_stats"]
    assert stats["iteration"] == 0
    assert stats["elapsed_per_iteration"] >= stats["elapsed_total"] / 2
