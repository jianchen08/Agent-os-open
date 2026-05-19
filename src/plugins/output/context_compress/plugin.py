"""上下文压缩 Output 插件。

包装 ContextCompressor → IOutputPlugin，在管道输出阶段
检查 token 用量并按需触发上下文压缩。

压缩后的数据保存到 ChunkService（L1/L2 块），
prompt_build 通过 _load_compressed_layer() 读取并拼入系统提示词。

State 命名空间：
    - memory.compressed : 本插件的压缩结果
"""

from __future__ import annotations

import logging
from typing import Any

from memory.memory_context_service import MemoryContextService
from memory.context_compressor import ContextCompressor, CompressionConfig
from memory.types import ChunkData
from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ContextCompressPlugin(IOutputPlugin):
    """上下文压缩 Output 插件。

    检查当前会话的 token 用量，如果超过阈值则触发
    递进压缩（L0→L1→L2），释放上下文空间。

    压缩流程：
    1. 通过 ctx.get_service("context_service") 获取共享 MemoryContextService
    2. 将管道 state 中的完整消息列表喂入 context_service
    3. 当 token 超阈值时，用 ContextCompressor 压缩旧消息
    4. 压缩结果保存到 ChunkService（供 prompt_build 读取）
    5. 旧消息从管道 state["messages"] 中移除，替换为压缩引用

    优先级：24（在记忆写入之前，确保压缩后空间）
    错误策略：SKIP（压缩失败不影响管道继续）

    Attributes:
        _config: 插件配置
        _context_compressor: 上下文压缩器（懒初始化）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        context_service: MemoryContextService | None = None,
        chunk_service: Any = None,
        context_compressor: ContextCompressor | None = None,
    ) -> None:
        if isinstance(config, MemoryContextService):
            logger.warning(
                "[%s] __init__ 收到 MemoryContextService 作为 config 参数，"
                "可能是旧调用方式，已自动修正",
                self.__class__.__name__,
            )
            context_service = config
            config = None

        self._config = config if isinstance(config, dict) else {}
        self._context_compressor = context_compressor

        logger.debug(
            "[%s] 初始化完成, config keys=%s",
            self.name,
            list(self._config.keys()),
        )

    @property
    def name(self) -> str:
        return "context_compress"

    @property
    def priority(self) -> int:
        return 24

    @property
    def route_signals(self) -> list[str]:
        return []

    def _get_context_service(self, ctx: PluginContext) -> MemoryContextService | None:
        """获取共享 MemoryContextService，优先从服务注册表取。"""
        try:
            return ctx.get_service("context_service")
        except (KeyError, AttributeError):
            pass
        # fallback: 自建（不推荐，但保证兼容）
        return MemoryContextService(
            config={
                "context_window": self._config.get("context_window", 128000),
                "compress_trigger_ratio": self._config.get("compress_trigger_ratio", 0.5),
            },
        )

    def _get_chunk_service(self, ctx: PluginContext) -> Any | None:
        """获取 ChunkService。"""
        try:
            return ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            return None

    async def _load_previous_l1(self, ctx: PluginContext) -> str:
        """从 ChunkService 加载该会话所有历史压缩的 L1 内容作为背景。"""
        chunk_service = self._get_chunk_service(ctx)
        if not chunk_service:
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

    async def _get_compressor(self, ctx: PluginContext) -> ContextCompressor | None:
        """获取或懒建 ContextCompressor。"""
        if self._context_compressor is not None:
            return self._context_compressor

        try:
            llm_core = ctx.get_service("llm_core")
        except (KeyError, AttributeError):
            logger.debug("[%s] No llm_core service, cannot create compressor", self.name)
            return None

        if not llm_core or not hasattr(llm_core, "_adapter"):
            return None

        context_window = self._resolve_context_window(ctx)
        compress_config = CompressionConfig(
            context_window=context_window,
            compress_trigger_ratio=self._config.get("compress_trigger_ratio", 0.5),
        )

        async def _llm_call_fn(prompt: str) -> str:
            from llm.adapter import LLMResponse

            kwargs: dict[str, Any] = {
                "model": llm_core._get_model_string(),
                "messages": [{"role": "user", "content": prompt}],
            }
            if llm_core._api_base:
                kwargs["api_base"] = llm_core._api_base
            if llm_core._api_key:
                kwargs["api_key"] = llm_core._api_key

            response: LLMResponse = await llm_core._adapter.completion(**kwargs)
            return response.text or ""

        self._context_compressor = ContextCompressor(
            llm_call_fn=_llm_call_fn,
            config=compress_config,
        )
        return self._context_compressor

    def _resolve_context_window(self, ctx: PluginContext) -> int:
        """从管道 state 读取模型实际的 context_window。"""
        state_cw = ctx.state.get("context_window")
        if state_cw and isinstance(state_cw, int) and state_cw > 0:
            return state_cw
        return self._config.get("context_window", 128000)

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """粗略估算消息列表的 token 数。"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if content:
                total += len(str(content)) // 2
            # tool_calls 也计入
            for tc in msg.get("tool_calls", []):
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    total += len(str(args)) // 2
        return total

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """检查并触发上下文压缩。"""
        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return OutputResult(
                state_updates={"memory.compressed": {"triggered": False, "reason": "no pipeline_run_id"}},
            )

        try:
            context_service = self._get_context_service(ctx)
            if not isinstance(context_service, MemoryContextService):
                return OutputResult(
                    state_updates={
                        "memory.compressed": {
                            "triggered": False,
                            "reason": f"invalid_context_service: {type(context_service).__name__}",
                        },
                    },
                )

            # 将管道完整消息列表喂入 context_service
            messages = ctx.state.get("messages", [])
            for msg in messages:
                content = msg.get("content", "")
                if content:
                    await context_service.add_message(
                        pipeline_run_id, {"role": msg.get("role", "user"), "content": str(content)[:2000]},
                    )

            # 检查 token 用量
            stats = await context_service.get_memory_stats(pipeline_run_id)
            usage_ratio = stats.get("usage_ratio", 0)
            total_tokens = stats.get("total_tokens", 0)

            context_window = self._resolve_context_window(ctx)
            trigger_ratio = self._config.get("compress_trigger_ratio", 0.5)
            triggered = usage_ratio >= trigger_ratio

            chunk_saved = False
            messages_updated = False

            if triggered and len(messages) > 10:
                compressor = await self._get_compressor(ctx)
                chunk_service = self._get_chunk_service(ctx)

                if compressor:
                    # 保留最近的消息（后 1/3），压缩旧的（前 2/3）
                    split_idx = max(len(messages) * 2 // 3, 1)
                    old_messages = messages[:split_idx]
                    recent_messages = messages[split_idx:]

                    # 加载前次 L1 作为背景
                    previous_l1 = await self._load_previous_l1(ctx)

                    # 一次性完成 L1 + L2 + 关键词
                    result = await compressor.compress_all(
                        old_messages, previous_l1=previous_l1,
                    )
                    l1_summary = result.get("l1", "")
                    l2_summary = result.get("l2", "")
                    keywords = result.get("keywords", [])

                    if l1_summary:
                        # 保存到 ChunkService
                        if chunk_service:
                            l1_chunk = ChunkData(
                                pipeline_run_id=pipeline_run_id,
                                layer="L1",
                                content=l1_summary,
                                token_count=len(l1_summary) // 2,
                                message_count=len(old_messages),
                                sequence_start=1,
                                sequence_end=split_idx,
                                keywords=keywords,
                            )
                            await chunk_service.save(l1_chunk)

                            if l2_summary:
                                l2_chunk = ChunkData(
                                    pipeline_run_id=pipeline_run_id,
                                    layer="L2",
                                    content=l2_summary,
                                    token_count=len(l2_summary) // 2,
                                    message_count=len(old_messages),
                                    sequence_start=1,
                                    sequence_end=split_idx,
                                    keywords=keywords,
                                )
                                await chunk_service.save(l2_chunk)

                            chunk_saved = True

                        logger.info(
                            "[%s] 压缩完成 | L1≈%d字符 L2≈%d字符 keywords=%d | 旧消息%d条→保留近%d条",
                            self.name,
                            len(l1_summary),
                            len(l2_summary),
                            len(keywords),
                            len(old_messages),
                            len(recent_messages),
                        )

                    # 构建压缩后的消息列表：一条摘要 + 近期消息
                    compressed_msg = {
                        "role": "user",
                        "content": f"[上下文压缩摘要]\n{ l1_summary or '（压缩失败，旧上下文已丢失）' }",
                        "name": "context_compress",
                    }
                    messages_updated = True

                    return OutputResult(
                        state_updates={
                            "memory.compressed": {
                                "triggered": True,
                                "total_tokens": total_tokens,
                                "usage_ratio": usage_ratio,
                                "chunk_saved": chunk_saved,
                                "compressed_msg_count": len(old_messages),
                                "retained_msg_count": len(recent_messages),
                            },
                            "messages": [compressed_msg] + recent_messages,
                        },
                    )

            return OutputResult(
                state_updates={
                    "memory.compressed": {
                        "triggered": triggered,
                        "total_tokens": total_tokens,
                        "usage_ratio": usage_ratio,
                        "chunk_saved": chunk_saved,
                    },
                },
            )

        except Exception as e:
            logger.warning("[%s] 上下文压缩失败: %s", self.name, e)
            return OutputResult(
                state_updates={"memory.compressed": {"triggered": False, "reason": str(e)}},
            )
