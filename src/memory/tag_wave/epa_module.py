"""
EPA Module - 嵌入投影分析模块

基于 VCPToolBox 的 EPA（Embedding Projection Analysis）算法实现

功能：
1. 构建语义空间的正交基
2. 计算向量的逻辑深度（Logic Depth）
3. 检测跨域共振（Resonance）
4. 识别主导语义轴
"""

import json
import logging
import math
import time
from typing import Any

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.memory import Tag

logger = logging.getLogger(__name__)


class EPAModule:
    """
    EPA（嵌入投影分析）模块

    核心算法：
    1. K-Means 聚类生成语义中心
    2. 加权 PCA 构建正交基
    3. 投影分析计算逻辑深度和熵

    引用：VCPToolBox TagMemo "浪潮"算法
    """

    def __init__(
        self,
        session: AsyncSession,
        max_basis_dim: int = 64,
        cluster_count: int = 32,
        dimension: int = 3072
    ):
        self.session = session
        self.max_basis_dim = max_basis_dim
        self.cluster_count = cluster_count
        self.dimension = dimension

        # 缓存
        self.ortho_basis: list[np.ndarray] | None = None
        self.basis_mean: np.ndarray | None = None
        self.basis_labels: list[str] | None = None
        self.basis_energies: np.ndarray | None = None
        self._cache_timestamp: float | None = None

        self.initialized = False

    async def initialize(self, force_refresh: bool = False) -> bool:
        """初始化正交基"""
        if self.initialized and not force_refresh:
            return True

        logger.info('[EPA] 🧠 Initializing EPA Module...')

        try:
            # 尝试从缓存加载
            if not force_refresh and await self._load_from_cache():
                logger.info('[EPA] 💾 Loaded basis from cache')
                self.initialized = True
                return True

            # 从数据库加载 Tags
            result = await self.session.execute(
                select(Tag).where(Tag.vector.isnot(None))
            )
            tags = result.scalars().all()

            if len(tags) < 8:
                logger.warning(f'[EPA] Not enough tags: {len(tags)}')
                return False

            # 转换为 NumPy 数组
            tag_data = []
            for tag in tags:
                vector = tag.vector if isinstance(tag.vector, list) else tag.vector
                tag_data.append({
                    'id': tag.id,
                    'name': tag.name,
                    'vector': np.array(vector, dtype=np.float32),
                    'frequency': tag.frequency or 1
                })

            # K-Means 聚类
            cluster_data = self._kmeans_clustering(tag_data, min(len(tag_data), self.cluster_count))

            # 加权 PCA
            U, S, mean_vector, labels = self._weighted_pca(cluster_data)

            # 选择维度
            K = self._select_dimensions(S)

            self.ortho_basis = U[:K]
            self.basis_energies = S[:K]
            self.basis_mean = mean_vector
            self.basis_labels = labels[:K]

            # 保存到缓存
            await self._save_to_cache()

            self.initialized = True
            logger.info(f'[EPA] ✅ Initialized with {K} dimensions, {len(tags)} tags')
            return True

        except Exception as e:
            logger.error(f'[EPA] ❌ Init failed: {e}')
            return False

    async def _save_to_cache(self):
        """保存正交基到缓存"""
        try:
            if self.ortho_basis is None or self.basis_mean is None:
                return

            data = {
                'basis': [b.tobytes().hex() for b in self.ortho_basis],
                'mean': self.basis_mean.tobytes().hex(),
                'energies': self.basis_energies.tolist() if self.basis_energies is not None else [],
                'labels': self.basis_labels,
                'timestamp': int(time.time()),
            }

            query = text("""
                INSERT OR REPLACE INTO kv_store (key, value)
                VALUES ('epa_basis_cache', :value)
            """)
            await self.session.execute(query, {'value': json.dumps(data)})
            await self.session.commit()
            logger.debug('[EPA] Basis saved to cache')

        except Exception as e:
            logger.warning(f'[EPA] Save cache error: {e}')

    async def _load_from_cache(self) -> bool:
        """从缓存加载正交基"""
        try:
            query = text("SELECT value FROM kv_store WHERE key = 'epa_basis_cache'")
            result = await self.session.execute(query)
            row = result.fetchone()

            if not row:
                return False

            data = json.loads(row.value)

            if 'mean' not in data or 'basis' not in data:
                return False

            self.ortho_basis = [
                np.frombuffer(bytes.fromhex(b64), dtype=np.float32)
                for b64 in data['basis']
            ]
            self.basis_mean = np.frombuffer(bytes.fromhex(data['mean']), dtype=np.float32)
            self.basis_energies = np.array(data['energies'], dtype=np.float32) if data.get('energies') else None
            self.basis_labels = data.get('labels', [])
            self._cache_timestamp = data.get('timestamp')

            return True

        except Exception as e:
            logger.warning(f'[EPA] Load cache failed: {e}')
            return False

    def project(self, vector: np.ndarray) -> dict[str, Any]:
        """
        投影向量到语义空间

        计算：
        - projections: 在各正交基上的投影
        - probabilities: 能量分布概率
        - entropy: 归一化熵（0-1）
        - logic_depth: 逻辑深度 = 1 - entropy
        - dominant_axes: 主导语义轴
        """
        if not self.initialized or self.ortho_basis is None:
            return {'entropy': 1.0, 'logic_depth': 0.0, 'dominant_axes': []}

        vec = np.array(vector, dtype=np.float32)
        K = len(self.ortho_basis)

        # 去中心化
        centered = vec - self.basis_mean

        # 投影
        projections = np.array([np.dot(centered, b) for b in self.ortho_basis])
        total_energy = np.sum(projections ** 2)

        if total_energy < 1e-12:
            return {'entropy': 1.0, 'logic_depth': 0.0, 'dominant_axes': []}

        # 概率分布
        probs = (projections ** 2) / total_energy

        # 熵计算
        entropy = -sum(p * math.log2(p) for p in probs if p > 1e-9)
        normalized_entropy = entropy / math.log2(K) if K > 1 else 0.0

        # 主导轴
        dominant = [
            {'index': i, 'label': self.basis_labels[i], 'energy': probs[i]}
            for i in range(K) if probs[i] > 0.05
        ]
        dominant.sort(key=lambda x: x['energy'], reverse=True)

        return {
            'projections': projections,
            'probabilities': probs,
            'entropy': normalized_entropy,
            'logic_depth': 1 - normalized_entropy,
            'dominant_axes': dominant
        }

    def detect_resonance(self, vector: np.ndarray) -> dict[str, Any]:
        """
        跨域共振检测

        检测不同语义域之间的关联强度
        """
        result = self.project(vector)
        axes = result['dominant_axes']

        if len(axes) < 2:
            return {'resonance': 0.0, 'bridges': []}

        bridges = []
        top = axes[0]

        for secondary in axes[1:]:
            co_activation = math.sqrt(top['energy'] * secondary['energy'])
            if co_activation > 0.15:
                bridges.append({
                    'from': top['label'],
                    'to': secondary['label'],
                    'strength': co_activation
                })

        return {
            'resonance': sum(b['strength'] for b in bridges),
            'bridges': bridges
        }

    def _kmeans_clustering(self, tags: list[dict], k: int) -> dict:
        """K-Means 聚类"""
        import random

        vectors = [t['vector'] for t in tags]
        n = len(vectors)

        # 随机初始化
        indices = random.sample(range(n), min(k, n))
        centroids = [vectors[i].copy() for i in indices]

        # 迭代
        for _ in range(30):
            clusters = [[] for _ in range(len(centroids))]

            # 分配
            for v in vectors:
                similarities = [np.dot(v, c) for c in centroids]
                best = int(np.argmax(similarities))
                clusters[best].append(v)

            # 更新
            new_centroids = []
            for i, cluster in enumerate(clusters):
                if not cluster:
                    new_centroids.append(centroids[i])
                else:
                    new_c = np.mean(cluster, axis=0)
                    new_c = new_c / (np.linalg.norm(new_c) + 1e-9)
                    new_centroids.append(new_c)

            centroids = new_centroids

        # 命名
        labels = []
        for c in centroids:
            best_idx = max(range(len(vectors)), key=lambda i: np.dot(vectors[i], c))
            labels.append(tags[best_idx]['name'])

        return {
            'vectors': centroids,
            'labels': labels,
            'weights': np.array([len(c) for c in clusters], dtype=np.float32)
        }

    def _weighted_pca(self, cluster_data: dict) -> tuple[list[np.ndarray], np.ndarray, np.ndarray, list[str]]:
        """加权 PCA"""
        vectors = cluster_data['vectors']
        weights = cluster_data['weights']
        n = len(vectors)

        # 加权平均
        total_weight = np.sum(weights)
        mean = sum(v * w for v, w in zip(vectors, weights, strict=False)) / total_weight

        # 中心化并缩放
        scaled = [(v - mean) * math.sqrt(w) for v, w in zip(vectors, weights, strict=False)]

        # Gram 矩阵
        gram = np.array([[np.dot(a, b) for b in scaled] for a in scaled])

        # Power Iteration
        eigenvectors = []
        eigenvalues = []
        gram_work = gram.copy()

        for _ in range(min(n, self.max_basis_dim)):
            v, val = self._power_iteration(gram_work, eigenvectors)
            if val < 1e-6:
                break

            eigenvectors.append(v)
            eigenvalues.append(val)

            # Deflation
            for i in range(n):
                for j in range(n):
                    gram_work[i, j] -= val * v[i] * v[j]

        # 映射回原始空间
        U = []
        for ev in eigenvectors:
            basis = sum(w * v for w, v in zip(ev, scaled, strict=False))
            basis = basis / (np.linalg.norm(basis) + 1e-9)
            U.append(basis)

        return U, np.array(eigenvalues), mean, cluster_data['labels']

    def _power_iteration(self, matrix: np.ndarray, existing: list) -> tuple[np.ndarray, float]:
        """Power Iteration"""
        n = matrix.shape[0]
        v = np.random.randn(n).astype(np.float32)
        v = v / np.linalg.norm(v)

        for _ in range(50):
            w = matrix @ v

            # 正交化
            for prev in existing:
                w -= np.dot(w, prev) * prev

            norm = np.linalg.norm(w)
            if norm < 1e-9:
                break

            v = w / norm

        val = v @ matrix @ v
        return v, val

    def _select_dimensions(self, S: np.ndarray) -> int:
        """选择维度（保留 95% 能量）"""
        total = np.sum(S)
        cumsum = 0.0

        for i, s in enumerate(S):
            cumsum += s
            if cumsum / total > 0.95:
                return max(i + 1, 8)

        return len(S)
