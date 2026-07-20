"""ConversationMode 输出插件 — 检测对话模式信号并产生 wait 路由。

当 human_interaction 工具以 conversation 模式返回 conversation_mode=True 时，
激活管道的对话循环模式：
1. 首次检测：从 tool_results 中读取 conversation_mode=True，激活状态，产生 wait
2. 对话循环中：conversation_mode 已激活且 LLM 纯文本回复（无工具调用），产生 wait
3. 对话结束：LLM 产生工具调用（开始继续执行任务），清除 conversation_mode

对话是否结束完全由 AI 的行为决定：
- AI 回复纯文本 → wait → 等待用户下一条消息
- AI 调用工具 → next_tool 路由优先级更高 → 对话自然结束 → 继续原任务
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys

logger = logging.getLogger(__name__)


class ConversationModeDetector(IOutputPlugin):
    """对话模式检测输出插件。

    检测 human_interaction 工具返回的 conversation_mode 信号，
    激活管道对话循环，或在对话中产生 wait 信号挂起管道等待用户输入。

    对话模式下 AI 纯文本回复（无工具调用）时挂起管道，
    AI 产生工具调用时清除对话模式，让 next_tool 路由接管，继续原任务。

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化对话模式检测插件。

        Args:
            config: 插件配置字典（当前未使用，预留扩展）
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "conversation_mode"

    @property
    def priority(self) -> int:
        """插件执行优先级，在 pending_tools(6) 之前执行。"""
        return 5

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型列表。"""
        return ["wait"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """检测对话模式状态并决定是否挂起管道。

        三个检测路径：
        1. conversation_mode 已激活 + 无工具调用 → 继续对话，产生 wait
        2. conversation_mode 已激活 + 有工具调用 → 对话结束，清除状态
        3. conversation_mode 未激活 → 检查 tool_results 是否包含激活信号

        Args:
            ctx: 插件执行上下文

        Returns:
            输出结果，包含路由信号（对话模式激活时）或空结果
        """
        state = ctx.state
        iteration = state.get("iteration", -1)

        if state.get(StateKeys.CONVERSATION_MODE):
            return self._handle_active_conversation(state, iteration)

        return self._detect_conversation_activation(state, iteration)

    def _handle_active_conversation(self, state: dict, iteration: int) -> OutputResult:
        """处理已激活的对话模式。

        对话模式下根据 AI 输出决定是否继续对话：
        - AI 纯文本回复（无工具调用）→ 继续对话，wait
        - AI 产生工具调用 → 对话自然结束，清除状态

        Args:
            state: 管道状态字典
            iteration: 当前迭代次数

        Returns:
            输出结果
        """
        raw_tool_calls = state.get(StateKeys.RAW_TOOL_CALLS, [])

        if raw_tool_calls:
            logger.info(
                "[%s][iter=%d] AI produced tool calls during conversation, clearing conversation_mode",
                self.name,
                iteration,
            )
            return OutputResult(
                state_updates={
                    StateKeys.CONVERSATION_MODE: False,
                    StateKeys.CONVERSATION_ROUND: 0,
                },
            )

        round_num = state.get(StateKeys.CONVERSATION_ROUND, 0) + 1
        logger.info(
            "[%s][iter=%d] Conversation round %d, suspending pipeline (wait)",
            self.name,
            iteration,
            round_num,
        )
        return OutputResult(
            state_updates={StateKeys.CONVERSATION_ROUND: round_num},
            route_signal=RouteSignal(
                route_type="wait",
                reason=f"conversation_mode: round {round_num}",
            ),
            skip_remaining=True,
        )

    def _extract_conversation_flag(self, data: dict[str, Any]) -> bool:
        """从 tool_result.output 中提取 conversation_mode 标志。

        tool_core 的 _normalize_tool_result 对 ToolExecutionResult 调用 to_dict()，
        返回完整结构 {"status": ..., "success": ..., "output": {...}, "data": {...}}，
        conversation_mode 在 output 或 data 子字段内，而非顶层。

        Args:
            data: tool_result["data"] 的值

        Returns:
            是否检测到 conversation_mode=True
        """
        if data.get("conversation_mode"):
            return True
        for key in ("output", "data"):
            inner = data.get(key)
            if isinstance(inner, dict) and inner.get("conversation_mode"):
                return True
        return False

    def _detect_conversation_activation(
        self,
        state: dict,
        iteration: int,
    ) -> OutputResult:
        """检测 tool_results 中是否包含对话模式激活信号。

        遍历 tool_results，查找 human_interaction 工具返回的
        conversation_mode=True 标记。

        Args:
            state: 管道状态字典
            iteration: 当前迭代次数

        Returns:
            输出结果，激活时包含 conversation_mode 状态更新和 wait 信号
        """
        tool_results = state.get(StateKeys.TOOL_RESULTS, [])
        if not tool_results:
            return OutputResult()

        for result in tool_results:
            if not isinstance(result, dict):
                continue
            if result.get("success") is not True:
                continue

            data = result.get("data", {})
            if not isinstance(data, dict):
                continue

            if self._extract_conversation_flag(data):
                logger.info(
                    "[%s][iter=%d] Detected conversation_mode=True in tool_results, activating conversation mode",
                    self.name,
                    iteration,
                )
                return OutputResult(
                    state_updates={
                        StateKeys.CONVERSATION_MODE: True,
                        StateKeys.CONVERSATION_ROUND: 1,
                    },
                    route_signal=RouteSignal(
                        route_type="wait",
                        reason="conversation_mode: user arrived, entering conversation",
                    ),
                    skip_remaining=True,
                )

        return OutputResult()
