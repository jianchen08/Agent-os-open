"""
混合检索器

实现向量检索 + 关键词检索 + TagWave (EPA + 残差金字塔) 检索的混合检索策略
"""

import math
import uuid
from typing import Any

import numpy as np
from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import EpisodesMemory, MemoryChunk, SemanticMemory, Tag, ToolLibrary
from src.memory.types import MemoryType, RetrievalConfig, SearchResult, TagBoostResult


class RRFMerger:
    """
    RRF (Reciprocal Rank Fusion) 融合器

    将多个检索结果列表融合为一个排序列表
    """

    def __init__(self, k: int = 60):
        """
        初始化 RRF 融合器

        Args:
            k: RRF 常数，默认 60
        """
        self.k = k

    def merge(
        self,
        vector_results: list[SearchResult],
        keyword_results: list[SearchResult],
    ) -> list[SearchResult]:
        """
        融合向量检索和关键词检索结果

        Args:
            vector_results: 向量检索结果
            keyword_results: 关键词检索结果

        Returns:
            融合后的结果列表
        """
        # 计算 RRF 得分
        scores: dict[uuid.UUID, float] = {}
        result_map: dict[uuid.UUID, SearchResult] = {}

        # 处理向量检索结果
        for rank, result in enumerate(vector_results):
            rrf_score = 1.0 / (self.k + rank + 1)
            scores[result.id] = scores.get(result.id, 0) + rrf_score
            result_map[result.id] = result

        # 处理关键词检索结果
        for rank, result in enumerate(keyword_results):
            rrf_score = 1.0 / (self.k + rank + 1)
            scores[result.id] = scores.get(result.id, 0) + rrf_score
            if result.id not in result_map:
                result_map[result.id] = result

        # 按 RRF 得分排序
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        # 构建结果列表，更新得分
        results = []
        for result_id in sorted_ids:
            result = result_map[result_id]
            # 归一化得分到 0-1
            normalized_score = min(scores[result_id] * self.k, 1.0)
            results.append(
                SearchResult(
                    id=result.id,
                    content=result.content,
                    score=normalized_score,
                    memory_type=result.memory_type,
                    metadata=result.metadata,
                    highlight=result.highlight,
                )
            )

        return results


class HybridRetriever:
    """
    混合检索器

    支持向量检索、关键词检索、TagWave (EPA + 残差金字塔) 检索及其融合
    """

    def __init__(
        self,
        session: AsyncSession,
        default_config: RetrievalConfig | None = None,
        tag_network: Any | None = None,
    ):
        """
        初始化混合检索器

        Args:
            session: 数据库会话
            default_config: 默认检索配置
            tag_network: Tag 网络检索器（可选，兼容旧接口）
        """
        self.session = session
        self.default_config = default_config or RetrievalConfig()
        self.merger = RRFMerger()
        self.tag_network = tag_network  # 兼容旧接口

        # TagWave 组件
        self._epa_basis: list[np.ndarray] | None = None
        self._epa_mean: np.ndarray | None = None
        self._epa_labels: list[str] | None = None
        self._chunk_vectors: list[dict] | None = None
        self._chunk_matrix: np.ndarray | None = None
        self._tagwave_initialized = False

    def set_tag_network(self, tag_network: Any):
        """设置 Tag 网络检索器（兼容旧接口）"""
        self.tag_network = tag_network

    async def _init_tagwave(self, executor_type: str | None = None, executor_id: str | None = None) -> bool:
        """
        初始化 TagWave 组件（EPA + 残差金字塔）

        基于 VCPToolBox TagMemo "浪潮"算法 V5
        """
        if self._tagwave_initialized:
            return True

        try:
            # 1. 加载 Tags 并计算 EPA 正交基
            from sqlalchemy import select
            result = await self.session.execute(
                select(Tag).where(Tag.vector.isnot(None))
            )
            tags = result.scalars().all()

            if len(tags) >= 8:
                tag_data = []
                for tag in tags:
                    vector = tag.vector if isinstance(tag.vector, list) else tag.vector
                    tag_data.append({
                        'name': tag.name,
                        'vector': np.array(vector, dtype=np.float32),
                        'frequency': tag.frequency or 1
                    })

                # K-Means 聚类
                cluster_data = self._kmeans_clustering(tag_data, min(len(tag_data), 32))

                # 加权 PCA 计算正交基
                U, S, mean_vector, labels = self._weighted_pca(cluster_data)

                # 选择维度（95% 能量）
                total = np.sum(S)
                cumsum = 0.0
                K = len(S)
                for i, s in enumerate(S):
                    cumsum += s
                    if cumsum / total > 0.95:
                        K = max(i + 1, 8)
                        break

                self._epa_basis = U[:K]
                self._epa_mean = mean_vector
                self._epa_labels = labels[:K]

            # 2. 加载 MemoryChunks
            filters = [MemoryChunk.embedding.isnot(None)]
            if executor_type:
                filters.append(MemoryChunk.executor_type == executor_type)
            if executor_id:
                filters.append(MemoryChunk.executor_id == executor_id)

            result = await self.session.execute(
                select(MemoryChunk).where(and_(*filters))
            )
            chunks = result.scalars().all()

            if len(chunks) >= 10:
                self._chunk_vectors = []
                for chunk in chunks:
                    vector = chunk.embedding if isinstance(chunk.embedding, list) else chunk.embedding
                    self._chunk_vectors.append({
                        'id': chunk.id,
                        'content': chunk.content,
                        'vector': np.array(vector, dtype=np.float32),
                        'layer': chunk.layer,
                        'session_id': chunk.session_id,
                        'executor_type': chunk.executor_type,
                        'executor_id': chunk.executor_id
                    })

                self._chunk_matrix = np.stack([c['vector'] for c in self._chunk_vectors])

            self._tagwave_initialized = True
            return True

        except Exception as e:
            print(f"[TagWave] 初始化失败: {e}")
            return False

    def _kmeans_clustering(self, tags: list[dict], k: int) -> dict:
        """K-Means 聚类"""
        import random
        vectors = [t['vector'] for t in tags]
        n = len(vectors)

        indices = random.sample(range(n), min(k, n))
        centroids = [vectors[i].copy() for i in indices]

        for _ in range(30):
            clusters = [[] for _ in range(len(centroids))]

            for v in vectors:
                similarities = [np.dot(v, c) for c in centroids]
                best = int(np.argmax(similarities))
                clusters[best].append(v)

            new_centroids = []
            for i, cluster in enumerate(clusters):
                if not cluster:
                    new_centroids.append(centroids[i])
                else:
                    new_c = np.mean(cluster, axis=0)
                    new_c = new_c / (np.linalg.norm(new_c) + 1e-9)
                    new_centroids.append(new_c)

            centroids = new_centroids

        labels = []
        for c in centroids:
            best_idx = max(range(len(vectors)), key=lambda i: np.dot(vectors[i], c))
            labels.append(tags[best_idx]['name'])

        return {
            'vectors': centroids,
            'labels': labels,
            'weights': np.array([len(c) for c in clusters], dtype=np.float32)
        }

    def _weighted_pca(self, cluster_data: dict) -> tuple:
        """加权 PCA"""
        vectors = cluster_data['vectors']
        weights = cluster_data['weights']
        n = len(vectors)

        total_weight = np.sum(weights)
        mean = sum(v * w for v, w in zip(vectors, weights, strict=False)) / total_weight

        scaled = [(v - mean) * math.sqrt(w) for v, w in zip(vectors, weights, strict=False)]

        gram = np.array([[np.dot(a, b) for b in scaled] for a in scaled])

        eigenvectors = []
        eigenvalues = []
        gram_work = gram.copy()

        for _ in range(min(n, 64)):
            v = np.random.randn(n).astype(np.float32)
            v = v / np.linalg.norm(v)

            for _ in range(50):
                w = gram_work @ v
                for prev in eigenvectors:
                    w -= np.dot(w, prev) * prev
                norm = np.linalg.norm(w)
                if norm < 1e-9:
                    break
                v = w / norm

            val = v @ gram_work @ v
            if val < 1e-6:
                break

            eigenvectors.append(v)
            eigenvalues.append(val)

            for i in range(n):
                for j in range(n):
                    gram_work[i, j] -= val * v[i] * v[j]

        U = []
        for ev in eigenvectors:
            basis = sum(w * v for w, v in zip(ev, scaled, strict=False))
            basis = basis / (np.linalg.norm(basis) + 1e-9)
            U.append(basis)

        return U, np.array(eigenvalues), mean, cluster_data['labels']

    def _epa_project(self, vector: np.ndarray) -> dict:
        """EPA 投影分析"""
        if self._epa_basis is None:
            return {'entropy': 1.0, 'logic_depth': 0.0, 'dominant_axes': []}

        centered = vector - self._epa_mean
        projections = np.array([np.dot(centered, b) for b in self._epa_basis])
        total_energy = np.sum(projections ** 2)

        if total_energy < 1e-12:
            return {'entropy': 1.0, 'logic_depth': 0.0, 'dominant_axes': []}

        probs = (projections ** 2) / total_energy

        entropy = -sum(p * math.log2(p) for p in probs if p > 1e-9)
        normalized_entropy = entropy / math.log2(len(self._epa_basis)) if len(self._epa_basis) > 1 else 0.0

        dominant = [
            {'index': i, 'label': self._epa_labels[i], 'energy': probs[i]}
            for i in range(len(self._epa_basis)) if probs[i] > 0.05
        ]
        dominant.sort(key=lambda x: x['energy'], reverse=True)

        return {
            'entropy': normalized_entropy,
            'logic_depth': 1 - normalized_entropy,
            'dominant_axes': dominant
        }

    def _residual_pyramid(self, query_vector: np.ndarray, max_levels: int = 3, top_k: int = 10) -> list[dict]:
        """残差金字塔分析"""
        if self._chunk_matrix is None:
            return []

        levels = []
        current_residual = query_vector.copy()

        for level_idx in range(max_levels):
            similarities = self._chunk_matrix @ current_residual
            top_indices = np.argsort(similarities)[-top_k:][::-1]

            level_chunks = []
            for rank, idx in enumerate(top_indices):
                chunk = self._chunk_vectors[idx]
                level_chunks.append({
                    'id': chunk['id'],
                    'content': chunk['content'][:200],
                    'similarity': float(similarities[idx]),
                    'rank': rank + 1
                })

            projection_vec = np.zeros_like(query_vector)
            for chunk_info in level_chunks:
                idx = next(i for i, c in enumerate(self._chunk_vectors) if c['id'] == chunk_info['id'])
                projection_vec += chunk_info['similarity'] * self._chunk_vectors[idx]['vector']

            proj_mag = np.linalg.norm(projection_vec)
            if proj_mag > 1e-9:
                projection_vec /= proj_mag

            residual_vec = current_residual - projection_vec
            residual_mag = np.linalg.norm(residual_vec)

            levels.append({
                'level': level_idx + 1,
                'chunks': level_chunks,
                'residual_magnitude': float(residual_mag)
            })

            current_residual = residual_vec

            if residual_mag < 1e-6:
                break

        return levels

    async def enhance_query_vector(
        self, query_vector: list[float], tag_boost: float = 0.3
    ) -> TagBoostResult:
        """
        使用 TagWave (EPA + 残差金字塔) 增强查询向量

        基于 VCPToolBox TagMemo "浪潮"算法 V5

        Args:
            query_vector: 原始查询向量
            tag_boost: 增强因子 (0-1)

        Returns:
            TagBoostResult 包含增强后的向量和调试信息
        """
        if tag_boost <= 0:
            return TagBoostResult(
                vector=query_vector,
                matched_tags=[],
                boost_factor=0,
                spike_count=0,
                total_spike_score=0,
            )

        # 初始化 TagWave
        await self._init_tagwave()

        query_np = np.array(query_vector, dtype=np.float32)

        # 1. EPA 分析
        epa_result = self._epa_project(query_np)

        # 2. 残差金字塔
        pyramid_levels = self._residual_pyramid(query_np)

        # 3. 收集 Tags
        matched_tags = []
        for level in pyramid_levels:
            for chunk in level['chunks'][:3]:
                matched_tags.append(chunk['content'][:50])

        # 4. 动态 Beta 融合
        L = epa_result['logic_depth']
        beta = self._sigmoid(L * 2 - 1)  # 简化版动态 Beta

        # 5. 向量融合
        if matched_tags and self._chunk_vectors:
            # 使用 Top chunks 的向量加权平均
            tag_vectors = []
            for level in pyramid_levels[:2]:
                for chunk in level['chunks'][:3]:
                    idx = next((i for i, c in enumerate(self._chunk_vectors) if c['id'] == chunk['id']), None)
                    if idx is not None:
                        tag_vectors.append(self._chunk_vectors[idx]['vector'])

            if tag_vectors:
                tag_centroid = np.mean(tag_vectors, axis=0)
                tag_centroid = tag_centroid / (np.linalg.norm(tag_centroid) + 1e-9)

                enhanced = (1 - beta) * query_np + beta * tag_centroid
                enhanced = enhanced / (np.linalg.norm(enhanced) + 1e-9)

                return TagBoostResult(
                    vector=enhanced.tolist(),
                    matched_tags=matched_tags[:10],
                    boost_factor=float(beta),
                    spike_count=len(pyramid_levels),
                    total_spike_score=epa_result['logic_depth'],
                )

        # 回退：原向量
        return TagBoostResult(
            vector=query_vector,
            matched_tags=matched_tags[:10] if matched_tags else [],
            boost_factor=0.0,
            spike_count=len(pyramid_levels),
            total_spike_score=epa_result['logic_depth'],
        )

    def _sigmoid(self, x: float) -> float:
        """Sigmoid 函数"""
        return 1.0 / (1.0 + math.exp(-x))

    async def search_episodes(
        self,
        user_id: uuid.UUID,
        query: str,
        query_vector: list[float] | None = None,
        filters: dict[str, Any] | None = None,
        config: RetrievalConfig | None = None,
    ) -> list[SearchResult]:
        """
        搜索情景记忆（混合检索：向量 + 关键词）

        Args:
            user_id: 用户 ID
            query: 查询文本
            query_vector: 查询向量（可选，用于向量检索）
            filters: 过滤条件（可包含 session_id, task_id）
            config: 检索配置

        Returns:
            搜索结果列表
        """
        cfg = config or self.default_config
        user_id_str = str(user_id) if user_id else None

        vector_results: list[SearchResult] = []
        keyword_results: list[SearchResult] = []

        # 提取 session_id 和 task_id
        session_id = filters.get("session_id") if filters else None
        task_id = filters.get("task_id") if filters else None

        # 1. 向量检索（如果提供了查询向量）
        if query_vector:
            vector_results = await self._vector_search_episodes(
                user_id_str,
                query_vector,
                cfg.top_k * 2,
                cfg.min_score,
                session_id=session_id,
                task_id=task_id,
            )

        # 2. 关键词检索
        if query:
            keyword_results = await self._keyword_search_episodes(
                user_id_str, query, filters, cfg.top_k * 2
            )

        # 3. RRF 融合
        if vector_results and keyword_results:
            merged = self.merger.merge(vector_results, keyword_results)
            return merged[: cfg.top_k]
        elif vector_results:
            return vector_results[: cfg.top_k]
        elif keyword_results:
            return keyword_results[: cfg.top_k]
        else:
            return []

    async def _vector_search_episodes(
        self,
        user_id_str: str,
        query_vector: list[float],
        limit: int,
        min_score: float,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> list[SearchResult]:
        """
        向量检索情景记忆（优化版）

        优化策略：
        1. 使用 final_score 过滤低质量记录
        2. 减少 limit * 3 到 limit * 2，降低内存占用
        3. 早期过滤无向量记录
        """
        from src.memory.constants import Retrieval

        # 构建基础查询，添加 final_score 过滤以提高效率
        stmt = select(EpisodesMemory).where(
            EpisodesMemory.user_id == user_id_str,
            EpisodesMemory.intent_vector.isnot(None),
            # 只检索评分高于阈值的记录
            EpisodesMemory.final_score >= (min_score * Retrieval.SCORE_THRESHOLD),
        )

        # 添加 session_id 和 task_id 过滤
        if session_id:
            stmt = stmt.where(EpisodesMemory.session_id == session_id)
        if task_id:
            stmt = stmt.where(EpisodesMemory.task_id == task_id)

        # 减少预取数量：从 limit * 3 降到 limit * 2
        stmt = stmt.limit(limit * 2)
        result = await self.session.execute(stmt)
        episodes = result.scalars().all()

        if not episodes:
            return []

        # 计算相似度并排序
        scored = []
        for ep in episodes:
            if ep.intent_vector:
                similarity = self._cosine_similarity(query_vector, ep.intent_vector)
                if similarity >= min_score:
                    scored.append((similarity, ep))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            SearchResult(
                id=ep.id,
                content=ep.intent_text,
                score=score,
                memory_type=MemoryType.EPISODE,
                metadata={
                    "session_id": (str(ep.session_id) if ep.session_id else None),
                    "task_id": str(ep.task_id) if ep.task_id else None,
                    "tags": ep.tags,
                },
            )
            for score, ep in scored[:limit]
        ]

    async def _keyword_search_episodes(
        self,
        user_id_str: str,
        query: str,
        filters: dict[str, Any] | None,
        limit: int,
    ) -> list[SearchResult]:
        """关键词检索情景记忆"""
        stmt = select(EpisodesMemory).where(EpisodesMemory.user_id == user_id_str)

        # 分词搜索：将查询拆分为关键词，任意匹配
        keywords = query.split()
        if keywords:
            keyword_conditions = []
            for kw in keywords:
                keyword_conditions.append(EpisodesMemory.intent_text.ilike(f"%{kw}%"))
                keyword_conditions.append(
                    EpisodesMemory.execution_summary.ilike(f"%{kw}%")
                )
            stmt = stmt.where(or_(*keyword_conditions))

        # 添加过滤条件
        if filters:
            if "tags" in filters:
                for tag in filters["tags"]:
                    stmt = stmt.where(
                        cast(EpisodesMemory.tags, String).like(f'%"{tag}"%')
                    )
            if "min_score" in filters:
                stmt = stmt.where(EpisodesMemory.final_score >= filters["min_score"])
            if "session_id" in filters and filters["session_id"]:
                stmt = stmt.where(EpisodesMemory.session_id == filters["session_id"])
            if "task_id" in filters and filters["task_id"]:
                stmt = stmt.where(EpisodesMemory.task_id == filters["task_id"])

        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        episodes = result.scalars().all()

        return [
            SearchResult(
                id=ep.id,
                content=ep.intent_text,
                score=ep.final_score or 0.5,
                memory_type=MemoryType.EPISODE,
                metadata={
                    "session_id": (str(ep.session_id) if ep.session_id else None),
                    "task_id": str(ep.task_id) if ep.task_id else None,
                    "tags": ep.tags,
                },
            )
            for ep in episodes
        ]

    def _cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """计算余弦相似度"""
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=False))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)

    async def search_knowledge(
        self,
        user_id: uuid.UUID,
        query: str,
        query_vector: list[float] | None = None,
        domain: str | None = None,
        config: RetrievalConfig | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> list[SearchResult]:
        """
        搜索语义记忆/知识（混合检索：向量 + 关键词）

        Args:
            user_id: 用户 ID
            query: 查询文本
            query_vector: 查询向量（可选，用于向量检索）
            domain: 领域过滤
            config: 检索配置
            session_id: 会话 ID（可选）
            task_id: 任务 ID（可选）

        Returns:
            搜索结果列表
        """
        cfg = config or self.default_config
        user_id_str = str(user_id) if user_id else None

        vector_results: list[SearchResult] = []
        keyword_results: list[SearchResult] = []

        # 1. 向量检索（如果提供了查询向量）
        if query_vector:
            vector_results = await self._vector_search_knowledge(
                user_id_str,
                query_vector,
                domain,
                cfg.top_k * 2,
                cfg.min_score,
            )

        # 2. 关键词检索
        if query:
            keyword_results = await self._keyword_search_knowledge(
                user_id_str,
                query,
                domain,
                cfg.top_k * 2,
            )

        # 3. RRF 融合
        if vector_results and keyword_results:
            merged = self.merger.merge(vector_results, keyword_results)
            return merged[: cfg.top_k]
        elif vector_results:
            return vector_results[: cfg.top_k]
        elif keyword_results:
            return keyword_results[: cfg.top_k]
        else:
            return []

    async def _vector_search_knowledge(
        self,
        user_id_str: str,
        query_vector: list[float],
        domain: str | None,
        limit: int,
        min_score: float,
    ) -> list[SearchResult]:
        """
        向量检索语义记忆

        Args:
            user_id_str: 用户 ID 字符串
            query_vector: 查询向量
            domain: 领域过滤
            limit: 返回数量限制
            min_score: 最小相似度阈值

        Returns:
            搜索结果列表
        """
        stmt = select(SemanticMemory).where(
            SemanticMemory.user_id == user_id_str,
            SemanticMemory.embedding.isnot(None),
        )

        if domain:
            stmt = stmt.where(
                cast(SemanticMemory.memory_metadata["domain"], String) == domain
            )

        # 减少预取数量：从 limit * 3 降到 limit * 2
        stmt = stmt.limit(limit * 2)
        result = await self.session.execute(stmt)
        knowledge_list = result.scalars().all()

        if not knowledge_list:
            return []

        # 计算相似度并排序
        scored = []
        for k in knowledge_list:
            if k.embedding:
                similarity = self._cosine_similarity(query_vector, k.embedding)
                if similarity >= min_score:
                    scored.append((similarity, k))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            SearchResult(
                id=k.id,
                content=k.content,
                score=score,
                memory_type=MemoryType.SEMANTIC,
                metadata={
                    "source_type": k.source_type,
                    "memory_metadata": k.memory_metadata,
                },
            )
            for score, k in scored[:limit]
        ]

    async def _keyword_search_knowledge(
        self,
        user_id_str: str,
        query: str,
        domain: str | None,
        limit: int,
    ) -> list[SearchResult]:
        """
        关键词检索语义记忆

        Args:
            user_id_str: 用户 ID 字符串
            query: 查询文本
            domain: 领域过滤
            limit: 返回数量限制

        Returns:
            搜索结果列表
        """
        stmt = select(SemanticMemory).where(SemanticMemory.user_id == user_id_str)

        # 分词搜索
        keywords = query.split()
        if keywords:
            keyword_conditions = [
                SemanticMemory.content.ilike(f"%{kw}%") for kw in keywords
            ]
            stmt = stmt.where(or_(*keyword_conditions))

        if domain:
            stmt = stmt.where(
                cast(SemanticMemory.memory_metadata["domain"], String) == domain
            )

        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        knowledge_list = result.scalars().all()

        return [
            SearchResult(
                id=k.id,
                content=k.content,
                score=0.7,  # 关键词匹配默认得分
                memory_type=MemoryType.SEMANTIC,
                metadata={
                    "source_type": k.source_type,
                    "memory_metadata": k.memory_metadata,
                },
            )
            for k in knowledge_list
        ]

    async def search_tools(
        self,
        requirement: str,
        config: RetrievalConfig | None = None,
    ) -> list[SearchResult]:
        """
        搜索工具（程序性记忆）

        Args:
            requirement: 功能需求描述
            config: 检索配置

        Returns:
            搜索结果列表
        """
        cfg = config or self.default_config

        # 构建查询
        stmt = select(ToolLibrary).where(ToolLibrary.status == "active")

        # 关键词搜索
        if requirement:
            stmt = stmt.where(
                or_(
                    ToolLibrary.name.ilike(f"%{requirement}%"),
                    ToolLibrary.description.ilike(f"%{requirement}%"),
                )
            )

        stmt = stmt.limit(cfg.top_k)

        result = await self.session.execute(stmt)
        tools = result.scalars().all()

        return [
            SearchResult(
                id=t.id,
                content=f"{t.name}: {t.description}",
                score=0.7,
                memory_type=MemoryType.PROCEDURAL,
                metadata={
                    "name": t.name,
                    "args_schema": t.args_schema,
                    "requires_approval": t.requires_approval,
                },
            )
            for t in tools
        ]

    async def hybrid_search(
        self,
        user_id: uuid.UUID,
        query: str,
        memory_types: list[MemoryType] | None = None,
        config: RetrievalConfig | None = None,
    ) -> list[SearchResult]:
        """
        混合搜索多种记忆类型

        Args:
            user_id: 用户 ID
            query: 查询文本
            memory_types: 要搜索的记忆类型
            config: 检索配置

        Returns:
            融合后的搜索结果
        """
        cfg = config or self.default_config
        types = memory_types or [MemoryType.EPISODE, MemoryType.SEMANTIC]

        all_results: list[SearchResult] = []

        # 搜索各类型记忆
        if MemoryType.EPISODE in types:
            episodes = await self.search_episodes(user_id, query, config=cfg)
            all_results.extend(episodes)

        if MemoryType.SEMANTIC in types:
            knowledge = await self.search_knowledge(user_id, query, config=cfg)
            all_results.extend(knowledge)

        if MemoryType.PROCEDURAL in types:
            tools = await self.search_tools(query, config=cfg)
            all_results.extend(tools)

        # 按得分排序
        all_results.sort(key=lambda x: x.score, reverse=True)

        # 过滤低分结果
        filtered = [r for r in all_results if r.score >= cfg.min_score]

        return filtered[: cfg.top_k]

    async def get_similar_episodes(
        self,
        user_id: uuid.UUID,
        intent: str,
        limit: int = 5,
    ) -> list[SearchResult]:
        """
        获取相似的历史任务

        Args:
            user_id: 用户 ID
            intent: 当前意图
            limit: 返回数量

        Returns:
            相似任务列表
        """
        config = RetrievalConfig(top_k=limit, min_score=0.5)
        return await self.search_episodes(
            user_id=user_id,
            query=intent,
            filters={"min_score": 0.7},
            config=config,
        )
