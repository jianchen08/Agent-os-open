"""追踪统计 — LLM 每轮 token / 耗时统计（原 pipeline_track 插件，2026-09-15 并入）。

并入动因（ADR 2026-09-15-track-merged-into-llm-core）：本模块的输入
``state["llm_usage"]`` 正是 llm_core 每轮写出的键；原实现是独立 post 步骤插件，
必须等引擎把 llm_core 的结果合并进 state 后才能读到本轮用量——「产出者」与
「累加者」跨插件边界分离，真值链多一跳。并入后在同一 execute 内完成累加与写回。

**本轮用量必须显式传入**（``current_usage``）：llm_core 在 core 步骤执行时，
``state["llm_usage"]`` 仍是上一轮的值（本轮结果由引擎在步骤返回后才合并），
从 state 重读会把上一轮重复累加。

保留的对外契约（跨插件/前端/内核的公开面，并入不得改变）：
- state 键 ``track.llm_usage`` / ``track.total_tokens``
  （persistent + export 白名单；前端 pipelines.ts、cost_control 预算、monitoring
  与 tasks 通知、context_window_guard 公式消费）；
- ``cost_update`` 前端推送 payload 形状（路由键 + 单轮值 + cumulative 块）；
- metrics 上报（total_tokens / cached_tokens / missed_tokens / cache_ratio）；
- cache 命中率异常告警（logger ``plugins.output.track.plugin``，测试钉死）。

已退场的面：``name`` / ``priority`` / ``route_signals`` 三个 IOutputPlugin 元数据
（不再是插件）；``track.execution_stats``（迭代号/耗时统计）随并入一并退役——
全仓无生产消费方，历史轨迹中亦无业务数据，不保留收窄语义（用户裁定 2026-09-15）。
连带退役其唯一输入 ``run_started_at`` 的 reads 声明。

「管道停在非 LLM 轮时 last_* 不回零」由结构自然保证：并入后只有 LLM 轮会重写
``track.llm_usage``，停在 tool 轮时该键保持最近一次 LLM 轮的值（原插件为此专门
写了继承分支——因为它每轮都跑）。
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.types import StateKeys

logger = logging.getLogger(__name__)

# 缓存异常检测专用 logger：稳定层级名便于测试捕获与生产日志按插件过滤
# （tests/test_track_stats_contract.py 钉死；并入后保留原名以维持日志过滤口径）。
_anomaly_logger = logging.getLogger("plugins.output.track.plugin")

# 单轮 cache 命中率告警阈值（config cache_hit_warn_threshold 可覆盖）
_CACHE_HIT_WARN_THRESHOLD = 0.9


def _resolve_service(ctx: Any, name: str) -> Any:
    """按名解析 ctx 上的服务，未注入/上下文不支持服务面 → None（静默跳过）。

    未注入（旧内核）与上下文无服务面（单测最小替身）同语义：观察出口不可用。
    服务实例自身抛出的异常不在吞没范围（只吞查找失败）。
    """
    getter = getattr(ctx, "get_service", None)
    if getter is None:
        return None
    try:
        return getter(name)
    except KeyError:
        return None


class TrackStats:
    """每轮 token / 耗时统计与观测出口（原 TrackPlugin 的统计职责）。

    宿主 llm_core 在成功路径调用 ``run(ctx, current_usage)``：同一调用内拿到
    本轮 usage，累加进 ``track.llm_usage``，并把 metrics / cost_update 两个
    观察出口走完。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化追踪统计。

        Args:
            config: 支持 enabled / track_token_usage / cache_hit_warn_threshold
                （与原 track 插件同键同默认；原插件 manifest 未声明 config_files，
                生产上恒为默认值）。
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._track_tokens = self._config.get("track_token_usage", True)

    async def run(self, ctx: Any, current_usage: dict[str, Any] | None) -> dict[str, Any]:
        """执行统计：返回应并入本轮 state_updates 的键值。

        Args:
            ctx: 插件执行上下文（读 state，写观察出口）。
            current_usage: 本轮的 ``llm_usage``（llm_core 本轮 API 返回构造；
                空/None = 本轮无用量 → 累计与单轮字段继承上一轮，不编造数据）。

        Returns:
            ``track.llm_usage`` / ``track.total_tokens`` 的更新字典
            （token 追踪关时为空 dict）。
        """
        if not self._enabled:
            return {}

        updates: dict[str, Any] = {}

        # 1. Token 用量追踪
        if self._track_tokens:
            usage = self.collect(ctx, current_usage)
            updates["track.llm_usage"] = usage
            # 写入标准累计 token 值，供 cost_control 插件读取
            updates["track.total_tokens"] = usage.get("total_tokens", 0)
            # cache 命中异常检测（本轮单轮语义，详见 check_cache_anomaly）；
            # 仅本轮有用量时检测——无用量轮 last_* 是继承值，重检会重复告警
            if current_usage:
                self.check_cache_anomaly(usage, ctx.state.get(StateKeys.PIPELINE_ID, "") or "")
            # 业务指标经 record_metric 上报内核聚合器（监控设计 §三 通道2）
            await self.report_metrics(ctx, usage, current_usage)
            # 推送本轮单轮 token 用量到前端（输入框进度条实时显示）
            await self.push_cost_update(ctx, usage, current_usage)

        return updates

    async def report_metrics(
        self, ctx: Any, usage: dict[str, Any], current_usage: dict[str, Any] | None
    ) -> None:
        """上报本轮 token 业务指标到内核聚合器（record_metric，G1）。

        与 cost_update 同点分支：同一批 usage 数字一份走 WS 实时推送（前端
        本次运行态），一份走 record_metric 累计（/metrics 与监控页历史统计），
        同源同量。本轮无用量（无 LLM 调用轮）跳过上报。

        出口：``ctx.get_service("metrics")``（MetricsReporter，由宿主 server.py
        从 metrics capability 桥接注入）。服务未注入（旧内核 / 单测环境）
        静默跳过；上报失败不阻断统计主流程（宿主侧隔离）。
        """
        if not current_usage:
            return
        metrics = _resolve_service(ctx, "metrics")
        if metrics is None:
            return

        labels: dict[str, str] = {}
        model = (
            current_usage.get("model")
            or ctx.state.get("llm_model")
            or ctx.state.get("model")
            or ""
        )
        provider = current_usage.get("provider") or ctx.state.get("llm_provider") or ""
        if model:
            labels["model"] = str(model)
        if provider:
            labels["provider"] = str(provider)

        await metrics.record("total_tokens", current_usage.get("total_tokens", 0), "counter", labels, "tokens")
        await metrics.record("cached_tokens", current_usage.get("cached_tokens", 0), "counter", labels, "tokens")
        missed = max(
            current_usage.get("input_tokens", 0) - current_usage.get("cached_tokens", 0), 0
        )
        await metrics.record("missed_tokens", missed, "counter", labels, "tokens")
        last_input = current_usage.get("input_tokens", 0)
        await metrics.record(
            "cache_ratio",
            (current_usage.get("cached_tokens", 0) / last_input) if last_input > 0 else 0.0,
            "gauge",
            labels,
        )

    async def push_cost_update(
        self, ctx: Any, usage: dict[str, Any], current_usage: dict[str, Any] | None
    ) -> None:
        """推送本轮 LLM 调用的 token 用量到前端（frontend.emit，ADR §3.5）。

        同时携带两套数据，覆盖前端两种语义需求：

        - 单轮值（顶层 input_tokens/output_tokens/cached_tokens/total_tokens +
          missed_tokens/cache_hit_ratio）：取自 ``current_usage``（本轮 API 返回），
          表达「当前上下文窗口占用」。前端 ChatInput 进度条据此计算占窗比。
        - 累计值（cumulative.*）：取自 ``usage``（跨轮累加的 total_* 字段），
          表达「整个管道的累计消耗」。前端统计区据此显示「缓存命中输入 /
          未命中输入 / 输出 分别加总」。missed = 总输入 - 缓存命中输入。

        本轮无用量（无 LLM 调用轮）跳过推送，避免用继承值覆盖前端显示。

        出口：``ctx.get_service("frontend")``（FrontendEmitter，由宿主 server.py
        从 frontend capability 桥接注入）。服务未注入（旧内核 / 单测环境）
        静默跳过；推送失败不阻断统计主流程。
        """
        if not current_usage:
            return
        frontend = _resolve_service(ctx, "frontend")
        if frontend is None:
            return

        payload: dict[str, Any] = {
            # 路由键（内核 frontend.emit 分支 + 前端硬门控）
            "thread_id": ctx.state.get(StateKeys.SESSION_ID, "") or "",
            "pipeline_id": ctx.state.get(StateKeys.PIPELINE_ID, "") or "",
            "message_id": ctx.state.get("message_id", "") or "",
            # 单轮值（当前上下文窗口占用）
            "input_tokens": current_usage.get("input_tokens", 0),
            "output_tokens": current_usage.get("output_tokens", 0),
            "cached_tokens": current_usage.get("cached_tokens", 0),
            "total_tokens": current_usage.get(
                "total_tokens",
                current_usage.get("input_tokens", 0) + current_usage.get("output_tokens", 0),
            ),
            "missed_tokens": usage.get("last_missed_tokens", 0),
            "cache_hit_ratio": usage.get("last_cache_hit_ratio", 0.0),
            # 累计值（整个管道累计消耗）
            "cumulative": {
                "total_input": usage.get("total_input_tokens", 0),
                "total_output": usage.get("total_output_tokens", 0),
                "total_cached": usage.get("total_cached_tokens", 0),
                "missed": usage.get("total_missed_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cache_hit_ratio": usage.get("total_cache_hit_ratio", 0.0),
            },
        }
        try:
            await frontend.emit("cost_update", payload)
        except Exception:
            # 推送失败不阻断统计主流程（frontend.emit 契约本身也是静默降级）
            logger.debug("cost_update 推送失败", exc_info=True)

    def check_cache_anomaly(self, usage: dict[str, Any], pipeline_id: str) -> None:
        """检测本轮 cache 命中率异常下降并告警（语义由测试钉死）。

        只看本轮单轮量，不混用累计值（累计未命中与末轮单轮
        input 量纲错配，累计命中率 94.9% 也会误报）：

        - 本轮未命中 = last_input - last_cached
        - 本轮命中率 = last_cached / last_input
        - last_input == 0（无 LLM 调用轮）无法判定，静默跳过

        Args:
            usage: collect 产出的统计 dict。
            pipeline_id: 当前管道 id（日志定位用）。
        """
        last_input = usage.get("last_input_tokens", 0) or 0
        last_cached = usage.get("last_cached_tokens", 0) or 0
        if last_input <= 0:
            return

        threshold = float(self._config.get("cache_hit_warn_threshold", _CACHE_HIT_WARN_THRESHOLD))
        ratio = last_cached / last_input
        if ratio < threshold:
            missed = last_input - last_cached
            _anomaly_logger.warning(
                "cache 命中率异常：本轮命中率 %.1f%%（命中 %d / %d，未命中 %d），"
                "pipeline=%s —— 本轮输入可能破坏了 cache 前缀",
                ratio * 100.0,
                last_cached,
                last_input,
                missed,
                pipeline_id,
            )

    def collect(self, ctx: Any, current_usage: dict[str, Any] | None) -> dict[str, Any]:
        """按 ``current_usage`` 与本轮 state 计算 token 统计（纯累加，不发出口）。

        含 cache 命中可观测字段（task_observability 1b）：
        missed = input - cached（未命中缓存而重新计费的输入）；
        cache_hit_ratio = cached / input（input == 0 时为 0.0，不除零）。

        本轮无用量时整块继承上一轮 ``track.llm_usage``（total_* 与 last_* 都
        保持原值）——不编造数据，也不清零（清零会让前端上下文占用显示假 0）。
        """
        prev_total = ctx.state.get("track.llm_usage", {})
        if not prev_total or not isinstance(prev_total, dict):
            prev_total = {}
        if not current_usage:
            prev_input = prev_total.get("total_input_tokens", 0)
            prev_cached = prev_total.get("total_cached_tokens", 0)
            return {
                "total_input_tokens": prev_input,
                "total_output_tokens": prev_total.get("total_output_tokens", 0),
                "total_tokens": prev_total.get("total_tokens", 0),
                "total_cached_tokens": prev_cached,
                "total_missed_tokens": max(prev_input - prev_cached, 0),
                "total_cache_hit_ratio": (prev_cached / prev_input) if prev_input > 0 else 0.0,
                "last_input_tokens": prev_total.get("last_input_tokens", 0),
                "last_output_tokens": prev_total.get("last_output_tokens", 0),
                "last_cached_tokens": prev_total.get("last_cached_tokens", 0),
                "last_missed_tokens": prev_total.get("last_missed_tokens", 0),
                "last_cache_hit_ratio": prev_total.get("last_cache_hit_ratio", 0.0),
            }

        total_input = prev_total.get("total_input_tokens", 0) + current_usage.get("input_tokens", 0)
        total_output = prev_total.get("total_output_tokens", 0) + current_usage.get("output_tokens", 0)
        total_cached = prev_total.get("total_cached_tokens", 0) + current_usage.get("cached_tokens", 0)
        last_input = current_usage.get("input_tokens", 0)
        last_cached = current_usage.get("cached_tokens", 0)

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "total_cached_tokens": total_cached,
            "total_missed_tokens": max(total_input - total_cached, 0),
            "total_cache_hit_ratio": (total_cached / total_input) if total_input > 0 else 0.0,
            "last_input_tokens": last_input,
            "last_output_tokens": current_usage.get("output_tokens", 0),
            "last_cached_tokens": last_cached,
            "last_missed_tokens": max(last_input - last_cached, 0),
            "last_cache_hit_ratio": (last_cached / last_input) if last_input > 0 else 0.0,
        }
