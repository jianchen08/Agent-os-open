"""即发即忘插件 — 不等待策略。

子管道路由后不做任何等待，直接返回。
适用于不需要子管道结果的"发射后不管"场景。
"""

from __future__ import annotations

import logging

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext

logger = logging.getLogger(__name__)


class FireAndForgetPlugin(IOutputPlugin):
    """即发即忘插件 — 不等待策略。

    子管道路由后什么都不做，直接返回。
    适用于"发射后不管"场景：父管道不关心子管道的执行结果，
    委派即结束当前管道。
    """

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "fire_and_forget"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 5

    @property
    def route_signals(self) -> list[str]:
        """本插件关注的路由信号类型列表（空=关注所有）。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行即发即忘逻辑。

        什么都不做，直接返回空 OutputResult。

        Args:
            ctx: 插件执行上下文

        Returns:
            空的 OutputResult
        """
        logger.debug("FireAndForget: no waiting for delegation result")
        return OutputResult()
