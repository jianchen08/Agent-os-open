"""ConversationMode 输出插件 — 检测对话模式激活信号。

当 human_interaction 工具以 conversation 模式返回 conversation_mode=True 时，
本插件从 tool_results 检测激活信号，写入 conversation_mode 状态并挂起管道
（state_updates 写 suspended=true——route_signal 全链零消费，挂起经
state.suspended 表达，引擎见 suspended 即停轮）。

对话循环的已激活态由管道配置路由承载（G10 DSL，autonomous.yaml post 链）：
- conversation_mode == True 且 LLM 纯文本回复（raw_tool_calls 为空）→
  then: loop + set: {suspended: true} 挂起等待用户下一条消息
- conversation_mode == True 且 LLM 产生工具调用 → set 清除 conversation_mode，
  按工具调用路由继续原任务

对话是否结束完全由 AI 的行为决定：
- AI 回复纯文本 → wait → 等待用户下一条消息
- AI 调用工具 → 对话自然结束 → 继续原任务
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class ConversationModeDetector(IOutputPlugin):
    """对话模式激活检测输出插件。

    检测 human_interaction 工具返回的 conversation_mode 信号并激活对话模式；
    已激活态的对话循环（wait 挂起 / 清除状态）由管道配置路由承载
    （autonomous.yaml post 链 next 分支，见模块 docstring）。

    Attributes:
        _config: 插件配置字典
    """

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
        """插件执行优先级。"""
        return 5

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型列表。"""
        return ["wait"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """检测 tool_results 中是否包含对话模式激活信号。

        遍历 tool_results，查找 human_interaction 工具返回的
        conversation_mode=True 标记；命中则激活状态并挂起管道。

        Args:
            ctx: 插件执行上下文

        Returns:
            激活时包含 conversation_mode 状态更新和 wait 信号
        """
        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
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
                    "[%s] Detected conversation_mode=True in tool_results, activating conversation mode",
                    self.name,
                )
                return OutputResult(
                    state_updates={
                        StateKeys.CONVERSATION_MODE: True,
                        StateKeys.CONVERSATION_ROUND: 1,
                        # 挂起经 state.suspended 表达（route_signal 全链零消费，
                        # 引擎见 suspended 即停轮）；suspended 属 per-run 键，
                        # 下轮派发自动复位
                        "suspended": True,
                    },
                    skip_remaining=True,
                )

        return OutputResult()

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
