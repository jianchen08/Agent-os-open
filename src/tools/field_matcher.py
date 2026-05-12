"""
字段映射引擎

基于 Schema 相似度生成字段映射建议
"""

import logging
from typing import Any

from src.workflows.types import FieldMappingSuggestion

logger = logging.getLogger(__name__)


class FieldMappingEngine:
    """
    字段映射引擎

    根据源 Schema 和目标 Schema 生成字段映射建议
    """

    def __init__(self):
        """初始化字段映射引擎"""
        self._matchers = [
            ExactNameMatcher(),
            FuzzyNameMatcher(),
            TypeMatcher(),
        ]

    async def suggest_mappings(
        self,
        source_schema: dict[str, Any],
        target_schema: dict[str, Any],
        threshold: float = 0.5,
    ) -> list[FieldMappingSuggestion]:
        """
        生成字段映射建议（按置信度排序）

        Args:
            source_schema: 源 Schema
            target_schema: 目标 Schema
            threshold: 置信度阈值（低于此值的建议会被过滤）

        Returns:
            FieldMappingSuggestion 列表（按置信度降序排序）
        """
        if not source_schema or not target_schema:
            return []

        source_props = source_schema.get("properties", {})
        target_props = target_schema.get("properties", {})

        if not source_props or not target_props:
            return []

        all_suggestions = []

        # 使用所有匹配器生成建议
        for matcher in self._matchers:
            suggestions = await matcher.match(source_props, target_props)
            all_suggestions.extend(suggestions)

        # 去重（保留置信度最高的）
        unique_suggestions = self._deduplicate_suggestions(all_suggestions)

        # 过滤低置信度建议
        filtered = [s for s in unique_suggestions if s.confidence >= threshold]

        # 按置信度降序排序
        filtered.sort(key=lambda x: -x.confidence)

        return filtered

    def _deduplicate_suggestions(
        self,
        suggestions: list[FieldMappingSuggestion],
    ) -> list[FieldMappingSuggestion]:
        """
        去重映射建议，保留每个目标字段的最高置信度映射

        Args:
            suggestions: 原始建议列表

        Returns:
            去重后的建议列表
        """
        # 按 target_field 分组，保留 confidence 最高的
        best_suggestions: dict[str, FieldMappingSuggestion] = {}

        for suggestion in suggestions:
            key = suggestion.target_field
            existing = best_suggestions.get(key)

            if not existing or suggestion.confidence > existing.confidence:
                best_suggestions[key] = suggestion

        return list(best_suggestions.values())


class FieldMatcher:
    """字段匹配器基类"""

    async def match(
        self,
        source_props: dict[str, Any],
        target_props: dict[str, Any],
    ) -> list[FieldMappingSuggestion]:
        """
        匹配字段

        Args:
            source_props: 源属性定义
            target_props: 目标属性定义

        Returns:
            FieldMappingSuggestion 列表
        """
        raise NotImplementedError


class ExactNameMatcher(FieldMatcher):
    """
    精确名称匹配器

    匹配完全相同的字段名
    """

    async def match(
        self,
        source_props: dict[str, Any],
        target_props: dict[str, Any],
    ) -> list[FieldMappingSuggestion]:
        suggestions = []

        for source_field, source_def in source_props.items():
            if source_field in target_props:
                target_def = target_props[source_field]
                source_type = source_def.get("type", "string")
                target_type = target_def.get("type", "string")

                # 精确匹配的置信度最高
                confidence = 1.0
                if source_type != target_type:
                    # 类型不同，置信度略降
                    confidence = 0.95

                suggestions.append(
                    FieldMappingSuggestion(
                        source_field=source_field,
                        target_field=source_field,
                        confidence=confidence,
                        reason="字段名完全匹配",
                        transformation=None,
                    )
                )

        return suggestions


class FuzzyNameMatcher(FieldMatcher):
    """
    模糊名称匹配器

    使用字符串相似度匹配字段名
    """

    async def match(
        self,
        source_props: dict[str, Any],
        target_props: dict[str, Any],
    ) -> list[FieldMappingSuggestion]:
        suggestions = []

        for source_field, source_def in source_props.items():
            best_match = None
            best_score = 0.0

            for target_field, target_def in target_props.items():
                # 跳过精确匹配（已由 ExactNameMatcher 处理）
                if source_field == target_field:
                    continue

                # 计算字符串相似度
                score = self._string_similarity(source_field, target_field)

                if score > best_score and score >= 0.7:  # 相似度阈值
                    best_score = score
                    best_match = (target_field, target_def)

            if best_match:
                target_field, target_def = best_match
                source_type = source_def.get("type", "string")
                target_type = target_def.get("type", "string")

                # 根据相似度和类型兼容性计算置信度
                confidence = best_score * 0.8  # 基础置信度
                if source_type == target_type:
                    confidence = min(confidence + 0.1, 0.9)

                suggestions.append(
                    FieldMappingSuggestion(
                        source_field=source_field,
                        target_field=target_field,
                        confidence=confidence,
                        reason=f"字段名相似（{best_score:.2f}）",
                        transformation=None,
                    )
                )

        return suggestions

    def _string_similarity(self, s1: str, s2: str) -> float:
        """
        计算两个字符串的相似度

        使用简化的编辑距离算法
        """
        if not s1 or not s2:
            return 0.0

        # 转换为小写进行比较
        s1_lower = s1.lower()
        s2_lower = s2.lower()

        if s1_lower == s2_lower:
            return 1.0

        # 检查一个是否是另一个的子串
        if s1_lower in s2_lower or s2_lower in s1_lower:
            return 0.8

        # 简化的编辑距离（Levenshtein distance）
        len1, len2 = len(s1), len(s2)
        if len1 == 0:
            return 0.0
        if len2 == 0:
            return 0.0

        # 动态规划计算编辑距离
        dp = [[0] * (len2 + 1) for _ in range(len1 + 1)]

        for i in range(len1 + 1):
            dp[i][0] = i
        for j in range(len2 + 1):
            dp[0][j] = j

        for i in range(1, len1 + 1):
            for j in range(1, len2 + 1):
                cost = 0 if s1_lower[i - 1] == s2_lower[j - 1] else 1
                dp[i][j] = min(
                    dp[i - 1][j] + 1,  # deletion
                    dp[i][j - 1] + 1,  # insertion
                    dp[i - 1][j - 1] + cost,  # substitution
                )

        max_len = max(len1, len2)
        distance = dp[len1][len2]
        similarity = 1.0 - (distance / max_len)

        return similarity


class TypeMatcher(FieldMatcher):
    """
    类型匹配器

    匹配相同类型的字段（当名称不匹配时）
    """

    # 类型兼容性映射
    TYPE_COMPATIBILITY = {
        ("string", "string"): 1.0,
        ("number", "number"): 1.0,
        ("number", "integer"): 0.9,
        ("integer", "number"): 0.9,
        ("integer", "integer"): 1.0,
        ("boolean", "boolean"): 1.0,
        ("array", "array"): 0.8,
        ("object", "object"): 0.8,
    }

    async def match(
        self,
        source_props: dict[str, Any],
        target_props: dict[str, Any],
    ) -> list[FieldMappingSuggestion]:
        suggestions = []

        # 收集已匹配的字段对（由其他匹配器处理）
        matched_pairs = set()

        for source_field, source_def in source_props.items():
            source_type = source_def.get("type", "string")

            for target_field, target_def in target_props.items():
                # 跳过精确匹配
                if source_field == target_field:
                    matched_pairs.add((source_field, target_field))
                    continue

                target_type = target_def.get("type", "string")

                # 检查类型兼容性
                type_pair = (source_type, target_type)
                reverse_pair = (target_type, source_type)

                compatibility = self.TYPE_COMPATIBILITY.get(
                    type_pair,
                    self.TYPE_COMPATIBILITY.get(reverse_pair, 0.0),
                )

                if compatibility >= 0.7:
                    suggestions.append(
                        FieldMappingSuggestion(
                            source_field=source_field,
                            target_field=target_field,
                            confidence=0.6 * compatibility,  # 类型匹配置信度较低
                            reason=f"字段类型匹配（{source_type} -> {target_type}）",
                            transformation=None,
                        )
                    )

        return suggestions
