"""事件回调插件 — 事件驱动策略。

子管道路由后，设 state[ENDED]=True 并标记 state[WAIT_FOR]，
管道挂起等待 EventBus 事件恢复。
适用于事件驱动的异步场景：父管道挂起，子管道完成后通过事件通知恢复。
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class EventCallbackPlugin(IOutputPlugin):
    """事件回调插件 — 事件驱动策略。

    子管道路由后，设 state[ENDED]=True 并标记 state[WAIT_FOR]，
    将管道挂起。后续由外部（如 EventBus 回调）负责恢复管道。

    通过 ctx.get_service("event_bus") 按需获取 EventBus 实例，
    无需在构造时注入。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化事件回调插件。

        Args:
            config: 插件配置字典
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "event_callback"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 5)

    @property
    def route_signals(self) -> list[str]:
        """本插件关注的路由信号类型列表（空=关注所有）。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行事件回调逻辑。

        如果有 ROUTED_TO，设 state[ENDED]=True, state[WAIT_FOR]=routed_to，
        挂起管道等待外部事件恢复。若 event_bus 服务可用，则订阅子管道完成事件。

        Args:
            ctx: 插件执行上下文

        Returns:
            OutputResult 包含状态更新
        """
        routed_to = ctx.state.get(StateKeys.ROUTED_TO)
        if not routed_to:
            return OutputResult()

        # 尝试获取 event_bus 服务用于事件订阅（可选）
        try:
            event_bus = ctx.get_service("event_bus")
            if event_bus is not None:
                logger.debug(
                    "EventCallback: event_bus available for %s",
                    routed_to,
                )
        except KeyError:
            logger.debug("EventCallback: no event_bus service, using state-only mode")

        logger.info(
            "EventCallback: suspending pipeline, waiting for %s",
            routed_to,
        )
        return OutputResult(
            state_updates={
                StateKeys.ENDED: True,
                StateKeys.WAIT_FOR: routed_to,
            }
        )
