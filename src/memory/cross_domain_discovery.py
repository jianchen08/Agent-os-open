"""跨域发现模块。

基于概念图谱实现三种跨域关联发现机制：
- 概念桥接（Concept Bridge）：通过共享核心概念发现跨域关联
- 间接引用分析（Indirect Reference）：通过 BFS 发现间接关联路径
- 语义相似度传播（Semantic Similarity Propagation）：沿图谱传播相似度到远距离节点

暴露接口：
- CrossDomainConfig: 跨域发现配置
- CrossDomainDiscovery: 跨域发现器
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# 默认领域关键词
# ============================================================

_DEFAULT_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "技术": ["代码", "算法", "数据库", "API", "编程", "python", "java",
             "索引", "框架", "服务器", "前端", "后端", "部署", "微服务"],
    "科学": ["实验", "理论", "研究", "公式", "定律", "量子", "物理",
             "化学", "生物", "假设", "验证", "推导", "观测"],
    "商业": ["市场", "营收", "客户", "产品", "运营", "销售", "增长",
             "策略", "竞争", "商业", "需求", "利润", "投资"],
    "艺术": ["设计", "音乐", "绘画", "创作", "美学", "视觉", "色彩",
             "构图", "风格", "艺术", "表现", "意境"],
}

# 中文分词辅助：按标点和空白拆分
_WORD_SPLIT_PATTERN = re.compile(
    r"[，。！？、；：\u201c\u201d\u2018\u2019\uff08\uff09"
    r"\[\]{}\s<>,!?;:\"'()\t\n\r]+",
)


# ============================================================
# 配置
# ============================================================


@dataclass
class CrossDomainConfig:
    """跨域发现配置。

    Attributes:
        min_bridge_strength: 最小桥接强度阈值
        max_path_length: 最大路径长度（BFS 深度）
        propagation_decay: 语义传播衰减因子
        min_shared_concepts: 最小共享概念数
        domain_keywords: 自定义领域关键词映射（合并到内置映射）
    """

    min_bridge_strength: float = 0.3
    max_path_length: int = 4
    propagation_decay: float = 0.6
    min_shared_concepts: int = 1
    domain_keywords: dict[str, list[str]] = field(default_factory=dict)


# ============================================================
# 跨域发现器
# ============================================================


class CrossDomainDiscovery:
    """跨域发现器。

    基于概念图谱，通过三种机制发现知识条目之间的跨域关联。

    Attributes:
        _config: 跨域发现配置
        _domain_keywords: 合并后的领域关键词映射
    """

    def __init__(self, config: CrossDomainConfig | None = None) -> None:
        """初始化跨域发现器。

        Args:
            config: 跨域发现配置，None 则使用默认值
        """
        self._config = config or CrossDomainConfig()
        self._domain_keywords = self._build_domain_keywords()

    def _build_domain_keywords(self) -> dict[str, list[str]]:
        """合并内置和自定义领域关键词。

        Returns:
            合并后的领域关键词映射
        """
        merged: dict[str, list[str]] = {}
        for domain, keywords in _DEFAULT_DOMAIN_KEYWORDS.items():
            merged[domain] = list(keywords)
        for domain, keywords in self._config.domain_keywords.items():
            if domain in merged:
                merged[domain] = merged[domain] + [
                    k for k in keywords if k not in merged[domain]
                ]
            else:
                merged[domain] = list(keywords)
        return merged

    # ================================================================
    # 公共接口
    # ================================================================

    def get_domain_for_item(self, item: dict[str, Any]) -> str:
        """根据知识条目的内容判断其所属领域。

        基于关键词匹配，每个领域统计命中次数，取最高分领域。
        自定义领域优先级与内置领域相同，按命中数排序。

        Args:
            item: 知识条目，需含 content 字段

        Returns:
            领域名称，无法匹配时返回 "其他"
        """
        content = item.get("content", "")
        if not content:
            return "其他"

        scores: dict[str, int] = {}
        for domain, keywords in self._domain_keywords.items():
            score = sum(1 for kw in keywords if kw in content)
            if score > 0:
                scores[domain] = score

        if not scores:
            return "其他"

        return max(scores, key=lambda d: scores[d])

    def build_concept_graph(
        self, knowledge_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """构建概念图谱。

        从知识条目中提取核心概念，构建节点-边-领域的图谱结构。
        节点 = 知识条目，边 = 概念关联，领域 = 按领域分组的条目列表。

        Args:
            knowledge_items: 知识条目列表

        Returns:
            {"nodes": [...], "edges": [...], "domains": {...}}
        """
        if not knowledge_items:
            return {"nodes": [], "edges": [], "domains": {}}

        # 构建节点
        nodes: list[dict[str, Any]] = []
        concepts_per_item: dict[str, set[str]] = {}
        domain_map: dict[str, str] = {}

        for item in knowledge_items:
            item_id = item.get("id", f"item_{len(nodes)}")
            domain = self.get_domain_for_item(item)
            concepts = self._extract_concepts(item)

            nodes.append({
                "id": item_id,
                "domain": domain,
                "concepts": list(concepts),
            })
            concepts_per_item[item_id] = concepts
            domain_map[item_id] = domain

        # 构建边：共享概念即为边
        edges: list[dict[str, Any]] = []
        item_ids = list(concepts_per_item.keys())
        for i in range(len(item_ids)):
            for j in range(i + 1, len(item_ids)):
                id_a, id_b = item_ids[i], item_ids[j]
                shared = concepts_per_item[id_a] & concepts_per_item[id_b]
                if shared:
                    strength = len(shared) / max(
                        len(concepts_per_item[id_a])
                        + len(concepts_per_item[id_b])
                        - len(shared),
                        1,
                    )
                    edges.append({
                        "source": id_a,
                        "target": id_b,
                        "shared_concepts": list(shared),
                        "strength": strength,
                    })

        # 领域分组
        domains: dict[str, list[str]] = defaultdict(list)
        for item_id, domain in domain_map.items():
            domains[domain].append(item_id)

        return {
            "nodes": nodes,
            "edges": edges,
            "domains": dict(domains),
        }

    async def discover(
        self,
        query: str,
        knowledge_items: list[dict[str, Any]],
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """发现跨域关联。

        综合三种发现机制，返回排序后的跨域关联列表。

        Args:
            query: 查询文本
            knowledge_items: 知识条目列表，每个含 id, content, tags 等字段
            top_k: 返回最大结果数

        Returns:
            跨域关联列表，每项包含 source_id, target_id, source_domain,
            target_domain, relation_type, strength, path, evidence
        """
        if not knowledge_items:
            return []

        # 构建概念图谱
        graph = self.build_concept_graph(knowledge_items)

        # 构建辅助映射
        id_to_item: dict[str, dict[str, Any]] = {}
        id_to_domain: dict[str, str] = {}
        concepts_per_item: dict[str, set[str]] = {}
        for item in knowledge_items:
            item_id = item.get("id", "")
            if not item_id:
                continue
            id_to_item[item_id] = item
            id_to_domain[item_id] = self.get_domain_for_item(item)
            concepts_per_item[item_id] = self._extract_concepts(item)

        if not id_to_item:
            return []

        # 构建邻接表（用于 BFS）
        adjacency = self._build_adjacency(graph, concepts_per_item)

        # 与查询相关的条目
        query_items = self._find_query_related(
            query, id_to_item, concepts_per_item,
        )

        all_relations: list[dict[str, Any]] = []

        # 机制 1: 概念桥接
        bridges = self._discover_concept_bridges(
            concepts_per_item, id_to_domain,
        )
        all_relations.extend(bridges)

        # 机制 2: 间接引用
        indirect_refs = self._discover_indirect_references(
            query_items, adjacency, id_to_domain, concepts_per_item,
        )
        all_relations.extend(indirect_refs)

        # 机制 3: 语义传播
        propagations = self._discover_semantic_propagation(
            query_items, adjacency, id_to_domain, concepts_per_item,
        )
        all_relations.extend(propagations)

        # 去重：同 source_id + target_id + relation_type 保留最高 strength
        seen: dict[tuple[str, str, str], dict[str, Any]] = {}
        for r in all_relations:
            key = (r["source_id"], r["target_id"], r["relation_type"])
            if key not in seen or r["strength"] > seen[key]["strength"]:
                seen[key] = r

        # 按 strength 降序排列，取 top_k
        sorted_results = sorted(
            seen.values(), key=lambda x: x["strength"], reverse=True,
        )

        # 过滤低于最小桥接强度的结果
        filtered = [
            r for r in sorted_results
            if r["strength"] >= self._config.min_bridge_strength
        ]

        return filtered[:top_k]

    # ================================================================
    # 概念提取
    # ================================================================

    def _extract_concepts(self, item: dict[str, Any]) -> set[str]:
        """从知识条目中提取核心概念。

        概念来源：tags + content 中的高频词/领域关键词。
        过滤单字符词以提升质量。

        Args:
            item: 知识条目

        Returns:
            概念集合
        """
        concepts: set[str] = set()

        # 从 tags 提取
        tags = item.get("tags", [])
        if isinstance(tags, list):
            for tag in tags:
                tag_str = str(tag).strip()
                if len(tag_str) >= 2:
                    concepts.add(tag_str)

        # 从 content 提取领域关键词
        content = item.get("content", "")
        for domain_keywords in self._domain_keywords.values():
            for kw in domain_keywords:
                if kw in content and len(kw) >= 2:
                    concepts.add(kw)

        # 从 content 拆分为片段作为补充概念
        segments = _WORD_SPLIT_PATTERN.split(content)
        for seg in segments:
            seg = seg.strip()
            if 2 <= len(seg) <= 8:
                concepts.add(seg)

        return concepts

    # ================================================================
    # 邻接表构建
    # ================================================================

    def _build_adjacency(
        self,
        graph: dict[str, Any],
        concepts_per_item: dict[str, set[str]],
    ) -> dict[str, list[tuple[str, float]]]:
        """从图谱边构建邻接表。

        Args:
            graph: 概念图谱
            concepts_per_item: 每个条目的概念集合

        Returns:
            邻接表 {item_id: [(neighbor_id, weight), ...]}
        """
        adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for edge in graph.get("edges", []):
            src = edge["source"]
            tgt = edge["target"]
            strength = edge["strength"]
            adjacency[src].append((tgt, strength))
            adjacency[tgt].append((src, strength))
        return dict(adjacency)

    # ================================================================
    # 查询相关条目
    # ================================================================

    def _find_query_related(
        self,
        query: str,
        id_to_item: dict[str, dict[str, Any]],
        concepts_per_item: dict[str, set[str]],
    ) -> list[str]:
        """找到与查询最相关的知识条目 ID。

        基于查询文本与条目概念的重叠度排序。

        Args:
            query: 查询文本
            id_to_item: 条目映射
            concepts_per_item: 概念映射

        Returns:
            按相关度排序的条目 ID 列表
        """
        if not query:
            return list(id_to_item.keys())

        query_chars = set(query)
        scores: list[tuple[str, float]] = []

        for item_id, concepts in concepts_per_item.items():
            overlap = sum(
                1 for c in concepts
                if c in query or any(ch in query_chars for ch in c)
            )
            if overlap > 0:
                scores.append((item_id, float(overlap)))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [item_id for item_id, _ in scores]

    # ================================================================
    # 机制 1: 概念桥接
    # ================================================================

    def _discover_concept_bridges(
        self,
        concepts_per_item: dict[str, set[str]],
        id_to_domain: dict[str, str],
    ) -> list[dict[str, Any]]:
        """发现概念桥接关联。

        在不同领域的条目之间寻找共享概念，计算桥接强度。
        strength = shared / (A_concepts + B_concepts - shared)

        Args:
            concepts_per_item: 每个条目的概念集合
            id_to_domain: 条目到领域的映射

        Returns:
            概念桥接关联列表
        """
        relations: list[dict[str, Any]] = []
        item_ids = list(concepts_per_item.keys())

        for i in range(len(item_ids)):
            for j in range(i + 1, len(item_ids)):
                id_a, id_b = item_ids[i], item_ids[j]
                domain_a = id_to_domain.get(id_a, "其他")
                domain_b = id_to_domain.get(id_b, "其他")

                # 仅保留跨域关联
                if domain_a == domain_b:
                    continue

                concepts_a = concepts_per_item[id_a]
                concepts_b = concepts_per_item[id_b]
                shared = concepts_a & concepts_b

                if len(shared) < self._config.min_shared_concepts:
                    continue

                denominator = len(concepts_a) + len(concepts_b) - len(shared)
                strength = len(shared) / max(denominator, 1)

                relations.append({
                    "source_id": id_a,
                    "target_id": id_b,
                    "source_domain": domain_a,
                    "target_domain": domain_b,
                    "relation_type": "concept_bridge",
                    "strength": round(strength, 4),
                    "path": [id_a, id_b],
                    "evidence": f"共享概念: {', '.join(sorted(shared))}",
                })

        return relations

    # ================================================================
    # 机制 2: 间接引用分析
    # ================================================================

    def _discover_indirect_references(
        self,
        query_items: list[str],
        adjacency: dict[str, list[tuple[str, float]]],
        id_to_domain: dict[str, str],
        concepts_per_item: dict[str, set[str]],
    ) -> list[dict[str, Any]]:
        """通过 BFS 发现间接引用关联。

        从查询相关条目出发，沿邻接表 BFS 搜索跨域路径。
        strength 随路径长度衰减: base_strength * decay^(path_length - 1)

        Args:
            query_items: 查询相关条目 ID 列表
            adjacency: 邻接表
            id_to_domain: 条目到领域映射
            concepts_per_item: 概念映射

        Returns:
            间接引用关联列表
        """
        relations: list[dict[str, Any]] = []
        max_depth = self._config.max_path_length
        decay = self._config.propagation_decay
        visited_pairs: set[tuple[str, str]] = set()

        for start_id in query_items:
            start_domain = id_to_domain.get(start_id, "其他")
            # BFS: (current_id, path, base_strength)
            queue: list[tuple[str, list[str], float]] = [
                (start_id, [start_id], 1.0),
            ]
            visited: set[str] = {start_id}

            while queue:
                current_id, path, base_strength = queue.pop(0)

                if len(path) > max_depth:
                    continue

                for neighbor_id, edge_strength in adjacency.get(
                    current_id, [],
                ):
                    if neighbor_id in visited:
                        continue

                    neighbor_domain = id_to_domain.get(neighbor_id, "其他")
                    new_path = path + [neighbor_id]
                    path_length = len(new_path) - 1

                    # 衰减后的强度
                    strength = (
                        base_strength * edge_strength * (decay ** (path_length - 1))
                    )
                    strength = min(strength, 1.0)

                    # 仅保留跨域关联
                    if start_domain != neighbor_domain:
                        pair_key = (
                            min(start_id, neighbor_id),
                            max(start_id, neighbor_id),
                        )
                        if pair_key not in visited_pairs:
                            visited_pairs.add(pair_key)

                            # 生成证据
                            shared = (
                                concepts_per_item.get(start_id, set())
                                & concepts_per_item.get(neighbor_id, set())
                            )
                            if shared:
                                evidence = (
                                    f"间接关联路径: {' → '.join(new_path)}"
                                    f"，共享概念: {', '.join(sorted(shared))}"
                                )
                            else:
                                evidence = (
                                    f"间接关联路径: {' → '.join(new_path)}"
                                )

                            relations.append({
                                "source_id": start_id,
                                "target_id": neighbor_id,
                                "source_domain": start_domain,
                                "target_domain": neighbor_domain,
                                "relation_type": "indirect_ref",
                                "strength": round(strength, 4),
                                "path": new_path,
                                "evidence": evidence,
                            })

                    visited.add(neighbor_id)
                    queue.append((neighbor_id, new_path, strength))

        return relations

    # ================================================================
    # 机制 3: 语义相似度传播
    # ================================================================

    def _discover_semantic_propagation(
        self,
        query_items: list[str],
        adjacency: dict[str, list[tuple[str, float]]],
        id_to_domain: dict[str, str],
        concepts_per_item: dict[str, set[str]],
    ) -> list[dict[str, Any]]:
        """通过语义相似度传播发现跨域关联。

        从查询相关条目出发，沿图谱传播相似度到远距离节点。
        每经过一个节点衰减，跨越领域边界时额外衰减。

        Args:
            query_items: 查询相关条目 ID 列表
            adjacency: 邻接表
            id_to_domain: 条目到领域映射
            concepts_per_item: 概念映射

        Returns:
            语义传播关联列表
        """
        relations: list[dict[str, Any]] = []
        decay = self._config.propagation_decay
        cross_domain_penalty = 0.7  # 跨域额外衰减
        visited_pairs: set[tuple[str, str]] = set()

        for start_id in query_items:
            start_domain = id_to_domain.get(start_id, "其他")
            # BFS: (current_id, path, propagated_strength)
            queue: list[tuple[str, list[str], float]] = [
                (start_id, [start_id], 1.0),
            ]
            visited: set[str] = {start_id}

            while queue:
                current_id, path, prop_strength = queue.pop(0)

                if len(path) > self._config.max_path_length:
                    continue
                if prop_strength < 0.05:
                    continue

                current_domain = id_to_domain.get(current_id, "其他")

                for neighbor_id, edge_strength in adjacency.get(
                    current_id, [],
                ):
                    if neighbor_id in visited:
                        continue

                    neighbor_domain = id_to_domain.get(neighbor_id, "其他")
                    new_path = path + [neighbor_id]

                    # 每步衰减
                    new_strength = prop_strength * edge_strength * decay

                    # 跨域额外衰减
                    if current_domain != neighbor_domain:
                        new_strength *= cross_domain_penalty

                    new_strength = min(new_strength, 1.0)

                    # 仅收集跨域结果
                    if (
                        start_domain != neighbor_domain
                        and new_strength >= self._config.min_bridge_strength
                    ):
                        pair_key = (
                            min(start_id, neighbor_id),
                            max(start_id, neighbor_id),
                        )
                        if pair_key not in visited_pairs:
                            visited_pairs.add(pair_key)

                            shared = (
                                concepts_per_item.get(start_id, set())
                                & concepts_per_item.get(neighbor_id, set())
                            )
                            if shared:
                                evidence = (
                                    f"语义传播: {' → '.join(new_path)}"
                                    f"，共享概念: {', '.join(sorted(shared))}"
                                    f"，传播强度: {new_strength:.3f}"
                                )
                            else:
                                evidence = (
                                    f"语义传播: {' → '.join(new_path)}"
                                    f"，传播强度: {new_strength:.3f}"
                                )

                            relations.append({
                                "source_id": start_id,
                                "target_id": neighbor_id,
                                "source_domain": start_domain,
                                "target_domain": neighbor_domain,
                                "relation_type": "semantic_propagation",
                                "strength": round(new_strength, 4),
                                "path": new_path,
                                "evidence": evidence,
                            })

                    visited.add(neighbor_id)
                    queue.append((neighbor_id, new_path, new_strength))

        return relations
