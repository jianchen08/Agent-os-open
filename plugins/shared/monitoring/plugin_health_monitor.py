"""插件组健康阈值告警（装机插件组事故 2026-10-17 卡5 + 卡9 全量四条规则）。

数据源两族（内核聚合器，metrics-admin 读面）：
- ``process.last_crash_ts`` gauge（per-plugin）：唯一写方是 invoker 恢复失败/
  判死回调（notify_crash → last_crash_ts），窗口内样本严格递增跳变数 = respawn
  恢复失败次数（10s 轮询重复写同值不计）。合宿组成员按成员插件行产出告警。
- 内核自采 counter（卡9，plugin="kernel" 命名空间）：``mcp.sidecar_stdout_eof_total``
  （sidecar stdout EOF，通道级死亡信号）/ ``mcp.tools_list_observe_fail_total``
  （G2 观测失败终态，通道级）/ ``mcp.tools_list_malformed_total``（tools/list
  坏条目，数据级）。counter 样本求和 = 窗口内增量。

四条规则：
- respawn_failure（high，≥N 可配置）：crash 跳变数 ≥ 阈值（默认 3）。
- group_start_failure（medium，≥1 即告）：同上硬信号的灵敏档。
- sidecar_eof_rate（high，速率阈值可配置默认 10 次/分）：EOF 窗口速率 ≥ 阈值
  （respawn crash-loop 的直接信号，与 crash 跳变互相独立——通道死亡可先于
  判死回调发生，事故当天 18:55-20:32「工具在场、通道死」即此形态）。
- tools_list_invalid（high，≥1 即告）：观测失败终态 + malformed 条目合计 ≥1
  （每轮装配剔除整组 → 模型可见工具面缩水的直接前兆）。

读面通道两轨（卡9）：HTTP 面 GET /ext/monitoring/plugin-health 带入站凭证
（Authorization 透传，内核侧 admin/viewer 复权）；插件后台轮询循环无凭证——
经 metrics-admin 的插件语境系统读（params 带 invoker 注入 `_plugin_id` 信任锚
且无 `_authorization` → 内核免复权放行，ADR 2026-10-18-metrics-admin-plugin-
context-read）。告警日志形态对齐 performance_monitor（「性能告警 [LEVEL]: …」
先例）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 内核 metrics-admin.query 认得的窗口串（parse_window 只认这三档，其余落 1h 默认）
_VALID_WINDOWS = ("5m", "1h", "24h")

# 窗口串 → 分钟数（速率规则的分母；非法窗口不会到这里——coerce_config 已收敛）
_WINDOW_MINUTES = {"5m": 5.0, "1h": 60.0, "24h": 1440.0}

# 卡9 内核自采 counter 名（plugin="kernel" 命名空间；kernel_reads 桥按此过滤）
EOF_COUNTER = "mcp.sidecar_stdout_eof_total"
TOOLS_LIST_OBSERVE_FAIL_COUNTER = "mcp.tools_list_observe_fail_total"
TOOLS_LIST_MALFORMED_COUNTER = "mcp.tools_list_malformed_total"

# 内核级规则的 series 归属（计数器无 per-plugin 标签，聚合在 kernel 命名空间）
_KERNEL_PLUGIN_ID = "kernel"

DEFAULT_CONFIG: dict[str, Any] = {
    "respawn_failure_threshold": 3,
    "group_start_failure_threshold": 1,
    "sidecar_eof_rate_threshold": 10,
    "tools_list_invalid_threshold": 1,
    "window": "5m",
    "cooldown_secs": 300.0,
    "poll_interval_secs": 60,
}


class HealthAlert(BaseModel):
    """插件组健康告警。"""

    rule: str = Field(..., description="规则名（respawn_failure / group_start_failure）")
    level: str = Field(..., description="告警级别（medium/high）")
    plugin_id: str = Field(..., description="插件 id（合宿成员按成员行）")
    count: int = Field(..., description="窗口内恢复失败（跳变）次数")
    threshold: int = Field(..., description="规则阈值")
    window: str = Field(..., description="评估窗口（5m/1h/24h）")
    message: str = Field(..., description="告警消息")


def coerce_config(raw: Any) -> dict[str, Any]:
    """插件注入配置容错收敛：非 dict/非法键值回落默认（键面与 DEFAULT_CONFIG 对齐）。

    - 各阈值须 ≥1 整数（≥1 即告警语义不允许 0 = 恒真）；
    - window 仅认内核 parse_window 三档，其余回落默认档；
    - cooldown_secs / poll_interval_secs 须 >0 数值。
    """
    config: dict[str, Any] = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return config
    for key in (
        "respawn_failure_threshold",
        "group_start_failure_threshold",
        "sidecar_eof_rate_threshold",
        "tools_list_invalid_threshold",
    ):
        threshold = raw.get(key)
        if isinstance(threshold, int) and not isinstance(threshold, bool) and threshold >= 1:
            config[key] = threshold
    window = raw.get("window")
    if window in _VALID_WINDOWS:
        config["window"] = window
    for key in ("cooldown_secs", "poll_interval_secs"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            config[key] = float(value) if key == "cooldown_secs" else value
    return config


def count_crash_advances(samples: list[Any]) -> int:
    """统计 last_crash_ts 样本的严格递增跳变数（每次跳变 = 一次恢复失败/判死回调）。

    - 10s 轮询重复写同值不计（gauge 重复样本）；
    - 值回落（乱序/时钟回拨）不计，回落后再创新高计 1 次；
    - 畸形条目（非 dict / 缺 value）跳过。
    """
    advances = 0
    prev: float | None = None
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        value = sample.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        current = float(value)
        if prev is not None and current > prev:
            advances += 1
        prev = current
    return advances


def evaluate(crash_samples: Mapping[str, list[Any]], config: Mapping[str, Any]) -> list[HealthAlert]:
    """按阈值规则评估各插件崩溃样本（纯函数）。

    两规则共用同一评估窗口（window 是「连续/计数」的操作定义）：
    - group_start_failure（medium）：窗口内跳变 ≥ ``group_start_failure_threshold``（默认 1）；
    - respawn_failure（high）：窗口内跳变 ≥ ``respawn_failure_threshold``（默认 3）。

    Args:
        crash_samples: {plugin_id: [{ts, value}, ...]}（kernel_reads.plugin_crash_samples 形态）。
        config: coerce_config 收敛后的配置。

    Returns:
        命中告警列表（按插件 id 稳定排序）。
    """
    alerts: list[HealthAlert] = []
    window = str(config.get("window", DEFAULT_CONFIG["window"]))
    for plugin_id in sorted(crash_samples):
        count = count_crash_advances(crash_samples[plugin_id])
        for rule, level, threshold_key in (
            ("group_start_failure", "medium", "group_start_failure_threshold"),
            ("respawn_failure", "high", "respawn_failure_threshold"),
        ):
            threshold = int(config.get(threshold_key, 1))
            if count < threshold:
                continue
            if rule == "respawn_failure":
                message = (
                    f"插件 {plugin_id} 连续 respawn 恢复失败 {count} 次 ≥ 阈值 {threshold}"
                    f"（窗口 {window}），插件可能 crash-loop"
                )
            else:
                message = (
                    f"插件 {plugin_id} 宿主恢复失败 {count} 次（窗口 {window}，≥1 即告警），"
                    "组启动失败或 respawn 恢复失败"
                )
            alerts.append(
                HealthAlert(
                    rule=rule,
                    level=level,
                    plugin_id=plugin_id,
                    count=count,
                    threshold=threshold,
                    window=window,
                    message=message,
                )
            )
    return alerts


def _samples_total(samples: list[Any]) -> float:
    """counter/gauge 样本窗口总量（聚合器 counter 样本 = 每 flush 增量，求和即窗口增量）。"""
    total = 0.0
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        value = sample.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
    return total


def evaluate_kernel_counters(
    kernel_counters: Mapping[str, list[Any]], config: Mapping[str, Any]
) -> list[HealthAlert]:
    """按卡9 两条规则评估内核自采计数器样本（纯函数）。

    计数器无 per-plugin 标签（聚合在 kernel 命名空间），告警归属
    ``plugin_id="kernel"``：

    - sidecar_eof_rate（high）：EOF 窗口速率（总量/窗口分钟）≥
      ``sidecar_eof_rate_threshold``（次/分，默认 10）；
    - tools_list_invalid（high）：观测失败终态 + malformed 条目合计 ≥
      ``tools_list_invalid_threshold``（默认 1，即 ≥1 即告）。

    Args:
        kernel_counters: {metric 名: [{ts, value}, ...]}（kernel_reads.kernel_plugin_counters 形态）。
        config: coerce_config 收敛后的配置。

    Returns:
        命中告警列表（规则名稳定序）。
    """
    alerts: list[HealthAlert] = []
    window = str(config.get("window", DEFAULT_CONFIG["window"]))
    minutes = _WINDOW_MINUTES.get(window, _WINDOW_MINUTES["1h"])

    eof_total = _samples_total(kernel_counters.get(EOF_COUNTER, []))
    eof_rate = eof_total / minutes
    eof_threshold = int(config.get("sidecar_eof_rate_threshold", 1))
    if eof_rate >= eof_threshold:
        alerts.append(
            HealthAlert(
                rule="sidecar_eof_rate",
                level="high",
                plugin_id=_KERNEL_PLUGIN_ID,
                count=int(eof_total),
                threshold=eof_threshold,
                window=window,
                message=(
                    f"sidecar stdout EOF 速率 {eof_rate:.1f} 次/分（窗口 {window} 内共 "
                    f"{int(eof_total)} 次）≥ 阈值 {eof_threshold} 次/分，"
                    "存在 sidecar 通道死亡/疑似 crash-loop"
                ),
            )
        )

    observe_total = int(_samples_total(kernel_counters.get(TOOLS_LIST_OBSERVE_FAIL_COUNTER, [])))
    malformed_total = int(_samples_total(kernel_counters.get(TOOLS_LIST_MALFORMED_COUNTER, [])))
    invalid_total = observe_total + malformed_total
    invalid_threshold = int(config.get("tools_list_invalid_threshold", 1))
    if invalid_total >= invalid_threshold:
        alerts.append(
            HealthAlert(
                rule="tools_list_invalid",
                level="high",
                plugin_id=_KERNEL_PLUGIN_ID,
                count=invalid_total,
                threshold=invalid_threshold,
                window=window,
                message=(
                    f"tools/list 非法 {invalid_total} 次（观测失败终态 {observe_total} + "
                    f"malformed 条目 {malformed_total}，窗口 {window}，≥1 即告警），"
                    "涉事插件工具面可能整组被剔除"
                ),
            )
        )
    return alerts


class PluginHealthMonitor:
    """插件组健康监控器：评估 + 告警日志/回调冷却（同键冷却期内只响一次）。

    冷却只去重告警出口（日志/回调）；:meth:`evaluate_and_alert` 返回值恒为
    当前全部命中告警——状态真值不被冷却吞，端点消费者每次都看到实况。
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        alert_callback: Callable[[HealthAlert], Any] | None = None,
    ):
        self._config = coerce_config(config)
        self._clock = clock
        self._alert_callback = alert_callback
        self._last_fired: dict[tuple[str, str], float] = {}

    def set_config(self, raw: Mapping[str, Any] | None) -> None:
        """刷新配置（每次评估前由调用方注入生效视图，设置页改阈值即热生效）。"""
        self._config = coerce_config(raw)

    async def evaluate_and_alert(
        self,
        crash_samples: Mapping[str, list[Any]],
        kernel_counters: Mapping[str, list[Any]] | None = None,
    ) -> list[HealthAlert]:
        """评估并在冷却闸后发告警（日志 + 回调），返回当前全部命中告警。

        两族规则同流评估：崩溃跳变族（crash_samples，per-plugin）+ 卡9 内核
        计数器族（kernel_counters，kernel 命名空间；None = 旧调用形态兼容）。
        """
        alerts = evaluate(crash_samples, self._config)
        if kernel_counters is not None:
            alerts.extend(evaluate_kernel_counters(kernel_counters, self._config))
        now = self._clock()
        cooldown = float(self._config.get("cooldown_secs", DEFAULT_CONFIG["cooldown_secs"]))
        for alert in alerts:
            key = (alert.rule, alert.plugin_id)
            last = self._last_fired.get(key)
            if last is not None and now - last < cooldown:
                continue
            self._last_fired[key] = now
            logger.warning("插件组健康告警 [%s]: %s", alert.level.upper(), alert.message)
            if self._alert_callback is not None:
                try:
                    await self._alert_callback(alert)
                except Exception as e:  # noqa: BLE001 — 告警面失败不阻断评估（同 performance_monitor）
                    logger.error("插件组健康告警回调执行失败: %s", e)
        return alerts
