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
from typing import Any, Callable, Awaitable

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
        _token_estimate_fn: token 估算函数
    """

    _MAX_COMPRESS_ROUNDS = 2

    def __init__(
        self,
        compressor: ContextCompressor | None = None,
        config: dict[str, Any] | None = None,
        token_estimate_fn: Callable[[str], int] | None = None,
        llm_call_fn: LLMCallFn | None = None,
    ) -> None:
        """初始化记忆上下文服务。

        Args:
            compressor: 上下文压缩器（可选）
            config: 服务配置，需包含 context_window, compress_trigger_ratio, budgets
            token_estimate_fn: token 估算函数
            llm_call_fn: LLM 调用函数（可选，支持后续通过 set_llm_call_fn 延迟注入）
        """
        compression_config = CompressionConfig.from_yaml_config(
            config.get("context_window", 128000) if config else 128000,
        )
        self._compressor = compressor or ContextCompressor(config=compression_config)
        self._config = config or {"context_window": 128000, "compress_trigger_ratio": 0.5}
        self._token_estimate_fn = token_estimate_fn or self._default_token_estimate
        self._llm_call_fn: LLMCallFn | None = llm_call_fn

        # 内存存储：{session_id: {"L0": [messages], "L1": str, "L2": str}}
        self._layers: dict[str, dict[str, Any]] = {}

        # 父执行记录 ID（用于上下文隔离）
        self.parent_record_id: str | None = None

        self._validate_config()

        logger.debug(
            "[MemoryContextService] 初始化完成 | "
            "context_window=%s",
            self._config.get("context_window"),
        )

    def _validate_config(self) -> None:
        """验证配置完整性。"""
        required_keys = ["context_window", "compress_trigger_ratio"]
        for key in required_keys:
            if key not in self._config:
                raise KeyError(f"配置缺失: {key}")

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

    @staticmethod
    def _default_token_estimate(text: str) -> int:
        """默认 token 估算（简化版）。

        Args:
            text: 文本

        Returns:
            估算的 token 数
        """
        return max(1, len(text) // 2) if text else 0

    def _get_session_data(self, session_id: str) -> dict[str, Any]:
        """获取会话数据。

        Args:
            session_id: 会话 ID

        Returns:
            会话数据字典
        """
        if session_id not in self._layers:
            self._layers[session_id] = {
                "L0": [],
                "L1": "",
                "L2": "",
            }
        return self._layers[session_id]

    async def add_message(
        self,
        session_id: str,
        message: dict[str, Any],
        parent_record_id: str | None = None,
    ) -> None:
        """写流程：添加消息，按需压缩保存。

        Args:
            session_id: 会话 ID
            message: 消息字典
            parent_record_id: 父执行记录 ID
        """
        data = self._get_session_data(session_id)

        # 1. 追加消息到 L0
        data["L0"].append(message)

        # 2. 检查总 token
        total_tokens = self._get_total_tokens(session_id)
        context_window = self._config["context_window"]
        trigger_ratio = self._config["compress_trigger_ratio"]
        trigger_threshold = int(context_window * trigger_ratio)

        # 3. 如果超过阈值，触发递进压缩
        if total_tokens > trigger_threshold:
            logger.info(
                "[MemoryContextService] 触发压缩 | "
                "total=%d, threshold=%d",
                total_tokens, trigger_threshold,
            )
            await self._compress_and_save(session_id)

    async def _compress_and_save(self, session_id: str) -> None:
        """执行递进压缩并保存结果。

        Args:
            session_id: 会话 ID
        """
        if not self._llm_call_fn:
            logger.warning(
                "[MemoryContextService] 跳过压缩：未提供 LLM 调用函数，"
                "请通过 set_llm_call_fn() 注入或初始化时传入 llm_call_fn 参数",
            )
            return

        data = self._get_session_data(session_id)

        l0_content = self._format_messages_to_string(data["L0"])
        l1_content = data.get("L1", "")
        l2_content = data.get("L2", "")

        # 计算预算
        budgets = self._calculate_budgets()

        try:
            new_l1, new_l2 = await self._compressor.progressive_compress(
                l0=l0_content,
                l1=l1_content,
                l2=l2_content,
                budgets=budgets,
            )

            # 保存压缩结果
            data["L0"] = []
            data["L1"] = new_l1
            data["L2"] = new_l2

            logger.info(
                "[MemoryContextService] 压缩完成 | "
                "L1≈%d字符, L2≈%d字符",
                len(new_l1), len(new_l2),
            )
        except Exception as e:
            logger.warning("[MemoryContextService] 压缩失败: %s，保留原文", e)

    def _calculate_budgets(self) -> dict[str, int]:
        """计算各层预算。

        Returns:
            各层 token 预算字典
        """
        context_window = self._config["context_window"]
        budgets_config = self._config.get("budgets", {"l1": 0.15, "l2": 0.05})

        return {
            "L1": int(context_window * budgets_config.get("l1", 0.15)),
            "L2": int(context_window * budgets_config.get("l2", 0.05)),
        }

    # ------------------------------------------------------------------
    # 预算驱动的完整压缩流程（供 context_window_guard 调用）
    # ------------------------------------------------------------------

    async def compress_messages(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        trigger_ratio: float = 0.6,
        previous_l1: str = "",
        save_chunk_fn: Callable[..., Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]] | None:
        """预算驱动的完整压缩流程。

        按CompressionConfig的recent_ratio计算recent预算，从尾部向前切分。
        压缩超出预算的旧消息为L1/L2/keywords，超预算逐级降级。
        支持多轮压缩验证。

        Args:
            messages: 完整消息列表
            context_window: 模型上下文窗口大小
            trigger_ratio: 触发压缩的比例
            previous_l1: 前次压缩的 L1 摘要（增量压缩背景）
            save_chunk_fn: 压缩块持久化回调（可选）

        Returns:
            压缩后的消息列表，无需压缩或失败返回 None
        """
        if not self._llm_call_fn:
            logger.warning("[MemoryContextService] 跳过压缩：未提供 LLM 调用函数")
            return None

        config = CompressionConfig.from_yaml_config(context_window)
        budgets = config.get_budgets()
        trigger_tokens = int(context_window * trigger_ratio)

        current_messages = messages
        compressed = None

        for round_idx in range(self._MAX_COMPRESS_ROUNDS):
            compressed = await self._do_compress_round(
                current_messages, context_window, budgets, previous_l1, save_chunk_fn,
            )
            if compressed is None:
                break

            total_tokens = sum(self._estimate_msg_tokens(m) for m in compressed)
            logger.info(
                "[MemoryContextService] 第 %d 轮压缩: %d -> %d 条, %d tokens (触发线 %d)",
                round_idx + 1, len(current_messages), len(compressed),
                total_tokens, trigger_tokens,
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
        previous_l1: str,
        save_chunk_fn: Callable[..., Awaitable[None]] | None,
    ) -> list[dict[str, Any]] | None:
        """执行一轮预算驱动的压缩。

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

        if not other_msgs:
            return None

        # 按 token 预算从尾部向前计算切分点
        recent_budget = budgets["recent"]
        split_idx = self._find_split_by_budget(other_msgs, recent_budget)
        if split_idx <= 0:
            return None

        # 保证工具调用配对完整
        old_msgs, recent_msgs = self._split_preserving_tool_pairs(
            other_msgs, split_idx,
        )

        if not old_msgs:
            return None

        recent_tokens = sum(self._estimate_msg_tokens(m) for m in recent_msgs)
        logger.info(
            "[MemoryContextService] 预算切分: recent=%d条/%dtokens (预算%d), "
            "old=%d条, existing_blocks=%d",
            len(recent_msgs), recent_tokens, recent_budget, len(old_msgs),
            len(old_blocks) // 2,
        )

        # 按压缩模型上下文窗口比例计算分片数
        # 每片大小不超过 context_window * batch_ratio，超出则均分
        old_tokens = sum(self._estimate_msg_tokens(m) for m in old_msgs)
        batch_ratio = 0.5
        batch_budget = int(context_window * batch_ratio)
        num_batches = max(1, -(-old_tokens // batch_budget))  # ceil division

        any_success = False
        current_previous_l1 = previous_l1

        for batch_idx in range(num_batches):
            start = batch_idx * len(old_msgs) // num_batches
            end = (batch_idx + 1) * len(old_msgs) // num_batches
            batch = old_msgs[start:end]
            if not batch:
                continue

            logger.info(
                "[MemoryContextService] 分批压缩 %d/%d: %d 条消息",
                batch_idx + 1, num_batches, len(batch),
            )

            comp_result = await self._build_compression_content(
                batch, context_window, budgets, current_previous_l1,
            )
            if not comp_result:
                logger.warning(
                    "[MemoryContextService] 第 %d 批压缩失败", batch_idx + 1,
                )
                continue

            if save_chunk_fn:
                try:
                    updated_bg = await save_chunk_fn(batch, comp_result)
                    if updated_bg:
                        current_previous_l1 = updated_bg
                except Exception as exc:
                    logger.warning("[MemoryContextService] 保存压缩块失败: %s", exc)

            any_success = True

        if not any_success:
            return None

        return pure_system_msgs + recent_msgs

    def _downgrade_blocks_if_needed(
        self,
        blocks: list[dict[str, Any]],
        l1_budget: int,
        l2_budget: int,
    ) -> list[dict[str, Any]]:
        """所有压缩块总量超 L1 预算时，从最老的块开始降级。

        降级链：L1 → L2 → keywords
        """
        summary_indices = [
            i for i, b in enumerate(blocks)
            if isinstance(b.get("content"), str)
            and b["content"].startswith("## 历史对话压缩摘要")
        ]
        if not summary_indices:
            return blocks

        total_tokens = sum(self._estimate_msg_tokens(blocks[i]) for i in summary_indices)
        if total_tokens <= l1_budget:
            return blocks

        logger.info(
            "[MemoryContextService] 压缩块总量超 L1 预算: %d > %d, 开始降级",
            total_tokens, l1_budget,
        )

        # 阶段 1：从最老 summary 开始 L1 → L2
        for idx in summary_indices:
            if total_tokens <= l1_budget:
                break
            block = blocks[idx]
            l2_content = block.get("_l2", "")
            if not l2_content:
                continue
            old_tokens = self._estimate_msg_tokens(block)
            blocks[idx] = {
                **{k: v for k, v in block.items() if k != "_l2"},
                "content": f"## 历史对话压缩摘要（L2）\n\n{l2_content}",
            }
            total_tokens -= old_tokens - self._estimate_msg_tokens(blocks[idx])

        if total_tokens <= l1_budget:
            return blocks

        # 阶段 2：从最老开始 L2 → keywords
        for idx in summary_indices:
            if total_tokens <= l1_budget:
                break
            block = blocks[idx]
            keywords = block.get("_keywords", [])
            if not keywords:
                continue
            old_tokens = self._estimate_msg_tokens(block)
            blocks[idx] = {
                **{k: v for k, v in block.items() if k != "_keywords"},
                "content": "## 历史对话压缩摘要（关键词）\n\n关键词: " + ", ".join(keywords),
            }
            total_tokens -= old_tokens - self._estimate_msg_tokens(blocks[idx])

        return blocks

    async def _build_compression_content(
        self,
        old_msgs: list[dict[str, Any]],
        context_window: int,
        budgets: dict[str, int],
        previous_l1: str,
    ) -> dict[str, Any] | None:
        """压缩旧消息，返回 L1/L2/keywords 三元组。

        Returns:
            {"l1": str, "l2": str, "keywords": list} 或 None
        """
        if not self._llm_call_fn:
            return None

        self._compressor.set_llm_call_fn(self._llm_call_fn)

        try:
            result = await self._compressor.compress_all(
                old_msgs, previous_l1=previous_l1,
            )
        except Exception as exc:
            logger.warning("[MemoryContextService] 压缩失败: %s", exc)
            return None

        l1 = result.get("l1", "")
        l2 = result.get("l2", "")
        kw = result.get("keywords", [])

        if not l1:
            return None

        logger.info(
            "[MemoryContextService] 压缩完成: L1≈%d字符 L2≈%d字符 keywords=%d",
            len(l1), len(l2), len(kw),
        )
        return {"l1": l1, "l2": l2, "keywords": kw}

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
        """估算单条消息的 token 数。"""
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

    def _estimate_text_tokens(self, text: str) -> int:
        """估算文本的 token 数。"""
        return self._token_estimate_fn(text) if text else 0

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

    def _format_messages_to_string(self, messages: list[dict[str, Any]]) -> str:
        """将消息列表格式化为字符串。

        Args:
            messages: 消息列表

        Returns:
            格式化后的字符串
        """
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                lines.append(f"[{role.upper()}]\n{content}")
        return "\n\n".join(lines)

    def _get_total_tokens(self, session_id: str) -> int:
        """获取会话总 token 数。

        Args:
            session_id: 会话 ID

        Returns:
            总 token 数
        """
        data = self._get_session_data(session_id)
        total = 0

        # L0
        for msg in data.get("L0", []):
            total += self._token_estimate_fn(msg.get("content", ""))

        # L1
        l1 = data.get("L1", "")
        if l1:
            total += self._token_estimate_fn(l1)

        # L2
        l2 = data.get("L2", "")
        if l2:
            total += self._token_estimate_fn(l2)

        return total

    async def get_context_prompt(self, session_id: str) -> str:
        """获取上下文提示词（读流程）。

        拼接各层内容返回完整提示词。

        Args:
            session_id: 会话 ID

        Returns:
            上下文提示词
        """
        data = self._get_session_data(session_id)
        parts: list[str] = []

        l2 = data.get("L2", "")
        if l2:
            parts.append(f"## 历史摘要\n\n{l2}")

        l1 = data.get("L1", "")
        if l1:
            parts.append(f"## 详细历史\n\n{l1}")

        l0_messages = data.get("L0", [])
        if l0_messages:
            recent = self._format_messages_to_string(l0_messages)
            parts.append(recent)

        return "\n\n".join(parts)

    async def get_memory_stats(
        self,
        session_id: str,
        parent_record_id: str | None = None,
    ) -> dict[str, Any]:
        """获取记忆统计信息。

        Args:
            session_id: 会话 ID
            parent_record_id: 父执行记录 ID

        Returns:
            统计信息字典
        """
        data = self._get_session_data(session_id)
        total_tokens = self._get_total_tokens(session_id)
        context_window = self._config["context_window"]

        l0_messages = data.get("L0", [])
        l0_tokens = sum(
            self._token_estimate_fn(msg.get("content", ""))
            for msg in l0_messages
        )

        l1_content = data.get("L1", "")
        l1_tokens = self._token_estimate_fn(l1_content)

        l2_content = data.get("L2", "")
        l2_tokens = self._token_estimate_fn(l2_content)

        return {
            "session_id": session_id,
            "context_window": context_window,
            "total_tokens": total_tokens,
            "usage_ratio": total_tokens / context_window if context_window > 0 else 0,
            "parent_record_id": parent_record_id or self.parent_record_id,
            "layers": {
                "L0": {"tokens": l0_tokens, "messages_count": len(l0_messages)},
                "L1": {"tokens": l1_tokens},
                "L2": {"tokens": l2_tokens},
            },
        }

    async def clear_memory(
        self,
        session_id: str,
        parent_record_id: str | None = None,
    ) -> None:
        """清空会话记忆。

        Args:
            session_id: 会话 ID
            parent_record_id: 父执行记录 ID
        """
        if session_id in self._layers:
            del self._layers[session_id]

        logger.info(
            "[MemoryContextService] 记忆已清空 | session_id=%s",
            session_id,
        )
