"""PendingTools 输出插件 — 检测工具调用并发出 next_tool 路由信号。

当 LLM 返回的结果中包含 tool_calls 时，该插件产生
RouteSignal("next_tool", target="tool_execute") 信号，
驱动管道引擎进入工具执行循环。

M3 阶段：仅负责信号生成，不执行工具本身（工具执行由 ToolCore 负责）。
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import RouteSignal, StateKeys

logger = logging.getLogger(__name__)


class PendingToolsOutput(IOutputPlugin):
    """待处理工具调用输出插件。

    在管道输出阶段检测 state["raw_tool_calls"]，
    若存在未处理的工具调用，发出 next_tool 路由信号。

    Attributes:
        _priority: 插件优先级，默认 6（较高优先级，尽早检测工具调用）
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 PendingTools 输出插件。

        Args:
            config: 插件配置字典（当前未使用，预留扩展）
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "pending_tools"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return 6

    @property
    def route_signals(self) -> list[str]:
        """本插件关注的路由信号类型列表。

        M1 阶段约定：空列表表示关注所有 core_type。
        """
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """检测待处理的工具调用并发出路由信号。

        从 state["raw_tool_calls"] 读取 LLM 返回的工具调用列表，
        若非空则发出 next_tool 路由信号，驱动管道进入工具执行阶段。

        Args:
            ctx: 插件执行上下文

        Returns:
            输出结果，包含路由信号（有工具调用时）或空结果（无工具调用时）
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        pipeline_id = ctx.state.get("pipeline_id", "?")
        iteration = ctx.state.get("iteration", -1)
        raw_result = ctx.state.get(StateKeys.RAW_RESULT, "")
        raw_result_preview = str(raw_result)[:200] if raw_result else "None"

        if not tool_calls:
            has_key = StateKeys.RAW_TOOL_CALLS in ctx.state
            logger.debug(
                "[%s] pipeline=%s iter=%d NO tool calls | key_exists=%s raw_result=%s",
                self.name,
                pipeline_id,
                iteration,
                has_key,
                raw_result_preview,
            )
            return OutputResult()

        tool_names = [tc.get("name", "unknown") for tc in tool_calls]
        logger.debug(
            "[%s] pipeline=%s iter=%d Detected %d pending tool call(s): %s",
            self.name,
            pipeline_id,
            iteration,
            len(tool_calls),
            tool_names,
        )

        return OutputResult(
            route_signal=RouteSignal(
                route_type="next_tool",
                target="tool_execute",
                reason=f"{len(tool_calls)} tool call(s) pending: {tool_names}",
            ),
        )
