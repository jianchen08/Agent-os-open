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

        split_idx = len(other_msgs) - keep_count
        old_msgs, recent_msgs = self._split_preserving_tool_pairs(
            other_msgs, split_idx,
        )

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

    @staticmethod
    def _split_preserving_tool_pairs(
        messages: list[dict[str, Any]],
        split_idx: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按 split_idx 分割消息列表，保证 tool call/result 配对完整。

        如果 recent 段包含 tool result 但对应的 assistant(tool_calls)
        落在了 old 段，则将那个 assistant 消息也移入 recent 段。

        Args:
            messages: 非系统消息列表
            split_idx: 初步分割位置

        Returns:
            (old_msgs, recent_msgs) 保证工具调用配对完整
        """
        old_msgs = list(messages[:split_idx])
        recent_msgs = list(messages[split_idx:])

        # 收集 recent 段中所有 tool result 的 tool_call_id
        recent_tool_ids: set[str] = set()
        for msg in recent_msgs:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    recent_tool_ids.add(tc_id)

        if not recent_tool_ids:
            return old_msgs, recent_msgs

        # 从 old 段末尾向前扫描，找到包含这些 tool_call_id 的 assistant 消息
        # 将它们（以及可能连续的 tool result）移入 recent 段
        move_count = 0
        for i in range(len(old_msgs) - 1, -1, -1):
            msg = old_msgs[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                call_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                if call_ids & recent_tool_ids:
                    move_count = len(old_msgs) - i
                    break

        if move_count > 0:
            migrated = old_msgs[-move_count:]
            old_msgs = old_msgs[:-move_count]
            recent_msgs = migrated + recent_msgs

        return old_msgs, recent_msgs

    async def _build_compression_summary(
        self,
        ctx: PluginContext,
        old_msgs: list[dict[str, Any]],
        context_window: int,
    ) -> str | None:
        """通过记忆系统 ContextCompressor 一次性压缩旧消息，并保存到 ChunkService。

        Args:
            ctx: 插件执行上下文
            old_msgs: 需要压缩的旧消息列表
            context_window: 模型上下文窗口大小

        Returns:
            压缩摘要文本（L1，或 L2 如果 L1 超预算），失败返回 None
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

            result = await compressor.compress_all(
                old_msgs,
                previous_l1=await self._load_previous_l1(ctx),
            )
            l1_summary = result.get("l1", "")
            l2_summary = result.get("l2", "")
            keywords = result.get("keywords", [])

            if not l1_summary:
                return None

            # 保存到 ChunkService
            await self._save_chunks(ctx, old_msgs, l1_summary, l2_summary, keywords)

            l1_tokens = len(l1_summary) // 2
            l1_budget = config.get_budgets().get("L1", 1000)
            if l1_tokens > l1_budget and l2_summary:
                logger.info(
                    "[%s] L1 超预算，使用 L2: %d -> %d 字符",
                    self.name, len(l1_summary), len(l2_summary),
                )
                return l2_summary

            logger.info(
                "[%s] 一次性压缩完成: %d 条消息 -> L1≈%d字符 L2≈%d字符",
                self.name, len(old_msgs), len(l1_summary), len(l2_summary),
            )
            return l1_summary

        except Exception as exc:
            logger.warning("[%s] 记忆系统压缩失败: %s", self.name, exc)
            return None

    async def _load_previous_l1(self, ctx: PluginContext) -> str:
        """从 ChunkService 加载该会话所有历史压缩的 L1 内容作为背景。

        Args:
            ctx: 插件执行上下文

        Returns:
            所有历史 L1 内容拼接，无则返回空字符串
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            return ""

        session_id = ctx.state.get("context.session_id", "")
        if not session_id:
            return ""

        try:
            chunks = await chunk_service.find_by_session(session_id, "L1")
            if chunks:
                return "\n\n---\n\n".join(chunk.content for chunk in chunks if chunk.content)
        except Exception:
            pass

        return ""

    async def _save_chunks(
        self,
        ctx: PluginContext,
        old_msgs: list[dict[str, Any]],
        l1_summary: str,
        l2_summary: str,
        keywords: list[str],
    ) -> None:
        """将压缩结果保存到 ChunkService。

        Args:
            ctx: 插件执行上下文
            old_msgs: 被压缩的旧消息列表
            l1_summary: L1 摘要 JSON
            l2_summary: L2 摘要 JSON
            keywords: 关键词列表
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            logger.debug("[%s] 无 chunk_service，跳过持久化", self.name)
            return

        from memory.types import ChunkData
        from pipeline.types import StateKeys

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return

        msg_count = len(old_msgs)

        try:
            l1_chunk = ChunkData(
                pipeline_run_id=pipeline_run_id,
                layer="L1",
                content=l1_summary,
                token_count=len(l1_summary) // 2,
                message_count=msg_count,
                sequence_start=1,
                sequence_end=msg_count,
                keywords=keywords,
            )
            await chunk_service.save(l1_chunk)

            if l2_summary:
                l2_chunk = ChunkData(
                    pipeline_run_id=pipeline_run_id,
                    layer="L2",
                    content=l2_summary,
                    token_count=len(l2_summary) // 2,
                    message_count=msg_count,
                    sequence_start=1,
                    sequence_end=msg_count,
                    keywords=keywords,
                )
                await chunk_service.save(l2_chunk)

            logger.info(
                "[%s] 压缩块已保存 | L1≈%d字符 L2≈%d字符 keywords=%d",
                self.name, len(l1_summary), len(l2_summary), len(keywords),
            )
        except Exception as exc:
            logger.warning("[%s] 保存压缩块失败: %s", self.name, exc)

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
        if llm_core and hasattr(llm_core, "_adapter") and hasattr(llm_core, "_get_model_string"):
            async def _call_via_core(prompt: str) -> str:
                kwargs: dict[str, Any] = {
                    "model": llm_core._get_model_string(),
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                }
                if getattr(llm_core, "_api_base", None):
                    kwargs["api_base"] = llm_core._api_base
                if getattr(llm_core, "_api_key", None):
                    kwargs["api_key"] = llm_core._api_key
                response = await llm_core._adapter.completion(**kwargs)
                return response.text or ""
            return _call_via_core

        try:
            from llm.adapter import LiteLLMAdapter

            model_name = ctx.state.get("model_name", "glm-5.1")
            api_base = ctx.state.get("api_base")
            api_key = ctx.state.get("api_key")

            if api_key:
                adapter = LiteLLMAdapter()

                async def _call_via_adapter(prompt: str) -> str:
                    response = await adapter.completion(
                        model=model_name,
                        messages=[{"role": "user", "content": prompt}],
                        stream=False,
                        api_base=api_base,
                        api_key=api_key,
                    )
                    return response.text or ""

                return _call_via_adapter
        except Exception as exc:
            logger.debug("[%s] LiteLLMAdapter 创建失败: %s", self.name, exc)

        return None
