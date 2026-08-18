"""主动式终止判断 Input 插件 — 整合预算/卡死/步数/耗时信号（task_observability 1c）。

定位：主动出口，不替换任何信号源——
- cost_control（Input，预算）：本插件读 cost_control.exceeded / usage_percent
- stuck_detector（Output，卡死）：本插件读 stuck_detected / stuck_reason
- track（Output，步数+耗时）：本插件读 track.execution_stats

每轮开头（prepare step）评估一次：
- 预算耗尽（cost_control.exceeded 或 usage_percent ≥ 100）
- 卡死（stuck_detected）
- 步数上限（iteration ≥ max_iterations，默认 50）
- 耗时上限（elapsed_total ≥ max_elapsed_s，默认 3600）

任一命中 → 写 StateKeys.SHOULD_STOP 让路由选 end（stop_check 在 post step
读取该标志终止管道），并写 termination_advisor.stop_reason 说明原因。

无论是否命中，每轮写 state["termination_advisor.status"]（前端「剩余预算」
+「收敛信号」指示器数据源）并经 frontend.emit 推 termination_status 事件。

State 命名空间：
    - termination_advisor.status : {convergence, should_stop, stop_reason,
      remaining_budget_percent, iteration, elapsed_s}
    - termination_advisor.stop_reason : 命中时的终止原因（与 status 同源）
    - should_stop : 命中时置 True（路由终止标准信号）
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)

# 默认步数上限（config max_iterations 可覆盖）
_DEFAULT_MAX_ITERATIONS = 50

# 默认耗时上限秒数（config max_elapsed_s 可覆盖）
_DEFAULT_MAX_ELAPSED_S = 3600.0

# 预算临界阈值（usage_percent ≥ 此值 → convergence = budget_critical）
_BUDGET_CRITICAL_PERCENT = 90.0


class TerminationAdvisorPlugin(IInputPlugin):
    """主动式终止判断 Input 插件。

    整合 cost_control / stuck_detector / track 三方信号，每轮开头显式评估
    是否应终止，避免反应式（已超钱/已卡住多轮后才触发）。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化终止判断插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
                - max_iterations: 步数上限（默认 50）
                - max_elapsed_s: 耗时上限秒（默认 3600）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._max_iterations = self._config.get("max_iterations", _DEFAULT_MAX_ITERATIONS)
        self._max_elapsed_s = self._config.get("max_elapsed_s", _DEFAULT_MAX_ELAPSED_S)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "termination_advisor"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 9)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行终止评估。"""
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """整合信号做主动终止判断。

        Args:
            ctx: 插件执行上下文

        Returns:
            终止评估结果字典（status + 命中时的 should_stop）
        """
        if not self._enabled:
            return {}

        state = ctx.state

        # ── 信号读取（缺失容忍：信号源插件未接入时不误判）──
        budget_exceeded = bool(state.get("cost_control.exceeded", False))
        usage_percent = state.get("cost_control.usage_percent")
        has_budget_signal = usage_percent is not None or budget_exceeded
        usage_percent = float(usage_percent) if usage_percent is not None else 0.0

        stuck = bool(state.get("stuck_detected", False))
        stuck_reason = state.get("stuck_reason", "") or ""

        stats = state.get("track.execution_stats", {}) or {}
        iteration = stats.get("iteration", state.get(StateKeys.ITERATION, 0)) or 0
        elapsed_total = float(stats.get("elapsed_total", 0.0) or 0.0)

        # ── 主动判断 ──
        reasons: list[str] = []
        if budget_exceeded or (has_budget_signal and usage_percent >= 100.0):
            reasons.append("budget exhausted (cost_control)")
        if stuck:
            reasons.append(f"stalled: {stuck_reason}" if stuck_reason else "stalled (stuck_detector)")
        if iteration >= self._max_iterations:
            reasons.append(
                f"iteration cap reached: {iteration} >= {self._max_iterations}"
            )
        if elapsed_total >= self._max_elapsed_s:
            reasons.append(
                f"elapsed cap reached: {elapsed_total:.0f}s >= {self._max_elapsed_s:.0f}s"
            )

        # ── 收敛信号 ──
        convergence = "converging"
        if stuck:
            convergence = "stalled"
        elif has_budget_signal and usage_percent >= _BUDGET_CRITICAL_PERCENT:
            convergence = "budget_critical"

        # 剩余预算（预算信号缺失 → None，前端显示「未启用」）
        remaining_budget_percent: float | None = None
        if has_budget_signal:
            remaining_budget_percent = round(max(100.0 - usage_percent, 0.0), 1)

        stop_reason = "; ".join(reasons)
        status: dict[str, Any] = {
            "convergence": convergence,
            "should_stop": bool(reasons),
            "stop_reason": stop_reason,
            "remaining_budget_percent": remaining_budget_percent,
            "iteration": iteration,
            "elapsed_s": round(elapsed_total, 1),
        }

        updates: dict[str, Any] = {"termination_advisor.status": status}
        if reasons:
            logger.warning(
                "[%s] 主动终止判断命中: %s (pipeline=%s)",
                self.name,
                stop_reason,
                state.get(StateKeys.PIPELINE_ID, ""),
            )
            updates[StateKeys.SHOULD_STOP] = True
            updates["termination_advisor.stop_reason"] = stop_reason

        # 每轮推送状态到前端（指示器数据源；frontend 缺失静默跳过）
        await self._notify_status(ctx, status)

        return updates

    async def _notify_status(self, ctx: PluginContext, status: dict[str, Any]) -> None:
        """经 frontend.emit 推送 termination_status 事件（fire-and-forget）。"""
        try:
            frontend = ctx.get_service("frontend")
        except KeyError:
            return
        if frontend is None:
            return
        payload = {
            "thread_id": ctx.state.get(StateKeys.SESSION_ID, "") or "",
            "pipeline_id": ctx.state.get(StateKeys.PIPELINE_ID, "") or "",
            **status,
        }
        try:
            await frontend.emit("termination_status", payload)
        except Exception:
            logger.debug("termination_status 推送失败", exc_info=True)
