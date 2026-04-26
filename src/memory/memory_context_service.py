"""记忆上下文服务。

从旧代码 src/memory/memory_context_service.py 搬迁。
移除 ContextRepository/LLMClient 等硬依赖，
通过注入的接口实现压缩和组装。

暴露接口：
- MemoryContextService: 记忆上下文服务
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from memory.context_compressor import CompressionConfig, ContextCompressor

# LLM 调用函数类型：接收 prompt 字符串，返回响应字符串
LLMCallFn = Callable[[str], Awaitable[str]]

logger = logging.getLogger(__name__)


class MemoryContextService:
    """记忆上下文服务。

    职责：
    - 协调压缩和组装流程
    - 写流程：接收消息 -> 检查阈值 -> 触发压缩 -> 保存
    - 读流程：读取各层 -> 组装 -> 返回提示词
    - 支持按 parent_record_id 隔离上下文

    Attributes:
        _compressor: 上下文压缩器
        _config: 服务配置
        _layers: 内存中的层级数据 {session_id: {layer: content}}
        _token_estimate_fn: token 估算函数
    """

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
        compression_config = CompressionConfig(
            context_window=config.get("context_window", 128000) if config else 128000,
            compress_trigger_ratio=config.get("compress_trigger_ratio", 0.5) if config else 0.5,
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
