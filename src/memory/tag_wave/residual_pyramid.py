"""
Residual Pyramid - 残差金字塔模块

基于 VCPToolBox 的残差金字塔算法实现

功能：
1. 多级语义分解
2. 残差向量计算
3. 跨层握手特征提取
4. 金字塔整体特征计算
"""

import logging
from typing import Any

import numpy as np
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.memory import MemoryChunk

logger = logging.getLogger(__name__)


class ResidualPyramid:
    """
    残差金字塔模块

    核心算法：
    1. 投影到 Chunk 向量空间
    2. 提取 Top-K 相关 Chunks
    3. 计算残差向量
    4. 递归分解
    5. 提取跨层握手特征

    引用：VCPToolBox TagMemo "浪潮"算法
    """

    def __init__(
        self,
        session: AsyncSession,
        epa_module: Any,
        max_levels: int = 3,
        top_k: int = 10,
        min_energy_ratio: float = 0.1
    ):
        self.session = session
        self.epa = epa_module
        self.max_levels = max_levels
        self.top_k = top_k
        self.min_energy_ratio = min_energy_ratio

        # 缓存
        self.chunk_vectors: list[dict] | None = None
        self.vector_matrix: np.ndarray | None = None
        self.executor_filter: dict | None = None

        self.initialized = False

    async def initialize(
        self,
        executor_type: str | None = None,
        executor_id: str | None = None,
        force_refresh: bool = False
    ) -> bool:
        """初始化 Chunk 向量矩阵"""
        if self.initialized and not force_refresh:
            return True

        logger.info(f'[ResidualPyramid] 🏗️ Initializing (executor={executor_type}:{executor_id})...')

        try:
            # 构建查询条件
            filters = [MemoryChunk.embedding.isnot(None)]

            if executor_type:
                filters.append(MemoryChunk.executor_type == executor_type)
            if executor_id:
                filters.append(MemoryChunk.executor_id == executor_id)

            # 查询 memory_chunks
            result = await self.session.execute(
                select(MemoryChunk).where(and_(*filters))
            )
            chunks = result.scalars().all()

            if len(chunks) < 10:
                logger.warning(f'[ResidualPyramid] Not enough chunks: {len(chunks)}')
                return False

            # 转换为 NumPy 数组
            self.chunk_vectors = []
            for chunk in chunks:
                vector = chunk.embedding if isinstance(chunk.embedding, list) else chunk.embedding

                self.chunk_vectors.append({
                    'id': chunk.id,
                    'content': chunk.content,
                    'vector': np.array(vector, dtype=np.float32),
                    'layer': chunk.layer,
                    'session_id': chunk.session_id,
                    'executor_type': chunk.executor_type,
                    'executor_id': chunk.executor_id,
                    'episode_id': chunk.episode_id
                })

            # 构建向量矩阵
            self.vector_matrix = np.stack([c['vector'] for c in self.chunk_vectors])
            self.executor_filter = {'type': executor_type, 'id': executor_id}

            self.initialized = True
            logger.info(f'[ResidualPyramid] ✅ Initialized with {len(chunks)} chunks')
            return True

        except Exception as e:
            logger.error(f'[ResidualPyramid] ❌ Init failed: {e}')
            return False

    def analyze(
        self,
        query_vector: np.ndarray,
        epa_result: dict[str, Any],
        max_levels: int | None = None,
        top_k: int | None = None
    ) -> dict[str, Any]:
        """
        执行残差金字塔分析

        步骤：
        1. 计算初始投影
        2. 提取 Top-K Chunks
        3. 计算残差向量
        4. 递归分解
        5. 提取握手特征
        """
        if not self.initialized or self.vector_matrix is None:
            return self._empty_result()

        max_levels = max_levels or self.max_levels
        top_k = top_k or self.top_k

        levels = []
        current_residual = query_vector.copy()
        total_energy = np.dot(query_vector, query_vector)
        total_explained = 0.0

        for level_idx in range(max_levels):
            # 1. 投影到 Chunk 向量空间
            similarities = self.vector_matrix @ current_residual

            # 2. 提取 Top-K
            top_indices = np.argsort(similarities)[-top_k:][::-1]

            level_chunks = []
            for rank, idx in enumerate(top_indices):
                chunk = self.chunk_vectors[idx]
                similarity = similarities[idx]

                level_chunks.append({
                    'id': chunk['id'],
                    'content': chunk['content'],
                    'similarity': float(similarity),
                    'rank': rank + 1,
                    'layer': chunk['layer'],
                    'session_id': chunk['session_id'],
                    'executor_type': chunk['executor_type'],
                    'executor_id': chunk['executor_id']
                })

            # 3. 构建投影向量
            projection_vec = np.zeros_like(query_vector)
            for chunk_info in level_chunks:
                idx = next(i for i, c in enumerate(self.chunk_vectors) if c['id'] == chunk_info['id'])
                chunk_vec = self.chunk_vectors[idx]['vector']
                projection_vec += chunk_info['similarity'] * chunk_vec

            # 归一化
            proj_mag = np.linalg.norm(projection_vec)
            if proj_mag > 1e-9:
                projection_vec /= proj_mag

            # 4. 计算残差
            residual_vec = current_residual - projection_vec
            residual_mag = np.linalg.norm(residual_vec)

            # 5. 计算能量
            projection_energy = proj_mag ** 2
            residual_energy = residual_mag ** 2
            level_energy = projection_energy + residual_energy

            if level_energy < 1e-12:
                break

            energy_ratio = projection_energy / level_energy
            total_explained += projection_energy

            # 6. 提取握手特征
            handshake = None
            if level_idx > 0:
                handshake = self._extract_handshake_features(
                    levels[level_idx - 1],
                    level_chunks,
                    current_residual
                )

            level = {
                'level': level_idx + 1,
                'chunks': level_chunks,
                'projection_magnitude': float(proj_mag),
                'residual_magnitude': float(residual_mag),
                'residual_energy_ratio': float(residual_energy / level_energy),
                'energy_explained': float(projection_energy / total_energy) if total_energy > 0 else 0.0,
                'handshake_features': handshake
            }
            levels.append(level)

            # 7. 更新残差
            current_residual = residual_vec

            # 8. 检查终止条件
            if energy_ratio < self.min_energy_ratio or residual_mag < 1e-6:
                logger.debug(f'[ResidualPyramid] Level {level_idx + 1} energy ratio {energy_ratio:.4f} < threshold, stopping')
                break

        # 计算总体特征
        features = self._compute_pyramid_features(levels, epa_result)

        return {
            'levels': levels,
            'total_explained_energy': total_explained / total_energy if total_energy > 0 else 0.0,
            'final_residual': current_residual,
            'features': features
        }

    def _extract_handshake_features(
        self,
        prev_level: dict,
        current_chunks: list[dict],
        current_residual: np.ndarray
    ) -> dict[str, float]:
        """
        提取握手特征 - 跨层语义关联

        握手特征表示相邻层级之间的语义关联强度
        """
        prev_top_chunks = {c['id'] for c in prev_level['chunks'][:5]}
        current_top_chunks = {c['id'] for c in current_chunks[:5]}

        # 1. 重叠度
        overlap = len(prev_top_chunks & current_top_chunks)
        overlap_ratio = overlap / len(prev_top_chunks) if prev_top_chunks else 0.0

        # 2. 语义漂移（基于向量相似度）
        if prev_level['chunks'] and current_chunks:
            prev_vectors = [
                next(c['vector'] for c in self.chunk_vectors if c['id'] == chunk_id)
                for chunk_id in prev_top_chunks
            ]
            current_vectors = [
                next(c['vector'] for c in self.chunk_vectors if c['id'] == chunk_id)
                for chunk_id in current_top_chunks
            ]

            if prev_vectors and current_vectors:
                prev_centroid = np.mean(prev_vectors, axis=0)
                current_centroid = np.mean(current_vectors, axis=0)

                # 归一化
                prev_centroid = prev_centroid / (np.linalg.norm(prev_centroid) + 1e-9)
                current_centroid = current_centroid / (np.linalg.norm(current_centroid) + 1e-9)

                semantic_drift = 1.0 - np.dot(prev_centroid, current_centroid)
            else:
                semantic_drift = 0.0
        else:
            semantic_drift = 0.0

        # 3. 残差相关性
        prev_residual_norm = prev_level['residual_magnitude']
        current_residual_norm = np.linalg.norm(current_residual)

        residual_correlation = 1.0 - abs(prev_residual_norm - current_residual_norm) / max(prev_residual_norm, current_residual_norm, 1e-9)

        # 4. 能量衰减率
        energy_decay = prev_level['residual_energy_ratio']

        return {
            'overlap_ratio': float(overlap_ratio),
            'semantic_drift': float(semantic_drift),
            'residual_correlation': float(residual_correlation),
            'energy_decay': float(energy_decay),
            'handshake_strength': float((overlap_ratio + residual_correlation) / 2)
        }

    def _compute_pyramid_features(
        self,
        levels: list[dict],
        epa_result: dict[str, Any]
    ) -> dict[str, float]:
        """计算金字塔整体特征"""
        features = {}

        if not levels:
            return features

        # 1. 层级数量
        features['pyramid_depth'] = len(levels)

        # 2. 平均能量解释率
        avg_energy = sum(l['energy_explained'] for l in levels) / len(levels)
        features['avg_energy_explained'] = float(avg_energy)

        # 3. 能量集中度
        if len(levels) > 1:
            first_level_energy = levels[0]['energy_explained']
            total_energy = sum(l['energy_explained'] for l in levels)
            features['energy_concentration'] = float(first_level_energy / total_energy) if total_energy > 0 else 0.0

        # 4. 平均握手强度
        handshake_strengths = [
            l['handshake_features']['handshake_strength']
            for l in levels[1:]
            if l['handshake_features']
        ]
        if handshake_strengths:
            features['avg_handshake_strength'] = float(sum(handshake_strengths) / len(handshake_strengths))

        # 5. 与 EPA 逻辑深度的关联
        features['logic_depth'] = epa_result.get('logic_depth', 0.5)
        features['entropy'] = epa_result.get('entropy', 0.5)

        # 6. 复杂度评分
        complexity = len(levels) * features['logic_depth'] * (1 - features['entropy'])
        features['complexity_score'] = float(complexity)

        return features

    def _empty_result(self) -> dict[str, Any]:
        """空结果"""
        return {
            'levels': [],
            'total_explained_energy': 0.0,
            'final_residual': np.array([]),
            'features': {}
        }
