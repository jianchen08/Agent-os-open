# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""stop_check 执行超时闸门契约测试（真实 run 时长锚 + 时间预算退役）。

事故锚（2026-10-20 恢复任务 c908246e2304）：run 实际运行 1748 秒，却被
``[stop_check] Execution timeout: 605.1 > 600 seconds`` 终结。两层根因：
① 代码缺省 600 与文件头「时间预算已退役（缺省 -1）」裁定矛盾；② 计时器是
共享 sidecar 实例上的单标量，别的管道插队即清零，报出的 elapsed 与 run
时长脱钩。

本文件锁定：
1. 缺省不设时长上限（-1）：长跑任务不被误杀（事故形态复现）；
2. 显式配置时长时按真实 run 时长判定（state.run_started_at 锚）；
3. 同一插件实例被两个 run 交错执行时 elapsed 互不污染（不互相清零）；
4. run 结束后再次进入不串账（新 run 计时归零重来）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/
_SYSTEM = _SHARED / "system"

for _d in [_DIR, _SHARED, _SYSTEM]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "stop_check_plugin_run_duration_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load spec loader"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeClock:
    """可手动推进的单调时钟（注入替代 time.monotonic，禁真实 sleep）。"""

    def __init__(self) -> None:
        self._now = 1000.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={}, _services={})


def _now() -> datetime:
    return datetime.now(UTC)


def _run_started_at_ago(seconds: float) -> str:
    """内核 run_init 写入的 run 起点墙钟（RFC3339，ago 秒前）。"""
    return (_now() - timedelta(seconds=seconds)).isoformat()


def _timeout_seconds_in_message(content: str) -> float:
    """从终止文案解析实际运行秒数（"已连续运行 1748.0 秒（上限 600 秒）"）。"""
    matched = re.search(r"已连续运行 ([\d.]+) 秒", content)
    assert matched is not None, f"终止文案缺运行秒数: {content}"
    return float(matched.group(1))


class TestNoImplicitDurationBudget:
    """缺省不设时长上限（时间预算已退役）：长跑任务不被误杀。"""

    def test_default_budget_does_not_kill_long_run(self, monkeypatch: Any) -> None:
        """缺省（无插件配置、无 agent 覆盖）不设时长闸：长跑不误杀。

        事故形态复现：run 已跑 700 秒（超旧缺省 600）仍须正常放行——旧实现
        缺省 600 把长跑任务判成"执行超时"强停。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state = {"pipeline_id": "p", "run_id": "run-1", "iteration": 1, "task.id": "t1"}

        first = _run(plugin.execute(_ctx(state)))
        assert first.state_updates.get("should_stop") is not True

        clock.advance(700)
        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("should_stop") is not True, (
            f"缺省不设时长上限，长跑任务不得被误杀: {res.state_updates}"
        )
        assert res.state_updates.get("router.stop_reason", "") == ""

    def test_default_budget_survives_interleaved_pipeline(self, monkeypatch: Any) -> None:
        """事故形态：run 已跑 1748 秒（真实墙钟）+ 他管道插队，仍不得被误杀。

        真实 run 时长由 state.run_started_at 给出（旧实现完全无视该键）。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "run_started_at": _run_started_at_ago(1748),
            "task.id": "t1",
        }

        clock.advance(605)  # 旧实现口径：独占实例 605 秒 → 旧实现此处即误杀
        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("should_stop") is not True, (
            f"缺省不设时长上限，1748 秒的长跑任务不得被误杀: {res.state_updates}"
        )


class TestExplicitBudgetAnchoredToRealRunDuration:
    """显式配置时长时按真实 run 时长判定（不是"自上次插队以来"）。"""

    def test_explicit_budget_uses_real_run_duration(self, monkeypatch: Any) -> None:
        """显式 600 秒上限 + run 真实已跑 1748 秒 → 命中超时（真实时长判定）。

        文案里的秒数必须是真实 run 时长（旧实现报的是插队后的实例占用时长）。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "run_started_at": _run_started_at_ago(1748),
            "task.id": "t1",
        }

        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("router.stop_reason") == "timeout", (
            f"显式上限 600 秒 + 真实时长 1748 秒应命中超时: {res.state_updates}"
        )
        content = res.state_updates["messages"]["_ops"][0]["msg"]["content"]
        elapsed = _timeout_seconds_in_message(content)
        assert 1700 <= elapsed <= 1800, f"文案秒数应为真实 run 时长（约 1748），实得 {elapsed}"

    def test_explicit_budget_not_triggered_by_other_pipeline_time(
        self, monkeypatch: Any
    ) -> None:
        """显式 600 秒上限 + 真实时长仅 100 秒 → 放行（他管道插队不计入）。

        反证锚：旧实现按实例占用挂钟计时，他管道插队后本 run 的计时被清零/
        重置，报出的 elapsed 与本 run 真实时长脱钩。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "run_started_at": _run_started_at_ago(100),
            "task.id": "t1",
        }

        clock.advance(700)  # 他管道插队占用实例 700 秒（本 run 真实时长未变）
        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("should_stop") is not True, (
            f"本 run 真实时长 100 秒（<600）应放行: {res.state_updates}"
        )


    def test_wall_clock_anchor_yields_exact_elapsed(self, monkeypatch: Any) -> None:
        """墙钟锚下 elapsed 精确可判：桩化当前时刻 → 文案秒数精确等于真实时长。

        内核 run 起点为 RFC3339 墙钟，elapsed = 当前时刻 − run 起点（秒）。"""
        mod = _load_plugin_module()
        monkeypatch.setattr(mod, "_utc_now", lambda: datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC))
        plugin = mod.StopCheckPlugin(config={})
        state = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "run_started_at": "2026-10-20T11:42:00+00:00",
            "task.id": "t1",
        }

        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("router.stop_reason") == "timeout"
        content = res.state_updates["messages"]["_ops"][0]["msg"]["content"]
        assert "已连续运行 1080.0 秒（上限 600 秒）" in content


class TestInterleavedRunsDoNotPolluteEachOther:
    """同一插件实例被两个 run 交错执行：elapsed 互不污染（不互相清零）。"""

    def test_interleaved_runs_keep_own_elapsed(self, monkeypatch: Any) -> None:
        """A/B 两 run 交错：A 自己的时长累计到超线即停，B 不被牵连。

        旧实现是共享单标量 + 身份变化即重置——B 每次插队都把 A 的计时清零，
        A 永远等不到自己的超线（假阴性）。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state_a = {
            "pipeline_id": "pipe-a",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "task.id": "t1",
        }
        state_b = {
            "pipeline_id": "pipe-b",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "task.id": "t2",
        }

        first_a = _run(plugin.execute(_ctx(state_a)))
        assert first_a.state_updates.get("should_stop") is not True

        clock.advance(700)
        first_b = _run(plugin.execute(_ctx(state_b)))
        assert first_b.state_updates.get("should_stop") is not True, (
            "B 刚起跑（自身时长近零），不得被 A 的时长牵连"
        )

        second_a = _run(plugin.execute(_ctx(state_a)))
        assert second_a.state_updates.get("router.stop_reason") == "timeout", (
            "A 自身已跑 700 秒（>600），B 插队不得清零 A 的计时"
        )

    def test_interleaved_runs_with_wall_clock_anchor(self, monkeypatch: Any) -> None:
        """墙钟锚下交错执行同样互不污染：A 按自己的真实起点，B 按自己的。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state_a = {
            "pipeline_id": "pipe-a",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "run_started_at": _run_started_at_ago(700),
            "task.id": "t1",
        }
        state_b = {
            "pipeline_id": "pipe-b",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "run_started_at": _run_started_at_ago(5),
            "task.id": "t2",
        }

        res_a = _run(plugin.execute(_ctx(state_a)))
        assert res_a.state_updates.get("router.stop_reason") == "timeout"

        res_b = _run(plugin.execute(_ctx(state_b)))
        assert res_b.state_updates.get("should_stop") is not True, (
            "B 的真实时长仅 5 秒，不得被 A 的 700 秒牵连"
        )


class TestRunEndDoesNotLeakIntoNextRun:
    """run 结束后再次进入不串账。"""

    def test_new_run_id_starts_fresh(self, monkeypatch: Any) -> None:
        """同管道新 run_id：计时归零重来（不继承上一 run 的起点）。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state_run1 = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "task.id": "t1",
        }
        state_run2 = dict(state_run1, run_id="run-2")

        _run(plugin.execute(_ctx(state_run1)))
        clock.advance(700)
        killed = _run(plugin.execute(_ctx(state_run1)))
        assert killed.state_updates.get("router.stop_reason") == "timeout"

        res = _run(plugin.execute(_ctx(state_run2)))
        assert res.state_updates.get("should_stop") is not True, (
            f"新 run 不得继承上一 run 的计时: {res.state_updates}"
        )

    def test_new_run_started_at_starts_fresh(self, monkeypatch: Any) -> None:
        """墙钟锚：新 run 的 run_started_at 更新 → 计时归零重来。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        old_run = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "run_started_at": _run_started_at_ago(900),
            "task.id": "t1",
        }
        new_run = dict(old_run, run_started_at=_run_started_at_ago(1))

        killed = _run(plugin.execute(_ctx(old_run)))
        assert killed.state_updates.get("router.stop_reason") == "timeout"

        res = _run(plugin.execute(_ctx(new_run)))
        assert res.state_updates.get("should_stop") is not True, (
            f"新 run（起点刚更新）不得被上一 run 的计时牵连: {res.state_updates}"
        )


class TestMissingAnchorIsHandled:
    """锚点全缺（run_started_at 与 run_id 均缺席）：跳过闸门，不崩不误杀。"""

    def test_no_anchor_skips_budget_without_error(self, monkeypatch: Any) -> None:
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state = {
            "pipeline_id": "p",
            "iteration": 1,
            "timeout_seconds": 600,
            "task.id": "t1",
        }

        clock.advance(700)
        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("should_stop") is not True, (
            f"无计时锚时不得凭猜测判超时: {res.state_updates}"
        )


class TestRunStartedAtParsing:
    """计时锚解析：可用格式按真实墙钟判定；不可用格式回落记账锚。"""

    def test_z_suffix_anchor_is_parsed_as_utc(self, monkeypatch: Any) -> None:
        """"…Z" 后缀按 UTC 解析：真实时长 100 秒 > 上限 50 秒 → 命中超时。

        反证锚：若 Z 后缀解析失败回落记账锚，elapsed 近零不会命中超时。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 50,
            "run_started_at": _run_started_at_ago(100).replace("+00:00", "Z"),
            "task.id": "t1",
        }

        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("router.stop_reason") == "timeout", (
            f"Z 后缀锚应被解析为 UTC 并按真实时长判定: {res.state_updates}"
        )

    def test_naive_anchor_is_treated_as_utc(self, monkeypatch: Any) -> None:
        """无时区锚按 UTC 解释：真实时长 100 秒 > 上限 50 秒 → 命中超时。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        naive = (_now() - timedelta(seconds=100)).replace(tzinfo=None).isoformat()
        state = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 50,
            "run_started_at": naive,
            "task.id": "t1",
        }

        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("router.stop_reason") == "timeout", (
            f"无时区锚应按 UTC 解释并按真实时长判定: {res.state_updates}"
        )

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "not-a-timestamp", "2026-13-45T99:99:99Z"],
    )
    def test_unparsable_anchor_falls_back_to_run_accounting(
        self, monkeypatch: Any, raw: str
    ) -> None:
        """锚不可解析：回落记账锚（按 run 记账），闸门仍可判定，不崩。

        反证锚：回落锚记的是本 run 自己的时长——推进挂钟后仍能命中超时，
        不是恒放行。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        state = {
            "pipeline_id": "p",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 600,
            "run_started_at": raw,
            "task.id": "t1",
        }

        first = _run(plugin.execute(_ctx(state)))
        assert first.state_updates.get("should_stop") is not True

        clock.advance(700)
        second = _run(plugin.execute(_ctx(state)))
        assert second.state_updates.get("router.stop_reason") == "timeout", (
            f"回落记账锚应按本 run 时长判定: {second.state_updates}"
        )


class TestFallbackAccountingIsBounded:
    """回落记账表容量上限：超出逐出最旧起点（防长驻 sidecar 无界增长）。"""

    def test_oldest_entry_is_evicted_beyond_capacity(self, monkeypatch: Any) -> None:
        """记账项超上限时最旧项被逐出：被逐出的 run 计时从头计。

        反证锚：未逐出的近期 run 仍按自己的起点累计并命中超时——闸门未失效。"""
        mod = _load_plugin_module()
        clock = _FakeClock()
        monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
        plugin = mod.StopCheckPlugin(config={})
        base = {"run_id": "r", "iteration": 1, "timeout_seconds": 600, "task.id": ""}

        for i in range(70):
            res = _run(plugin.execute(_ctx(dict(base, pipeline_id=f"p{i}"))))
            assert res.state_updates.get("should_stop") is not True

        clock.advance(700)
        evicted = _run(plugin.execute(_ctx(dict(base, pipeline_id="p0"))))
        assert evicted.state_updates.get("should_stop") is not True, (
            "最旧的记账项已被逐出——该 run 的回落计时从头计"
        )
        kept = _run(plugin.execute(_ctx(dict(base, pipeline_id="p69"))))
        assert kept.state_updates.get("router.stop_reason") == "timeout", (
            "未被逐出的 run 仍按自己的起点累计并命中超时"
        )
