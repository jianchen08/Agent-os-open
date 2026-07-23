"""Tag 网络检索器（三阶段透镜-拓展-聚焦算法）。

参考 src/memory/tag_network.py 的 TagNetworkRetriever 三阶段算法：
- 阶段1 透镜扩散（query 向量 vs tag 向量余弦）
- 阶段2 毛刺拓展（共现矩阵查关联 tag）
- 阶段3 聚焦投影（加权融合向量）

Tag 数据（tags/tag_cooccurrences）存 SQLite（由 VectorRetriever 代理读写）。
TagNetworkRetriever 产出"增强后的 query 向量"，再喂给 VectorRetriever 二次检索。

实现 IRetriever 接口。

暴露接口：
- TagNetworkConfig: 配置
- TagCooccurrenceMatrix: 共现矩阵
- TagNetworkRetriever: 三阶段检索器
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from models import SearchResult, TagBoostResult
from ports import IRetriever

logger = logging.getLogger(__name__)


@dataclass
class TagNetworkConfig:
    """Tag 网络配置。

    Attributes:
        lens_top_k: 透镜阶段召回 Tag 数量
        spike_max_expand: 毛刺阶段最大扩展 Tag 数量
        alpha_min: 最小增强指数
        alpha_max: 最大增强指数
        beta_base: 基础降噪常数
        beta_range: 降噪常数动态范围
        min_cooccurrence: 最小共现次数阈值
        tag_boost: 向量增强因子（0-1）
    """

    lens_top_k: int = 10
    spike_max_expand: int = 30
    alpha_min: float = 1.5
    alpha_max: float = 3.5
    beta_base: float = 2.0
    beta_range: float = 3.0
    min_cooccurrence: int = 1
    tag_boost: float = 0.3


class TagCooccurrenceMatrix:
    """Tag 共现矩阵。

    存储 Tag 之间的共现关系，用于毛刺拓展阶段。
    数据结构: {tag_id: {related_tag_id: weight}}

    Attributes:
        _matrix: 共现矩阵字典
        _tag_freq: Tag 频率字典
        _initialized: 是否已初始化
    """

    def __init__(self) -> None:
        """初始化共现矩阵。"""
        self._matrix: dict[int, dict[int, int]] = defaultdict(dict)
        self._tag_freq: dict[int, int] = {}
        self._initialized = False

    def build_from_data(
        self,
        cooccurrence_data: list[tuple[int, int, int]],
        frequency_data: dict[int, int] | None = None,
    ) -> None:
        """从数据构建共现矩阵。

        Args:
            cooccurrence_data: 共现关系列表 [(tag1_id, tag2_id, weight), ...]
            frequency_data: Tag 频率字典 {tag_id: frequency}
        """
        self._matrix.clear()
        for tag1, tag2, weight in cooccurrence_data:
            self._matrix[tag1][tag2] = weight
            self._matrix[tag2][tag1] = weight  # 对称填充

        if frequency_data:
            self._tag_freq = dict(frequency_data)
        self._initialized = True

    def get_related_tags(
        self,
        tag_id: int,
        exclude_ids: list[int] | None = None,
    ) -> list[tuple[int, int]]:
        """获取与指定 Tag 相关的 Tag 列表。

        Args:
            tag_id: Tag ID
            exclude_ids: 排除的 Tag ID 列表

        Returns:
            相关 Tag 列表 [(tag_id, weight), ...]，按权重降序
        """
        if tag_id not in self._matrix:
            return []
        exclude_set = set(exclude_ids or [])
        related = [(tid, w) for tid, w in self._matrix[tag_id].items() if tid not in exclude_set]
        return sorted(related, key=lambda x: x[1], reverse=True)

    def get_tag_frequency(self, tag_id: int) -> int:
        """获取 Tag 的全局频率（不存在返回 1）。"""
        return self._tag_freq.get(tag_id, 1)

    def set_tag_frequency(self, tag_id: int, frequency: int) -> None:
        """设置 Tag 频率。"""
        self._tag_freq[tag_id] = frequency

    @property
    def size(self) -> int:
        """矩阵大小。"""
        return len(self._matrix)


class TagNetworkRetriever(IRetriever):
    """Tag 网络检索器（三阶段算法）。

    工作流程：query → embedding → apply_tag_boost(三阶段增强) →
    VectorRetriever.retrieve_by_vector(增强向量二次检索)。

    Attributes:
        _cooccurrence: 共现矩阵
        _tag_vectors: Tag 向量缓存
        _tag_names: Tag 名称缓存
        _config: 检索配置
        _vector_retriever: 关联的向量检索器（用于读 Tag 数据 + 二次检索）
        _embed_fn: 同步嵌入函数
    """

    def __init__(
        self,
        vector_retriever: Any,
        embed_fn: Any,
        config: TagNetworkConfig | None = None,
    ) -> None:
        """初始化 Tag 网络检索器。

        Args:
            vector_retriever: VectorRetriever 实例（需 load_all_tags/load_cooccurrences/retrieve_by_vector）
            embed_fn: 同步嵌入函数（list[str] -> list[list[float]]）
            config: 检索配置
        """
        self._vector_retriever = vector_retriever
        self._embed_fn = embed_fn
        self._config = config or TagNetworkConfig()
        self._cooccurrence = TagCooccurrenceMatrix()
        self._tag_vectors: dict[int, list[float]] = {}
        self._tag_names: dict[int, str] = {}
        self._initialized = False

    def init_from_store(self) -> int:
        """从关联的 VectorRetriever(→SQLite) 加载 Tag 数据。

        Returns:
            加载的 Tag 数量
        """
        try:
            tags = self._vector_retriever.load_all_tags()
        except Exception as e:
            logger.warning("[TagNetworkRetriever] 加载 Tag 失败: %s", e)
            tags = []

        freq_map: dict[int, int] = {}
        for tag in tags:
            tid = tag.get("id")
            name = tag.get("name", "")
            vector = tag.get("vector")
            self._tag_names[tid] = name
            if vector is not None:
                self._tag_vectors[tid] = vector
            freq = tag.get("frequency", 1)
            freq_map[tid] = freq
            self._cooccurrence.set_tag_frequency(tid, freq)

        try:
            cooccurrences = self._vector_retriever.load_cooccurrences()
        except Exception as e:
            logger.warning("[TagNetworkRetriever] 加载共现关系失败: %s", e)
            cooccurrences = []

        if cooccurrences:
            self._cooccurrence.build_from_data(cooccurrences, freq_map)

        self._initialized = True
        logger.info(
            "[TagNetworkRetriever] 初始化完成 | tags=%d | cooccurrences=%d",
            len(tags),
            len(cooccurrences),
        )
        return len(tags)

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """三阶段 Tag 增强 + 向量二次检索。

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量
            memory_type: 记忆类型
            filters: 额外过滤条件

        Returns:
            搜索结果列表
        """
        if not query:
            return []

        try:
            query_vectors = self._embed_fn([query])
            if not query_vectors:
                return []
            query_vector = query_vectors[0]
        except Exception as e:
            logger.warning("[TagNetworkRetriever] 生成查询向量失败: %s", e)
            return []

        boost = self.apply_tag_boost(query_vector, self._config.tag_boost)
        enhanced_vector = boost.vector

        return self._vector_retriever.retrieve_by_vector(
            enhanced_vector, user_id=user_id, top_k=top_k, memory_type=memory_type
        )

    def apply_tag_boost(
        self,
        query_vector: list[float],
        tag_boost: float = 0.3,
    ) -> TagBoostResult:
        """三阶段 Tag 增强。

        阶段1 透镜扩散 → 阶段2 毛刺拓展 → 阶段3 聚焦投影。

        Args:
            query_vector: 查询向量
            tag_boost: 增强因子

        Returns:
            Tag 增强结果（含增强后向量）
        """
        if tag_boost <= 0 or not self._tag_vectors:
            return TagBoostResult(vector=query_vector, matched_tags=[])

        # ========== 阶段 1: 透镜扩散 ==========
        lens_results = self._search_similar_tags(query_vector, k=self._config.lens_top_k)
        if not lens_results:
            return TagBoostResult(vector=query_vector, matched_tags=[])

        avg_score = sum(r[1] for r in lens_results) / len(lens_results)
        dynamic_alpha = self._compute_dynamic_alpha(avg_score)
        dynamic_beta = self._compute_dynamic_beta(avg_score)

        # ========== 阶段 2: 毛刺拓展 ==========
        original_tag_ids = [r[0] for r in lens_results]
        expanded_tags = self._expand_tags(lens_results, exclude_ids=original_tag_ids)
        if not expanded_tags:
            expanded_tags = [
                (tid, score, self._cooccurrence.get_tag_frequency(tid)) for tid, score in lens_results
            ]

        # ========== 阶段 3: 聚焦投影 ==========
        fused_vector, total_score = self._fuse_vectors(
            query_vector, expanded_tags, tag_boost, dynamic_alpha, dynamic_beta
        )

        matched_tag_names = [self._tag_names.get(tid, f"tag_{tid}") for tid, _, _ in expanded_tags]
        return TagBoostResult(
            vector=fused_vector,
            matched_tags=matched_tag_names,
            boost_factor=tag_boost,
            spike_count=len(expanded_tags),
            total_spike_score=total_score,
        )

    def _search_similar_tags(
        self,
        query_vector: list[float],
        k: int = 10,
    ) -> list[tuple[int, float]]:
        """透镜扩散：搜索与查询向量最相似的 Tag。

        Args:
            query_vector: 查询向量
            k: 返回数量

        Returns:
            相似 Tag 列表 [(tag_id, similarity), ...]
        """
        if not self._tag_vectors:
            return []

        query_norm = _vector_norm(query_vector)
        if query_norm < 1e-9:
            return []
        query_normalized = [v / query_norm for v in query_vector]

        similarities: list[tuple[int, float]] = []
        for tag_id, tag_vec in self._tag_vectors.items():
            tag_norm = _vector_norm(tag_vec)
            if tag_norm < 1e-9:
                continue
            sim = sum(q * t for q, t in zip(query_normalized, tag_vec, strict=False)) / tag_norm
            similarities.append((tag_id, sim))

        similarities.sort(key=lambda x: x[1], reverse=True)
        return similarities[:k]

    def _expand_tags(
        self,
        lens_results: list[tuple[int, float]],
        exclude_ids: list[int],
    ) -> list[tuple[int, float, int]]:
        """毛刺拓展：从共现矩阵查找关联 Tag。

        Args:
            lens_results: 透镜阶段结果
            exclude_ids: 排除的 Tag ID 列表

        Returns:
            扩展 Tag 列表 [(tag_id, weight, frequency), ...]
        """
        co_tags: dict[int, float] = defaultdict(float)
        for tag_id, tag_score in lens_results:
            for related_id, weight in self._cooccurrence.get_related_tags(tag_id, exclude_ids=exclude_ids):
                co_tags[related_id] += weight * tag_score

        sorted_tags = sorted(co_tags.items(), key=lambda x: x[1], reverse=True)[: self._config.spike_max_expand]
        return [(tid, weight, self._cooccurrence.get_tag_frequency(tid)) for tid, weight in sorted_tags]

    def _fuse_vectors(
        self,
        query_vector: list[float],
        expanded_tags: list[tuple[int, float, int]],
        tag_boost: float,
        alpha: float,
        beta: float,
    ) -> tuple[list[float], float]:
        """聚焦投影：向量融合。

        Args:
            query_vector: 查询向量
            expanded_tags: 扩展 Tag 列表
            tag_boost: 增强因子
            alpha: 增强指数
            beta: 降噪常数

        Returns:
            (融合向量, 总得分)
        """
        dim = len(query_vector)
        context_vec = [0.0] * dim
        total_score = 0.0

        for tag_id, co_weight, global_freq in expanded_tags:
            if tag_id not in self._tag_vectors:
                continue
            tag_vec = self._tag_vectors[tag_id]

            logic_strength = math.pow(max(co_weight, 1), alpha)
            noise_penalty = math.log(global_freq + beta)
            if noise_penalty < 1e-9:
                noise_penalty = 1.0
            score = logic_strength / noise_penalty
            if not math.isfinite(score):
                score = 0

            context_vec = [c + t * score for c, t in zip(context_vec, tag_vec, strict=False)]
            total_score += score

        if total_score > 0:
            context_vec = [c / total_score for c in context_vec]
            mag = _vector_norm(context_vec)
            if mag > 1e-9:
                context_vec = [c / mag for c in context_vec]
        else:
            return query_vector, 0.0

        fused = [(1 - tag_boost) * q + tag_boost * c for q, c in zip(query_vector, context_vec, strict=False)]
        fused_mag = _vector_norm(fused)
        if fused_mag > 1e-9:
            fused = [f / fused_mag for f in fused]
        return fused, total_score

    def _compute_dynamic_alpha(self, avg_score: float) -> float:
        """计算动态增强指数（高相似度→高增强）。"""
        alpha = self._config.alpha_min + (self._config.alpha_max - self._config.alpha_min) * avg_score
        return min(self._config.alpha_max, max(self._config.alpha_min, alpha))

    def _compute_dynamic_beta(self, avg_score: float) -> float:
        """计算动态降噪常数（低相似度→高 beta→宽容高频词）。"""
        return self._config.beta_base + (1 - avg_score) * self._config.beta_range


def _vector_norm(vec: list[float]) -> float:
    """计算向量范数（纯 Python）。"""
    return math.sqrt(sum(v * v for v in vec))
