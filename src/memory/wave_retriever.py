"""波浪算法 RAG 检索器。

参考 VCPToolBox 的波浪算法实现，融合四种检索策略：
- EPA 分析（Entity-Property-Action）：多维度文本分解与匹配
- 残差金字塔：多层次递进检索，保留残差传递
- 浪潮扩散：多跳传播发现远距离关联
- 霰弹枪检索：多角度并行查询与融合

暴露接口：
- WaveRetrieverConfig: 波浪检索器配置
- WaveRetriever: 波浪算法检索器
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from memory.ports import IRetriever
from memory.types import MemoryType, SearchResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 中文停用词（高频虚词）
_CN_STOP_WORDS: frozenset[str] = frozenset(
    {
        "的",
        "了",
        "在",
        "是",
        "我",
        "有",
        "和",
        "就",
        "不",
        "人",
        "都",
        "一",
        "一个",
        "上",
        "也",
        "很",
        "到",
        "说",
        "要",
        "去",
        "你",
        "会",
        "着",
        "没有",
        "看",
        "好",
        "自己",
        "这",
    }
)

# 中文动词后缀
_CN_VERB_SUFFIXES: tuple[str, ...] = (
    "处理",
    "分析",
    "执行",
    "优化",
    "管理",
    "查询",
    "检索",
    "搜索",
    "创建",
    "删除",
    "更新",
    "部署",
    "运行",
    "设计",
    "开发",
    "实现",
    "构建",
    "测试",
    "调试",
    "配置",
    "安装",
    "编程",
    "详解",
    "实践",
)

# 英文停用词
_EN_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "can",
        "could",
        "of",
        "in",
        "to",
        "for",
        "with",
        "on",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "because",
        "if",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
    }
)

# 英文动词列表（常见行为动词）
_EN_VERBS: frozenset[str] = frozenset(
    {
        "process",
        "analyze",
        "execute",
        "optimize",
        "manage",
        "query",
        "retrieve",
        "search",
        "create",
        "delete",
        "update",
        "deploy",
        "run",
        "design",
        "develop",
        "implement",
        "build",
        "test",
        "debug",
        "configure",
        "install",
        "handle",
        "compute",
        "generate",
        "transform",
        "extract",
        "merge",
        "split",
        "validate",
        "parse",
    }
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _vector_norm(vec: list[float]) -> float:
    """计算向量 L2 范数（纯 Python 实现）。

    Args:
        vec: 向量

    Returns:
        向量范数
    """
    return math.sqrt(sum(v * v for v in vec))


def _tokenize(text: str) -> list[str]:
    """将文本分割为 token 列表。

    同时支持中文（逐字符 + 常见词组）和英文（按空格/标点分割）。

    Args:
        text: 输入文本

    Returns:
        token 列表
    """
    tokens: list[str] = []

    # 英文单词：2 个字符以上
    en_words = re.findall(r"[a-zA-Z]{2,}", text)
    tokens.extend(w.lower() for w in en_words)

    # 中文：2-4 字词组（滑动窗口）+ 单字
    cn_chars = re.findall(r"[\u4e00-\u9fff]", text)
    for char in cn_chars:
        tokens.append(char)
    for i in range(len(cn_chars) - 1):
        tokens.append(cn_chars[i] + cn_chars[i + 1])
    for i in range(len(cn_chars) - 2):
        tokens.append(cn_chars[i] + cn_chars[i + 1] + cn_chars[i + 2])
    for i in range(len(cn_chars) - 3):
        tokens.append(cn_chars[i] + cn_chars[i + 1] + cn_chars[i + 2] + cn_chars[i + 3])

    return tokens


def _extract_keywords(text: str) -> list[str]:
    """从文本中提取关键词（去停用词后的 token）。

    Args:
        text: 输入文本

    Returns:
        去重后的关键词列表
    """
    tokens = _tokenize(text)
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokens:
        if tok in _CN_STOP_WORDS or tok in _EN_STOP_WORDS:
            continue
        if len(tok) == 1 and re.match(r"[\u4e00-\u9fff]", tok):
            # 单个中文字跳过（太短，信息量不足）
            continue
        if tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


def _map_memory_type(memory_type: str) -> MemoryType:
    """将字符串映射为 MemoryType 枚举。

    Args:
        memory_type: 记忆类型字符串

    Returns:
        对应的 MemoryType 枚举值
    """
    mapping: dict[str, MemoryType] = {
        "semantic": MemoryType.SEMANTIC,
        "episode": MemoryType.EPISODE,
        "procedural": MemoryType.PROCEDURAL,
    }
    return mapping.get(memory_type, MemoryType.SEMANTIC)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class WaveRetrieverConfig:
    """波浪检索器配置。

    Attributes:
        max_hops: 浪潮扩散最大跳数
        decay_factor: 每跳衰减因子
        shotgun_angles: 霰弹枪检索角度数
        epa_weight: EPA 匹配权重
        residual_weight: 残差匹配权重
        wave_weight: 浪潮扩散权重
        shotgun_weight: 霰弹枪检索权重
        min_score: 最低返回分数阈值
    """

    max_hops: int = 3
    decay_factor: float = 0.7
    shotgun_angles: int = 3
    epa_weight: float = 0.3
    residual_weight: float = 0.3
    wave_weight: float = 0.2
    shotgun_weight: float = 0.2
    min_score: float = 0.1


# ---------------------------------------------------------------------------
# 检索器
# ---------------------------------------------------------------------------


class WaveRetriever(IRetriever):  # type: ignore[misc]
    """波浪算法检索器。

    融合 EPA 分析、残差金字塔、浪潮扩散和霰弹枪检索四种策略，
    实现多层次、多角度的知识检索。

    Attributes:
        _embedding_service: 可选的向量嵌入服务
        _tag_network: 可选的 TagNetworkRetriever 实例
        _knowledge_items_provider: 异步 callable，返回知识条目列表
        _config: 检索器配置
    """

    def __init__(
        self,
        embedding_service: Any = None,
        tag_network: Any = None,
        knowledge_items_provider: Any = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """初始化波浪检索器。

        Args:
            embedding_service: 可选，用于生成向量嵌入
            tag_network: 可选，TagNetworkRetriever 实例
            knowledge_items_provider: 可选，async callable 返回知识列表
            config: 配置字典，覆盖默认配置
        """
        self._embedding_service = embedding_service
        self._tag_network = tag_network
        self._knowledge_items_provider = knowledge_items_provider
        self._config = self._build_config(config)

    @staticmethod
    def _build_config(config_dict: dict[str, Any] | None) -> WaveRetrieverConfig:
        """从字典构建配置对象。

        Args:
            config_dict: 配置字典

        Returns:
            配置实例
        """
        if not config_dict:
            return WaveRetrieverConfig()
        valid_keys = {f.name for f in WaveRetrieverConfig.__dataclass_fields__.values()}
        filtered = {k: v for k, v in config_dict.items() if k in valid_keys}
        return WaveRetrieverConfig(**filtered)

    # ======================================================================
    # 公开接口
    # ======================================================================

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """检索相关记忆。

        Args:
            query: 查询文本
            user_id: 用户 ID（用于过滤）
            top_k: 返回数量上限
            memory_type: 记忆类型（semantic / episode / procedural）
            filters: 额外过滤条件

        Returns:
            搜索结果列表，按相关性降序排列
        """
        if not query or not query.strip():
            return []

        if not self._knowledge_items_provider:
            logger.debug("[WaveRetriever] 无 knowledge_items_provider，返回空")
            return []

        # 获取知识条目
        all_items = await self._knowledge_items_provider()
        if not all_items:
            return []

        # 应用 filters 过滤
        items = self._apply_filters(all_items, user_id, filters)

        mt_enum = _map_memory_type(memory_type)

        # 四路并行检索
        residual_results = self._residual_pyramid(query, items)
        wave_results = self._wave_diffusion(residual_results, items)
        shotgun_results = self._shotgun_retrieve(query, items)

        # EPA 维度单独打分
        epa_results = self._epa_score(query, items)

        # 加权合并
        result_groups: list[list[SearchResult]] = []
        if residual_results:
            result_groups.append(
                self._scale_scores(residual_results, self._config.residual_weight),
            )
        if wave_results:
            result_groups.append(
                self._scale_scores(wave_results, self._config.wave_weight),
            )
        if shotgun_results:
            result_groups.append(
                self._scale_scores(shotgun_results, self._config.shotgun_weight),
            )
        if epa_results:
            result_groups.append(
                self._scale_scores(epa_results, self._config.epa_weight),
            )

        merged = self._merge_results(result_groups)

        # 过滤低分
        merged = [r for r in merged if r.score >= self._config.min_score]

        # 设置 memory_type
        for r in merged:
            r.memory_type = mt_enum

        # 截断
        merged = merged[:top_k]

        logger.debug(
            "[WaveRetriever] 检索完成 | query='%s' | results=%d",
            query[:30],
            len(merged),
        )
        return merged

    # ======================================================================
    # EPA 分析
    # ======================================================================

    def _extract_epa(self, text: str) -> dict[str, list[str]]:  # noqa: PLR0912
        """EPA 分析：将文本分解为 Entity、Property、Action 三个维度。

        基于关键词和简单规则提取，不依赖 NLP 库。

        Args:
            text: 输入文本

        Returns:
            包含 entity / property / action 三个列表的字典
        """
        result: dict[str, list[str]] = {
            "entity": [],
            "property": [],
            "action": [],
        }

        if not text or not text.strip():
            return result

        seen_entity: set[str] = set()
        seen_property: set[str] = set()
        seen_action: set[str] = set()

        # ---------- 提取数字及量词 ----------
        numbers = re.findall(r"\d+", text)
        for num in numbers:
            if num not in seen_property:
                seen_property.add(num)
                result["property"].append(num)

        # ---------- 提取中文 EPA ----------
        cn_chars = re.findall(r"[\u4e00-\u9fff]+", text)

        for segment in cn_chars:
            # 中文动词
            for verb in _CN_VERB_SUFFIXES:
                if verb in segment and verb not in seen_action:
                    seen_action.add(verb)
                    result["action"].append(verb)

            # 中文名词：滑动窗口提取 2-4 字词组
            for length in (4, 3, 2):
                i = 0
                while i <= len(segment) - length:
                    phrase = segment[i : i + length]
                    if (
                        phrase not in _CN_STOP_WORDS
                        and phrase not in seen_entity
                        and not any(phrase.endswith(v) for v in _CN_VERB_SUFFIXES if len(v) < len(phrase))
                    ):
                        seen_entity.add(phrase)
                        result["entity"].append(phrase)
                    i += 1

            # 单字名词（2 字以上片段中的每个非停用词单字）
            for char in segment:
                if len(char) == 1 and char not in _CN_STOP_WORDS and char not in seen_entity:
                    seen_entity.add(char)
                    result["entity"].append(char)

        # ---------- 提取英文 EPA ----------
        en_words = re.findall(r"[a-zA-Z]+", text)

        for word in en_words:
            lower = word.lower()
            if lower in _EN_STOP_WORDS or len(lower) < 2:
                continue

            if lower in _EN_VERBS:
                if lower not in seen_action:
                    seen_action.add(lower)
                    result["action"].append(lower)
            elif lower not in seen_entity:
                seen_entity.add(lower)
                result["entity"].append(lower)

        # ---------- 提取中文形容词/副词属性 ----------
        adj_patterns = re.findall(
            r"[\u4e00-\u9fff]*?(高|低|快|慢|大|小|多|少|优|劣|强|弱|新|旧|简单|复杂|稳定|高效|智能|自动|快速|安全|可靠)",
            text,
        )
        for adj in adj_patterns:
            if adj and adj not in seen_property:
                seen_property.add(adj)
                result["property"].append(adj)

        return result

    def _epa_score(
        self,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[SearchResult]:
        """基于 EPA 维度匹配进行打分。

        Args:
            query: 查询文本
            candidates: 候选知识条目列表

        Returns:
            EPA 匹配得分后的搜索结果列表
        """
        query_epa = self._extract_epa(query)
        results: list[SearchResult] = []

        query_terms: set[str] = set()
        for key in ("entity", "property", "action"):
            query_terms.update(t.lower() for t in query_epa.get(key, []))

        for item in candidates:
            item_epa = item.get("epa", {})
            if not item_epa:
                # 没有 EPA 数据时，用 content 关键词匹配
                content_terms = {t.lower() for t in _extract_keywords(item.get("content", ""))}
                overlap = query_terms & content_terms
                if overlap:
                    score = len(overlap) / max(len(query_terms), 1)
                    results.append(
                        SearchResult(
                            id=item["id"],
                            content=item.get("content", ""),
                            score=min(score, 1.0),
                        )
                    )
                continue

            item_terms: set[str] = set()
            for key in ("entity", "property", "action"):
                item_terms.update(t.lower() for t in item_epa.get(key, []))

            if not query_terms:
                continue

            overlap = query_terms & item_terms
            if overlap:
                score = len(overlap) / max(len(query_terms), 1)
                results.append(
                    SearchResult(
                        id=item["id"],
                        content=item.get("content", ""),
                        score=min(score, 1.0),
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ======================================================================
    # 残差金字塔
    # ======================================================================

    def _residual_pyramid(
        self,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[SearchResult]:
        """残差金字塔：多层次递进检索。

        第 1 层（粗粒度）：关键词快速匹配，获取候选集。
        第 2 层（中粒度）：EPA 维度匹配，精化候选集。
        第 3 层（细粒度）：语义向量相似度（如有 embedding），最终排序。

        每层保留与上层预测的残差（未匹配部分），传递到下一层继续匹配。

        Args:
            query: 查询文本
            candidates: 候选知识条目列表

        Returns:
            排序后的搜索结果列表
        """
        if not candidates:
            return []

        query_keywords = {kw.lower() for kw in _extract_keywords(query)}
        query_epa = self._extract_epa(query)
        query_vec = self._get_query_embedding(query)

        scored: dict[str, float] = {}

        for item in candidates:
            item_id = item.get("id", "")
            total = 0.0

            # --- 第 1 层：关键词匹配 ---
            content_keywords = {kw.lower() for kw in _extract_keywords(item.get("content", ""))}
            tag_set = {t.lower() for t in item.get("tags", [])}
            all_kw = content_keywords | tag_set

            if query_keywords:
                kw_overlap = query_keywords & all_kw
                kw_score = len(kw_overlap) / max(len(query_keywords), 1)
                total += kw_score * 0.4

            # --- 第 2 层：EPA 维度匹配 ---
            item_epa = item.get("epa", {})
            if item_epa:
                for dim in ("entity", "property", "action"):
                    q_terms = {t.lower() for t in query_epa.get(dim, [])}
                    i_terms = {t.lower() for t in item_epa.get(dim, [])}
                    if q_terms and i_terms:
                        dim_overlap = q_terms & i_terms
                        if dim_overlap:
                            total += (len(dim_overlap) / max(len(q_terms), 1)) * 0.3

            # --- 第 3 层：向量相似度 ---
            item_vec = item.get("embedding")
            if query_vec is not None and item_vec is not None:
                vec_sim = self._cosine_similarity(query_vec, item_vec)
                total += vec_sim * 0.3

            if total > 0:
                scored[item_id] = min(total, 1.0)

        results: list[SearchResult] = []
        for item in candidates:
            item_id = item.get("id", "")
            if item_id in scored:
                results.append(
                    SearchResult(
                        id=item_id,
                        content=item.get("content", ""),
                        score=scored[item_id],
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ======================================================================
    # 浪潮扩散
    # ======================================================================

    def _wave_diffusion(  # noqa: PLR0912
        self,
        initial_results: list[SearchResult],
        all_items: list[dict[str, Any]],
    ) -> list[SearchResult]:
        """浪潮扩散：从初始匹配出发，多跳传播发现远距离关联。

        第 1 跳：直接匹配的知识条目。
        第 2 跳：通过共享 Tag / EPA 维度关联的条目。
        第 3 跳：通过共现关系间接关联的条目。

        每跳有衰减因子（decay_factor^n），距离越远得分越低。

        Args:
            initial_results: 初始匹配结果
            all_items: 全部知识条目

        Returns:
            扩散后的搜索结果列表
        """
        if not initial_results:
            return []

        # 构建索引
        item_map: dict[str, dict[str, Any]] = {item["id"]: item for item in all_items if "id" in item}

        # 初始分数
        scores: dict[str, float] = {r.id: r.score for r in initial_results}
        visited: set[str] = set(scores.keys())
        frontier: set[str] = set(scores.keys())

        decay = self._config.decay_factor

        for hop in range(1, self._config.max_hops + 1):
            next_frontier: set[str] = set()

            for node_id in frontier:
                if node_id not in item_map:
                    continue
                node = item_map[node_id]
                parent_score = scores.get(node_id, 0.0)
                if parent_score <= 0:
                    continue

                # 邻居：related_ids + 共享 tag 的条目
                neighbors = set(node.get("related_ids", []))
                node_tags = {t.lower() for t in node.get("tags", [])}
                node_epa = node.get("epa", {})

                for other_id, other in item_map.items():
                    if other_id in visited or other_id == node_id:
                        continue
                    # 通过 tag 关联
                    other_tags = {t.lower() for t in other.get("tags", [])}
                    if node_tags and other_tags and (node_tags & other_tags):
                        neighbors.add(other_id)
                        continue
                    # 通过 EPA 关联
                    other_epa = other.get("epa", {})
                    if node_epa and other_epa:
                        for dim in ("entity", "action"):
                            n_set = {t.lower() for t in node_epa.get(dim, [])}
                            o_set = {t.lower() for t in other_epa.get(dim, [])}
                            if n_set and o_set and (n_set & o_set):
                                neighbors.add(other_id)
                                break

                for neighbor_id in neighbors:
                    if neighbor_id in visited:
                        continue
                    hop_score = parent_score * (decay**hop)
                    if neighbor_id not in scores or hop_score > scores.get(neighbor_id, 0.0):
                        scores[neighbor_id] = hop_score
                    next_frontier.add(neighbor_id)

            visited.update(next_frontier)
            frontier = next_frontier

        results: list[SearchResult] = []
        for item_id, score in scores.items():
            content = item_map.get(item_id, {}).get("content", "")
            results.append(SearchResult(id=item_id, content=content, score=score))

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ======================================================================
    # 霰弹枪检索
    # ======================================================================

    def _shotgun_retrieve(
        self,
        query: str,
        all_items: list[dict[str, Any]],
    ) -> list[SearchResult]:
        """霰弹枪检索：同时从多个角度发射查询并合并结果。

        角度 1：原始查询文本的关键词匹配。
        角度 2：EPA 分解后的各维度查询。
        角度 3：查询改写（同义词扩展、概念泛化）。

        Args:
            query: 查询文本
            all_items: 全部知识条目

        Returns:
            合并去重后的搜索结果列表
        """
        result_groups: list[list[SearchResult]] = []

        # 角度 1：关键词匹配
        kw_results = self._keyword_match(query, all_items)
        if kw_results:
            result_groups.append(kw_results)

        # 角度 2：EPA 维度匹配
        epa_results = self._epa_score(query, all_items)
        if epa_results:
            result_groups.append(epa_results)

        # 角度 3：查询改写（同义词扩展、概念泛化）
        rewritten = self._rewrite_query(query)
        for rw in rewritten:
            rw_results = self._keyword_match(rw, all_items)
            if rw_results:
                result_groups.append(
                    self._scale_scores(rw_results, 0.8),
                )

        return self._merge_results(result_groups)

    def _keyword_match(
        self,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[SearchResult]:
        """基于关键词的快速匹配。

        Args:
            query: 查询文本
            candidates: 候选知识条目列表

        Returns:
            匹配的搜索结果列表
        """
        query_keywords = {kw.lower() for kw in _extract_keywords(query)}
        if not query_keywords:
            return []

        results: list[SearchResult] = []
        for item in candidates:
            content = item.get("content", "")
            tags = item.get("tags", [])
            content_kw = {kw.lower() for kw in _extract_keywords(content)}
            tag_set = {t.lower() for t in tags}
            all_kw = content_kw | tag_set
            overlap = query_keywords & all_kw

            if overlap:
                score = len(overlap) / max(len(query_keywords), 1)
                results.append(
                    SearchResult(
                        id=item["id"],
                        content=content,
                        score=min(score, 1.0),
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def _rewrite_query(self, query: str) -> list[str]:
        """查询改写：生成同义词扩展和概念泛化版本。

        Args:
            query: 原始查询

        Returns:
            改写后的查询列表
        """
        rewritten: list[str] = []
        epa = self._extract_epa(query)

        # EPA 实体拼接
        if epa.get("entity"):
            rewritten.append(" ".join(epa["entity"]))

        # EPA 动词拼接
        if epa.get("action"):
            entity_str = " ".join(epa.get("entity", []))
            action_str = " ".join(epa["action"])
            if entity_str:
                rewritten.append(f"{entity_str} {action_str}")

        return rewritten

    # ======================================================================
    # 结果合并
    # ======================================================================

    def _merge_results(
        self,
        result_groups: list[list[SearchResult]],
    ) -> list[SearchResult]:
        """合并多路检索结果，按 ID 去重，保留最高分，按得分降序排列。

        Args:
            result_groups: 多组搜索结果列表

        Returns:
            合并去重后的搜索结果列表
        """
        best: dict[str, SearchResult] = {}

        for group in result_groups:
            for result in group:
                if result.id in best:
                    if result.score > best[result.id].score:
                        best[result.id] = result
                else:
                    best[result.id] = result

        merged = sorted(best.values(), key=lambda r: r.score, reverse=True)
        return merged

    @staticmethod
    def _scale_scores(
        results: list[SearchResult],
        factor: float,
    ) -> list[SearchResult]:
        """按权重因子缩放搜索结果的分数。

        Args:
            results: 搜索结果列表
            factor: 缩放因子

        Returns:
            缩放后的搜索结果列表（新对象）
        """
        return [
            SearchResult(
                id=r.id,
                content=r.content,
                score=r.score * factor,
                memory_type=r.memory_type,
                metadata=r.metadata,
                highlight=r.highlight,
            )
            for r in results
        ]

    # ======================================================================
    # 向量工具
    # ======================================================================

    def _cosine_similarity(
        self,
        vec_a: list[float],
        vec_b: list[float],
    ) -> float:
        """计算两个向量的余弦相似度。

        纯 Python 实现，不依赖 numpy/scipy。

        Args:
            vec_a: 向量 A
            vec_b: 向量 B

        Returns:
            余弦相似度 [-1, 1]，零向量或维度不匹配返回 0.0
        """
        if len(vec_a) != len(vec_b) or not vec_a:
            return 0.0

        norm_a = _vector_norm(vec_a)
        norm_b = _vector_norm(vec_b)

        if norm_a < 1e-9 or norm_b < 1e-9:
            return 0.0

        dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=False))
        return dot / (norm_a * norm_b)

    # ======================================================================
    # 内部工具
    # ======================================================================

    def _get_query_embedding(self, query: str) -> list[float] | None:
        """获取查询文本的向量嵌入。

        优先使用注入的 embedding_service，否则返回 None。

        Args:
            query: 查询文本

        Returns:
            向量嵌入列表，不可用时返回 None
        """
        if self._embedding_service is not None:
            try:
                embed_fn = getattr(self._embedding_service, "embed", None)
                if embed_fn is not None and callable(embed_fn):
                    result: Any = embed_fn(query)
                    if isinstance(result, list):
                        return result
            except Exception:
                logger.debug("[WaveRetriever] embedding_service 调用失败", exc_info=True)
        return None

    @staticmethod
    def _apply_filters(
        items: list[dict[str, Any]],
        user_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """应用 user_id 和 filters 过滤知识条目。

        Args:
            items: 原始知识条目列表
            user_id: 用户 ID（可选过滤）
            filters: 额外过滤条件

        Returns:
            过滤后的条目列表
        """
        result = items

        if user_id:
            result = [it for it in result if it.get("user_id") in (user_id, None, "")]

        if filters:
            for key, value in filters.items():
                if value is None:
                    continue
                result = [it for it in result if it.get(key) == value or it.get(key) is None]

        return result

    def get_stats(self) -> dict[str, Any]:
        """获取检索器统计信息。

        Returns:
            统计信息字典
        """
        return {
            "has_embedding_service": self._embedding_service is not None,
            "has_tag_network": self._tag_network is not None,
            "has_provider": self._knowledge_items_provider is not None,
            "config": {
                "max_hops": self._config.max_hops,
                "decay_factor": self._config.decay_factor,
                "min_score": self._config.min_score,
            },
        }
