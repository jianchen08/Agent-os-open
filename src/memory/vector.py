"""
向量检索模块

包含向量索引和检索相关功能
"""

import hashlib
from datetime import datetime
from typing import Any

import cachetools
import numpy as np

from src.core.tokenizer import get_token_counter


class VectorIndex:
    """
    向量索引

    管理内存中的向量索引，支持 LRU 缓存
    """

    def __init__(self, maxsize: int = 5000):
        """
        初始化向量索引

        Args:
            maxsize: 最大缓存大小
        """
        # 使用 LRU 缓存避免无限增长
        self._vector_cache: cachetools.LRUCache[str, dict[str, Any]] = (
            cachetools.LRUCache(maxsize=maxsize)
        )
        self.token_counter = get_token_counter()

    def add(self, content: str, embedding: list[float]) -> str:
        """
        添加内容到向量索引

        Args:
            content: 内容文本
            embedding: 向量

        Returns:
            缓存键
        """
        # 使用内容哈希作为缓存键，避免重复
        cache_key = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        self._vector_cache[cache_key] = {
            "content": content,
            "embedding": embedding,
            "timestamp": datetime.now().isoformat(),
        }
        return cache_key

    def get(self, cache_key: str) -> dict[str, Any] | None:
        """
        获取向量

        Args:
            cache_key: 缓存键

        Returns:
            向量数据，如果不存在返回 None
        """
        return self._vector_cache.get(cache_key)

    def get_all(self) -> list[dict[str, Any]]:
        """
        获取所有向量

        Returns:
            向量列表
        """
        return list(self._vector_cache.values())

    def clear(self):
        """
        清空向量索引
        """
        self._vector_cache.clear()

    def size(self) -> int:
        """
        获取向量索引大小

        Returns:
            向量数量
        """
        return len(self._vector_cache)

    def get_stats(self) -> dict[str, Any]:
        """
        获取向量索引统计信息

        Returns:
            统计信息
        """
        return {
            "size": len(self._vector_cache),
            "maxsize": self._vector_cache.maxsize,
            "usage_percent": len(self._vector_cache) / self._vector_cache.maxsize * 100
            if self._vector_cache.maxsize > 0
            else 0,
        }


class VectorRetriever:
    """
    向量检索器

    负责向量相似度计算和检索
    """

    def __init__(self, embedding_service: Any):
        """
        初始化向量检索器

        Args:
            embedding_service: 嵌入服务
        """
        self.embedding_service = embedding_service
        self.token_counter = get_token_counter()

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """
        计算余弦相似度

        Args:
            vec1: 向量1
            vec2: 向量2

        Returns:
            相似度值
        """
        try:
            a, b = np.array(vec1), np.array(vec2)
            return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        except Exception:
            return 0.0

    async def retrieve_relevant(
        self,
        query: str,
        vectors: list[dict[str, Any]],
        top_k: int = 5,
        max_tokens: int = 1000,
        tag_network: Any | None = None,
        tag_boost: float = 0.3,
    ) -> list[str]:
        """
        检索与查询相关的内容

        Args:
            query: 查询文本
            vectors: 向量列表
            top_k: 返回数量
            max_tokens: 最大 token 数
            tag_network: Tag 网络（可选）
            tag_boost: Tag 增强因子

        Returns:
            相关内容列表
        """
        if not vectors:
            return []

        try:
            # 1. 生成原始查询向量
            query_embedding = await self.embedding_service.embed_text(query)

            # 2. Tag 网络增强（如果启用）
            if tag_network and tag_boost > 0:
                try:
                    query_np = np.array(query_embedding, dtype=np.float32)
                    boost_result = await tag_network.apply_tag_boost(
                        query_np, tag_boost
                    )
                    # 使用增强后的向量
                    query_embedding = (
                        boost_result.vector.tolist()
                        if hasattr(boost_result.vector, "tolist")
                        else list(boost_result.vector)
                    )
                except Exception:
                    pass

            # 3. 计算相似度并排序
            similarities = []
            for item in vectors:
                sim = self.cosine_similarity(query_embedding, item["embedding"])
                similarities.append((item["content"], sim))

            similarities.sort(key=lambda x: x[1], reverse=True)

            # 4. 返回 top_k，但不超过 max_tokens
            results = []
            total_tokens = 0
            seen_contents = set()  # 去重

            for content, _ in similarities[: top_k * 2]:  # 多取一些用于去重
                if content in seen_contents:
                    continue
                seen_contents.add(content)

                content_tokens = self.token_counter.count_tokens(content)
                if total_tokens + content_tokens <= max_tokens:
                    results.append(content)
                    total_tokens += content_tokens

                if len(results) >= top_k:
                    break

            return results

        except Exception:
            return []

    async def generate_embedding(self, text: str) -> list[float] | None:
        """
        生成向量

        Args:
            text: 文本

        Returns:
            向量，如果失败返回 None
        """
        try:
            return await self.embedding_service.embed_text(text)
        except Exception:
            return None
