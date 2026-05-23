"""上下文窗口守卫 Input 插件。

在每次 LLM 调用前检查上下文大小，超阈值时委托给
MemoryContextService.compress_messages 进行预算驱动的分层压缩。

压缩算法由 MemoryContextService 实现：
- 预算驱动切分（recent_ratio=0.3）
- 单块替换 + 超预算降级（L1→L2→keywords）
- 多轮验证（最多 2 轮）

本插件只负责：检查阈值 → 获取服务 → 调用压缩 → 更新 state。

State 命名空间:
    - messages : 压缩后替换的消息列表
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

from memory.memory_context_service import _COMPRESSION_NOTICE

logger = logging.getLogger(__name__)


class ContextWindowGuardPlugin(IInputPlugin):
    """上下文窗口守卫 Input 插件。

    检查 messages 的估算 token 数，超阈值时委托 MemoryContextService 压缩。

    优先级：5（在 prompt_build 的 10 之前执行）
    错误策略：SKIP（压缩失败不阻塞管线）

    Attributes:
        _config: 插件配置字典
        _trigger_ratio: 触发压缩的阈值比例（默认 0.5）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文窗口守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
                - trigger_ratio: 触发压缩的阈值比例（默认 0.5）
        """
        self._config = config or {}
        self._trigger_ratio = self._config.get("trigger_ratio", 0.5)
        self._compression_model: str | None = self._config.get("compression_model")

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "context_window_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级，在 prompt_build 之前执行。"""
        return self._config.get("priority", 5)

    def _estimate_tokens(
        self, messages: list[dict[str, Any]], ctx: PluginContext,
    ) -> int:
        """估算当前 messages 的 token 数。

        基于 messages 做字符估算，再用上次实际用量校准：
        取 max(字符估算, 上次实际用量 * 1.15)。
        input_tokens 包含 system_prompt、压缩块、tool schemas 等预算内开销，
        反映真实上下文使用量，用于校准是合理的。
        """
        cjk = 0
        other = 0
        for m in messages:
            content = str(m.get("content", ""))
            for c in content:
                if "一" <= c <= "鿿" or "぀" <= c <= "ヿ":
                    cjk += 1
                else:
                    other += 1
            for tc in m.get("tool_calls", []):
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    other += len(args)
        char_estimate = int(cjk * 1.5 + other * 0.25)

        llm_usage = ctx.state.get("llm_usage", {})
        if isinstance(llm_usage, dict):
            prev_input = llm_usage.get("input_tokens", 0)
            if prev_input > 0:
                return max(char_estimate, round(prev_input * 1.15))

        return char_estimate

    _warned_no_context_window = False

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """检查上下文大小并在超阈值时触发记忆系统压缩。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含压缩后 messages 的插件执行结果
        """
        context_window = ctx.state.get("context_window")
        if not context_window:
            if not self._warned_no_context_window:
                self._warned_no_context_window = True
                logger.error(
                    "[%s] context_window 未设置，上下文守卫无法工作！"
                    " 请检查模型配置（llm.yaml）是否包含 context_window，"
                    "以及 core_plugins 是否正确合并了模型配置。",
                    self.name,
                )
            return PluginResult()

        messages = ctx.state.get("messages", [])
        if not messages:
            return PluginResult()

        # 窗口变更检测：清理旧压缩摘要，交由 prompt_build 从 chunk_service 重加载
        cleaned = await self._clean_if_window_changed(ctx, context_window, messages)
        if cleaned is not None:
            messages = cleaned

        estimated_tokens = self._estimate_tokens(messages, ctx)

        trigger_tokens = int(context_window * self._trigger_ratio)
        if estimated_tokens < trigger_tokens:
            return PluginResult()

        logger.info(
            "[%s] 上下文接近窗口限制: estimated_tokens=%d, trigger_tokens=%d, "
            "context_window=%d, trigger_ratio=%.2f, msg_count=%d",
            self.name, estimated_tokens, trigger_tokens,
            context_window, self._trigger_ratio, len(messages),
        )

        # 快速截断：按 recent 预算从尾部截取，旧消息由 prompt_build 通过压缩块加载
        truncated = await self._try_fast_truncate(ctx, messages, context_window)
        if truncated is not None and len(truncated) < len(messages):
            coverage_info = await self._check_compression_coverage(
                ctx, messages, truncated,
            )
            if coverage_info["fully_covered"]:
                logger.info(
                    "[%s] 快速截断: %d -> %d 条消息",
                    self.name, len(messages), len(truncated),
                )
                return PluginResult(state_updates={"messages": truncated})
            if coverage_info["covered_count"] > 0:
                # 增量压缩：已有块覆盖了部分消息，只压缩未覆盖的部分
                messages = self._trim_covered_messages(
                    messages, coverage_info["covered_count"],
                )
                logger.info(
                    "[%s] 增量压缩: 已有块覆盖 %d 条，待压缩 %d 条",
                    self.name, coverage_info["covered_count"],
                    sum(1 for m in messages if m.get("role") != "system"),
                )
            else:
                logger.info(
                    "[%s] 快速截断跳过: 被截断的消息无压缩块覆盖, %d -> %d 条消息, 走完整压缩",
                    self.name, len(messages), len(truncated),
                )

        service = self._get_memory_service(ctx)
        if not service:
            return PluginResult()

        llm_call_fn = self._get_llm_call_fn(ctx)
        if not llm_call_fn:
            logger.warning("[%s] 无法获取 LLM 调用函数，跳过压缩", self.name)
            return PluginResult()

        service.set_llm_call_fn(llm_call_fn)

        previous_l1 = await self._load_previous_l1(ctx)
        save_fn = await self._make_save_chunk_fn(ctx)

        # BUG-FIX-fix_20260522_compression_no_signal:
        # 通过 on_chunk 发送压缩进度，让前端知道系统正在工作
        _on_chunk = ctx.state.get("on_chunk")
        if _on_chunk:
            try:
                _on_chunk({
                    "type": "compression_start",
                    "pipeline_id": ctx.state.get("pipeline_id", ""),
                })
            except Exception:
                pass

        compressed = await service.compress_messages(
            messages=messages,
            context_window=context_window,
            trigger_ratio=self._trigger_ratio,
            previous_l1=previous_l1,
            save_chunk_fn=save_fn,
        )

        if compressed and len(compressed) < len(messages):
            logger.info(
                "[%s] 压缩完成: %d -> %d 条消息",
                self.name, len(messages), len(compressed),
            )
            # BUG-FIX-fix_user_input_lost_in_compression:
            # 问题根因: 旧代码用 _read_recent_from_l0 的 L0 回读结果替换了
            #   compress_messages 返回的 compressed。但 L0 只包含已持久化的历史消息，
            #   不包含当前轮用户输入（用户输入还没经过管道处理，未被持久化到 L0）。
            #   导致 state["messages"] 被覆盖后用户输入丢失，LLM 收不到用户说了什么。
            # 修复方案: 直接使用 compress_messages 返回的 compressed，不再用 L0 回读覆盖。
            #   compress_messages 内部按 recent_ratio(30%) 从尾部保留 recent 消息，
            #   用户输入在消息列表末尾，一定会被保留在 recent 部分，不会丢失。
            # 影响范围: 长对话触发上下文压缩时，用户输入被吞掉。
            # 修复日期: 2026-05-08
            return PluginResult(state_updates={"messages": compressed})

        # 窗口变更但不需要压缩时，返回清理后的 messages
        if cleaned is not None:
            return PluginResult(state_updates={"messages": messages})

        return PluginResult()

    async def _try_fast_truncate(
        self,
        ctx: PluginContext,
        messages: list[dict[str, Any]],
        context_window: int,
    ) -> list[dict[str, Any]] | None:
        """按 recent 预算从尾部截取消息，不调 LLM。

        messages 超阈值时，先尝试按 recent 预算截断。
        被截掉的旧消息由 prompt_build 通过 chunk_service 加载压缩块来补充。
        只有当截断后仍在阈值内时才返回截断结果，否则返回 None 让调用方走完整压缩。

        Returns:
            截断后的消息列表，或 None（需要走完整压缩）
        """
        from memory.context_compressor import CompressionConfig

        config = CompressionConfig.from_yaml_config(context_window)
        recent_budget = config.get_budgets()["recent"]
        trigger_tokens = config.get_trigger_threshold()

        # 分离 system 消息和其他消息
        system_msgs: list[dict[str, Any]] = []
        other_msgs: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                content = str(m.get("content", ""))
                if not content.startswith("## 历史对话压缩摘要") and content != _COMPRESSION_NOTICE:
                    system_msgs.append(m)
            else:
                other_msgs.append(m)

        if not other_msgs:
            return None

        # 按 recent 预算从尾部截取
        accumulated = 0
        split_idx = len(other_msgs)
        for i in range(len(other_msgs) - 1, -1, -1):
            msg_tokens = self._estimate_msg_tokens_simple(other_msgs[i])
            if accumulated + msg_tokens > recent_budget:
                split_idx = i + 1
                break
            accumulated += msg_tokens

        if split_idx <= 0:
            return None

        # 保证 tool call 配对完整
        from memory.memory_context_service import MemoryContextService
        _, recent_msgs = MemoryContextService._split_preserving_tool_pairs(
            other_msgs, split_idx,
        )

        truncated = system_msgs + recent_msgs
        truncated_tokens = sum(self._estimate_msg_tokens_simple(m) for m in truncated)

        if truncated_tokens >= trigger_tokens:
            return None

        return truncated

    async def _check_compression_coverage(
        self,
        ctx: PluginContext,
        original_messages: list[dict[str, Any]],
        truncated_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """检查被截断掉的旧消息是否已被压缩块覆盖。

        Args:
            ctx: 插件上下文
            original_messages: 截断前的完整消息列表
            truncated_messages: 截断后的消息列表

        Returns:
            {"fully_covered": bool, "covered_count": int}
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            return {"fully_covered": False, "covered_count": 0}

        from pipeline.types import StateKeys

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return {"fully_covered": False, "covered_count": 0}

        try:
            chunks = await chunk_service.find_by_pipeline(
                pipeline_run_id, "L1",
            )
        except Exception:
            return {"fully_covered": False, "covered_count": 0}

        if not chunks:
            return {"fully_covered": False, "covered_count": 0}

        non_system_original = [
            m for m in original_messages if m.get("role") != "system"
        ]
        non_system_truncated = [
            m for m in truncated_messages if m.get("role") != "system"
        ]

        if len(non_system_truncated) >= len(non_system_original):
            return {"fully_covered": True, "covered_count": len(non_system_original)}

        removed_count = len(non_system_original) - len(non_system_truncated)

        # 已有块的最大覆盖范围
        max_coverage = max(
            (c.sequence_end for c in chunks if c.sequence_end),
            default=0,
        )

        if max_coverage >= removed_count:
            return {"fully_covered": True, "covered_count": removed_count}

        logger.info(
            "[%s] 压缩块覆盖不足: removed=%d, max_coverage=%d",
            self.name, removed_count, max_coverage,
        )
        return {"fully_covered": False, "covered_count": max_coverage}

    @staticmethod
    def _trim_covered_messages(
        messages: list[dict[str, Any]],
        covered_count: int,
    ) -> list[dict[str, Any]]:
        """裁剪已被压缩块覆盖的消息，只保留未覆盖的部分用于增量压缩。

        Args:
            messages: 原始消息列表
            covered_count: 已被压缩块覆盖的非 system 消息数量

        Returns:
            裁剪后的消息列表（system 消息保留 + 未覆盖的非 system 消息）
        """
        result: list[dict[str, Any]] = []
        non_system_seen = 0
        for m in messages:
            if m.get("role") == "system":
                result.append(m)
            else:
                non_system_seen += 1
                if non_system_seen > covered_count:
                    result.append(m)
        return result

    @staticmethod
    def _estimate_msg_tokens_simple(msg: dict[str, Any]) -> int:
        """简单 token 估算（用于快速截断）。"""
        tokens = 0.0
        content = str(msg.get("content", ""))
        for c in content:
            if "一" <= c <= "鿿" or "぀" <= c <= "ヿ":
                tokens += 1.5
            else:
                tokens += 0.25
        for tc in msg.get("tool_calls", []):
            args = tc.get("function", {}).get("arguments", "")
            if args:
                tokens += len(args) * 0.25
        return int(tokens)

    async def _clean_if_window_changed(
        self,
        ctx: PluginContext,
        context_window: int,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """检测 context_window 是否变化，变化时清理 messages 中的旧压缩摘要。

        压缩摘要由 prompt_build 从 chunk_service 按新预算重新加载，
        无需保留在 messages 列表中。

        Returns:
            清理后的 messages 列表，或 None（窗口未变，无需处理）
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            return None

        from pipeline.types import StateKeys

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return None

        try:
            chunks = await chunk_service.find_by_pipeline(
                pipeline_run_id, "L1",
            )
        except Exception:
            return None

        if not chunks:
            return None

        latest_chunk = max(chunks, key=lambda c: c.sequence_end)
        chunk_window = latest_chunk.context_window

        # 窗口未变 → 不处理
        if not chunk_window or chunk_window == context_window:
            return None

        # 窗口变了 → 清理旧压缩摘要 system msg
        compression_notice = (
            "[系统提示] 由于对话历史过长，较早的上下文已被记忆系统分层压缩。"
            "压缩摘要包含在上方消息中，请基于压缩摘要和当前剩余上下文继续完成任务。"
        )
        cleaned = [
            m for m in messages
            if not (
                m.get("role") == "system"
                and (
                    str(m.get("content", "")).startswith("## 历史对话压缩摘要")
                    or str(m.get("content", "")) == compression_notice
                )
            )
        ]

        if len(cleaned) == len(messages):
            return None

        logger.info(
            "[%s] context_window 变更: %d → %d, 清理 %d 条旧压缩摘要",
            self.name, chunk_window, context_window,
            len(messages) - len(cleaned),
        )
        return cleaned

    def _read_recent_from_l0(
        self, ctx: PluginContext, context_window: int,
    ) -> list[dict[str, Any]] | None:
        """从 ExecutionRecordStorage（L0）回读近期原始消息。

        按预算从后往前截取，预算之外的旧消息由 ChunkService 的 L1/L2 摘要覆盖。

        Returns:
            消息列表，失败返回 None（回退到压缩后的消息）
        """
        try:
            storage = ctx.get_service("execution_record_storage")
        except (KeyError, AttributeError):
            return None

        from pipeline.types import StateKeys

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return None

        # recent 预算 = context_window * 30%（与 compress_messages 的 recent_ratio 一致）
        recent_budget = int(context_window * 0.3)

        try:
            return storage.reconstruct_messages(
                pipeline_run_id, budget=recent_budget,
            )
        except Exception as exc:
            logger.warning(
                "[%s] L0 回读失败，回退到压缩消息: %s", self.name, exc,
            )
            return None

    def _get_memory_service(self, ctx: PluginContext):
        """获取 MemoryContextService 实例。"""
        try:
            return ctx.get_service("context_service")
        except (KeyError, AttributeError):
            pass

        from memory.memory_context_service import MemoryContextService

        try:
            context_window = ctx.state.get("context_window", 128000)
            return MemoryContextService(
                config={"context_window": context_window, "compress_trigger_ratio": 0.5},
            )
        except Exception:
            return None

    async def _load_previous_l1(self, ctx: PluginContext) -> str:
        """从 ChunkService 加载历史 L1 内容作为增量压缩背景。

        只取最早和最新的压缩块，避免背景信息膨胀导致压缩 prompt 超限。
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            return ""

        from pipeline.types import StateKeys

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return ""

        try:
            chunks = await chunk_service.find_by_pipeline(
                pipeline_run_id, "L1",
            )
            if not chunks:
                return ""

            # 只保留最早 + 最新的块，中间的全部丢弃
            if len(chunks) == 1:
                parts = [chunks[0].content]
            else:
                parts = [chunks[0].content, chunks[-1].content]

            return "\n\n---\n\n".join(p for p in parts if p)
        except Exception:
            pass

        return ""

    async def _make_save_chunk_fn(self, ctx: PluginContext):
        """构建压缩块持久化回调。"""
        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            logger.debug("[%s] chunk_service 不可用，跳过持久化", self.name)
            return None

        from memory.types import ChunkData
        from pipeline.types import StateKeys

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        session_id = ctx.state.get("context.session_id", "")
        context_window = ctx.state.get("context_window", 0)
        _bg_earliest = ""

        async def save_fn(old_msgs, comp_result):
            """保存压缩块到 ChunkService，返回更新后的背景信息供下一批使用。

            保存后从 chunk_service 重新加载所有 L1 块，保持与 prompt_build
            一致的背景信息视图。

            Args:
                old_msgs: 被压缩的原始消息列表
                comp_result: {"l1": str, "l2": str, "keywords": list}

            Returns:
                更新后的 previous_l1 背景（最早+最新块的拼接）
            """
            if isinstance(comp_result, str):
                comp_result = {"l1": comp_result, "l2": "", "keywords": []}

            l1_content = comp_result.get("l1", "")
            l2_content = comp_result.get("l2", "")
            keywords = comp_result.get("keywords", [])
            msg_count = len(old_msgs)

            # 从已有块计算正确的序列范围
            sequence_start = 1
            try:
                existing = await chunk_service.find_by_pipeline(
                    pipeline_run_id, "L1",
                )
                if existing:
                    max_end = max(c.sequence_end for c in existing if c.sequence_end)
                    sequence_start = max_end + 1
            except Exception:
                pass
            sequence_end = sequence_start + msg_count - 1

            # L1 chunk
            l1_chunk = ChunkData(
                pipeline_run_id=pipeline_run_id,
                session_id=session_id,
                layer="L1",
                content=l1_content,
                l2_content=l2_content,
                token_count=max(1, len(l1_content) // 2),
                message_count=msg_count,
                sequence_start=sequence_start,
                sequence_end=sequence_end,
                keywords=keywords,
                context_window=context_window,
            )
            l1_id = await chunk_service.save(l1_chunk)

            # L2 chunk（独立保存，供 prompt_build 按 layer="L2" 检索）
            if l2_content:
                l2_chunk = ChunkData(
                    pipeline_run_id=pipeline_run_id,
                    session_id=session_id,
                    layer="L2",
                    content=l2_content,
                    token_count=max(1, len(l2_content) // 2),
                    message_count=msg_count,
                    sequence_start=sequence_start,
                    sequence_end=sequence_end,
                    keywords=keywords,
                    context_window=context_window,
                )
                await chunk_service.save(l2_chunk)

            logger.info(
                "[%s] 压缩块已保存: L1_id=%s (%d字符), "
                "L2≈%d字符, keywords=%d",
                self.name, l1_id, len(l1_content),
                len(l2_content), len(keywords),
            )

            # 返回更新后的背景：最早(_bg_earliest) + 最新(l1_content)
            # 下一批用这个作为 previous_l1，与 _load_previous_l1 逻辑一致
            nonlocal _bg_earliest
            if not _bg_earliest and l1_content:
                _bg_earliest = l1_content
            if _bg_earliest and l1_content and _bg_earliest != l1_content:
                updated_bg = _bg_earliest + "\n\n---\n\n" + l1_content
            else:
                updated_bg = l1_content or _bg_earliest

            return updated_bg

        return save_fn

    def _get_llm_call_fn(self, ctx: PluginContext):
        """从上下文中获取 LLM 调用函数，优先使用专用压缩模型。"""
        # 如果配置了 compression_model，用它构建独立调用函数
        if self._compression_model:
            fn = self._build_compression_model_fn(ctx, self._compression_model)
            if fn:
                return fn

        # 回退：用主模型的 adapter
        llm_core = ctx.get_service("llm_core")
        if llm_core and hasattr(llm_core, "_adapter") and hasattr(llm_core, "_model"):
            _use_router = hasattr(llm_core._adapter, '_router')
            _model_str = llm_core._model if _use_router else llm_core._get_model_string()

            async def _call_via_core(prompt: str) -> str:
                kwargs: dict[str, Any] = {
                    "model": _model_str,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                }
                if not _use_router:
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

    def _build_compression_model_fn(self, ctx: PluginContext, model_id: str):
        """用指定的压缩模型构建 LLM 调用函数。

        从 model_loader 获取模型配置（api_base、api_key 等），
        构建独立的 LiteLLMAdapter 调用函数。
        """
        try:
            from config.models import get_model_config_loader
            from llm.adapter import LiteLLMAdapter

            loader = get_model_config_loader()
            conf = loader.get_llm_core_config(model_id)
            if not conf:
                logger.warning(
                    "[%s] 压缩模型 %r 未在 llm.yaml 中找到，回退到主模型",
                    self.name, model_id,
                )
                return None

            adapter = LiteLLMAdapter()
            api_base = conf.get("api_base")
            api_key = conf.get("api_key")
            provider = conf.get("provider", "")
            bare_name = conf.get("model_name", model_id)

            from llm.router_factory import _get_litellm_model_string
            litellm_model = _get_litellm_model_string(provider, bare_name)

            async def _call_compression_model(prompt: str) -> str:
                response = await adapter.completion(
                    model=litellm_model,
                    messages=[{"role": "user", "content": prompt}],
                    stream=False,
                    api_base=api_base,
                    api_key=api_key,
                )
                return response.text or ""

            logger.info(
                "[%s] 压缩使用独立模型: %s (provider=%s)",
                self.name, model_id, provider,
            )
            return _call_compression_model
        except Exception as exc:
            logger.warning(
                "[%s] 构建压缩模型调用函数失败: %s，回退到主模型",
                self.name, exc,
            )
            return None
