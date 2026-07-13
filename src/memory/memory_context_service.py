"""记忆上下文服务。

从旧代码 src/memory/memory_context_service.py 搬迁。
移除 ContextRepository/LLMClient 等硬依赖，
通过注入的接口实现压缩和组装。

暴露接口：
- MemoryContextService: 记忆上下文服务

压缩算法：
- 预算驱动：按 CompressionConfig 的 recent_ratio 计算 recent 预算，
  从尾部向前累加 token 确定切分点（不是固定条数）
- 单块替换：每次压缩产生一个 L1 和一个 L2，新压缩替换旧的
- 超预算降级：L1 超预算用 L2，L2 也超预算用 keywords
- 多轮验证：压缩后检查总 tokens，仍超预算则再压一轮（最多 2 轮）
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from memory.context_compressor import CompressionConfig, ContextCompressor

# LLM 调用函数类型：接收 prompt 字符串，返回响应字符串
LLMCallFn = Callable[[str], Awaitable[str]]

logger = logging.getLogger(__name__)

_COMPRESSION_NOTICE = (
    "[系统提示] 由于对话历史过长，较早的上下文已被记忆系统分层压缩。"
    "压缩摘要包含在上方消息中，请基于压缩摘要和当前剩余上下文继续完成任务。"
)


class MemoryContextService:
    """记忆上下文服务。

    职责：
    - 协调压缩和组装流程
    - 写流程：接收消息 -> 检查阈值 -> 触发压缩 -> 保存
    - 读流程：读取各层 -> 组装 -> 返回提示词
    - 支持按 parent_record_id 隔离上下文
    - compress_messages：预算驱动的完整压缩流程

    Attributes:
        _compressor: 上下文压缩器
        _config: 服务配置
        _layers: 内存中的层级数据 {session_id: {layer: content}}
    """

    _MAX_COMPRESS_ROUNDS = 2

    def __init__(
        self,
        compressor: ContextCompressor | None = None,
        config: dict[str, Any] | None = None,
        llm_call_fn: LLMCallFn | None = None,
    ) -> None:
        """初始化记忆上下文服务。

        Args:
            compressor: 上下文压缩器（可选）
            config: 服务配置，需包含 context_window, compress_trigger_ratio
            llm_call_fn: LLM 调用函数（可选，支持后续通过 set_llm_call_fn 延迟注入）
        """
        compression_config = CompressionConfig.from_yaml_config(
            config.get("context_window", 128000) if config else 128000,
        )
        self._compressor = compressor or ContextCompressor(config=compression_config)
        self._config = config or {
            "context_window": 128000,
            "compress_trigger_ratio": 0.55,
        }  # 0.55 见 config.defaults.COMPRESS_TRIGGER_RATIO
        self._llm_call_fn: LLMCallFn | None = llm_call_fn

        # 内存存储：{session_id: {"L0": [messages], "L1": str, "L2": str}}
        self._layers: dict[str, dict[str, Any]] = {}

        # 父执行记录 ID（用于上下文隔离）
        self.parent_record_id: str | None = None

        # 外部依赖（通过 setup() 注入）
        self._chunk_service = None
        self._memory_service = None
        self._llm_core = None
        self._pipeline_id = ""
        self._session_id = ""
        self._user_id = ""
        self._compression_model_id = None
        self._model_name = ""

        self._validate_config()

        logger.debug(
            "[MemoryContextService] 初始化完成 | context_window=%s",
            self._config.get("context_window"),
        )

    def _validate_config(self) -> None:
        """验证配置完整性，缺失字段用默认值填充。"""
        defaults = {
            "context_window": 128000,
            "compress_trigger_ratio": 0.55,  # 见 config.defaults.COMPRESS_TRIGGER_RATIO
        }
        for key, default in defaults.items():
            if key not in self._config:
                self._config[key] = default

    def set_llm_call_fn(self, llm_call_fn: LLMCallFn) -> None:
        """延迟注入 LLM 调用函数。

        允许在服务创建后才提供 LLM 能力（例如从 services 中获取 llm_core 后），
        压缩器将在首次压缩时自动使用此函数。

        Args:
            llm_call_fn: 异步 LLM 调用函数
        """
        self._llm_call_fn = llm_call_fn
        self._compressor.set_llm_call_fn(llm_call_fn)
        logger.debug("[MemoryContextService] LLM 调用函数已注入")

    def setup(
        self,
        *,
        chunk_service=None,
        memory_service=None,
        llm_core=None,
        pipeline_id="",
        session_id="",
        context_window=0,
        user_id="",
        compression_model_id=None,
        model_name="",
    ) -> None:
        """注入外部依赖，供 context_window_guard 调用。"""
        if chunk_service is not None:
            self._chunk_service = chunk_service
        if memory_service is not None:
            self._memory_service = memory_service
        if llm_core is not None:
            self._llm_core = llm_core
        if pipeline_id:
            self._pipeline_id = pipeline_id
        if session_id:
            self._session_id = session_id
        if context_window:
            self._config["context_window"] = context_window
        if user_id:
            self._user_id = user_id
        if compression_model_id is not None:
            self._compression_model_id = compression_model_id
        if model_name:
            self._model_name = model_name

    # ------------------------------------------------------------------
    # 预算驱动的完整压缩流程（供 context_window_guard 调用）
    # ------------------------------------------------------------------

    async def compress_messages(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        trigger_ratio: float = 0.55,  # 见 config.defaults.COMPRESS_TRIGGER_RATIO
        state_snapshot: str = "",
        recent_process_blocks: str = "",
        save_chunk_fn: Callable[..., Awaitable[None]] | None = None,
        compression_window: int | None = None,
    ) -> list[dict[str, Any]] | None:
        """预算驱动的完整压缩流程。

        按CompressionConfig的recent_ratio计算recent预算，从尾部向前切分。
        压缩超出预算的旧消息为L1/L2/keywords，超预算逐级降级。
        支持多轮压缩验证。

        Args:
            messages: 完整消息列表
            context_window: 主模型上下文窗口大小（用于预算切分）
            trigger_ratio: 触发压缩的比例
            state_snapshot: 当前累积的状态快照（JSON）
            recent_process_blocks: 最近的过程块样本（采样文本）
            save_chunk_fn: 压缩块持久化回调（已弃用，内部自动保存）
            compression_window: 压缩模型上下文窗口大小（用于分片大小计算），
                为 None 时回退到 context_window

        Returns:
            压缩后的消息列表，无需压缩或失败返回 None
        """
        try:
            return await self._compress_messages_impl(
                messages,
                context_window,
                trigger_ratio,
                state_snapshot,
                recent_process_blocks,
                save_chunk_fn,
                compression_window,
            )
        except Exception as exc:
            logger.error(
                "[MemoryContextService] compress_messages 顶层异常: %s",
                exc,
                exc_info=True,
            )
            return None

    async def _compress_messages_impl(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        trigger_ratio: float,
        state_snapshot: str,
        recent_process_blocks: str,
        save_chunk_fn,
        compression_window: int | None,
    ) -> list[dict[str, Any]] | None:
        """compress_messages 的实际实现。"""
        logger.info("[MemoryContextService] _compress_messages_impl 开始执行")
        # 自动加载背景和构建依赖
        if not state_snapshot and self._chunk_service:
            logger.info("[MemoryContextService] 加载背景信息...")
            bg = await self._load_background()
            state_snapshot = bg["state_snapshot"]
            if not recent_process_blocks:
                recent_process_blocks = bg["process_blocks"]
            logger.info(
                "[MemoryContextService] 背景加载完成: snapshot=%d, blocks=%d",
                len(state_snapshot),
                len(recent_process_blocks),
            )

        if not self._llm_call_fn:
            logger.info("[MemoryContextService] 构建 LLM 调用函数...")
            fn = self._build_llm_call_fn()
            if fn:
                self.set_llm_call_fn(fn)
                logger.info("[MemoryContextService] LLM 调用函数构建成功")
            else:
                logger.warning(
                    "[MemoryContextService] compress_messages: 无法构建 LLM 调用函数"
                    " (compression_model_id=%s, llm_core=%s, model_name=%s)",
                    self._compression_model_id,
                    type(self._llm_core).__name__ if self._llm_core else None,
                    self._model_name,
                )

        if not compression_window:
            compression_window = self._get_compression_window(context_window)

        logger.info(
            "[MemoryContextService] compress_messages 入口: "
            "msg_count=%d, context_window=%d, compression_window=%s, "
            "trigger_ratio=%.2f, llm_fn=%s, chunk_service=%s, "
            "state_snapshot_len=%d, process_blocks_len=%d",
            len(messages),
            context_window,
            compression_window or context_window,
            trigger_ratio,
            "有" if self._llm_call_fn else "无",
            "有" if self._chunk_service else "无",
            len(state_snapshot),
            len(recent_process_blocks),
        )

        if not self._llm_call_fn:
            logger.warning("[MemoryContextService] 跳过压缩：未提供 LLM 调用函数")
            return None

        # 预算切分用主模型窗口，分片大小用压缩模型窗口
        config = CompressionConfig.from_yaml_config(context_window)
        budgets = config.get_budgets()
        trigger_tokens = int(context_window * trigger_ratio)
        comp_window = compression_window or context_window

        current_messages = messages
        compressed = None

        for round_idx in range(self._MAX_COMPRESS_ROUNDS):
            compressed = await self._do_compress_round(
                current_messages,
                context_window,
                budgets,
                state_snapshot,
                recent_process_blocks,
                compression_window=comp_window,
            )
            if compressed is None:
                break

            total_tokens = sum(self._estimate_msg_tokens(m) for m in compressed)
            logger.info(
                "[MemoryContextService] 第 %d 轮压缩: %d -> %d 条, %d tokens (触发线 %d)",
                round_idx + 1,
                len(current_messages),
                len(compressed),
                total_tokens,
                trigger_tokens,
            )

            if total_tokens < trigger_tokens:
                return compressed

            current_messages = compressed

        return compressed

    async def _do_compress_round(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        budgets: dict[str, int],
        state_snapshot: str,
        recent_process_blocks: str,
        compression_window: int | None = None,
    ) -> list[dict[str, Any]] | None:
        """执行一轮预算驱动的压缩。

        预算切分（recent_budget）基于主模型 context_window，
        分片大小（batch_budget）基于 compression_window（防止压缩模型上下文不够）。

        多块追加模式：
        - 识别已有压缩块，保留不动
        - 只压缩最后一个压缩块之后的新消息
        - 产生新压缩块追加在旧块后面
        - 所有块总量超 L1 预算时从最老块开始降级

        组装：pure_system + [block_1, ..., block_N, NEW] + recent
        """
        # 三路分离：纯 system / 旧压缩块 / 其他消息
        pure_system_msgs: list[dict[str, Any]] = []
        old_blocks: list[dict[str, Any]] = []
        other_msgs: list[dict[str, Any]] = []

        for m in messages:
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if role != "system":
                other_msgs.append(m)
            elif content.startswith("## 历史对话压缩摘要") or content == _COMPRESSION_NOTICE:
                old_blocks.append(m)
            else:
                pure_system_msgs.append(m)

        logger.info(
            "[MemoryContextService] _do_compress_round: "
            "total=%d, pure_system=%d, old_blocks=%d, other=%d, "
            "recent_budget=%d, context_window=%d, compression_window=%s",
            len(messages),
            len(pure_system_msgs),
            len(old_blocks),
            len(other_msgs),
            budgets.get("recent", 0),
            context_window,
            compression_window or context_window,
        )

        if not other_msgs:
            return None

        # 按 token 预算从尾部向前计算切分点（基于主模型窗口）
        recent_budget = budgets["recent"]
        split_idx = self._find_split_by_budget(other_msgs, recent_budget)
        if split_idx <= 0:
            total_est = sum(self._estimate_msg_tokens(m) for m in other_msgs)
            logger.warning(
                "[MemoryContextService] split_idx=%d, 所有消息都在 recent 预算内: "
                "total_estimated=%d tokens, recent_budget=%d, context_window=%d, msg_count=%d",
                split_idx,
                total_est,
                recent_budget,
                context_window,
                len(other_msgs),
            )
            return None

        # 保证工具调用配对完整
        old_msgs, recent_msgs = self._split_preserving_tool_pairs(
            other_msgs,
            split_idx,
        )

        if not old_msgs:
            return None

        recent_tokens = sum(self._estimate_msg_tokens(m) for m in recent_msgs)
        logger.info(
            "[MemoryContextService] 预算切分: recent=%d条/%dtokens (预算%d), old=%d条, existing_blocks=%d",
            len(recent_msgs),
            recent_tokens,
            recent_budget,
            len(old_msgs),
            len(old_blocks) // 2,
        )

        # 分片大小按压缩模型窗口算（防止压缩模型上下文不够）
        # 每片大小不超过 compression_window * batch_ratio，超出则均分
        comp_win = compression_window or context_window
        old_tokens = sum(self._estimate_msg_tokens(m) for m in old_msgs)
        batch_ratio = 0.5
        batch_budget = int(comp_win * batch_ratio)
        num_batches = max(1, -(-old_tokens // batch_budget))  # ceil division

        any_success = False

        for batch_idx in range(num_batches):
            start = batch_idx * len(old_msgs) // num_batches
            end = (batch_idx + 1) * len(old_msgs) // num_batches
            batch = old_msgs[start:end]
            if not batch:
                continue

            logger.info(
                "[MemoryContextService] 分批压缩 %d/%d: %d 条消息",
                batch_idx + 1,
                num_batches,
                len(batch),
            )

            comp_result = await self._build_compression_content(
                batch,
                context_window,
                budgets,
                state_snapshot,
                recent_process_blocks,
            )
            if not comp_result:
                logger.warning(
                    "[MemoryContextService] 第 %d 批压缩失败",
                    batch_idx + 1,
                )
                continue

            try:
                await self._save_compression_result(batch, comp_result)
            except Exception as exc:
                logger.warning("[MemoryContextService] 保存压缩块失败: %s", exc)

            any_success = True

        if not any_success:
            return None

        return pure_system_msgs + recent_msgs

    # ------------------------------------------------------------------
    # 压缩背景加载
    # ------------------------------------------------------------------

    async def _load_background(self) -> dict[str, str]:
        """加载压缩背景信息（state_snapshot + 过程块采样）。"""
        state_snapshot = ""
        process_blocks = ""

        if self._chunk_service and self._pipeline_id:
            try:
                snapshots = await self._chunk_service.find_by_pipeline(
                    self._pipeline_id,
                    "STATE_SNAPSHOT",
                )
                if snapshots:
                    state_snapshot = snapshots[0].content or ""
            except Exception as e:
                logger.warning(
                    "[MemoryContextService] 加载 state_snapshot 失败: %s",
                    e,
                )

            try:
                chunks = await self._chunk_service.find_by_pipeline(
                    self._pipeline_id,
                    "L1",
                )
                if chunks:
                    sorted_chunks = sorted(chunks, key=lambda c: c.sequence_start)
                    if len(sorted_chunks) <= 3:
                        samples = sorted_chunks
                    else:
                        mid_idx = len(sorted_chunks) // 2
                        samples = [
                            sorted_chunks[0],
                            sorted_chunks[mid_idx],
                            sorted_chunks[-1],
                        ]
                    process_blocks = "\n\n---\n\n".join(
                        f"[{chunk.sequence_start}-{chunk.sequence_end}] {chunk.content}" for chunk in samples
                    )
            except Exception as e:
                logger.warning(
                    "[MemoryContextService] 加载 L1 过程块失败: %s",
                    e,
                )

        return {"state_snapshot": state_snapshot, "process_blocks": process_blocks}

    # ------------------------------------------------------------------
    # 压缩结果持久化
    # ------------------------------------------------------------------

    async def _save_compression_result(  # noqa: PLR0912,PLR0915
        self,
        old_msgs: list[dict[str, Any]],
        comp_result: dict[str, Any],
    ) -> None:
        """保存压缩块到 ChunkService + 覆盖状态快照 + 写入长期记忆。"""
        if not self._chunk_service or not self._pipeline_id:
            return

        from memory.types import ChunkData  # noqa: PLC0415

        l1_content = comp_result.get("l1", "")
        l2_content = comp_result.get("l2", "")
        keywords = comp_result.get("keywords", [])
        state_snapshot = comp_result.get("state_snapshot", {})
        memory_items = comp_result.get("memory_items", {})
        msg_count = len(old_msgs)
        context_window = self._config.get("context_window", 0)

        sequences = [
            m["_record_sequence"]
            for m in old_msgs
            if "_record_sequence" in m and isinstance(m["_record_sequence"], int)
        ]
        if sequences:
            sequence_start = min(sequences)
            sequence_end = max(sequences)
        else:
            # 兜底：消息没有 sequence 信息时，从已有块递增
            sequence_start = 1
            try:
                existing_l1 = await self._chunk_service.find_by_pipeline(
                    self._pipeline_id,
                    "L1",
                )
                if existing_l1:
                    max_end = max(c.sequence_end for c in existing_l1 if c.sequence_end)
                    sequence_start = max_end + 1
            except Exception:
                pass
            sequence_end = sequence_start + msg_count - 1

        # L1 过程块
        l1_chunk = ChunkData(
            pipeline_run_id=self._pipeline_id,
            session_id=self._session_id,
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
        l1_id = await self._chunk_service.save(l1_chunk)

        # L2 块
        if l2_content:
            l2_chunk = ChunkData(
                pipeline_run_id=self._pipeline_id,
                session_id=self._session_id,
                layer="L2",
                content=l2_content,
                token_count=max(1, len(l2_content) // 2),
                message_count=msg_count,
                sequence_start=sequence_start,
                sequence_end=sequence_end,
                keywords=keywords,
                context_window=context_window,
            )
            await self._chunk_service.save(l2_chunk)

        # STATE_SNAPSHOT（覆盖）
        if state_snapshot:
            try:
                old_snapshots = await self._chunk_service.find_by_pipeline(
                    self._pipeline_id,
                    "STATE_SNAPSHOT",
                )
                for old in old_snapshots:
                    await self._chunk_service.delete(old.id)
            except Exception:
                pass

            import json  # noqa: PLC0415

            ss_content = json.dumps(state_snapshot, ensure_ascii=False, indent=2)
            snapshot_chunk = ChunkData(
                pipeline_run_id=self._pipeline_id,
                session_id=self._session_id,
                layer="STATE_SNAPSHOT",
                content=ss_content,
                token_count=max(1, len(ss_content) // 2),
                message_count=msg_count,
                sequence_start=1,
                sequence_end=sequence_end,
                context_window=context_window,
            )
            await self._chunk_service.save(snapshot_chunk)

        # memory_items → memory_service
        if memory_items and any(v for v in memory_items.values() if v and v != "null") and self._memory_service:
            try:
                tag_map = {
                    "user_profile_updates": "user_profile",
                    "project_knowledge_updates": "project_knowledge",
                    "experience_updates": "experience",
                }
                extracted = 0
                for key, value in memory_items.items():
                    if value and value != "null":
                        await self._memory_service.add_memory(
                            user_id=self._user_id,
                            memory_type="semantic",
                            tags=[tag_map.get(key, key)],
                            content=value,
                            source="compression",
                        )
                        extracted += 1
                if extracted:
                    logger.info(
                        "[MemoryContextService] 长期记忆提取: %d 条",
                        extracted,
                    )
            except Exception as exc:
                logger.warning("[MemoryContextService] 长期记忆写入失败: %s", exc)

        logger.info(
            "[MemoryContextService] 压缩块已保存: L1_id=%s (%d字符), L2≈%d字符, keywords=%d, state_snapshot=%s",
            l1_id,
            len(l1_content),
            len(l2_content),
            len(keywords),
            "有" if state_snapshot else "无",
        )

    # ------------------------------------------------------------------
    # LLM 调用函数构建
    # ------------------------------------------------------------------

    def _build_llm_call_fn(self):
        """构建 LLM 调用函数，统一走 router_factory 的 KeyPoolAdapter。

        优先级：
        1. 通过 router_factory 的共享 Adapter（正确处理 keys 列表 + 并发控制）
        2. 回退到 llm_core 的 adapter
        """
        # 优先：通过 router_factory 获取共享 Adapter（统一通道）
        try:
            from config.models import get_model_config_loader  # noqa: PLC0415
            from llm.router_factory import get_or_create_adapter  # noqa: PLC0415

            loader = get_model_config_loader()
            adapter = get_or_create_adapter(loader)
            model_id = self._compression_model_id or self._model_name
            if model_id:
                # 获取 litellm 模型字符串
                model_conf = loader.get_model_config(model_id)
                if model_conf:
                    provider = model_conf.get("provider", "")
                    bare_name = model_conf.get("model_name", model_id)
                    from llm.router_factory import _get_litellm_model_string  # noqa: PLC0415

                    litellm_model = _get_litellm_model_string(provider, bare_name)

                    async def _call_via_shared_adapter(prompt: str) -> str:
                        response = await adapter.completion(
                            model=litellm_model,
                            messages=[{"role": "user", "content": prompt}],
                            stream=False,
                        )
                        return response.text or ""

                    logger.info(
                        "[MemoryContextService] 压缩使用共享Adapter: model=%s (provider=%s)",
                        model_id,
                        provider,
                    )
                    return _call_via_shared_adapter
        except Exception as exc:
            logger.warning("[MemoryContextService] 共享Adapter构建失败: %s", exc)

        # 回退：llm_core 的 adapter
        if self._llm_core and hasattr(self._llm_core, "_adapter") and hasattr(self._llm_core, "_model"):
            _use_router = hasattr(self._llm_core._adapter, "_router")
            _model_str = self._llm_core._model if _use_router else self._llm_core._get_model_string()

            async def _call_via_core(prompt: str) -> str:
                kwargs: dict[str, Any] = {
                    "model": _model_str,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                }
                if not _use_router:
                    if getattr(self._llm_core, "_api_base", None):
                        kwargs["api_base"] = self._llm_core._api_base
                    if getattr(self._llm_core, "_api_key", None):
                        kwargs["api_key"] = self._llm_core._api_key
                response = await self._llm_core._adapter.completion(**kwargs)
                return response.text or ""

            return _call_via_core

        return None

    # ------------------------------------------------------------------
    # 压缩模型窗口获取
    # ------------------------------------------------------------------

    def _get_compression_window(self, context_window: int) -> int | None:
        """获取压缩模型的 context_window。"""
        if not self._compression_model_id:
            return None
        try:
            from config.models import get_model_config_loader  # noqa: PLC0415

            loader = get_model_config_loader()
            conf = loader.get_llm_core_config(self._compression_model_id)
            if conf and conf.get("context_window"):
                return conf["context_window"]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 窗口变更检测与清理
    # ------------------------------------------------------------------

    async def clean_if_window_changed(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
    ) -> list[dict[str, Any]] | None:
        """检测 context_window 是否变化，变化时清理旧压缩摘要。"""
        if not self._chunk_service or not self._pipeline_id:
            return None

        try:
            chunks = await self._chunk_service.find_by_pipeline(
                self._pipeline_id,
                "L1",
            )
        except Exception:
            return None

        if not chunks:
            return None

        latest_chunk = max(chunks, key=lambda c: c.sequence_end)
        chunk_window = latest_chunk.context_window

        if not chunk_window or chunk_window == context_window:
            return None

        cleaned = [
            m
            for m in messages
            if not (
                m.get("role") == "system"
                and (
                    str(m.get("content", "")).startswith("## 历史对话压缩摘要")
                    or str(m.get("content", "")) == _COMPRESSION_NOTICE
                )
            )
        ]

        if len(cleaned) == len(messages):
            return None

        logger.info(
            "[MemoryContextService] context_window 变更: %d → %d, 清理 %d 条旧压缩摘要",
            chunk_window,
            context_window,
            len(messages) - len(cleaned),
        )
        return cleaned

    # ------------------------------------------------------------------
    # 压缩内容构建
    # ------------------------------------------------------------------

    async def _build_compression_content(
        self,
        old_msgs: list[dict[str, Any]],
        context_window: int,
        budgets: dict[str, int],
        state_snapshot: str,
        recent_process_blocks: str,
    ) -> dict[str, Any] | None:
        """压缩旧消息，返回 L1/L2/keywords/state_snapshot/memory_items。

        Returns:
            {"l1": str, "l2": str, "keywords": list,
             "state_snapshot": dict, "memory_items": dict} 或 None
        """
        if not self._llm_call_fn:
            return None

        self._compressor.set_llm_call_fn(self._llm_call_fn)

        try:
            result = await self._compressor.compress_all(
                old_msgs,
                state_snapshot=state_snapshot,
                recent_process_blocks=recent_process_blocks,
            )
        except Exception as exc:
            logger.warning("[MemoryContextService] 压缩失败: %s", exc)
            return None

        # compress_all 失败时返回 None（LLM 空响应/JSON 解析失败），跳过保存
        if result is None:
            return None

        l1 = result.get("l1", "")
        l2 = result.get("l2", "")
        kw = result.get("keywords", [])
        ss = result.get("state_snapshot", {})
        mi = result.get("memory_items", {})

        if not l1:
            return None

        logger.info(
            "[MemoryContextService] 压缩完成: L1≈%d字符 L2≈%d字符 keywords=%d state_snapshot=%d字段",
            len(l1),
            len(l2),
            len(kw),
            sum(1 for v in ss.values() if v) if isinstance(ss, dict) else 0,
        )
        return {"l1": l1, "l2": l2, "keywords": kw, "state_snapshot": ss, "memory_items": mi}

    # ------------------------------------------------------------------
    # 预算切分辅助
    # ------------------------------------------------------------------

    def _find_split_by_budget(
        self,
        messages: list[dict[str, Any]],
        token_budget: int,
    ) -> int:
        """从尾部向前累加 token，找到预算内的切分点。

        返回 split_idx:
          messages[:split_idx] → 待压缩
          messages[split_idx:] → 保留（在预算内）
        """
        accumulated = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = self._estimate_msg_tokens(messages[i])
            if accumulated + msg_tokens > token_budget:
                return i + 1
            accumulated += msg_tokens
        return 0

    def _estimate_msg_tokens(self, msg: dict[str, Any]) -> int:
        """估算单条消息的 token 数（简化版）。"""
        content = str(msg.get("content", ""))
        tokens = max(1, len(content) // 2) if content else 0
        for tc in msg.get("tool_calls", []):
            args = tc.get("function", {}).get("arguments", "")
            if args:
                tokens += max(1, len(args) // 2)
        return tokens

    @staticmethod
    def _split_preserving_tool_pairs(
        messages: list[dict[str, Any]],
        split_idx: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按 split_idx 分割消息列表，保证 tool call/result 配对完整。"""
        old_msgs = list(messages[:split_idx])
        recent_msgs = list(messages[split_idx:])

        recent_tool_ids: set[str] = set()
        for msg in recent_msgs:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    recent_tool_ids.add(tc_id)

        if not recent_tool_ids:
            return old_msgs, recent_msgs

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
