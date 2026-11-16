# @feature: FP-0.2.〇 管道引擎 | @vision: V1 可观测 | @ci: python-coverage
"""插件组健康监控（卡5）规则引擎单元测试。

覆盖（对齐 plugins/shared/monitoring/plugin_health_monitor.py）：
1. count_crash_advances：严格递增跳变计数（触发/不触发/时钟回拨/畸形样本）
2. evaluate：两条规则（respawn_failure ≥N 可配置 / group_start_failure ≥1）
   的阈值两侧 + 多插件独立 + 阈值非硬编码（自定义配置面）
3. PluginHealthMonitor：告警日志/回调冷却（fake clock 注入断言时序不变量），
   返回值不被冷却吞（状态真值恒全量）

外部依赖：无（纯逻辑模块）；时钟经注入 fake。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from plugin_health_monitor import (  # noqa: E402
    DEFAULT_CONFIG,
    PluginHealthMonitor,
    count_crash_advances,
    evaluate,
    evaluate_kernel_counters,
)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _samples(*values: float, start_ts: int = 1000, step: int = 10) -> list[dict[str, Any]]:
    """构造 [{ts, value}] 样本（ts 递增，value 按给定序列）。"""
    return [{"ts": start_ts + i * step, "value": v} for i, v in enumerate(values)]


# ═══════════════════════════════════════════════════════════
# count_crash_advances：跳变计数
# ═══════════════════════════════════════════════════════════


class TestCountCrashAdvances:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ((1000.0,), 0),  # 单样本无跳变
            ((1000.0, 1000.0, 1000.0), 0),  # 10s 轮询重复写同值 = 零跳变
            ((1000.0, 1010.0), 1),  # 单次推进
            ((1000.0, 1010.0, 1020.0, 1030.0), 3),  # 连续三次恢复失败
            ((1000.0, 1005.0, 1010.0), 2),  # 不规则间隔照样计
            ((1000.0, 990.0), 0),  # 值回落（乱序/回拨）不算跳变
            ((1000.0, 990.0, 1020.0), 1),  # 回落后仅新增一次推进
        ],
    )
    def test_counts_strict_increases(self, values: tuple[float, ...], expected: int) -> None:
        # 性质断言：跳变数 = 相邻样本严格递增的次数（非字面值拟合）
        increases = sum(1 for a, b in zip(values, values[1:], strict=False) if b > a)
        assert expected == increases
        assert count_crash_advances(_samples(*values)) == expected

    def test_empty_and_missing_value_tolerated(self) -> None:
        assert count_crash_advances([]) == 0
        assert count_crash_advances([{"ts": 1}, {"value": 5}, "junk", {"ts": 2, "value": 9}]) == 1


# ═══════════════════════════════════════════════════════════
# evaluate：两条阈值规则
# ═══════════════════════════════════════════════════════════


class TestEvaluate:
    def test_below_all_thresholds_no_alerts(self) -> None:
        # 零跳变（恒真重复样本）：两规则都不响——事故前常态
        alerts = evaluate({"task_submit": _samples(1000.0, 1000.0, 1000.0)}, DEFAULT_CONFIG)
        assert alerts == []

    def test_first_advance_fires_group_rule_only(self) -> None:
        # 规则3（组启动失败，≥1 即告）：首次恢复失败即 medium，未达规则2 阈值
        alerts = evaluate({"task_submit": _samples(1000.0, 1010.0)}, DEFAULT_CONFIG)
        assert [(a.rule, a.level) for a in alerts] == [("group_start_failure", "medium")]
        assert alerts[0].plugin_id == "task_submit"
        assert alerts[0].count == 1

    def test_three_advances_fire_both_rules(self) -> None:
        # 规则2（连续 respawn 失败 ≥3）：升级为 high，且与规则3 并存
        alerts = evaluate({"task_submit": _samples(1000.0, 1010.0, 1020.0, 1030.0)}, DEFAULT_CONFIG)
        rules = sorted((a.rule, a.level) for a in alerts)
        assert rules == [("group_start_failure", "medium"), ("respawn_failure", "high")]
        respawn = next(a for a in alerts if a.rule == "respawn_failure")
        assert respawn.count == 3
        assert respawn.threshold == 3

    def test_threshold_from_config_not_hardcoded(self) -> None:
        # 删值实验面：阈值走配置面——config 阈值 2 时 2 次跳变即响，
        # 证明判定消费配置而非写死 3
        config = {**DEFAULT_CONFIG, "respawn_failure_threshold": 2}
        samples = _samples(1000.0, 1010.0, 1020.0)
        assert count_crash_advances(samples) == 2
        alerts = evaluate({"task_submit": samples}, config)
        assert any(a.rule == "respawn_failure" for a in alerts)

    def test_multi_plugin_independent(self) -> None:
        samples = {
            "task_submit": _samples(1000.0, 1010.0, 1020.0, 1030.0),
            "file_read": _samples(2000.0, 2000.0),
        }
        alerts = evaluate(samples, DEFAULT_CONFIG)
        assert {a.plugin_id for a in alerts} == {"task_submit"}

    def test_message_carries_plugin_window_and_count(self) -> None:
        alerts = evaluate({"task_submit": _samples(1000.0, 1010.0, 1020.0, 1030.0)}, DEFAULT_CONFIG)
        for alert in alerts:
            assert "task_submit" in alert.message
            assert str(alert.count) in alert.message
            assert alert.window in alert.message


# ═══════════════════════════════════════════════════════════
# evaluate_kernel_counters：卡9 两条内核计数器规则
# ═══════════════════════════════════════════════════════════

_EOF = "mcp.sidecar_stdout_eof_total"
_OBSERVE_FAIL = "mcp.tools_list_observe_fail_total"
_MALFORMED = "mcp.tools_list_malformed_total"


class TestEvaluateKernelCounters:
    def test_eof_rate_below_threshold_no_alert(self) -> None:
        # 窗口 5m 内 20 次 EOF = 4 次/分 < 10 → 不告警
        alerts = evaluate_kernel_counters({_EOF: _samples(*[1.0] * 20)}, DEFAULT_CONFIG)
        assert alerts == []

    def test_eof_rate_at_threshold_fires(self) -> None:
        # 边界（≥）：5m 内 50 次 = 恰 10 次/分 = 阈值 → 告警
        alerts = evaluate_kernel_counters({_EOF: _samples(*[1.0] * 50)}, DEFAULT_CONFIG)
        assert [(a.rule, a.level) for a in alerts] == [("sidecar_eof_rate", "high")]
        alert = alerts[0]
        assert alert.plugin_id == "kernel"
        assert alert.count == 50  # count = 窗口内总次数
        assert alert.threshold == 10  # threshold = 速率阈值（次/分）
        assert "10" in alert.message  # 速率入消息

    def test_eof_rate_scales_with_window_not_sample_count(self) -> None:
        # 速率语义（防拟合）：同样 50 次摊在 1h = 0.83 次/分 → 不告警。
        # 若实现错把「样本和 ≥ 阈值」当判定，本用例必红。
        alerts = evaluate_kernel_counters({_EOF: _samples(*[1.0] * 50)}, {**DEFAULT_CONFIG, "window": "1h"})
        assert alerts == []

    def test_eof_threshold_configurable(self) -> None:
        # 删值实验面：阈值走配置面——threshold 2 时 5m 内 10 次（2 次/分）恰触发，
        # 证明判定消费配置而非写死 10
        config = {**DEFAULT_CONFIG, "sidecar_eof_rate_threshold": 2}
        assert evaluate_kernel_counters({_EOF: _samples(*[1.0] * 9)}, config) == []
        alerts = evaluate_kernel_counters({_EOF: _samples(*[1.0] * 10)}, config)
        assert [a.rule for a in alerts] == ["sidecar_eof_rate"]

    def test_tools_list_single_observe_fail_fires(self) -> None:
        # ≥1 即告：单次观测失败即 high（通道级故障）
        alerts = evaluate_kernel_counters({_OBSERVE_FAIL: _samples(1.0)}, DEFAULT_CONFIG)
        assert [(a.rule, a.level) for a in alerts] == [("tools_list_invalid", "high")]
        assert alerts[0].count == 1
        assert alerts[0].plugin_id == "kernel"

    def test_tools_list_single_malformed_entry_fires(self) -> None:
        # 数据级异常同样 ≥1 即告（事故当天「tools/list 吐非法」形态）
        alerts = evaluate_kernel_counters({_MALFORMED: _samples(1.0, 2.0)}, DEFAULT_CONFIG)
        assert [(a.rule, a.level) for a in alerts] == [("tools_list_invalid", "high")]
        assert alerts[0].count == 3

    def test_tools_list_combines_both_counter_keys(self) -> None:
        # 两键同窗合计（同属 tools/list 非法语义域，一条规则消费）
        alerts = evaluate_kernel_counters(
            {_OBSERVE_FAIL: _samples(2.0), _MALFORMED: _samples(3.0)}, DEFAULT_CONFIG
        )
        assert len(alerts) == 1
        assert alerts[0].count == 5
        # 分键明细入消息（观测失败 2 与 malformed 3 各自出现）
        assert "2" in alerts[0].message
        assert "3" in alerts[0].message

    def test_empty_and_missing_keys_no_alert(self) -> None:
        assert evaluate_kernel_counters({}, DEFAULT_CONFIG) == []
        assert evaluate_kernel_counters({_EOF: []}, DEFAULT_CONFIG) == []

    def test_crash_rules_and_counter_rules_coexist(self) -> None:
        # 崩溃规则（evaluate）与计数器规则（evaluate_kernel_counters）是并列
        # 两族——monitor 合流评估时四条规则可同窗并存
        crash_alerts = evaluate({"task_submit": _samples(1000.0, 1010.0)}, DEFAULT_CONFIG)
        counter_alerts = evaluate_kernel_counters({_OBSERVE_FAIL: _samples(1.0)}, DEFAULT_CONFIG)
        rules = {a.rule for a in crash_alerts} | {a.rule for a in counter_alerts}
        assert rules == {"group_start_failure", "tools_list_invalid"}


# ═══════════════════════════════════════════════════════════
# PluginHealthMonitor：冷却（fake clock）+ 返回值恒全量
# ═══════════════════════════════════════════════════════════


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class CallbackRecorder:
    def __init__(self) -> None:
        self.alerts: list[Any] = []

    async def __call__(self, alert: Any) -> None:
        self.alerts.append(alert)


class TestPluginHealthMonitor:
    def test_cooldown_suppresses_repeat_callback_but_not_return(self) -> None:
        """冷却窗口内重复评估：回调/日志去重一次，返回值仍全量（状态真值）。"""
        clock = FakeClock()
        recorder = CallbackRecorder()
        monitor = PluginHealthMonitor(DEFAULT_CONFIG, clock=clock, alert_callback=recorder)
        samples = {"task_submit": _samples(1000.0, 1010.0, 1020.0, 1030.0)}

        first = _run(monitor.evaluate_and_alert(samples))
        assert len(first) == 2
        assert len(recorder.alerts) == 2

        clock.now = 60.0  # 冷却（300s）内再评
        second = _run(monitor.evaluate_and_alert(samples))
        assert len(second) == 2  # 返回值不被冷却吞
        assert len(recorder.alerts) == 2  # 回调被去重

        clock.now = 301.0  # 冷却已过 → 再次告警
        _run(monitor.evaluate_and_alert(samples))
        assert len(recorder.alerts) == 4

    def test_alert_log_follows_performance_alert_form(self, caplog: pytest.LogCaptureFixture) -> None:
        """日志形态对齐 performance_monitor 先例（性能告警 [LEVEL]: …）。"""
        monitor = PluginHealthMonitor(DEFAULT_CONFIG, clock=FakeClock())
        with caplog.at_level("WARNING", logger="plugin_health_monitor"):
            _run(monitor.evaluate_and_alert({"task_submit": _samples(1000.0, 1010.0)}))
        assert any("插件组健康告警 [MEDIUM]:" in r.message for r in caplog.records)

    def test_cooldown_key_is_rule_and_plugin(self) -> None:
        """冷却按（规则, 插件）分键：他插件/他规则首评独立告警。"""
        clock = FakeClock()
        recorder = CallbackRecorder()
        monitor = PluginHealthMonitor(DEFAULT_CONFIG, clock=clock, alert_callback=recorder)
        _run(monitor.evaluate_and_alert({"task_submit": _samples(1000.0, 1010.0, 1020.0, 1030.0)}))
        _run(
            monitor.evaluate_and_alert(
                {
                    "task_submit": _samples(1000.0, 1010.0, 1020.0, 1030.0),
                    "file_read": _samples(2000.0, 2010.0, 2020.0, 2030.0),
                }
            )
        )
        # 第二轮只有 file_read 的 2 条过冷却闸
        assert [a.plugin_id for a in recorder.alerts[2:]] == ["file_read", "file_read"]

    def test_callback_failure_does_not_break_evaluation(self) -> None:
        """回调抛异常不中断评估（告警面失败面隔离，同 performance_monitor）。"""
        monitor = PluginHealthMonitor(
            DEFAULT_CONFIG, clock=FakeClock(), alert_callback=_BoomCallback()
        )
        alerts = _run(monitor.evaluate_and_alert({"task_submit": _samples(1000.0, 1010.0)}))
        assert len(alerts) == 1

    def test_kernel_counters_flow_through_evaluate_and_alert(self) -> None:
        """卡9：内核计数器经 evaluate_and_alert 与崩溃规则同流评估 + 冷却分键。"""
        clock = FakeClock()
        recorder = CallbackRecorder()
        monitor = PluginHealthMonitor(DEFAULT_CONFIG, clock=clock, alert_callback=recorder)
        crash = {"task_submit": _samples(1000.0, 1010.0)}
        counters = {
            "mcp.sidecar_stdout_eof_total": _samples(*[1.0] * 50),
            "mcp.tools_list_observe_fail_total": _samples(1.0),
        }
        alerts = _run(monitor.evaluate_and_alert(crash, kernel_counters=counters))
        rules = sorted(a.rule for a in alerts)
        assert rules == ["group_start_failure", "sidecar_eof_rate", "tools_list_invalid"]
        assert len(recorder.alerts) == 3
        # 冷却按（规则, plugin_id）分键：第二轮同输入只回放返回值，回调不重响
        clock.now = 60.0
        _run(monitor.evaluate_and_alert(crash, kernel_counters=counters))
        assert len(recorder.alerts) == 3

    def test_kernel_counters_none_keeps_legacy_shape(self) -> None:
        """kernel_counters 缺省（None）= 旧调用形态不破坏（卡5 既有面零迁移）。"""
        monitor = PluginHealthMonitor(DEFAULT_CONFIG, clock=FakeClock())
        alerts = _run(monitor.evaluate_and_alert({"task_submit": _samples(1000.0, 1010.0)}))
        assert [a.rule for a in alerts] == ["group_start_failure"]


class _BoomCallback:
    async def __call__(self, alert: Any) -> None:
        raise RuntimeError("callback boom")


# ═══════════════════════════════════════════════════════════
# 配置收敛
# ═══════════════════════════════════════════════════════════


class TestConfigCoercion:
    def test_invalid_config_falls_back_to_defaults(self) -> None:
        from plugin_health_monitor import coerce_config

        for raw in (None, "junk", {"respawn_failure_threshold": 0}, {"window": "7h"}):
            config = coerce_config(raw)
            assert config["respawn_failure_threshold"] >= 1
            assert config["group_start_failure_threshold"] == 1
            assert config["window"] in ("5m", "1h", "24h")

    def test_valid_partial_config_merges(self) -> None:
        from plugin_health_monitor import coerce_config

        config = coerce_config({"respawn_failure_threshold": 5, "window": "1h"})
        assert config["respawn_failure_threshold"] == 5
        assert config["window"] == "1h"
        assert config["group_start_failure_threshold"] == DEFAULT_CONFIG["group_start_failure_threshold"]
        assert config["cooldown_secs"] == DEFAULT_CONFIG["cooldown_secs"]

    def test_card9_keys_coerce_and_default(self) -> None:
        """卡9 新键：EOF 速率阈值 / tools-list 阈值 / 轮询间隔——非法回落默认，
        合法部分覆盖；≥1 即告警语义不允许 0（0 = 恒真）。"""
        from plugin_health_monitor import coerce_config

        config = coerce_config(
            {
                "sidecar_eof_rate_threshold": 20,
                "tools_list_invalid_threshold": 2,
                "poll_interval_secs": 30,
            }
        )
        assert config["sidecar_eof_rate_threshold"] == 20
        assert config["tools_list_invalid_threshold"] == 2
        assert config["poll_interval_secs"] == 30
        for raw in (
            {"sidecar_eof_rate_threshold": 0},
            {"tools_list_invalid_threshold": "junk"},
            {"poll_interval_secs": -1},
            {"sidecar_eof_rate_threshold": True},
        ):
            coerced = coerce_config(raw)
            assert coerced["sidecar_eof_rate_threshold"] == DEFAULT_CONFIG["sidecar_eof_rate_threshold"]
            assert coerced["tools_list_invalid_threshold"] == DEFAULT_CONFIG["tools_list_invalid_threshold"]
            assert coerced["poll_interval_secs"] == DEFAULT_CONFIG["poll_interval_secs"]


# ═══════════════════════════════════════════════════════════
# kernel_reads.plugin_crash_samples：metrics-admin 读面桥
# ═══════════════════════════════════════════════════════════


class TestPluginCrashSamples:
    @pytest.fixture(autouse=True)
    def _reset_providers(self):
        import kernel_reads

        yield
        kernel_reads.reset_providers()

    def test_queries_with_metric_filter_and_auth_passthrough(self) -> None:
        import kernel_reads

        captured: dict[str, Any] = {}

        async def fake_provider(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "status": 200,
                "body": {
                    "metrics": [
                        {
                            "plugin_id": "task_submit",
                            "name": "process.last_crash_ts",
                            "samples": [{"ts": 1, "value": 1000.0}, {"ts": 2, "value": 1010.0}],
                        },
                        {
                            "plugin_id": "kernel",
                            "name": "lifecycle.plugin_error_total",
                            "samples": [{"ts": 1, "value": 5.0}],
                        },
                    ]
                },
            }

        kernel_reads.set_provider("metrics-admin-query", fake_provider)
        result = _run(kernel_reads.plugin_crash_samples(window="5m", authorization="Bearer tok-x"))
        assert captured["window"] == "5m"
        assert captured["metric"] == "process.last_crash_ts"
        # provider 闭包形参 authorization（混沌演练 2026-10-18：传 _authorization
        # 会 TypeError 静默降级空数据，带凭证 HTTP 面全盲——卡9 收口修）
        assert captured["authorization"] == "Bearer tok-x"
        # 只留 process.last_crash_ts series，按插件组行
        assert result == {"task_submit": [{"ts": 1, "value": 1000.0}, {"ts": 2, "value": 1010.0}]}

    def test_provider_missing_degrades_empty(self) -> None:
        import kernel_reads

        kernel_reads.reset_providers()
        assert _run(kernel_reads.plugin_crash_samples()) == {}

    def test_provider_failure_degrades_empty(self) -> None:
        import kernel_reads

        async def broken(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("down")

        kernel_reads.set_provider("metrics-admin-query", broken)
        assert _run(kernel_reads.plugin_crash_samples()) == {}


class TestKernelPluginCounters:
    """kernel_reads.kernel_plugin_counters：卡9 内核计数器读面桥（卡9 规则数据源）。"""

    _COUNTER_NAMES = (
        "mcp.sidecar_stdout_eof_total",
        "mcp.tools_list_observe_fail_total",
        "mcp.tools_list_malformed_total",
    )

    @pytest.fixture(autouse=True)
    def _reset_providers(self):
        import kernel_reads

        yield
        kernel_reads.reset_providers()

    def test_queries_kernel_namespace_without_credential(self) -> None:
        import kernel_reads

        captured: dict[str, Any] = {}

        async def fake_provider(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "status": 200,
                "body": {
                    "metrics": [
                        {"plugin_id": "kernel", "name": self._COUNTER_NAMES[0],
                         "samples": [{"ts": 1, "value": 3.0}]},
                        {"plugin_id": "kernel", "name": "lifecycle.plugin_error_total",
                         "samples": [{"ts": 1, "value": 9.0}]},
                        {"plugin_id": "kernel", "name": self._COUNTER_NAMES[1],
                         "samples": []},
                    ]
                },
            }

        kernel_reads.set_provider("metrics-admin-query", fake_provider)
        result = _run(kernel_reads.kernel_plugin_counters(window="5m"))
        # 后台循环无凭证：不带 _authorization（内核插件语境系统读放行）
        assert "_authorization" not in captured
        assert captured["window"] == "5m"
        # provider 闭包签名的 kwarg 是 plugin_id（闭包内换算为 capability 层
        # 的 plugin 键）——传 plugin 会 TypeError 静默降级空数据（混沌演练
        # 2026-10-18 实证缺陷，卡9 收口修）
        assert captured["plugin_id"] == "kernel"
        # 只留卡9 三条计数器名（lifecycle 等无关名剔除、零样本序列不占键）
        assert set(result) == {self._COUNTER_NAMES[0]}
        assert result[self._COUNTER_NAMES[0]] == [{"ts": 1, "value": 3.0}]

    def test_authorization_passthrough_when_given(self) -> None:
        import kernel_reads

        captured: dict[str, Any] = {}

        async def fake_provider(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": 200, "body": {"metrics": []}}

        kernel_reads.set_provider("metrics-admin-query", fake_provider)
        result = _run(kernel_reads.kernel_plugin_counters(window="1h", authorization="Bearer t"))
        assert captured["authorization"] == "Bearer t"
        assert result == {}

    def test_provider_missing_degrades_empty(self) -> None:
        import kernel_reads

        kernel_reads.reset_providers()
        assert _run(kernel_reads.kernel_plugin_counters()) == {}
