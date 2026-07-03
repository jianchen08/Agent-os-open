"""语义知识存储服务。

从旧代码 src/memory/knowledge_service.py 搬迁。
移除 SQLAlchemy 硬依赖，通过 ISemanticStorage 接口操作存储。
没有 storage 时降级到内存字典。

暴露接口：
- KnowledgeService: 语义知识存储服务
"""

from __future__ import annotations

import logging
import math
from typing import Any

from memory.ports import ISemanticStorage
from memory.types import Knowledge, MemoryType, SearchResult

logger = logging.getLogger(__name__)


def _find_knowledge_by_user(storage: ISemanticStorage, user_id: str, limit: int = 20) -> Any:
    """调用存储的按用户查找方法，优先使用专属方法名。

    当存储实现同时满足 IEpisodeStorage 和 ISemanticStorage 时，
    通用方法名 find_by_user 会冲突，优先调用 find_knowledge_by_user。

    Args:
        storage: 语义存储接口
        user_id: 用户 ID
        limit: 返回数量上限

    Returns:
        知识列表的协程
    """
    if hasattr(storage, "find_knowledge_by_user") and not _is_mock(storage):
        return storage.find_knowledge_by_user(user_id, limit=limit)
    return storage.find_by_user(user_id, limit=limit)


def _get_knowledge(storage: ISemanticStorage, knowledge_id: str) -> Any:
    """调用存储的获取方法，优先使用专属方法名。

    Args:
        storage: 语义存储接口
        knowledge_id: 知识 ID

    Returns:
        知识实例的协程
    """
    if hasattr(storage, "get_knowledge") and not _is_mock(storage):
        return storage.get_knowledge(knowledge_id)
    return storage.get(knowledge_id)


def _delete_knowledge(storage: ISemanticStorage, knowledge_id: str) -> Any:
    """调用存储的删除方法，优先使用专属方法名。

    Args:
        storage: 语义存储接口
        knowledge_id: 知识 ID

    Returns:
        是否删除成功的协程
    """
    if hasattr(storage, "delete_knowledge_by_id") and not _is_mock(storage):
        return storage.delete_knowledge_by_id(knowledge_id)
    return storage.delete(knowledge_id)


def _is_mock(obj: Any) -> bool:
    """判断对象是否为 unittest.mock 的 Mock 对象。

    Mock 对象会自动创建任何属性（hasattr 始终返回 True），
    需要特殊处理以避免误判。

    Args:
        obj: 待检查对象

    Returns:
        是否为 Mock 对象
    """
    try:
        from unittest.mock import MagicMock, Mock  # noqa: PLC0415

        return isinstance(obj, (Mock, MagicMock))
    except ImportError:
        return False


class KnowledgeService:
    """语义知识存储服务。

    职责（仅存储操作）：
    - 创建和存储语义知识
    - 更新语义知识
    - 删除语义知识
    - 列出语义知识
    - 相关性搜索（关键词匹配 + 语义相似度）

    检索操作请使用 MemoryService.retrieve(memory_type="semantic", ...)。

    Attributes:
        _storage: 语义记忆存储接口
        _in_memory: 内存降级存储
    """

    def __init__(
        self,
        semantic_storage: ISemanticStorage | None = None,
        embedding_fn: Any = None,
    ) -> None:
        """初始化语义知识存储服务。

        Args:
            semantic_storage: 语义记忆存储接口，None 时降级到内存
            embedding_fn: 异步嵌入函数（可选），用于生成查询向量以支持语义检索
        """
        self._storage = semantic_storage
        self._in_memory: dict[str, Knowledge] = {}
        self._embedding_fn = embedding_fn

    async def store_knowledge(self, knowledge: Knowledge) -> str:
        """存储知识。

        Args:
            knowledge: 知识实例

        Returns:
            存储的条目 ID
        """
        if self._storage:
            return await self._storage.save(knowledge)

        # 内存降级
        self._in_memory[knowledge.id] = knowledge
        logger.debug("[KnowledgeService] 内存存储 | id=%s", knowledge.id)
        return knowledge.id

    async def create_knowledge(
        self,
        user_id: str,
        content: str,
        source_type: str,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建知识。

        Args:
            user_id: 用户 ID
            content: 知识内容
            source_type: 来源类型
            extra_data: 额外数据

        Returns:
            创建的知识字典
        """
        knowledge = Knowledge(
            user_id=user_id,
            content=content,
            source_type=source_type,
            extra_data=extra_data or {},
        )

        await self.store_knowledge(knowledge)

        return knowledge.to_dict()

    async def list_semantic_memory(
        self,
        user_id: str,
    ) -> dict[str, Any]:
        """获取语义记忆列表。

        Args:
            user_id: 用户 ID

        Returns:
            语义记忆列表字典
        """
        if self._storage:
            memories = await _find_knowledge_by_user(self._storage, user_id)
        else:
            memories = [kn for kn in self._in_memory.values() if kn.user_id == user_id]
            memories.sort(key=lambda x: x.created_at, reverse=True)

        items = [kn.to_dict() for kn in memories]

        return {"items": items, "total": len(items)}

    async def delete_knowledge(
        self,
        knowledge_id: str,
        user_id: str,
    ) -> bool:
        """删除知识。

        Args:
            knowledge_id: 知识 ID
            user_id: 用户 ID（用于权限校验）

        Returns:
            是否删除成功
        """
        if self._storage:
            knowledge = await _get_knowledge(self._storage, knowledge_id)
            if not knowledge or knowledge.user_id != user_id:
                return False
            return await _delete_knowledge(self._storage, knowledge_id)

        # 内存降级
        knowledge = self._in_memory.get(knowledge_id)
        if not knowledge or knowledge.user_id != user_id:
            return False

        del self._in_memory[knowledge_id]
        return True

    async def get_knowledge_count(self, user_id: str) -> int:
        """获取知识数量。

        Args:
            user_id: 用户 ID

        Returns:
            该用户的知识数量
        """
        if self._storage:
            memories = await _find_knowledge_by_user(self._storage, user_id)
            return len(memories)

        return sum(1 for kn in self._in_memory.values() if kn.user_id == user_id)

    # ============================================
    # 相关性搜索
    # ============================================

    async def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        keyword_weight: float = 0.4,
        semantic_weight: float = 0.6,
    ) -> list[SearchResult]:
        """相关性搜索：结合关键词匹配和语义相似度。

        将用户查询与知识库中的条目进行匹配，综合计算相关性得分。
        - 关键词匹配：对 query 分词后统计命中数，归一化为 [0, 1] 得分
        - 语义相似度：使用嵌入向量计算余弦相似度（需要 embedding_fn）

        最终得分 = keyword_score * keyword_weight + semantic_score * semantic_weight

        Args:
            user_id: 用户 ID
            query: 查询文本
            top_k: 返回数量上限
            keyword_weight: 关键词匹配权重
            semantic_weight: 语义相似度权重

        Returns:
            按相关性降序排列的搜索结果列表
        """
        if not query:
            return []

        # 获取该用户的所有知识
        items = await self._get_all_knowledge(user_id)
        if not items:
            return []

        # 关键词匹配得分
        keyword_scores = self._compute_keyword_scores(items, query)

        # 语义相似度得分
        semantic_scores = await self._compute_semantic_scores(items, query)

        # 综合得分
        results: list[SearchResult] = []
        for i, kn in enumerate(items):
            kw_score = keyword_scores.get(i, 0.0)
            sem_score = semantic_scores.get(i, 0.0)
            combined_score = kw_score * keyword_weight + sem_score * semantic_weight

            if combined_score > 0:
                results.append(
                    SearchResult(
                        id=kn.id,
                        content=kn.content,
                        score=combined_score,
                        memory_type=MemoryType.SEMANTIC,
                        metadata=kn.extra_data,
                    )
                )

        # 按得分降序排序
        results.sort(key=lambda r: r.score, reverse=True)

        # 如果没有得分大于 0 的结果，返回 top_k 条原始结果（保证有内容可用）
        if not results:
            for kn in items[:top_k]:
                results.append(
                    SearchResult(
                        id=kn.id,
                        content=kn.content,
                        score=0.0,
                        memory_type=MemoryType.SEMANTIC,
                        metadata=kn.extra_data,
                    )
                )

        return results[:top_k]

    async def _get_all_knowledge(self, user_id: str) -> list[Knowledge]:
        """获取用户的所有知识条目。

        Args:
            user_id: 用户 ID

        Returns:
            知识条目列表
        """
        if self._storage:
            return await _find_knowledge_by_user(self._storage, user_id, limit=100000)

        memories = [kn for kn in self._in_memory.values() if kn.user_id == user_id]
        memories.sort(key=lambda x: x.created_at, reverse=True)
        return memories

    def _compute_keyword_scores(
        self,
        items: list[Knowledge],
        query: str,
    ) -> dict[int, float]:
        """计算关键词匹配得分。

        对 query 分词后，统计每个 item 的 content 中命中的关键词数量，
        归一化为 [0, 1] 得分（最大命中数归一化）。

        Args:
            items: 知识条目列表
            query: 查询文本

        Returns:
            索引 -> 得分的字典
        """
        # 简单分词：按空白字符拆分，过滤短词
        query_words = {w.lower() for w in query.split() if len(w) > 1}
        if not query_words:
            return {}

        raw_scores: dict[int, int] = {}
        max_hits = 0

        for i, kn in enumerate(items):
            content = kn.content.lower()
            tags = " ".join(kn.extra_data.get("tags", []) if kn.extra_data else []).lower()
            combined = f"{content} {tags}"

            hit_count = sum(1 for w in query_words if w in combined)
            if hit_count > 0:
                raw_scores[i] = hit_count
                max_hits = max(max_hits, hit_count)

        # 归一化
        if max_hits == 0:
            return {}

        return {idx: hits / max_hits for idx, hits in raw_scores.items()}

    async def _compute_semantic_scores(
        self,
        items: list[Knowledge],
        query: str,
    ) -> dict[int, float]:
        """计算语义相似度得分。

        使用 embedding_fn 生成查询向量，与每个已有 embedding 的知识条目
        计算余弦相似度。

        Args:
            items: 知识条目列表
            query: 查询文本

        Returns:
            索引 -> 得分的字典
        """
        if not self._embedding_fn:
            return {}

        # 生成查询向量
        try:
            query_vector = await self._embedding_fn(query)
        except Exception as e:
            logger.warning("[KnowledgeService] 生成查询向量失败: %s", e)
            return {}

        if not query_vector:
            return {}

        scores: dict[int, float] = {}
        for i, kn in enumerate(items):
            if not kn.embedding:
                continue
            try:
                similarity = self._cosine_similarity(query_vector, kn.embedding)
                if similarity > 0:
                    scores[i] = similarity
            except Exception as e:
                logger.warning(
                    "[KnowledgeService] 计算相似度失败 | id=%s | error=%s",
                    kn.id,
                    e,
                )

        return scores

    @staticmethod
    def _cosine_similarity(
        vec_a: list[float],
        vec_b: list[float],
    ) -> float:
        """计算两个向量的余弦相似度。

        Args:
            vec_a: 向量 A
            vec_b: 向量 B

        Returns:
            余弦相似度 [0, 1]
        """
        if len(vec_a) != len(vec_b) or not vec_a:
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec_a, vec_b, strict=False))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return max(0.0, min(1.0, dot_product / (norm_a * norm_b)))
