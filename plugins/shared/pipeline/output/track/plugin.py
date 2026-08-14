"""追踪统计 Output 插件 — 仅保留 token / 耗时统计。

历史版本还承担逐动作执行记录的 YAML 持久化（ExecutionRecordStorage），
0.2 架构下该职责已由内核 pipeline_loop 下沉到 SQLite（messages / traces /
pipeline_checkpoints 表）。YAML 写入路径无新数据产出，已整体移除。

保留下来的职责：
- 收集每轮 LLM 调用的 token 用量（累计 + 单轮），写入 state["track.llm_usage"]
- 写入 state["track.total_tokens"]，供 cost_control 插件做预算管控
- 收集每轮执行耗时，写入 state["track.execution_stats"]
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)

# 缓存异常检测专用 logger：本模块以裸名 plugin 导入（__name__ == "plugin"），
# 用稳定层级名便于测试捕获与生产日志按插件过滤（tests/test_track_cache_anomaly.py 钉死）。
_anomaly_logger = logging.getLogger("plugins.output.track.plugin")

# 单轮 cache 命中率告警阈值（config cache_hit_warn_threshold 可覆盖）
_CACHE_HIT_WARN_THRESHOLD = 0.9


class TrackPlugin(IOutputPlugin):
    """追踪统计 Output 插件。"""

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化追踪统计插件。"""
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._track_tokens = self._config.get("track_token_usage", True)
        self._track_time = self._config.get("track_execution_time", True)
        self._start_time = time.monotonic()

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "track"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 15)

    @property
    def route_signals(self) -> list[str]:
        """本插件不产出路由信号。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """收集追踪统计信息。"""
        result = await self._do_work(ctx)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行追踪统计逻辑。"""
        if not self._enabled:
            return {}

        updates: dict[str, Any] = {}
        now = time.monotonic()

        # 1. Token 用量追踪
        if self._track_tokens:
            usage = self._collect_token_usage(ctx)
            updates["track.llm_usage"] = usage
            # 写入标准累计 token 值，供 cost_control 插件读取
            updates["track.total_tokens"] = usage.get("total_tokens", 0)
            # cache 命中异常检测（本轮单轮语义，详见 _check_cache_anomaly）
            self._check_cache_anomaly(usage, ctx.state.get(StateKeys.PIPELINE_ID, "") or "")
            # 推送本轮单轮 token 用量到前端（输入框进度条实时显示）
            await self._try_notify_cost_update(ctx, usage)

        # 2. 执行耗时追踪
        elapsed = now - self._start_time
        if self._track_time:
            iteration = ctx.state.get(StateKeys.ITERATION, 0)
            stats = {
                "iteration": iteration,
                "elapsed_total": round(elapsed, 3),
                "elapsed_per_iteration": round(elapsed / max(iteration, 1), 3),
                "core_type": ctx.state.get(StateKeys.CORE_TYPE, ""),
                "execution_status": ctx.state.get(StateKeys.EXECUTION_STATUS, ""),
            }
            updates["track.execution_stats"] = stats

        return updates

    async def _try_notify_cost_update(self, ctx: PluginContext, usage: dict[str, Any]) -> None:
        """推送本轮 LLM 调用的 token 用量到前端（frontend.emit，ADR §3.5）。

        同时携带两套数据，覆盖前端两种语义需求：

        - 单轮值（顶层 input_tokens/output_tokens/cached_tokens/total_tokens +
          missed_tokens/cache_hit_ratio）：取自 state["llm_usage"]（llm_core 写入
          的本轮 API 返回），表达「当前上下文窗口占用」。前端 ChatInput 进度条
          据此计算占窗比。
        - 累计值（cumulative.*）：取自 usage（_collect_token_usage 跨轮累加的
          total_* 字段），表达「整个管道的累计消耗」。前端统计区据此显示
          「缓存命中输入 / 未命中输入 / 输出 分别加总」。
          missed = 总输入 - 缓存命中输入。

        tool_execute 轮 llm_usage 为上一轮残留，跳过推送避免覆盖。

        出口：ctx.get_service("frontend")（FrontendEmitter，由 track/server.py
        从 frontend capability 桥接注入）。服务未注入（旧内核 / 单测环境）
        静默跳过；推送失败不阻断统计主流程。
        """
        # tool_execute 轮 llm_usage 是上一轮残留，跳过推送避免覆盖
        if ctx.state.get(StateKeys.CORE_TYPE, "") != "llm_call":
            return
        if not ctx.state.get("llm_usage"):
            return
        try:
            frontend = ctx.get_service("frontend")
        except KeyError:
            return
        if frontend is None:
            return

        current = ctx.state.get("llm_usage", {})
        payload: dict[str, Any] = {
            # 路由键（内核 frontend.emit 分支 + 前端硬门控）
            "thread_id": ctx.state.get(StateKeys.SESSION_ID, "") or "",
            "pipeline_id": ctx.state.get(StateKeys.PIPELINE_ID, "") or "",
            "message_id": ctx.state.get("message_id", "") or "",
            # 单轮值（当前上下文窗口占用）
            "input_tokens": current.get("input_tokens", 0),
            "output_tokens": current.get("output_tokens", 0),
            "cached_tokens": current.get("cached_tokens", 0),
            "total_tokens": current.get(
                "total_tokens",
                current.get("input_tokens", 0) + current.get("output_tokens", 0),
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

    def _check_cache_anomaly(self, usage: dict[str, Any], pipeline_id: str) -> None:
        """检测本轮 cache 命中率异常下降并告警（语义由测试钉死）。

        只看本轮单轮量，不混用累计值（历史教训：累计未命中与末轮单轮
        input 量纲错配，累计命中率 94.9% 也会误报）：

        - 本轮未命中 = last_input - last_cached
        - 本轮命中率 = last_cached / last_input
        - last_input == 0（tool_execute 轮 / 无 LLM 调用）无法判定，静默跳过

        Args:
            usage: _collect_token_usage 产出的统计 dict。
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

    def _collect_token_usage(self, ctx: PluginContext) -> dict[str, Any]:
        """收集 token 用量统计（含 cache 命中可观测字段，task_observability 1b）。

        missed = input - cached（未命中缓存而重新计费的输入）；
        cache_hit_ratio = cached / input（input == 0 时为 0.0，不除零）。
        """
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "")
        current_usage = ctx.state.get("llm_usage", {})

        # tool_execute 轮不累加 token（llm_usage 是上一轮残留）
        if core_type != "llm_call" or not current_usage:
            prev_total = ctx.state.get("track.llm_usage", {})
            prev_input = prev_total.get("total_input_tokens", 0)
            prev_cached = prev_total.get("total_cached_tokens", 0)
            return {
                "total_input_tokens": prev_input,
                "total_output_tokens": prev_total.get("total_output_tokens", 0),
                "total_tokens": prev_total.get("total_tokens", 0),
                "total_cached_tokens": prev_cached,
                "total_missed_tokens": max(prev_input - prev_cached, 0),
                "total_cache_hit_ratio": (prev_cached / prev_input) if prev_input > 0 else 0.0,
                "last_input_tokens": 0,
                "last_output_tokens": 0,
                "last_cached_tokens": 0,
                "last_missed_tokens": 0,
                "last_cache_hit_ratio": 0.0,
            }

        prev_total = ctx.state.get("track.llm_usage", {})
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
