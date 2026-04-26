"""上下文窗口守卫 Input 插件。

在每次 LLM 调用前检查上下文大小，超阈值时通过记忆系统的
ContextCompressor 进行分层递进压缩（L0→L1→L2），
将旧消息压缩为摘要，保留最近消息，避免 context overflow。

BUG-FIX-fix_20260426_context_guard:
问题根因: 1) context_window_guard 未被加入 input_routes，永远不执行
          2) 自写的 _compress_messages 只是简单截断丢弃旧消息，信息丢失严重
修复方案: 使用记忆系统 ContextCompressor 分层压缩，旧消息压缩为结构化摘要
影响范围: 所有 LLM 调用的上下文管理

State 命名空间:
    - messages : 压缩后替换的消息列表
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

_COMPRESSION_NOTICE = (
    "[系统提示] 由于对话历史过长，较早的上下文已被记忆系统分层压缩。"
    "压缩摘要包含在上方消息中，请基于压缩摘要和当前剩余上下文继续完成任务。"
)


class ContextWindowGuardPlugin(IInputPlugin):
    """上下文窗口守卫 Input 插件。

    在管道输入阶段检查 messages 的估算 token 数，
    超过 context_window 的 trigger_ratio 时触发记忆系统压缩。

    压缩流程：
    1. 将旧消息分离出来
    2. 通过 ContextCompressor 进行 L0→L1→L2 分层压缩
    3. 压缩摘要作为 system 消息保留
    4. 保留最近的 N 条消息

    优先级：5（在 prompt_build 的 10 之前执行）
    错误策略：SKIP（压缩失败不阻塞管线）

    Attributes:
        _config: 插件配置字典
        _trigger_ratio: 触发压缩的阈值比例（默认 0.5）
        _recent_keep_count: 保留最近消息条数（默认 20）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文窗口守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
                - trigger_ratio: 触发压缩的阈值比例（默认 0.5）
                - recent_keep_count: 保留最近消息条数（默认 20）
        """
        self._config = config or {}
        self._trigger_ratio = self._config.get("trigger_ratio", 0.5)
        self._recent_keep_count = self._config.get("recent_keep_count", 20)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "context_window_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级，在 prompt_build 之前执行。"""
        return self._config.get("priority", 5)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """检查上下文大小并在超阈值时触发记忆系统压缩。

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
            return PluginResult()

        logger.info(
            "[%s] 上下文接近窗口限制: estimated_tokens=%d, trigger_tokens=%d, "
            "context_window=%d, trigger_ratio=%.2f, msg_count=%d",
            self.name, estimated_tokens, trigger_tokens,
            context_window, self._trigger_ratio, len(messages),
        )

        compressed = await self._compress_with_memory_system(ctx, messages, context_window)
        if compressed and len(compressed) < len(messages):
            logger.info(
                "[%s] 记忆系统压缩完成: %d -> %d 条消息",
                self.name, len(messages), len(compressed),
            )
            return PluginResult(state_updates={"messages": compressed})

        return PluginResult()

    async def _compress_with_memory_system(
        self,
        ctx: PluginContext,
        messages: list[dict[str, Any]],
        context_window: int,
    ) -> list[dict[str, Any]] | None:
        """使用记忆系统 ContextCompressor 进行分层压缩。

        将旧消息通过 L0→L1→L2 递进压缩为结构化摘要，
        保留 system 消息和最近 N 条消息。

        Args:
            ctx: 插件执行上下文
            messages: 原始消息列表
            context_window: 模型上下文窗口大小

        Returns:
            压缩后的消息列表，压缩失败返回 None
        """
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        if not other_msgs:
            return None

        keep_count = min(self._recent_keep_count, len(other_msgs))
        if len(other_msgs) <= keep_count:
            return None

        old_msgs = other_msgs[:-keep_count]
        recent_msgs = other_msgs[-keep_count:]

        summary = await self._build_compression_summary(ctx, old_msgs, context_window)
        if not summary:
            return None

        notice_msg = {"role": "system", "content": _COMPRESSION_NOTICE}
        summary_msg = {"role": "system", "content": f"## 历史对话压缩摘要\n\n{summary}"}

        result = system_msgs + [notice_msg, summary_msg] + recent_msgs

        logger.info(
            "[%s] 压缩详情: 压缩 %d 条旧消息为摘要, "
            "保留 %d system + 2 摘要 + %d 最近消息",
            self.name, len(old_msgs), len(system_msgs), len(recent_msgs),
        )

        return result

    async def _build_compression_summary(
        self,
        ctx: PluginContext,
        old_msgs: list[dict[str, Any]],
        context_window: int,
    ) -> str | None:
        """通过记忆系统 ContextCompressor 压缩旧消息为摘要。

        Args:
            ctx: 插件执行上下文
            old_msgs: 需要压缩的旧消息列表
            context_window: 模型上下文窗口大小

        Returns:
            压缩摘要文本，失败返回 None
        """
        try:
            from memory.context_compressor import CompressionConfig, ContextCompressor

            llm_call_fn = self._get_llm_call_fn(ctx)
            if not llm_call_fn:
                logger.warning("[%s] 无法获取 LLM 调用函数，跳过记忆系统压缩", self.name)
                return None

            config = CompressionConfig(context_window=context_window)
            compressor = ContextCompressor(
                llm_call_fn=llm_call_fn,
                config=config,
            )

            summary = await compressor.compress_to_l1(old_msgs)

            if summary:
                l1_tokens = len(summary) // 2
                l1_budget = config.get_budgets().get("L1", 1000)
                if l1_tokens > l1_budget:
                    l2_summary = await compressor.compress_to_l2(summary)
                    if l2_summary:
                        logger.info(
                            "[%s] L1 摘要超预算，进一步压缩到 L2: %d -> %d 字符",
                            self.name, len(summary), len(l2_summary),
                        )
                        return l2_summary

                logger.info(
                    "[%s] L1 压缩完成: %d 条消息 -> %d 字符摘要",
                    self.name, len(old_msgs), len(summary),
                )
                return summary

            return None

        except Exception as exc:
            logger.warning("[%s] 记忆系统压缩失败: %s", self.name, exc)
            return None

    def _get_llm_call_fn(self, ctx: PluginContext):
        """从上下文中获取 LLM 调用函数。

        优先从 services 中获取已注册的 LLMCore 实例，
        其次通过 LiteLLMAdapter 构建轻量调用函数。

        Args:
            ctx: 插件执行上下文

        Returns:
            异步 LLM 调用函数，或 None
        """
        llm_core = ctx.get_service("llm_core")
        if llm_core and hasattr(llm_core, "_adapter"):
            async def _call_via_core(prompt: str) -> str:
                from llm.adapter import LLMResponse
                response: LLMResponse = await llm_core._adapter.call(
                    messages=[{"role": "user", "content": prompt}],
                    stream=False,
                )
                return response.content or ""
            return _call_via_core

        try:
            from llm.adapter import LiteLLMAdapter

            model_name = ctx.state.get("model_name", "glm-5.1")
            api_base = ctx.state.get("api_base")
            api_key = ctx.state.get("api_key")

            if api_key:
                adapter = LiteLLMAdapter(
                    model=model_name,
                    api_base=api_base,
                    api_key=api_key,
                )

                async def _call_via_adapter(prompt: str) -> str:
                    from llm.adapter import LLMResponse
                    response: LLMResponse = await adapter.call(
                        messages=[{"role": "user", "content": prompt}],
                        stream=False,
                    )
                    return response.content or ""

                return _call_via_adapter
        except Exception as exc:
            logger.debug("[%s] LiteLLMAdapter 创建失败: %s", self.name, exc)

        return None
