"""
Result Deduplicator Module
结果去重器模块 - VCPToolBox 移植版本

功能：
1. 基于 SVD 的主题提取
2. 基于残差投影的智能去重
3. 保留最大新信息量的结果
"""

import logging

import numpy as np

from .types import SearchCandidate, TagWaveConfig

logger = logging.getLogger(__name__)


class ResultDeduplicator:
    """
    结果去重器 - 基于 SVD 和残差投影的智能去重

    核心算法：
    1. SVD 提取潜在主题
    2. 残差投影计算新信息量
    3. 贪心选择最大新信息量的结果
    """

    def __init__(self, config: TagWaveConfig | None = None):
        self.config = config or TagWaveConfig()

    def deduplicate(
        self,
        candidates: list[SearchCandidate],
        max_results: int = 20,
        topic_count: int = 8,
        redundancy_threshold: float = 0.85
    ) -> list[SearchCandidate]:
        """
        去重并选择最优结果

        步骤：
        1. 构建候选矩阵
        2. SVD 提取主题
        3. 残差投影计算新信息量
        4. 贪心选择
        """
        if not candidates:
            return []

        if len(candidates) <= max_results:
            return candidates

        # 1. 构建向量矩阵
        vectors = []
        valid_candidates = []

        for c in candidates:
            if c.vector is not None:
                vectors.append(c.vector)
                valid_candidates.append(c)

        if len(valid_candidates) <= max_results:
            return valid_candidates

        matrix = np.stack(vectors)
        n, dim = matrix.shape

        # 2. SVD 提取主题
        logger.debug(f'[Deduplicator] Performing SVD on {n} candidates...')

        try:
            # 使用 numpy 的 SVD
            U, S, Vt = np.linalg.svd(matrix, full_matrices=False)

            # 选择前 k 个主题
            k = min(topic_count, n, dim)

            if k < 1:
                logger.warning('[Deduplicator] Not enough dimensions for SVD')
                return valid_candidates[:max_results]

            # 3. 残差投影计算新信息量
            selected = self._select_by_residual_projection(
                valid_candidates, U, S, k, max_results, redundancy_threshold
            )

            logger.info(f'[Deduplicator] Selected {len(selected)} results from {len(valid_candidates)} candidates')
            return selected

        except Exception as e:
            logger.error(f'[Deduplicator] SVD failed: {e}')
            return valid_candidates[:max_results]

    def _select_by_residual_projection(
        self,
        candidates: list[SearchCandidate],
        U: np.ndarray,
        S: np.ndarray,
        k: int,
        max_results: int,
        threshold: float
    ) -> list[SearchCandidate]:
        """
        基于残差投影选择结果

        逻辑：
        1. 对每个候选，计算其在已选主题上的投影
        2. 计算残差（未解释部分）
        3. 选择残差最大的（新信息量最大）
        """
        n = len(candidates)
        selected_indices = []
        selected_topics = []

        # 贪心选择
        for _ in range(max_results):
            best_idx = -1
            best_residual_norm = -1.0

            for i in range(n):
                if i in selected_indices:
                    continue

                # 获取候选的特征向量（SVD 的左奇异向量）
                candidate_features = U[i, :k]

                # 计算在已选主题上的投影
                if selected_topics:
                    # 构建投影矩阵
                    projection = np.zeros(k)

                    for topic in selected_topics:
                        # 计算与已选主题的相似度
                        similarity = np.dot(candidate_features, topic)
                        projection += similarity * topic

                    # 计算残差
                    residual = candidate_features - projection
                    residual_norm = np.linalg.norm(residual)
                else:
                    # 第一个选择：使用总能量
                    residual_norm = np.linalg.norm(candidate_features)

                # 检查冗余度
                if selected_topics:
                    max_similarity = max(
                        abs(np.dot(candidate_features, topic))
                        for topic in selected_topics
                    )

                    if max_similarity > threshold:
                        continue  # 太冗余，跳过

                if residual_norm > best_residual_norm:
                    best_residual_norm = residual_norm
                    best_idx = i

            if best_idx == -1:
                break

            selected_indices.append(best_idx)

            # 添加新的主题
            new_topic = U[best_idx, :k].copy()
            new_topic = new_topic / (np.linalg.norm(new_topic) + 1e-9)
            selected_topics.append(new_topic)

        # 按原始顺序返回
        selected_indices.sort()
        return [candidates[i] for i in selected_indices]

    def compute_diversity_score(
        self,
        results: list[SearchCandidate]
    ) -> float:
        """计算结果集的多样性分数"""
        if len(results) < 2:
            return 1.0

        vectors = [r.vector for r in results if r.vector is not None]

        if len(vectors) < 2:
            return 1.0

        # 计算平均成对相似度
        total_sim = 0.0
        count = 0

        for i in range(len(vectors)):
            for j in range(i + 1, len(vectors)):
                v1 = vectors[i] / (np.linalg.norm(vectors[i]) + 1e-9)
                v2 = vectors[j] / (np.linalg.norm(vectors[j]) + 1e-9)

                sim = np.dot(v1, v2)
                total_sim += abs(sim)
                count += 1

        avg_similarity = total_sim / count if count > 0 else 0.0
        diversity = 1.0 - avg_similarity

        return float(diversity)

    def rerank_by_diversity(
        self,
        results: list[SearchCandidate],
        query_vector: np.ndarray
    ) -> list[SearchCandidate]:
        """
        基于多样性和相关性重新排序

        MMR (Maximal Marginal Relevance) 风格
        """
        if not results:
            return []

        selected = []
        remaining = results.copy()

        lambda_param = 0.5  # 平衡相关性和多样性

        while remaining and len(selected) < len(results):
            best_candidate = None
            best_score = -float('inf')

            for candidate in remaining:
                # 相关性
                if candidate.vector is not None:
                    q_norm = query_vector / (np.linalg.norm(query_vector) + 1e-9)
                    c_norm = candidate.vector / (np.linalg.norm(candidate.vector) + 1e-9)
                    relevance = np.dot(q_norm, c_norm)
                else:
                    relevance = candidate.score

                # 多样性（与已选的最大相似度）
                if selected:
                    max_sim = max(
                        self._compute_similarity(candidate, s)
                        for s in selected
                    )
                    diversity = 1.0 - max_sim
                else:
                    diversity = 1.0

                # MMR 分数
                mmr_score = lambda_param * relevance + (1 - lambda_param) * diversity

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_candidate = candidate

            if best_candidate:
                selected.append(best_candidate)
                remaining.remove(best_candidate)
            else:
                break

        return selected

    def _compute_similarity(
        self,
        c1: SearchCandidate,
        c2: SearchCandidate
    ) -> float:
        """计算两个候选的相似度"""
        if c1.vector is None or c2.vector is None:
            return 0.0

        v1 = c1.vector / (np.linalg.norm(c1.vector) + 1e-9)
        v2 = c2.vector / (np.linalg.norm(c2.vector) + 1e-9)

        return float(np.dot(v1, v2))
