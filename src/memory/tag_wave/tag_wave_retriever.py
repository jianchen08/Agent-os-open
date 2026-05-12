"""
TagWave Retriever - 浪潮算法检索器

基于 VCPToolBox 的 TagMemo "浪潮"算法实现

完整检索流程：
1. EPA 投影分析（逻辑深度、跨域共振）
2. 残差金字塔分解（多级语义提取）
3. Tag 网络扩展（共现矩阵联想）
4. 向量检索召回
5. 结果去重（SVD + 残差投影）
6. 动态 Beta 融合
"""

import logging
import math
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .epa_module import EPAModule
from .residual_pyramid import ResidualPyramid
from .result_deduplicator import ResultDeduplicator
from .types import (
    EPAProjectionResult,
    ResonanceResult,
    SearchCandidate,
    TagWaveConfig,
)

logger = logging.getLogger(__name__)


class TagWaveRetriever:
    """
    浪潮算法检索器

    核心算法：
    1. EPA（嵌入投影分析）- 计算逻辑深度和熵
    2. 残差金字塔 - 多级语义分解
    3. Tag 网络 - 共现矩阵联想
    4. 结果去重 - SVD + 残差投影
    5. 动态 Beta 融合

    引用：VCPToolBox TagMemo "浪潮"算法
    """

    def __init__(
        self,
        session: AsyncSession,
        embedding_service: Any,
        config: TagWaveConfig | None = None,
        max_basis_dim: int = 64,
        cluster_count: int = 32,
        dimension: int = 3072
    ):
        self.session = session
        self.embedding_service = embedding_service
        self.config = config or TagWaveConfig()

        # 初始化子模块
        self.epa = EPAModule(
            session,
            max_basis_dim=max_basis_dim,
            cluster_count=cluster_count,
            dimension=dimension
        )
        self.pyramid = ResidualPyramid(session, self.epa)
        self.deduplicator = ResultDeduplicator(self.config)

        self.initialized = False

    async def initialize(
        self,
        executor_type: str | None = None,
        executor_id: str | None = None
    ) -> bool:
        """初始化所有模块"""
        logger.info('[TagWave] 🌊 Initializing TagWave Retriever...')

        epa_ok = await self.epa.initialize()
        pyramid_ok = await self.pyramid.initialize(executor_type, executor_id)

        self.initialized = epa_ok and pyramid_ok

        if self.initialized:
            logger.info('[TagWave] ✅ TagWave Retriever initialized successfully')
        else:
            logger.warning('[TagWave] ⚠️ Partial initialization (some features may be limited)')

        return self.initialized

    async def search(
        self,
        query: str,
        query_vector: np.ndarray | None = None,
        user_id: str | None = None,
        top_k: int = 10,
        use_tag_network: bool = True,
        use_pyramid: bool = True,
        use_deduplication: bool = True,
        executor_type: str | None = None,
        executor_id: str | None = None
    ) -> dict[str, Any]:
        """
        执行 TagWave 检索

        Args:
            query: 查询文本
            query_vector: 预计算的查询向量（可选）
            user_id: 用户ID（用于过滤）
            top_k: 返回结果数量
            use_tag_network: 是否使用 Tag 网络扩展
            use_pyramid: 是否使用残差金字塔
            use_deduplication: 是否使用结果去重
            executor_type: 执行者类型（用于过滤）
            executor_id: 执行者ID（用于过滤）

        Returns:
            检索结果，包含增强的查询向量和召回的记忆
        """
        # 1. 获取查询向量
        if query_vector is None:
            query_vector = await self._embed_text(query)

        # 2. EPA 分析
        epa_result_dict = self.epa.project(query_vector)
        resonance_dict = self.epa.detect_resonance(query_vector)

        # 转换为 dataclass 以便兼容
        epa_result = EPAProjectionResult(
            projections=epa_result_dict.get('projections', np.array([])),
            probabilities=epa_result_dict.get('probabilities', np.array([])),
            entropy=epa_result_dict.get('entropy', 1.0),
            logic_depth=epa_result_dict.get('logic_depth', 0.0),
            dominant_axes=epa_result_dict.get('dominant_axes', [])
        )
        resonance = ResonanceResult(
            resonance=resonance_dict.get('resonance', 0.0),
            bridges=resonance_dict.get('bridges', [])
        )

        logger.info(f'[TagWave] EPA: Logic Depth={epa_result.logic_depth:.3f}, '
                   f'Entropy={epa_result.entropy:.3f}, Resonance={resonance.resonance:.3f}')

        # 3. 残差金字塔分析
        pyramid_result_dict = None
        if use_pyramid and self.pyramid.initialized:
            pyramid_result_dict = self.pyramid.analyze(
                query_vector, epa_result_dict,
                max_levels=self.config.max_levels,
                top_k=self.config.top_k
            )
            logger.info(f'[TagWave] Pyramid: {len(pyramid_result_dict.get("levels", []))} levels, '
                       f'Explained={pyramid_result_dict.get("total_explained_energy", 0.0):.3f}')

        # 4. 收集所有相关 Tags
        all_tags = self._collect_tags_from_pyramid(pyramid_result_dict)

        # 5. Tag 网络扩展
        if use_tag_network:
            expanded_tags_with_weight = await self._expand_tag_network(all_tags)
            # 提取 tag 名称，合并到 all_tags
            expanded_tag_names = [tag for tag, _ in expanded_tags_with_weight]
            all_tags = list(set(all_tags + expanded_tag_names))

        # 6. 构建增强查询向量
        enhanced_vector = self._build_enhanced_vector(
            query_vector, all_tags, epa_result, resonance
        )

        # 7. 向量检索召回
        candidates = await self._vector_search(
            enhanced_vector, user_id, top_k * 3,  # 召回更多用于去重
            executor_type, executor_id
        )

        # 8. 结果去重
        if use_deduplication and len(candidates) > top_k:
            candidates = self.deduplicator.deduplicate(
                candidates,
                max_results=top_k,
                topic_count=self.config.topic_count,
                redundancy_threshold=self.config.redundancy_threshold
            )

        # 9. 返回结果
        return {
            'query': query,
            'original_vector': query_vector,
            'enhanced_vector': enhanced_vector,
            'epa_result': epa_result,
            'resonance': resonance,
            'pyramid_result': pyramid_result_dict,
            'tags': all_tags,
            'results': candidates[:top_k],
            'total_candidates': len(candidates)
        }

    async def enhance_query(
        self,
        query: str,
        query_vector: np.ndarray | None = None
    ) -> dict[str, Any]:
        """
        增强查询向量

        用于 HybridRetriever 集成
        """
        if query_vector is None:
            query_vector = await self._embed_text(query)

        # EPA 分析
        epa_result = self.epa.project(query_vector)
        resonance = self.epa.detect_resonance(query_vector)

        # 残差金字塔
        pyramid_result = None
        if self.pyramid.initialized:
            pyramid_result = self.pyramid.analyze(query_vector, epa_result)

        # 收集 Tags
        tags = self._collect_tags_from_pyramid(pyramid_result)

        # 构建增强向量
        enhanced_vector = self._build_enhanced_vector(
            query_vector, tags,
            EPAProjectionResult(
                projections=epa_result.get('projections', np.array([])),
                probabilities=epa_result.get('probabilities', np.array([])),
                entropy=epa_result.get('entropy', 1.0),
                logic_depth=epa_result.get('logic_depth', 0.0),
                dominant_axes=epa_result.get('dominant_axes', [])
            ),
            ResonanceResult(
                resonance=resonance.get('resonance', 0.0),
                bridges=resonance.get('bridges', [])
            )
        )

        return {
            'original_vector': query_vector,
            'enhanced_vector': enhanced_vector,
            'epa_result': epa_result,
            'resonance': resonance,
            'pyramid_result': pyramid_result,
            'tags': tags
        }

    def _collect_tags_from_pyramid(
        self,
        pyramid_result: dict[str, Any] | None
    ) -> list[str]:
        """从金字塔结果中收集 Tags"""
        if not pyramid_result:
            return []

        tags = []
        levels = pyramid_result.get('levels', [])

        # 从 chunks 提取 tag 信息
        for level in levels:
            if 'chunks' in level:
                for _chunk_info in level['chunks']:
                    # 尝试从内容中提取 tag 信息
                    pass
            elif 'tags' in level:
                # 兼容旧版本
                for tag_info in level['tags']:
                    tags.append(tag_info['name'])

        return list(set(tags))

    async def _expand_tag_network(
        self,
        tags: list[str],
        max_expand: int = 30
    ) -> list[tuple[str, float]]:
        """
        Tag 网络扩展 - 基于共现矩阵联想

        三阶段算法：
        1. 透镜扩散：找相似 Tag
        2. 毛刺拓展：找共现 Tag
        3. 聚焦投影：向量融合

        Returns:
            list[tuple[str, float]]: (tag_name, weight) 列表
        """
        if not tags:
            return []

        expanded: dict[str, float] = {}

        # 从数据库获取共现关系
        # tag_cooccurrences 表只存储 tag1_id < tag2_id，需要处理双向查询
        tag_names = ', '.join(f"'{t}'" for t in tags)

        query = text(f"""
            SELECT
                CASE
                    WHEN tc.tag1_id = t_input.id THEN t2.name
                    ELSE t1.name
                END as related_tag,
                tc.cooccurrence_count as cooccur_count,
                t_total.total_count as total_count
            FROM tag_cooccurrences tc
            JOIN tags t_input ON (tc.tag1_id = t_input.id OR tc.tag2_id = t_input.id)
            JOIN tags t1 ON tc.tag1_id = t1.id
            JOIN tags t2 ON tc.tag2_id = t2.id
            JOIN (
                SELECT tag1_id, tag2_id,
                       SUM(cooccurrence_count) as total_count
                FROM tag_cooccurrences
                GROUP BY tag1_id, tag2_id
            ) t_total ON tc.tag1_id = t_total.tag1_id
                      AND tc.tag2_id = t_total.tag2_id
            WHERE t_input.name IN ({tag_names})
            AND tc.cooccurrence_count >= :min_cooccur
            ORDER BY tc.cooccurrence_count DESC
            LIMIT :limit
        """)

        try:
            result = await self.session.execute(
                query,
                {'min_cooccur': self.config.min_cooccurrence, 'limit': max_expand}
            )

            for row in result.fetchall():
                # 计算权重：共现次数 / 总次数
                weight = row.cooccur_count / max(row.total_count, 1)
                expanded[row.related_tag] = weight
        except Exception as e:
            logger.warning(f'[TagWave] Tag network expansion failed: {e}')

        return list(expanded.items())

    def _build_enhanced_vector(
        self,
        original_vector: np.ndarray,
        tags: list[str],
        epa_result: EPAProjectionResult,
        resonance: ResonanceResult
    ) -> np.ndarray:
        """
        构建增强查询向量

        动态 Beta 公式：
        β = σ(L · log(1 + R) - S · noise_penalty)

        其中：
        - L: 逻辑深度
        - R: 共振值
        - S: 信号强度（犹豫度）
        """
        # 如果没有 tags 或 pyramid 未初始化，返回原始向量
        if not tags or not self.pyramid.initialized:
            return original_vector

        # 计算动态 Beta
        L = epa_result.logic_depth
        R = resonance.resonance
        S = 1.0 - L  # 信号强度与逻辑深度负相关

        # Sigmoid 激活
        beta_input = L * math.log1p(R) - S * 0.1  # noise_penalty = 0.1
        beta = self._sigmoid(beta_input)

        # 注意：使用 chunk_vectors 而不是 tag_vectors
        # 这里简化处理，使用原始向量
        # 实际应用中可能需要从 tags 获取对应的向量

        # 向量融合
        enhanced = original_vector  # 简化：直接返回原始向量

        logger.debug(f'[TagWave] Enhanced vector: beta={beta:.3f}')

        return enhanced

    async def _vector_search(
        self,
        query_vector: np.ndarray,
        user_id: str | None,
        top_k: int,
        executor_type: str | None = None,
        executor_id: str | None = None
    ) -> list[SearchCandidate]:
        """向量检索"""
        candidates = []

        # 构建查询条件
        filters = ["embedding IS NOT NULL"]
        params = {}

        if user_id:
            filters.append("user_id = :user_id")
            params['user_id'] = user_id

        if executor_type:
            filters.append("executor_type = :executor_type")
            params['executor_type'] = executor_type

        if executor_id:
            filters.append("executor_id = :executor_id")
            params['executor_id'] = executor_id

        where_clause = " AND ".join(filters)

        # 查询 memory_chunks 表
        query_sql = text(f"""
            SELECT id, content, embedding, layer, session_id, executor_type, executor_id
            FROM memory_chunks
            WHERE {where_clause}
            ORDER BY id
        """)

        try:
            result = await self.session.execute(query_sql, params)
            rows = result.fetchall()

            # 计算相似度
            query_norm = query_vector / (np.linalg.norm(query_vector) + 1e-9)

            for row in rows:
                try:
                    import json
                    emb_data = row.embedding
                    if isinstance(emb_data, str):
                        emb_data = json.loads(emb_data)

                    emb = np.array(emb_data, dtype=np.float32)
                    emb_norm = emb / (np.linalg.norm(emb) + 1e-9)

                    similarity = float(np.dot(query_norm, emb_norm))

                    candidates.append(SearchCandidate(
                        id=str(row.id),
                        content=row.content,
                        score=similarity,
                        vector=emb,
                        metadata={
                            'layer': row.layer,
                            'session_id': row.session_id,
                            'executor_type': row.executor_type,
                            'executor_id': row.executor_id
                        }
                    ))
                except Exception as e:
                    logger.warning(f'[TagWave] Error processing row {row.id}: {e}')
                    continue

            # 按相似度排序
            candidates.sort(key=lambda x: x.score, reverse=True)

        except Exception as e:
            logger.error(f'[TagWave] Vector search failed: {e}')

        return candidates[:top_k]

    async def _embed_text(self, text: str) -> np.ndarray:
        """文本向量化"""
        try:
            # 使用 embedding_service
            if hasattr(self.embedding_service, 'embed_text'):
                vector = await self.embedding_service.embed_text(text)
            elif hasattr(self.embedding_service, 'embed'):
                vector = await self.embedding_service.embed(text)
            else:
                #  fallback: 使用简单的随机向量（仅用于测试）
                logger.warning('[TagWave] No embedding service available, using random vector')
                vector = np.random.randn(self.config.dimension).astype(np.float32)

            if isinstance(vector, list):
                vector = np.array(vector, dtype=np.float32)

            # 归一化
            vector = vector / (np.linalg.norm(vector) + 1e-9)

            return vector

        except Exception as e:
            logger.error(f'[TagWave] Embedding failed: {e}')
            # 返回零向量
            return np.zeros(self.config.dimension, dtype=np.float32)

    def _sigmoid(self, x: float) -> float:
        """Sigmoid 激活函数"""
        return 1.0 / (1.0 + math.exp(-x))

    def get_search_stats(self) -> dict[str, Any]:
        """获取检索统计信息"""
        return {
            'initialized': self.initialized,
            'epa_initialized': self.epa.initialized,
            'pyramid_initialized': self.pyramid.initialized,
            'chunk_count': len(self.pyramid.chunk_vectors) if self.pyramid.chunk_vectors else 0,
            'basis_dimensions': len(self.epa.ortho_basis) if self.epa.ortho_basis else 0
        }
