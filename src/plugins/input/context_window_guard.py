"""上下文窗口守卫 Input 插件。

在每次 LLM 调用前检查上下文大小，超阈值时触发压缩，
避免 context overflow 错误浪费 API 调用。

压缩策略（轻量级，不调用 LLM）：
- 保留所有 system 消息
- 保留最近的 N 条消息（N = 总消息数 * 30%，最少 6 条，最多 20 条）
- 较早的消息直接丢弃
- 在保留的消息前插入一条 system 消息说明上下文被压缩

State 命名空间：
    - messages : 压缩后替换的消息列表
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

# 压缩策略参数
_KEEP_RATIO = 0.3    # 保留最近 30% 的对话消息
_MIN_KEEP = 6        # 最少保留 6 条
_MAX_KEEP = 20       # 最多保留 20 条

_COMPRESSION_NOTICE = (
    "[系统提示] 由于对话历史过长，较早的上下文已被自动压缩移除。"
    "请基于当前剩余的上下文继续完成任务。"
)


class ContextWindowGuardPlugin(IInputPlugin):
    """上下文窗口守卫 Input 插件。

    在管道输入阶段检查 messages 的估算 token 数，
    超过 context_window 的 trigger_ratio 时触发轻量级压缩。

    优先级：5（在 context_build 的 10 之前执行）
    错误策略：SKIP（压缩失败不阻塞管线）

    Attributes:
        _config: 插件配置字典
        _trigger_ratio: 触发压缩的阈值比例（默认 0.7）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文窗口守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
                - trigger_ratio: 触发压缩的阈值比例（默认 0.7）
        """
        self._config = config or {}
        self._trigger_ratio = self._config.get("trigger_ratio", 0.7)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "context_window_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级，在 context_build 之前执行。"""
        return self._config.get("priority", 5)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """检查上下文大小并在超阈值时触发压缩。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含压缩后 messages 的插件执行结果
        """
        context_window = ctx.state.get("context_window")
        if not context_window:
            return PluginResult()

        messages = ctx.state.get("messages", [])
        if not messages:
            return PluginResult()

        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        estimated_tokens = total_chars // 2

        trigger_tokens = int(context_window * self._trigger_ratio)
        if estimated_tokens < trigger_tokens:
            return PluginResult()  # 不需要压缩

        # 触发压缩
        logger.info(
            "[%s] 上下文接近窗口限制: estimated_tokens=%d, trigger_tokens=%d, "
            "context_window=%d, trigger_ratio=%.2f",
            self.name, estimated_tokens, trigger_tokens,
            context_window, self._trigger_ratio,
        )

        compressed = self._compress_messages(messages)
        if compressed and len(compressed) < len(messages):
            logger.info(
                "[%s] 压缩完成: %d -> %d 条消息",
                self.name, len(messages), len(compressed),
            )
            return PluginResult(state_updates={"messages": compressed})

        return PluginResult()

    @staticmethod
    def _compress_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """压缩消息列表，保留 system 消息和最近 N 条消息。

        Args:
            messages: 原始消息列表

        Returns:
            压缩后的消息列表
        """
        if not messages:
            return messages

        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        if not other_msgs:
            return messages

        # 计算保留数量
        keep_count = max(
            _MIN_KEEP,
            min(_MAX_KEEP, int(len(other_msgs) * _KEEP_RATIO)),
        )

        if len(other_msgs) <= keep_count:
            return messages

        kept = other_msgs[-keep_count:]
        truncated_count = len(other_msgs) - len(kept)

        # 插入压缩说明
        notice_msg = {"role": "system", "content": _COMPRESSION_NOTICE}

        result = system_msgs + [notice_msg] + kept

        logger.debug(
            "[context_window_guard] 压缩详情: 移除 %d 条旧消息, "
            "保留 %d system + 1 通知 + %d 最近消息",
            truncated_count, len(system_msgs), len(kept),
        )

        return result
