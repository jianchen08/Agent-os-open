"""对话历史缓冲区。

从旧代码 src/memory/history_buffer.py 搬迁。
移除 EmbeddingService/tokenizer 硬依赖和 cachetools 依赖，
向量搜索降级为可选（需要 embedding_service 时才启用）。

暴露接口：
- MessageEntry: 消息条目
- HistoryBuffer: 对话历史缓冲区
- ConversationHistory: 对话历史管理器
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any, Callable, Awaitable

from memory.constants import HistoryConfig

logger = logging.getLogger(__name__)


def _get_env_int(key: str, default: int) -> int:
    """从环境变量读取整数值，失败时回退到默认值。

    Args:
        key: 环境变量名
        default: 默认值

    Returns:
        环境变量解析后的整数值，或默认值
    """
    value = os.environ.get(key, "")
    if not value:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        logger.warning("环境变量 %s='%s' 不是有效整数，使用默认值 %d", key, value, default)
        return default


class MessageEntry:
    """消息条目。

    Attributes:
        id: 消息唯一标识
        message: 消息字典
        embedding: 消息向量嵌入
        timestamp: 时间戳
    """

    def __init__(
        self,
        message: dict[str, Any],
        embedding: list[float] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """初始化消息条目。

        Args:
            message: 消息字典
            embedding: 向量嵌入
            timestamp: 时间戳
        """
        self.id = str(uuid.uuid4())
        self.message = message
        self.embedding = embedding
        self.timestamp = timestamp or datetime.now(UTC)


class HistoryBuffer:
    """对话历史缓冲区。

    提供对话历史的存储、检索和向量搜索功能。
    不依赖外部 token 计数器，使用简化估算。

    Attributes:
        max_size: 最大消息数
        enable_vector_search: 是否启用向量搜索
        _messages: 消息存储
        _vector_index: 向量索引
        _embedding_fn: 嵌入计算函数
    """

    def __init__(
        self,
        max_size: int | None = None,
        enable_vector_search: bool = True,
        embedding_fn: Callable[[str], Awaitable[list[float]]] | None = None,
    ) -> None:
        """初始化历史缓冲区。

        Args:
            max_size: 最大消息数量，None 时从环境变量或常量读取默认值
            enable_vector_search: 是否启用向量搜索
            embedding_fn: 异步嵌入计算函数
        """
        if max_size is None:
            max_size = _get_env_int(
                HistoryConfig.ENV_KEY_MAX_SIZE, HistoryConfig.DEFAULT_MAX_SIZE,
            )
        self.max_size = max_size
        self.enable_vector_search = enable_vector_search
        self._embedding_fn = embedding_fn

        self._messages: deque[MessageEntry] = deque(maxlen=max_size)
        self._vector_index: dict[str, MessageEntry] = {}

    async def add_message(
        self, message: dict[str, Any], compute_embedding: bool = True,
    ) -> str:
        """添加消息。

        Args:
            message: 消息字典
            compute_embedding: 是否计算嵌入

        Returns:
            消息 ID
        """
        embedding = None
        if compute_embedding and self.enable_vector_search and self._embedding_fn:
            content = message.get("content", "")
            if content:
                try:
                    embedding = await self._embedding_fn(content)
                except Exception:
                    pass

        entry = MessageEntry(message, embedding)
        self._messages.append(entry)

        if embedding is not None:
            self._vector_index[entry.id] = entry

        return entry.id

    async def add_messages(self, messages: list[dict[str, Any]]) -> list[str]:
        """批量添加消息。

        Args:
            messages: 消息列表

        Returns:
            消息 ID 列表
        """
        ids: list[str] = []
        for msg in messages:
            msg_id = await self.add_message(msg)
            ids.append(msg_id)
        return ids

    def get_recent(
        self, n: int = 10, include_tool: bool = False,
    ) -> list[dict[str, Any]]:
        """获取最近的消息。

        Args:
            n: 获取数量
            include_tool: 是否包含工具消息

        Returns:
            消息字典列表
        """
        recent_entries = list(self._messages)[-n:]

        if not include_tool:
            recent_entries = [
                e for e in recent_entries if e.message.get("role") != "tool"
            ]

        return [e.message for e in recent_entries]

    def get_old_messages(
        self, skip_recent: int = 10, include_tool: bool = False,
    ) -> list[dict[str, Any]]:
        """获取旧消息（除了最近的消息）。

        Args:
            skip_recent: 跳过最近的消息数
            include_tool: 是否包含工具消息

        Returns:
            消息字典列表
        """
        if len(self._messages) <= skip_recent:
            return []

        old_entries = list(self._messages)[:-skip_recent]

        if not include_tool:
            old_entries = [e for e in old_entries if e.message.get("role") != "tool"]

        return [e.message for e in old_entries]

    async def retrieve_relevant(
        self,
        query: str,
        top_k: int = 3,
        min_similarity: float = 0.5,
        filter_roles: list[str] | None = None,
    ) -> list[str]:
        """检索相关历史消息。

        Args:
            query: 查询文本
            top_k: 返回数量
            min_similarity: 最小相似度
            filter_roles: 角色过滤列表

        Returns:
            格式化的相关消息列表
        """
        if not self.enable_vector_search or not self._embedding_fn or not self._vector_index:
            return []

        try:
            query_embedding = await self._embedding_fn(query)

            entries = list(self._vector_index.values())
            if filter_roles:
                entries = [e for e in entries if e.message.get("role") in filter_roles]

            vectors = [e.embedding for e in entries if e.embedding is not None]
            valid_entries = [e for e in entries if e.embedding is not None]

            if not vectors:
                return []

            top_results = _batch_cosine_similarity(
                query_embedding, vectors, top_k=top_k, min_similarity=min_similarity,
            )

            results: list[str] = []
            for idx, _ in top_results:
                entry = valid_entries[idx]
                content = entry.message.get("content", "")
                role = entry.message.get("role", "")
                results.append(f"[{role.upper()}] {content}")

            return results

        except Exception:
            return []

    def get_total_tokens(self) -> int:
        """获取所有消息的总 token 数（简化估算）。

        Returns:
            估算的总 token 数
        """
        total = 0
        for entry in self._messages:
            content = entry.message.get("content", "")
            total += max(1, len(content) // 2) if content else 0
        return total

    def get_message_count(self) -> int:
        """获取消息数量。

        Returns:
            消息数量
        """
        return len(self._messages)

    def clear(self) -> None:
        """清空缓冲区。"""
        self._messages.clear()
        self._vector_index.clear()

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。

        Returns:
            统计信息字典
        """
        messages = [e.message for e in self._messages]

        role_counts: dict[str, int] = {}
        for msg in messages:
            role = msg.get("role", "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1

        return {
            "total_messages": len(messages),
            "total_tokens": self.get_total_tokens(),
            "vector_index_size": len(self._vector_index),
            "role_counts": role_counts,
            "max_size": self.max_size,
            "usage_percent": len(messages) / self.max_size * 100 if self.max_size > 0 else 0,
        }


class ConversationHistory:
    """对话历史管理器（高级封装）。

    提供更高级的对话历史管理功能。

    Attributes:
        buffer: 底层历史缓冲区
        max_tokens: 最大 token 数
    """

    def __init__(
        self,
        max_tokens: int | None = None,
        max_messages: int | None = None,
        embedding_fn: Callable[[str], Awaitable[list[float]]] | None = None,
    ) -> None:
        """初始化对话历史管理器。

        Args:
            max_tokens: 最大 token 数，None 时从环境变量或常量读取默认值
            max_messages: 最大消息数，None 时从环境变量或常量读取默认值
            embedding_fn: 异步嵌入计算函数
        """
        if max_messages is None:
            max_messages = _get_env_int(
                HistoryConfig.ENV_KEY_MAX_MESSAGES,
                HistoryConfig.DEFAULT_MAX_MESSAGES,
            )
        if max_tokens is None:
            max_tokens = _get_env_int(
                HistoryConfig.ENV_KEY_MAX_TOKENS,
                HistoryConfig.DEFAULT_MAX_TOKENS,
            )
        self.buffer = HistoryBuffer(
            max_size=max_messages, embedding_fn=embedding_fn,
        )
        self.max_tokens = max_tokens

    async def add_message(self, message: dict[str, Any]) -> str:
        """添加消息。

        Args:
            message: 消息字典

        Returns:
            消息 ID
        """
        return await self.buffer.add_message(message)

    async def get_context_for_llm(
        self, user_query: str, recent_count: int = 10, retrieve_count: int = 3,
    ) -> list[dict[str, Any]]:
        """获取用于 LLM 的上下文。

        Args:
            user_query: 用户查询
            recent_count: 最近消息数
            retrieve_count: 检索相关消息数

        Returns:
            上下文消息列表
        """
        recent_messages = self.buffer.get_recent(n=recent_count)
        relevant_history = await self.buffer.retrieve_relevant(
            query=user_query, top_k=retrieve_count,
        )

        if relevant_history:
            context_message = {
                "role": "system",
                "content": "以下是相关的历史对话：\n" + "\n".join(relevant_history),
            }
            return [context_message] + recent_messages
        else:
            return recent_messages

    def get_token_count(self) -> int:
        """获取当前 Token 数。

        Returns:
            当前 Token 数
        """
        return self.buffer.get_total_tokens()

    def is_over_limit(self, threshold: float = 0.5) -> bool:
        """检查是否超过限制。

        Args:
            threshold: 使用比例阈值

        Returns:
            是否超过阈值
        """
        current_tokens = self.get_token_count()
        return current_tokens > self.max_tokens * threshold

    def clear(self) -> None:
        """清空历史。"""
        self.buffer.clear()

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。

        Returns:
            统计信息字典
        """
        stats = self.buffer.get_stats()
        stats["max_tokens"] = self.max_tokens
        stats["token_usage_percent"] = (
            self.get_token_count() / self.max_tokens * 100
            if self.max_tokens > 0 else 0
        )
        return stats


def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """计算余弦相似度。

    优先使用 numpy 加速（约 50-100 倍），不可用时回退纯 Python。

    Args:
        vec1: 向量 1
        vec2: 向量 2

    Returns:
        余弦相似度
    """
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0

    try:
        import numpy as np
        a = np.asarray(vec1, dtype=np.float32)
        b = np.asarray(vec2, dtype=np.float32)
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom == 0:
            return 0.0
        return float(np.dot(a, b) / denom)
    except ImportError:
        dot = sum(v1 * v2 for v1, v2 in zip(vec1, vec2))
        norm1 = sum(v * v for v in vec1) ** 0.5
        norm2 = sum(v * v for v in vec2) ** 0.5
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)


def _batch_cosine_similarity(
    query: list[float], vectors: list[list[float]], top_k: int, min_similarity: float = 0.0,
) -> list[tuple[int, float]]:
    """批量计算余弦相似度并返回 top-k 结果。

    使用 numpy 矩阵运算一次性计算所有相似度，比逐条计算快数十倍。

    Args:
        query: 查询向量
        vectors: 候选向量列表
        top_k: 返回数量
        min_similarity: 最小相似度阈值

    Returns:
        [(原始索引, 相似度), ...] 按相似度降序排列
    """
    if not query or not vectors:
        return []

    try:
        import numpy as np
        q = np.asarray(query, dtype=np.float32)
        mat = np.asarray(vectors, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        mat_norms = np.linalg.norm(mat, axis=1)

        denom = q_norm * mat_norms
        denom[denom == 0] = 1.0
        similarities = np.dot(mat, q) / denom

        mask = similarities >= min_similarity
        indices = np.where(mask)[0]
        valid_sims = similarities[indices]

        top_local = np.argsort(valid_sims)[::-1][:top_k]
        return [(int(indices[i]), float(valid_sims[i])) for i in top_local]
    except ImportError:
        results = []
        for idx, vec in enumerate(vectors):
            sim = _cosine_similarity(query, vec)
            if sim >= min_similarity:
                results.append((idx, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]
