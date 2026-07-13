"""成本控制 Input 插件 — Token 预算检查与超限保护。

在每轮迭代开始时检查累计 Token 用量是否超过预算，
超预算则终止管道执行，防止成本失控。

Token 预算来源（按优先级）：
1. 任务 metadata 中的 token_budget 字段
2. 管道 state 中的 cost_control.budget 配置
3. 插件配置中的 default_budget（默认 100000）

TrackPlugin 在 Output 阶段将累计 token 写入 state["track.total_tokens"]，
本插件在 Input 阶段读取该值与预算比较。

State 命名空间：
    - cost_control.budget : 本插件写入的预算值
    - cost_control.usage_percent : 本插件写入的用量百分比
    - cost_control.exceeded : 本插件写入的超限标记
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)

# 默认 Token 预算
_DEFAULT_BUDGET = 100000

# 警告阈值（用量百分比）
_WARNING_THRESHOLD = 0.80

# 临界阈值（用量百分比）
_CRITICAL_THRESHOLD = 0.90


class CostControlPlugin(IInputPlugin):
    """成本控制 Input 插件。

    在每轮迭代前检查累计 Token 用量是否超预算，
    超预算则设置 SHOULD_STOP=True 终止管道。

    预算配置优先级：
    1. 任务 metadata.token_budget
    2. state["cost_control.budget"]（管道配置）
    3. 插件 config default_budget

    优先级：8（在 pause_guard 之后，context_build 之前）
    错误策略：SKIP（检查失败不阻塞管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化成本控制插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用成本控制（默认 True）
                - default_budget: 默认 Token 预算（默认 100000）
                - warning_threshold: 警告阈值（默认 0.80）
                - critical_threshold: 临界阈值（默认 0.90）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._default_budget = self._config.get("default_budget", _DEFAULT_BUDGET)
        self._warning_threshold = self._config.get("warning_threshold", _WARNING_THRESHOLD)
        self._critical_threshold = self._config.get("critical_threshold", _CRITICAL_THRESHOLD)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "cost_control"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 8)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行成本检查。

        检查累计 Token 用量是否超预算，超预算则终止管道。
        异常时设置保守的默认预算值，确保不会完全绕过成本控制。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含成本控制状态的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行成本控制逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            成本控制结果字典
        """
        if not self._enabled:
            return {"cost_control.budget": self._default_budget, "cost_control.exceeded": False}

        # 1. 确定预算
        budget = self._resolve_budget(ctx)

        # 2. 获取累计 token 用量（TrackPlugin 在 Output 阶段写入）
        total_tokens = ctx.state.get("track.total_tokens", 0)

        # 3. 计算用量百分比
        usage_percent = total_tokens / budget if budget > 0 else 0.0

        # 4. 检查是否超限
        exceeded = total_tokens >= budget
        updates: dict[str, Any] = {
            "cost_control.budget": budget,
            "cost_control.usage_percent": round(usage_percent * 100, 1),
            "cost_control.total_tokens": total_tokens,
            "cost_control.exceeded": exceeded,
        }

        if exceeded:
            logger.warning(
                "[%s] Token budget exceeded! used=%d, budget=%d (%.1f%%)",
                self.name,
                total_tokens,
                budget,
                usage_percent * 100,
            )
            updates[StateKeys.SHOULD_STOP] = True
            updates["cost_control.stop_reason"] = f"Token budget exceeded: {total_tokens}/{budget}"
        elif usage_percent >= self._critical_threshold:
            logger.warning(
                "[%s] Token usage critical: %d/%d (%.1f%%)",
                self.name,
                total_tokens,
                budget,
                usage_percent * 100,
            )
        elif usage_percent >= self._warning_threshold:
            logger.info(
                "[%s] Token usage warning: %d/%d (%.1f%%)",
                self.name,
                total_tokens,
                budget,
                usage_percent * 100,
            )

        return updates

    def _resolve_budget(self, ctx: PluginContext) -> int:
        """解析 Token 预算值。

        按优先级从多个来源获取预算：
        1. 任务 metadata 中的 token_budget
        2. state 中已有的 cost_control.budget
        3. 插件默认配置

        Args:
            ctx: 插件执行上下文

        Returns:
            Token 预算值
        """
        # 来源 1：任务 metadata
        task_id = ctx.state.get(StateKeys.TASK_ID, "")
        if task_id:
            try:
                task_service = ctx.get_service("task_service")
                task = task_service.get_task(task_id)
                if task and task.metadata.get("token_budget"):
                    return int(task.metadata["token_budget"])
            except (KeyError, ValueError, TypeError):
                pass

        # 来源 2：state 中已有的配置
        state_budget = ctx.state.get("cost_control.budget")
        if state_budget and isinstance(state_budget, (int, float)):
            return int(state_budget)

        # 来源 3：默认配置
        return self._default_budget
