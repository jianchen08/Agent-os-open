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
            # 推送本轮单轮 token 用量到前端（输入框进度条实时显示）
            await self._try_notify_cost_update(ctx)

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

    async def _try_notify_cost_update(self, ctx: PluginContext) -> None:
        """推送本轮 LLM 调用的 token 用量到前端。

        同时携带两套数据，覆盖前端两种语义需求：

        - 单轮值（顶层 input_tokens/output_tokens/cached_tokens/total_tokens）：
          取自 state["llm_usage"]（llm_core 写入的本轮 API 返回），表达
          「当前上下文窗口占用」。前端 ChatInput 进度条据此计算占窗比。
        - 累计值（cumulative.*）：取自 state["track.llm_usage"] 的 total_* 字段
          （由 _collect_token_usage 跨轮累加），表达「整个管道的累计消耗」。
          前端统计区据此显示「缓存命中输入 / 未命中输入 / 输出 分别加总」。
          missed_tokens = 总输入 - 缓存命中输入。

        tool_execute 轮 llm_usage 为上一轮残留，跳过推送避免覆盖。

        会话标识取 session_id（state 标准字段）；thread_id 未必存在时由
        TargetedSink 按 pipeline_id 从 registry 自解析，不在此硬守卫。

        0.2 推送改走 frontend.emit capability（ADR §3.5，插件 → 内核 → 前端唯一出口），
        SDK 暂未实现该 capability；当前 cost_update 推送静默跳过，0.2 栈不再依赖
        0.1 的 src/channels/websocket/ws_interaction_notifier（task_11 P2-7）。
        待 SDK 实现 frontend.emit 后，在此用 ctx.frontend.emit(scope=...) 恢复推送，
        恢复时按上述两套数据（单轮 + cumulative）组装 payload。
        """

    def _collect_token_usage(self, ctx: PluginContext) -> dict[str, Any]:
        """收集 token 用量统计。"""
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "")
        current_usage = ctx.state.get("llm_usage", {})

        # tool_execute 轮不累加 token（llm_usage 是上一轮残留）
        if core_type != "llm_call" or not current_usage:
            prev_total = ctx.state.get("track.llm_usage", {})
            return {
                "total_input_tokens": prev_total.get("total_input_tokens", 0),
                "total_output_tokens": prev_total.get("total_output_tokens", 0),
                "total_tokens": prev_total.get("total_tokens", 0),
                "total_cached_tokens": prev_total.get("total_cached_tokens", 0),
                "last_input_tokens": 0,
                "last_output_tokens": 0,
                "last_cached_tokens": 0,
            }

        prev_total = ctx.state.get("track.llm_usage", {})
        total_input = prev_total.get("total_input_tokens", 0) + current_usage.get("input_tokens", 0)
        total_output = prev_total.get("total_output_tokens", 0) + current_usage.get("output_tokens", 0)
        total_cached = prev_total.get("total_cached_tokens", 0) + current_usage.get("cached_tokens", 0)

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "total_cached_tokens": total_cached,
            "last_input_tokens": current_usage.get("input_tokens", 0),
            "last_output_tokens": current_usage.get("output_tokens", 0),
            "last_cached_tokens": current_usage.get("cached_tokens", 0),
        }
