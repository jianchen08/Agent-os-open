"""结构化目录生成模块。

从知识条目中提取概念，按共同标签聚类，生成概念页、索引页和层次结构。
纯 Python 实现，不依赖外部库。

暴露接口：
- DirectoryConfig: 目录生成配置
- DirectoryGenerator: 结构化目录生成器
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DirectoryConfig:
    """目录生成配置。

    Attributes:
        min_items_per_concept: 每个概念页最少包含的知识条目数
        max_concepts: 最大概念数
        similarity_threshold: 概念聚类相似度阈值
        hierarchy_max_depth: 层次结构最大深度
    """

    min_items_per_concept: int = 2
    max_concepts: int = 100
    similarity_threshold: float = 0.5
    hierarchy_max_depth: int = 3


class DirectoryGenerator:
    """结构化目录生成器。

    从知识条目中按标签共现关系聚类，生成概念页、索引页和层次结构。

    Attributes:
        _config: 目录生成配置
    """

    def __init__(self, config: DirectoryConfig | None = None) -> None:
        """初始化目录生成器。

        Args:
            config: 目录生成配置，为 None 时使用默认配置
        """
        self._config = config or DirectoryConfig()

    def generate_concept_pages(
        self, knowledge_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """生成概念页。

        对知识条目进行概念聚类，为每个概念生成一页结构化摘要。

        Args:
            knowledge_items: 知识条目列表，每项含 id, content, tags

        Returns:
            概念页列表，每页含 page_id, title, concept, domain 等字段
        """
        if not knowledge_items:
            return []

        clusters = self.cluster_by_concept(knowledge_items)
        pages: list[dict[str, Any]] = []

        for concept_name, items in clusters.items():
            page = self._build_concept_page(concept_name, items)
            pages.append(page)

        # 按条目数降序排列
        pages.sort(key=lambda p: p["item_count"], reverse=True)
        return pages

    def generate_index_page(
        self, concept_pages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """生成索引页。

        汇总所有概念页的统计信息，按领域和类别组织。

        Args:
            concept_pages: 概念页列表

        Returns:
            索引页字典，含 title, domains, categories, concept_index, statistics
        """
        total_items = sum(p["item_count"] for p in concept_pages)
        total_concepts = len(concept_pages)

        # 按领域分组
        domain_map: dict[str, list[dict[str, Any]]] = {}
        for page in concept_pages:
            domain_name = page["domain"]
            if domain_name not in domain_map:
                domain_map[domain_name] = []
            domain_map[domain_name].append(page)

        domains: list[dict[str, Any]] = [
            {
                "name": name,
                "concept_count": len(domain_pages),
                "item_count": sum(p["item_count"] for p in domain_pages),
            }
            for name, domain_pages in sorted(domain_map.items())
        ]

        # 按领域生成分类
        categories: list[dict[str, Any]] = [
            {
                "name": name,
                "pages": [p["page_id"] for p in domain_pages],
            }
            for name, domain_pages in sorted(domain_map.items())
        ]

        # 概念名 → page_id 索引
        concept_index: dict[str, str] = {
            p["concept"]: p["page_id"] for p in concept_pages
        }

        return {
            "title": "知识目录索引",
            "domains": domains,
            "categories": categories,
            "concept_index": concept_index,
            "statistics": {
                "total_items": total_items,
                "total_concepts": total_concepts,
                "total_domains": len(domain_map),
            },
        }

    def cluster_by_concept(
        self, knowledge_items: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """按概念聚类知识条目。

        基于标签共现关系的聚类算法：
        1. 统计所有标签的共现关系
        2. 将共现频率高的标签归为同一概念
        3. 每个概念对应一组知识条目

        Args:
            knowledge_items: 知识条目列表

        Returns:
            概念到条目列表的映射 {concept_name: [items]}
        """
        if not knowledge_items:
            return {}

        # 第一步：构建标签共现矩阵
        cooccurrence = self._build_cooccurrence(knowledge_items)

        # 第二步：基于共现关系对标签进行分组
        tag_groups = self._group_tags_by_cooccurrence(cooccurrence)

        # 第三步：将条目分配到标签组对应的簇
        raw_clusters = self._assign_items_to_groups(knowledge_items, tag_groups)

        # 第四步：合并小簇并应用 min_items_per_concept 约束
        clusters = self._merge_small_clusters(raw_clusters)

        # 第五步：限制最大概念数
        if len(clusters) > self._config.max_concepts:
            clusters = self._trim_clusters(clusters)

        return clusters

    def build_hierarchy(
        self, concept_pages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """构建概念层次结构。

        通过标签的包含关系判断父子关系：
        如果概念 A 的所有关键词都是概念 B 的子集，则 A 是 B 的子概念。

        Args:
            concept_pages: 概念页列表

        Returns:
            树形层次结构，根节点含 children 列表
        """
        if not concept_pages:
            return {"name": "root", "children": []}

        # 计算每个概念的标签集合
        concept_tags: dict[str, set[str]] = {}
        for page in concept_pages:
            concept_tags[page["concept"]] = set(page["keywords"])

        # 找出父子关系
        parent_map = self._find_parent_relationships(concept_pages, concept_tags)

        # 找出根节点（没有父概念的概念）并构建树
        roots = self._build_tree(concept_pages, parent_map, concept_tags)

        return {
            "name": "root",
            "children": roots,
        }

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    def _build_cooccurrence(
        self, items: list[dict[str, Any]],
    ) -> dict[str, dict[str, int]]:
        """构建标签共现矩阵。

        Args:
            items: 知识条目列表

        Returns:
            共现矩阵 {tag1: {tag2: count}}
        """
        cooccurrence: dict[str, dict[str, int]] = {}

        for item in items:
            tags = item.get("tags", [])
            for tag in tags:
                if tag not in cooccurrence:
                    cooccurrence[tag] = {}
            for i, tag1 in enumerate(tags):
                for j, tag2 in enumerate(tags):
                    if i != j:
                        cooccurrence[tag1][tag2] = cooccurrence[tag1].get(tag2, 0) + 1
                    else:
                        # 自身频率计数
                        cooccurrence[tag1][tag2] = cooccurrence[tag1].get(tag2, 0) + 1

        return cooccurrence

    def _group_tags_by_cooccurrence(
        self, cooccurrence: dict[str, dict[str, int]],
    ) -> list[set[str]]:
        """基于共现关系对标签进行分组。

        使用 Union-Find 风格的合并算法：
        如果两个标签的共现频率超过阈值，则合并为同一组。

        Args:
            cooccurrence: 标签共现矩阵

        Returns:
            标签组列表，每组是一个标签集合
        """
        all_tags = set(cooccurrence.keys())
        parent: dict[str, str] = {tag: tag for tag in all_tags}

        def find(tag: str) -> str:
            while parent[tag] != tag:
                parent[tag] = parent[parent[tag]]
                tag = parent[tag]
            return tag

        def union(t1: str, t2: str) -> None:
            r1, r2 = find(t1), find(t2)
            if r1 != r2:
                parent[r1] = r2

        # 计算共现强度并合并
        for tag1 in cooccurrence:
            freq1 = cooccurrence[tag1].get(tag1, 1)
            for tag2, count in cooccurrence[tag1].items():
                if tag1 >= tag2:
                    continue
                freq2 = cooccurrence.get(tag2, {}).get(tag2, 1)
                # Jaccard-like 相似度：共现 / (freq1 + freq2 - 共现)
                denominator = freq1 + freq2 - count
                if denominator <= 0:
                    continue
                similarity = count / denominator
                if similarity >= self._config.similarity_threshold:
                    union(tag1, tag2)

        # 收集分组
        groups_map: dict[str, set[str]] = {}
        for tag in all_tags:
            root = find(tag)
            if root not in groups_map:
                groups_map[root] = set()
            groups_map[root].add(tag)

        return [group for group in groups_map.values() if group]

    def _assign_items_to_groups(
        self,
        items: list[dict[str, Any]],
        tag_groups: list[set[str]],
    ) -> dict[str, list[dict[str, Any]]]:
        """将知识条目分配到标签组对应的簇中。

        每个条目分配到与其标签重叠最多的簇。

        Args:
            items: 知识条目列表
            tag_groups: 标签分组列表

        Returns:
            概念名到条目列表的映射
        """
        clusters: dict[str, list[dict[str, Any]]] = {}

        # 为每组选择一个概念名（使用最频繁的标签）
        group_names = self._name_tag_groups(tag_groups, items)

        for item in items:
            item_tags = set(item.get("tags", []))
            if not item_tags:
                # 无标签条目归入"未分类"
                clusters.setdefault("未分类", []).append(item)
                continue

            best_group = -1
            best_overlap = 0
            for i, group in enumerate(tag_groups):
                overlap = len(item_tags & group)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_group = i

            if best_group >= 0:
                concept_name = group_names[best_group]
                clusters.setdefault(concept_name, []).append(item)
            else:
                clusters.setdefault("未分类", []).append(item)

        return clusters

    def _name_tag_groups(
        self,
        tag_groups: list[set[str]],
        items: list[dict[str, Any]],
    ) -> list[str]:
        """为每个标签组选择概念名称。

        使用组内出现频率最高的标签作为概念名。

        Args:
            tag_groups: 标签分组列表
            items: 知识条目列表（用于计算频率）

        Returns:
            概念名列表，与 tag_groups 一一对应
        """
        # 统计标签频率
        tag_freq: dict[str, int] = {}
        for item in items:
            for tag in item.get("tags", []):
                tag_freq[tag] = tag_freq.get(tag, 0) + 1

        names: list[str] = []
        for group in tag_groups:
            # 选择频率最高的标签作为概念名
            best_tag = max(group, key=lambda t: tag_freq.get(t, 0))
            names.append(best_tag)

        return names

    def _merge_small_clusters(
        self, clusters: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        """合并过小的簇到最相关的大簇或"未分类"。

        Args:
            clusters: 原始簇

        Returns:
            合并后的簇
        """
        min_count = self._config.min_items_per_concept
        result: dict[str, list[dict[str, Any]]] = {}
        small_items: list[dict[str, Any]] = []

        for name, items in clusters.items():
            if len(items) >= min_count:
                result[name] = list(items)
            else:
                small_items.extend(items)

        if small_items:
            if "未分类" in result:
                result["未分类"].extend(small_items)
            else:
                result["未分类"] = small_items

        return result

    def _trim_clusters(
        self, clusters: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        """限制簇数量，保留最大的 N 个簇。

        Args:
            clusters: 簇集合

        Returns:
            裁剪后的簇
        """
        sorted_clusters = sorted(
            clusters.items(), key=lambda x: len(x[1]), reverse=True,
        )
        trimmed: dict[str, list[dict[str, Any]]] = {}
        for name, items in sorted_clusters[: self._config.max_concepts]:
            trimmed[name] = items

        # 将被裁剪的条目归入最后一个簇或"未分类"
        remaining_items: list[dict[str, Any]] = []
        for _, items in sorted_clusters[self._config.max_concepts :]:
            remaining_items.extend(items)

        if remaining_items:
            if "未分类" in trimmed:
                trimmed["未分类"].extend(remaining_items)
            else:
                last_name = list(trimmed.keys())[-1] if trimmed else "未分类"
                if last_name in trimmed:
                    trimmed[last_name].extend(remaining_items)
                else:
                    trimmed["未分类"] = remaining_items

        return trimmed

    def _build_concept_page(
        self,
        concept_name: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """构建单个概念页。

        Args:
            concept_name: 概念名称
            items: 该概念下的条目

        Returns:
            概念页字典
        """
        page_id = self._generate_page_id(concept_name, items)
        keywords = self._extract_keywords(items)
        domain = self._infer_domain(keywords)
        summary = self._generate_summary(concept_name, items)
        related_items = [item["id"] for item in items]

        return {
            "page_id": page_id,
            "title": concept_name,
            "concept": concept_name,
            "domain": domain,
            "summary": summary,
            "related_items": related_items,
            "sub_concepts": [],
            "parent_concepts": [],
            "keywords": keywords,
            "item_count": len(items),
        }

    def _generate_page_id(
        self, concept_name: str, items: list[dict[str, Any]],
    ) -> str:
        """生成概念页唯一标识。

        Args:
            concept_name: 概念名称
            items: 条目列表

        Returns:
            page_id 字符串
        """
        raw = f"{concept_name}:{sorted(i['id'] for i in items)}"
        digest = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"page-{concept_name}-{digest}"

    def _extract_keywords(self, items: list[dict[str, Any]]) -> list[str]:
        """从条目中提取关键词（基于标签频率）。

        Args:
            items: 知识条目列表

        Returns:
            关键词列表，按频率降序排列
        """
        tag_freq: dict[str, int] = {}
        for item in items:
            for tag in item.get("tags", []):
                tag_freq[tag] = tag_freq.get(tag, 0) + 1

        sorted_tags = sorted(tag_freq.items(), key=lambda x: x[1], reverse=True)
        return [tag for tag, _ in sorted_tags]

    def _infer_domain(self, keywords: list[str]) -> str:
        """根据关键词推断概念所属领域。

        简单策略：使用最高频的关键词作为领域。
        如果关键词列表为空，返回"通用"。

        Args:
            keywords: 关键词列表

        Returns:
            领域名称
        """
        if not keywords:
            return "通用"

        # 默认使用第一个（最高频的）关键词作为领域
        return keywords[0]

    def _generate_summary(
        self, concept_name: str, items: list[dict[str, Any]],
    ) -> str:
        """生成概念摘要。

        Args:
            concept_name: 概念名称
            items: 相关条目列表

        Returns:
            概要文本
        """
        count = len(items)
        if count == 0:
            return f"{concept_name}：暂无相关知识条目。"
        return f"{concept_name}：共包含 {count} 条相关知识。"

    def _find_parent_relationships(
        self,
        concept_pages: list[dict[str, Any]],
        concept_tags: dict[str, set[str]],
    ) -> dict[str, str | None]:
        """找出每个概念的直接父概念。

        如果概念 A 的关键词是概念 B 的真子集，则 B 是 A 的父概念。
        选择最接近的（最小的）父概念。

        Args:
            concept_pages: 概念页列表
            concept_tags: 概念名到标签集合的映射

        Returns:
            概念名到父概念名的映射（无父则为 None）
        """
        parent_map: dict[str, str | None] = {}

        concepts = [p["concept"] for p in concept_pages]

        for concept in concepts:
            tags = concept_tags.get(concept, set())
            if not tags:
                parent_map[concept] = None
                continue

            # 找最小的父概念（包含当前概念标签的超集中最小的那个）
            best_parent: str | None = None
            best_parent_size = float("inf")

            for other in concepts:
                if other == concept:
                    continue
                other_tags = concept_tags.get(other, set())
                # A 的标签是 B 标签的真子集 → B 是 A 的父
                if tags < other_tags and len(other_tags) < best_parent_size:
                    best_parent = other
                    best_parent_size = len(other_tags)

            parent_map[concept] = best_parent

        return parent_map

    def _build_tree(
        self,
        concept_pages: list[dict[str, Any]],
        parent_map: dict[str, str | None],
        concept_tags: dict[str, set[str]],
    ) -> list[dict[str, Any]]:
        """从父子关系构建树形结构。

        Args:
            concept_pages: 概念页列表
            parent_map: 概念名到父概念名的映射
            concept_tags: 概念名到标签集合的映射

        Returns:
            根节点的 children 列表
        """
        # 构建页面信息索引
        page_by_concept: dict[str, dict[str, Any]] = {
            p["concept"]: p for p in concept_pages
        }

        # 构建子节点映射
        children_map: dict[str | None, list[str]] = {}
        for concept, parent in parent_map.items():
            if parent not in children_map:
                children_map[parent] = []
            children_map[parent].append(concept)

        # 递归构建树（限制深度）
        def _build_node(
            concept_name: str, depth: int,
        ) -> dict[str, Any]:
            page = page_by_concept.get(concept_name, {})
            node: dict[str, Any] = {
                "name": concept_name,
                "page_id": page.get("page_id", ""),
                "keywords": sorted(concept_tags.get(concept_name, set())),
                "children": [],
            }

            if depth < self._config.hierarchy_max_depth:
                child_concepts = children_map.get(concept_name, [])
                node["children"] = [
                    _build_node(child, depth + 1)
                    for child in child_concepts
                ]

            return node

        # 根节点是没有父概念的概念
        root_concepts = children_map.get(None, [])
        return [_build_node(rc, 1) for rc in root_concepts]
