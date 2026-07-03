"""结果格式化 Output 插件。

负责在管道循环的输出阶段格式化 Core 的执行结果，
将工具执行结果转换为 LLM 可理解的消息格式。

M6c 阶段：从旧代码 agents/formatters/ 的工具消息格式化逻辑迁移。

State 命名空间：
    - tool.formatted_results : 本插件写入的格式化结果
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys
from tools.format_manager import FormatManager, ToolFormat, get_format_manager

logger = logging.getLogger(__name__)


class ResultFormatPlugin(IOutputPlugin):
    """结果格式化 Output 插件。

    从旧代码 agents/formatters/ 迁移而来。将工具执行结果
    转换为 LLM 可理解的消息格式，包含在 messages 列表中。

    格式化规则：
    1. 工具成功：返回工具名称和结果内容
    2. 工具失败：返回工具名称和错误信息
    3. 超时：返回超时提示

    同时负责对 messages 中 role="tool" 的消息内容进行截断，
    基于 context_window 动态计算截断阈值。

    优先级：20（副作用型，在 persist 之后）
    错误策略：SKIP（格式化失败不影响当轮结果）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    # 工具输出截断配置：基于 context_window 动态计算
    TOOL_OUTPUT_RATIO = 0.05  # 单个工具输出占上下文窗口的最大比例
    TOOL_OUTPUT_MIN_TOKENS = 2000  # 最小保留 token 数
    CHARS_PER_TOKEN = 2  # token 估算：1 token ≈ 2 字符

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化结果格式化插件。

        Args:
            config: 插件配置字典，支持以下键：
                - max_result_length: 单个结果最大长度（默认 2000）
                - include_tool_name: 是否包含工具名称（默认 True）
                - truncate_message: 截断时的提示信息（默认 "...[truncated]"）
        """
        self._config = config or {}
        self._max_length = self._config.get("max_result_length", 2000)
        self._include_tool_name = self._config.get("include_tool_name", True)
        self._truncate_msg = self._config.get("truncate_message", "...[truncated]")
        fmt_str = self._config.get("result_format", "yaml")
        try:
            self._result_format = ToolFormat(fmt_str.lower())
        except ValueError:
            self._result_format = ToolFormat.YAML
        self._format_manager: FormatManager = get_format_manager()

    @classmethod
    def _calc_max_output_chars(cls, context_window: int) -> int:
        """根据 context_window 计算单个工具输出的最大字符数。

        Args:
            context_window: LLM 上下文窗口大小（token 数）

        Returns:
            最大允许的字符数
        """
        max_tokens = max(cls.TOOL_OUTPUT_MIN_TOKENS, int(context_window * cls.TOOL_OUTPUT_RATIO))
        return max_tokens * cls.CHARS_PER_TOKEN

    @staticmethod
    def _truncate_tool_output(content: str, max_chars: int) -> str:
        """截断工具输出内容，附加截断提示信息。

        Args:
            content: 原始内容字符串
            max_chars: 最大允许字符数

        Returns:
            截断后的内容（含截断提示），或原始内容（未超限时）
        """
        if len(content) <= max_chars:
            return content
        original_len = len(content)
        truncated = content[:max_chars]
        return (
            truncated
            + f"\n\n[输出已截断，原始 {original_len} 字符"
            + f"（约 {original_len // 2} tokens），"
            + f"已截断至 {max_chars} 字符]"
        )

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "result_format"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 20)

    @property
    def route_signals(self) -> list[str]:
        """本插件不产出路由信号。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """格式化工具执行结果。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含格式化结果状态更新的输出结果
        """
        result = await self._do_work(ctx)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行结果格式化逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            格式化结果字典
        """
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")

        if core_type != "tool_execute":
            return {}  # 只格式化工具执行结果

        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        if not tool_results:
            return {}

        formatted = []
        for tr in tool_results:
            tool_name = tr.get("name", "unknown")
            success = tr.get("success", True)
            result_content = tr.get("result", "")
            error = tr.get("error", "")

            if success:
                content = self._format_success(tool_name, result_content)
            else:
                content = self._format_error(tool_name, error)

            formatted.append(
                {
                    "role": "tool",
                    "name": tool_name,
                    "content": content,
                }
            )

        # 对 messages 中 role="tool" 的消息内容进行截断
        self._truncate_tool_messages(ctx)

        return {"tool.formatted_results": formatted}

    def _truncate_tool_messages(self, ctx: PluginContext) -> None:
        """截断 messages 中 role="tool" 的消息内容。

        从 ctx.state 读取 context_window，按 5% 比例计算截断阈值。
        如果 context_window 未设置，跳过截断并记录警告日志。

        Args:
            ctx: 插件执行上下文
        """
        context_window = ctx.state.get("context_window")
        if not context_window:
            logger.warning(
                "[%s] context_window 未设置，工具输出截断不可用。请检查 LLMCore 配置是否包含 context_window。",
                self.name,
            )
            return

        max_output_chars = self._calc_max_output_chars(context_window)
        messages = ctx.state.get("messages")
        if not messages or not isinstance(messages, list):
            return

        truncated_count = 0
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            truncated = self._truncate_tool_output(content, max_output_chars)
            if truncated is not content:  # 引用不同说明发生了截断
                msg["content"] = truncated
                truncated_count += 1

        if truncated_count:
            logger.debug(
                "[%s] 截断了 %d 条 tool 消息（阈值 %d 字符）",
                self.name,
                truncated_count,
                max_output_chars,
            )

    def _format_success(self, tool_name: str, result: Any) -> str:
        """格式化工具成功结果。"""
        if isinstance(result, str):
            result_str = result
        else:
            result_str = self._format_manager.serialize(result, fmt=self._result_format)

        if len(result_str) > self._max_length:
            result_str = result_str[: self._max_length] + self._truncate_msg

        if self._include_tool_name:
            return f"[{tool_name}] {result_str}"
        return result_str

    def _format_error(self, tool_name: str, error: str) -> str:
        """格式化工具错误结果。

        Args:
            tool_name: 工具名称
            error: 错误信息

        Returns:
            格式化后的错误字符串
        """
        return f"[{tool_name}] Error: {error}"
