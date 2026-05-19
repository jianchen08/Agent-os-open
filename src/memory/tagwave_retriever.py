"""TagWave 检索算法 — 透镜-拓展-聚焦三阶段。

实现思路：
1. **透镜阶段（Lens）**：基于查询标签与候选标签的重叠度快速过滤，
   缩小候选集范围，避免后续阶段的全量扫描。
2. **拓展阶段（Expand）**：从透镜结果出发，沿标签共现路径扩展关联记忆，
   发现间接关联（如 A 共现 B，B 共现 C → 发现 C）。
3. **聚焦阶段（Focus）**：综合标签相关度、向量相似度、时效性、重要性
   进行精排，去重后返回最终结果。

设计约束：
- 纯 Python 实现，不硬依赖 numpy / SQLAlchemy。
- 不改动递归压缩策略的任何代码。
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------


class MemoryCandidate(Protocol):
    """候选记忆的最小接口协议。

    只要对象拥有以下属性即可作为候选传入检索器，
    无需继承特定基类。
    """

    id: str
    content: str
    tags: list[str]
    vector: list[float]
    timestamp: float
    importance: float


@dataclass
class TagWaveConfig:
    """TagWave 检索配置。

    Attributes:
        lens_min_score: 透镜阶段最低标签匹配分数（低于此值的候选仍保留，但排序靠后）。
        max_expand: 拓展阶段最大扩展标签数。
        expand_max_hops: 拓展阶段最大共现跳数。
        weight_tag: 聚焦阶段标签相关度权重。
        weight_vector: 聚焦阶段向量相似度权重。
        weight_recency: 聚焦阶段时效性权重。
        weight_importance: 聚焦阶段重要性权重。
        recency_half_life: 时效性半衰期（秒），用于时间衰减计算。
    """

    lens_min_score: float = 0.0
    max_expand: int = 30
    expand_max_hops: int = 2
    weight_tag: float = 0.35
    weight_vector: float = 0.35
    weight_recency: float = 0.15
    weight_importance: float = 0.15
    recency_half_life: float = 86400.0  # 1 天


@dataclass
class ScoredItem:
    """带分数的候选项。

    Attributes:
        id: 候选 ID。
        content: 候选内容。
        tags: 候选标签。
        vector: 候选向量。
        timestamp: 时间戳。
        importance: 重要性。
        lens_score: 透镜阶段分数。
        expand_score: 拓展阶段分数。
        final_score: 聚焦阶段最终分数。
    """

    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    vector: list[float] = field(default_factory=list)
    timestamp: float = 0.0
    importance: float = 0.5
    lens_score: float = 0.0
    expand_score: float = 0.0
    final_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TagWaveResult:
    """TagWave 检索结果。

    Attributes:
        results: 精排后的结果列表。
        lens_count: 透镜阶段候选数量。
        expand_count: 拓展阶段候选数量。
        focus_count: 最终返回数量。
        elapsed_ms: 检索耗时（毫秒）。
    """

    results: list[ScoredItem] = field(default_factory=list)
    lens_count: int = 0
    expand_count: int = 0
    focus_count: int = 0
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# 纯 Python 向量工具
# ---------------------------------------------------------------------------


def _vec_norm(vec: list[float]) -> float:
    """计算向量 L2 范数。"""
    return math.sqrt(sum(v * v for v in vec))


def _vec_normalize(vec: list[float]) -> list[float]:
    """归一化向量。"""
    mag = _vec_norm(vec)
    if mag < 1e-9:
        return vec
    return [v / mag for v in vec]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = _vec_norm(a)
    mag_b = _vec_norm(b)
    if mag_a < 1e-9 or mag_b < 1e-9:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# TagWaveRetriever
# ---------------------------------------------------------------------------


class TagWaveRetriever:
    """TagWave 三阶段检索器。

    核心算法：
    1. **透镜阶段**：标签匹配快速过滤 → 缩小候选集。
    2. **拓展阶段**：共现路径扩展 → 发现间接关联。
    3. **聚焦阶段**：多维度精排 → 综合标签 + 向量 + 时效 + 重要性。

    使用方式::

        retriever = TagWaveRetriever()
        retriever.build_index(cooccurrence_data, tag_frequency)
        result = retriever.retrieve(
            query_tags=["Python", "异步"],
            query_vector=[0.9, 0.1, ...],
            candidates=memory_items,
            top_k=5,
        )
    """

    def __init__(self, config: TagWaveConfig | None = None) -> None:
        self._config = config or TagWaveConfig()

        # 共现矩阵: {tag_name: {related_tag_name: weight}}
        self._cooccurrence: dict[str, dict[str, int]] = defaultdict(dict)
        # 标签频率: {tag_name: frequency}
        self._tag_frequency: dict[str, int] = {}
        # 标签倒排索引: {tag_name: set(candidate_id)}（运行时由候选构建）
        self._tag_inverted: dict[str, set[str]] = defaultdict(set)
        self._indexed = False

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------

    def build_index(
        self,
        cooccurrence_data: list[tuple[str, str, int]],
        tag_frequency: dict[str, int] | None = None,
    ) -> None:
        """构建共现索引和标签频率。

        Args:
            cooccurrence_data: 共现关系列表 ``[(tag1, tag2, weight), ...]``。
            tag_frequency: 标签全局频率 ``{tag_name: frequency}``。
        """
        self._cooccurrence.clear()
        self._tag_frequency.clear()

        for tag1, tag2, weight in cooccurrence_data:
            self._cooccurrence[tag1][tag2] = weight
            self._cooccurrence[tag2][tag1] = weight  # 对称

        if tag_frequency:
            self._tag_frequency = dict(tag_frequency)

        self._indexed = True
        logger.info(
            "[TagWave] 索引构建完成 | 共现关系: %d 对 | 标签频率: %d 个",
            len(cooccurrence_data),
            len(self._tag_frequency),
        )

    def _build_inverted_index(self, candidates: list[Any]) -> None:
        """为候选集构建标签倒排索引（加速透镜过滤）。"""
        self._tag_inverted.clear()
        for c in candidates:
            cid = c.id
            for tag in getattr(c, "tags", []):
                self._tag_inverted[tag].add(cid)

    # ------------------------------------------------------------------
    # 透镜阶段（Lens）
    # ------------------------------------------------------------------

    def lens_phase(
        self,
        query_tags: list[str],
        candidates: list[Any],
    ) -> list[ScoredItem]:
        """透镜阶段：基于标签重叠度快速过滤候选。

        算法：
        1. 计算每个候选与查询标签的 Jaccard 相似度。
        2. 无查询标签时，所有候选以最低分通过（交给后续阶段）。
        3. 结果按 lens_score 降序排列。

        Args:
            query_tags: 查询标签列表。
            candidates: 候选记忆列表。

        Returns:
            带透镜分数的候选列表。
        """
        if not candidates:
            return []

        query_tag_set = set(query_tags)
        scored: list[ScoredItem] = []

        for c in candidates:
            c_tags = set(getattr(c, "tags", []))

            if query_tag_set and c_tags:
                # Jaccard 相似度
                intersection = len(query_tag_set & c_tags)
                union = len(query_tag_set | c_tags)
                tag_score = intersection / union if union > 0 else 0.0
            elif not query_tag_set:
                # 无查询标签 → 全部通过，分数相同
                tag_score = 0.0
            else:
                # 候选无标签
                tag_score = 0.0

            scored.append(self._to_scored_item(c, lens_score=tag_score))

        scored.sort(key=lambda x: x.lens_score, reverse=True)

        # 有查询标签时，过滤掉零分候选（无任何标签重叠）
        if query_tag_set:
            scored = [s for s in scored if s.lens_score > 0]

        return scored

    # ------------------------------------------------------------------
    # 拓展阶段（Expand）
    # ------------------------------------------------------------------

    def expand_phase(
        self,
        query_tags: list[str],
        lens_results: list[ScoredItem],
        candidates: list[Any],
    ) -> list[ScoredItem]:
        """拓展阶段：沿共现路径扩展关联记忆。

        算法：
        1. 从查询标签出发，沿共现路径最多 ``expand_max_hops`` 跳，
           收集所有可达标签及累计共现权重。
        2. 从透镜结果中提取已有标签，补充共现路径上的新标签。
        3. 将候选按拓展标签匹配度重新打分，合并透镜结果。
        4. 去重。

        Args:
            query_tags: 查询标签列表。
            lens_results: 透镜阶段的结果。
            candidates: 完整候选列表。

        Returns:
            拓展后的候选列表（含透镜结果）。
        """
        # 1. 收集拓展标签及其累计权重
        expanded_tags = self._collect_expanded_tags(query_tags)

        # 2. 从透镜结果中补充标签
        for item in lens_results:
            for tag in item.tags:
                if tag not in expanded_tags:
                    # 透镜结果中的标签权重设为透镜分数（保底 0.1）
                    expanded_tags[tag] = max(item.lens_score, 0.1)

        # 3. 为所有候选计算拓展分数
        #    将透镜结果映射为字典以便合并
        lens_map: dict[str, ScoredItem] = {item.id: item for item in lens_results}
        result_map: dict[str, ScoredItem] = {}

        for c in candidates:
            cid = c.id
            c_tags = set(getattr(c, "tags", []))

            # 计算拓展匹配分数：候选标签与拓展标签的加权匹配
            expand_score = 0.0
            for tag in c_tags:
                if tag in expanded_tags:
                    expand_score += expanded_tags[tag]

            # 归一化：除以候选标签数（避免标签多的候选天然占优）
            if c_tags:
                expand_score /= len(c_tags)

            if cid in lens_map:
                # 透镜结果直接加入，拓展分数取 max
                item = lens_map[cid]
                item.expand_score = max(item.lens_score, expand_score)
                result_map[cid] = item
            elif expand_score > 0:
                # 仅通过拓展阶段发现的候选
                item = self._to_scored_item(c, expand_score=expand_score)
                item.lens_score = 0.0
                result_map[cid] = item

        # 也加入透镜阶段中未被拓展覆盖的结果（保证透镜结果不丢失）
        for item in lens_results:
            if item.id not in result_map:
                item.expand_score = item.lens_score
                result_map[item.id] = item

        results = list(result_map.values())
        results.sort(key=lambda x: x.expand_score, reverse=True)

        # 限制最大数量
        return results[:self._config.max_expand] if self._config.max_expand > 0 else results

    def _collect_expanded_tags(
        self,
        seed_tags: list[str],
    ) -> dict[str, float]:
        """沿共现路径收集可达标签。

        Args:
            seed_tags: 起始标签列表。

        Returns:
            可达标签及其累计权重 ``{tag_name: weight}``。
        """
        # 当前层标签及其累计权重
        current: dict[str, float] = {t: 1.0 for t in seed_tags}
        # 已访问标签
        visited: set[str] = set(seed_tags)
        # 结果：所有可达标签
        result: dict[str, float] = dict(current)

        for hop in range(self._config.expand_max_hops):
            next_layer: dict[str, float] = {}
            for tag, weight in current.items():
                if tag not in self._cooccurrence:
                    continue
                for related, co_weight in self._cooccurrence[tag].items():
                    if related in visited:
                        continue
                    # 衰减系数：每跳衰减 0.5
                    propagated = weight * co_weight / max(
                        self._tag_frequency.get(tag, 1), 1
                    ) * 0.5
                    next_layer[related] = next_layer.get(related, 0.0) + propagated

            visited.update(next_layer.keys())
            # 合并到结果
            for tag, w in next_layer.items():
                result[tag] = result.get(tag, 0.0) + w

            current = next_layer
            if not current:
                break

        return result

    # ------------------------------------------------------------------
    # 聚焦阶段（Focus）
    # ------------------------------------------------------------------

    def focus_phase(
        self,
        candidates: list[Any],
        query_vector: list[float] | None,
        top_k: int = 10,
    ) -> list[ScoredItem]:
        """聚焦阶段：综合多维度精排。

        算法：
        1. 对每个候选计算四维分数：
           - **标签相关度**（lens_score 或标签匹配度）
           - **向量相似度**（与查询向量的余弦相似度）
           - **时效性**（时间衰减函数）
           - **重要性**（原始 importance）
        2. 加权求和得到 final_score。
        3. 按 final_score 降序排列。
        4. 去重（按 id）。
        5. 返回 top_k 结果。

        Args:
            candidates: 带分数的候选列表（ScoredItem）或原始候选。
            query_vector: 查询向量（可选）。
            top_k: 返回数量。

        Returns:
            精排后的结果列表。
        """
        if not candidates:
            return []

        # 归一化查询向量
        norm_query = _vec_normalize(query_vector) if query_vector else None

        # 计算时间参考点（最新候选的时间戳）
        timestamps = []
        for c in candidates:
            ts = getattr(c, "timestamp", 0.0)
            if ts > 0:
                timestamps.append(ts)
        max_timestamp = max(timestamps) if timestamps else time.time()

        scored_items: list[ScoredItem] = []
        seen_ids: set[str] = set()

        for c in candidates:
            cid = c.id
            # 去重
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            # 确保 ScoredItem
            if isinstance(c, ScoredItem):
                item = c
            else:
                item = self._to_scored_item(c)

            # --- 标签相关度 ---
            tag_score = item.lens_score

            # --- 向量相似度 ---
            vec_score = 0.0
            if norm_query and item.vector:
                vec_score = _cosine_similarity(norm_query, item.vector)

            # --- 时效性（指数衰减） ---
            recency_score = 0.0
            if max_timestamp > 0 and item.timestamp > 0:
                age = max_timestamp - item.timestamp
                recency_score = math.exp(-age / self._config.recency_half_life)
            elif max_timestamp == 0:
                recency_score = 0.5  # 无时间信息时给中等分

            # --- 重要性 ---
            importance_score = min(max(item.importance, 0.0), 1.0)

            # 加权求和
            w = self._config
            final_score = (
                w.weight_tag * tag_score
                + w.weight_vector * vec_score
                + w.weight_recency * recency_score
                + w.weight_importance * importance_score
            )

            item.final_score = final_score
            scored_items.append(item)

        # 排序
        scored_items.sort(key=lambda x: x.final_score, reverse=True)
        return scored_items[:top_k]

    # ------------------------------------------------------------------
    # 完整检索流程
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query_tags: list[str],
        query_vector: list[float] | None,
        candidates: list[Any],
        top_k: int = 10,
    ) -> TagWaveResult:
        """执行完整的三阶段检索。

        Args:
            query_tags: 查询标签列表。
            query_vector: 查询向量（可选）。
            candidates: 候选记忆列表。
            top_k: 返回数量。

        Returns:
            TagWaveResult 检索结果。
        """
        start = time.perf_counter()

        # 阶段 1: 透镜
        lens_results = self.lens_phase(query_tags, candidates)
        lens_count = len(lens_results)

        # 阶段 2: 拓展
        expanded = self.expand_phase(query_tags, lens_results, candidates)
        expand_count = len(expanded)

        # 阶段 3: 聚焦
        focused = self.focus_phase(expanded, query_vector, top_k=top_k)
        focus_count = len(focused)

        elapsed_ms = (time.perf_counter() - start) * 1000

        return TagWaveResult(
            results=focused,
            lens_count=lens_count,
            expand_count=expand_count,
            focus_count=focus_count,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _to_scored_item(
        candidate: Any,
        lens_score: float = 0.0,
        expand_score: float = 0.0,
    ) -> ScoredItem:
        """将任意候选对象转换为 ScoredItem。

        Args:
            candidate: 符合 MemoryCandidate 协议的对象。
            lens_score: 初始透镜分数。
            expand_score: 初始拓展分数。

        Returns:
            ScoredItem 实例。
        """
        return ScoredItem(
            id=getattr(candidate, "id", ""),
            content=getattr(candidate, "content", ""),
            tags=list(getattr(candidate, "tags", [])),
            vector=list(getattr(candidate, "vector", [])),
            timestamp=getattr(candidate, "timestamp", 0.0),
            importance=getattr(candidate, "importance", 0.5),
            lens_score=lens_score,
            expand_score=expand_score,
            metadata=getattr(candidate, "metadata", {}),
        )

    def get_stats(self) -> dict[str, Any]:
        """获取检索器统计信息。"""
        return {
            "indexed": self._indexed,
            "cooccurrence_pairs": sum(
                len(related) for related in self._cooccurrence.values()
            ) // 2,
            "tag_frequency_count": len(self._tag_frequency),
            "config": {
                "max_expand": self._config.max_expand,
                "expand_max_hops": self._config.expand_max_hops,
                "weight_tag": self._config.weight_tag,
                "weight_vector": self._config.weight_vector,
            },
        }
