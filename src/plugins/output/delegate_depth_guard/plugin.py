"""委派深度守卫 Output 插件。

在跨管道路由（delegate）时检查嵌套深度，防止无限递归。
深度计数存储在 state 的自定义字段 `delegate_depth` 中，
由本插件维护递增，无需修改 StateKeys 或管道基础设施。

当深度超过配置的 max_depth 时，拦截 delegate 路由信号，
改为 end 信号并记录错误。

State 命名空间：
    - delegate_depth : 当前委派深度（本插件维护）
    - max_delegate_depth : 最大允许深度（本插件读取/初始化）
    - delegation.depth_blocked : 拦截信息
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys

logger = logging.getLogger(__name__)


class DelegateDepthGuardPlugin(IOutputPlugin):
    """委派深度守卫 Output 插件。

    在检测到 delegate 路由信号时，检查当前嵌套深度是否超过限制。
    超限时拦截 delegate，改为 end 信号并记录错误信息。

    深度计数在 state["delegate_depth"] 中维护，不修改 StateKeys。
    初始值由管道首次运行时设置（默认 0）。

    优先级：3（在系统级检查之后，委派策略之前）
    错误策略：SKIP（深度超限不是错误，是保护机制）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化委派深度守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用深度守卫（默认 True）
                - max_depth: 最大允许委派深度（默认 3）
                - depth_key: state 中深度字段的键名（默认 "delegate_depth"）
                - max_depth_key: state 中最大深度字段的键名（默认 "max_delegate_depth"）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._max_depth = self._config.get("max_depth", 3)
        self._depth_key = self._config.get("depth_key", "delegate_depth")
        self._max_depth_key = self._config.get("max_depth_key", "max_delegate_depth")

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "delegate_depth_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 3)

    @property
    def route_signals(self) -> list[str]:
        """本插件关注的路由信号类型列表。"""
        return ["delegate"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行委派深度检查。

        检查当前委派深度是否超限。如果超限，产生 end 路由信号
        替代原来的 delegate 信号。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含状态更新和可能的拦截路由信号的 OutputResult
        """
        if not self._enabled:
            return OutputResult()

        # 初始化深度字段（首次运行时）
        state_updates: dict[str, Any] = {}
        if self._depth_key not in ctx.state:
            state_updates[self._depth_key] = 0
        if self._max_depth_key not in ctx.state:
            state_updates[self._max_depth_key] = self._max_depth

        # 读取当前深度和最大深度
        current_depth = ctx.state.get(self._depth_key, 0)
        max_depth = ctx.state.get(self._max_depth_key, self._max_depth)

        # 检查是否有 delegate 路由信号
        # 本插件只在有 delegate 信号时触发深度检查
        ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        routed_to = ctx.state.get(StateKeys.ROUTED_TO, None)

        # 如果已经路由了（delegate 已发生），递增深度
        if routed_to is not None:
            new_depth = current_depth + 1
            state_updates[self._depth_key] = new_depth
            logger.debug(
                "[%s] Delegate depth incremented: %d → %d (max=%d)",
                self.name,
                current_depth,
                new_depth,
                max_depth,
            )

            # 检查是否超限
            if new_depth > max_depth:
                logger.warning(
                    "[%s] Delegate depth exceeded! depth=%d, max=%d. Blocking delegation.",
                    self.name,
                    new_depth,
                    max_depth,
                )
                state_updates["delegation.depth_blocked"] = {
                    "depth": new_depth,
                    "max_depth": max_depth,
                    "reason": f"Delegate depth {new_depth} exceeds max {max_depth}",
                }
                # 产生 end 信号替代 delegate
                return OutputResult(
                    state_updates=state_updates,
                    route_signal=RouteSignal(
                        route_type="end",
                        reason=f"Delegate depth exceeded: {new_depth} > {max_depth}",
                    ),
                )

        return OutputResult(state_updates=state_updates)
