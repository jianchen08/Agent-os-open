"""等待结果插件 — 轮询策略。

子管道路由后，持续轮询 registry.get_result() 直到获取结果或超时。
适用于需要子管道结果才能继续执行的场景。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class WaitForResultPlugin(IOutputPlugin):
    """等待结果插件 — 轮询策略。

    检查 state[ROUTED_TO]，轮询 registry.get_result()，
    拿到结果后回写 state[DELEGATION_RESULT/DELEGATION_SCORE]，
    超时设 state[DELEGATION_ERROR]。

    Attributes:
        _registry: PipelineRegistry 实例，用于查询子管道结果
        _poll_interval: 轮询间隔（秒）
        _timeout: 超时时间（秒）
    """

    def __init__(
        self,
        registry: Any,
        poll_interval: float = 0.1,
        timeout: float = 300.0,
    ) -> None:
        """初始化等待结果插件。

        Args:
            registry: PipelineRegistry 实例
            poll_interval: 轮询间隔秒数，默认 0.1
            timeout: 超时秒数，默认 300.0
        """
        self._registry = registry
        self._poll_interval = poll_interval
        self._timeout = timeout

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "wait_for_result"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 5

    @property
    def route_signals(self) -> list[str]:
        """本插件关注的路由信号类型列表（空=关注所有）。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行等待结果逻辑。

        检查 state[ROUTED_TO]，轮询 registry.get_result()，
        拿到后回写 state[DELEGATION_RESULT/DELEGATION_SCORE]，
        超时设 state[DELEGATION_ERROR]。

        Args:
            ctx: 插件执行上下文

        Returns:
            OutputResult 包含状态更新
        """
        routed_to = ctx.state.get(StateKeys.ROUTED_TO)
        if not routed_to:
            return OutputResult()

        elapsed = 0.0
        while elapsed < self._timeout:
            result = self._registry.get_result(routed_to)
            if result is not None:
                # 回写结果到状态
                state_updates: dict[str, Any] = {
                    StateKeys.DELEGATION_RESULT: result,
                }
                # 如果结果中有评分信息，也回写
                if isinstance(result, dict):
                    score = result.get("score") or result.get("delegation_score")
                    if score is not None:
                        state_updates[StateKeys.DELEGATION_SCORE] = score
                logger.info(
                    "WaitForResult: got result for pipeline %s after %.1fs",
                    routed_to, elapsed,
                )
                return OutputResult(state_updates=state_updates)

            await asyncio.sleep(self._poll_interval)
            elapsed += self._poll_interval

        # 超时
        logger.warning(
            "WaitForResult: timeout after %.1fs for pipeline %s",
            self._timeout, routed_to,
        )
        return OutputResult(state_updates={
            StateKeys.DELEGATION_ERROR: f"Delegation timeout after {self._timeout}s",
        })
