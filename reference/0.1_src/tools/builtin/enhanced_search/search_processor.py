"""
搜索结果处理器

暴露接口：
- process_search_results(results: list[dict[str, Any]], query: str, config: ProcessorConfig | None) -> list[dict[str, Any]]：process_search_results功能
- deduplicate_results(results: list[dict[str, Any]], similarity_threshold: float) -> list[dict[str, Any]]：deduplicate_results功能
- filter_results(results: list[dict[str, Any]], blocked_domains: list[str] | None, blocked_keywords: list[str] | None, min_length: int) -> list[dict[str, Any]]：filter_results功能
- to_dict(self) -> dict[str, Any]：to_dict功能
- from_dict(cls, data: dict[str, Any]) -> 'SearchResult'：from_dict功能
- process(self, results: list[dict[str, Any]], query: str, custom_filters: list[Callable[[SearchResult], bool]] | None) -> list[dict[str, Any]]：process功能
- reset(self)：reset功能
- is_duplicate(self, result: dict[str, Any]) -> bool：is_duplicate功能
- deduplicate_batch(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]：deduplicate_batch功能
- add_domain_filter(self, blocked_domains: list[str]) -> 'SearchResultFilter'：add_domain_filter功能
- add_keyword_filter(self, blocked_keywords: list[str]) -> 'SearchResultFilter'：add_keyword_filter功能
- add_length_filter(self, min_length: int, max_length: int) -> 'SearchResultFilter'：add_length_filter功能
- add_custom_filter(self, filter_func: Callable[[dict[str, Any]], bool]) -> 'SearchResultFilter'：add_custom_filter功能
- apply(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]：apply功能
- filter_func(result: dict[str, Any]) -> bool：filter_func功能
- filter_func(result: dict[str, Any]) -> bool：filter_func功能
- filter_func(result: dict[str, Any]) -> bool：filter_func功能
- SearchResult：SearchResult类
- ProcessorConfig：ProcessorConfig类
- SearchResultProcessor：SearchResultProcessor类
- SearchResultDeduplicator：SearchResultDeduplicator类
- SearchResultFilter：SearchResultFilter类
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class SearchResult:
    """搜索结果数据结构"""

    url: str
    title: str
    snippet: str
    source: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "source": self.source,
            "score": self.score,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchResult":
        return cls(
            url=data.get("url", ""),
            title=data.get("title", ""),
            snippet=data.get("snippet", data.get("description", "")),
            source=data.get("source", ""),
            score=data.get("score", 0.0),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ProcessorConfig:
    """处理器配置"""

    # 去重配置
    enable_url_dedup: bool = True
    enable_content_dedup: bool = True
    content_similarity_threshold: float = 0.85

    # 去噪配置
    enable_noise_filter: bool = True
    blocked_domains: list[str] = field(default_factory=list)
    blocked_keywords: list[str] = field(default_factory=list)
    min_snippet_length: int = 20
    max_snippet_length: int = 500

    # 排序配置
    enable_ranking: bool = True
    query_relevance_weight: float = 0.6
    quality_weight: float = 0.4

    # 限制
    max_results: int = 20


class SearchResultProcessor:
    """
    搜索结果处理器

    提供去重、去噪、排序等后处理能力
    """

    # 默认黑名单域名（广告、低质量站点）
    DEFAULT_BLOCKED_DOMAINS = [
        "adservice",
        "adserver",
        "doubleclick",
        "adsystem",
        "advertising",
        "clickserver",
        "trackerserver",
        "trackingserver",
        "popupserver",
        "bannerserver",
    ]

    # 默认噪声关键词
    DEFAULT_NOISE_KEYWORDS = [
        "点击这里",
        "立即购买",
        "限时优惠",
        "click here",
        "buy now",
        "limited offer",
        "subscribe",
        "newsletter",
    ]

    def __init__(self, config: ProcessorConfig | None = None):
        """初始化处理器"""
        self.config = config or ProcessorConfig()

        # 合并默认黑名单
        self.blocked_domains = set(self.DEFAULT_BLOCKED_DOMAINS)
        self.blocked_domains.update(self.config.blocked_domains)

        self.blocked_keywords = set(self.DEFAULT_NOISE_KEYWORDS)
        self.blocked_keywords.update(self.config.blocked_keywords)

    def process(
        self,
        results: list[dict[str, Any]],
        query: str = "",
        custom_filters: list[Callable[[SearchResult], bool]] | None = None,
    ) -> list[dict[str, Any]]:
        """处理搜索结果"""
        # 转换为 SearchResult 对象
        search_results = [SearchResult.from_dict(r) for r in results]

        # 1. 去噪过滤
        if self.config.enable_noise_filter:
            search_results = self._filter_noise(search_results)

        # 2. 应用自定义过滤器
        if custom_filters:
            for filter_func in custom_filters:
                search_results = [r for r in search_results if filter_func(r)]

        # 3. URL 去重
        if self.config.enable_url_dedup:
            search_results = self._dedup_by_url(search_results)

        # 4. 内容相似度去重
        if self.config.enable_content_dedup:
            search_results = self._dedup_by_content(search_results)

        # 5. 计算分数并排序
        if self.config.enable_ranking:
            search_results = self._rank_results(search_results, query)

        # 6. 限制结果数量
        search_results = search_results[: self.config.max_results]

        return [r.to_dict() for r in search_results]

    def _filter_noise(self, results: list[SearchResult]) -> list[SearchResult]:
        """过滤噪声结果"""
        filtered = []

        for result in results:
            # 检查域名黑名单
            if self._is_blocked_domain(result.url):
                continue

            # 检查噪声关键词
            if self._contains_noise_keywords(result):
                continue

            # 检查内容长度
            if not self._check_content_length(result):
                continue

            # 检查 URL 有效性
            if not self._is_valid_url(result.url):
                continue

            filtered.append(result)

        return filtered

    def _is_blocked_domain(self, url: str) -> bool:
        """检查是否为黑名单域名"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            domain_parts = domain.split(".")
            return any(blocked in domain_parts for blocked in self.blocked_domains)
        except Exception:
            return True

    def _contains_noise_keywords(self, result: SearchResult) -> bool:
        """检查是否包含噪声关键词"""
        text = f"{result.title} {result.snippet}".lower()

        return any(keyword.lower() in text for keyword in self.blocked_keywords)

    def _check_content_length(self, result: SearchResult) -> bool:
        """检查内容长度是否合适"""
        snippet_len = len(result.snippet)

        return not snippet_len < self.config.min_snippet_length

    def _is_valid_url(self, url: str) -> bool:
        """检查 URL 是否有效"""
        try:
            parsed = urlparse(url)
            return bool(parsed.scheme and parsed.netloc)
        except Exception:
            return False

    def _dedup_by_url(self, results: list[SearchResult]) -> list[SearchResult]:
        """
        基于 URL 去重

        会规范化 URL 后再比较
        """
        seen_urls = set()
        deduped = []

        for result in results:
            normalized_url = self._normalize_url(result.url)

            if normalized_url not in seen_urls:
                seen_urls.add(normalized_url)
                deduped.append(result)

        return deduped

    def _normalize_url(self, url: str) -> str:
        """
        规范化 URL

        - 移除 tracking 参数
        - 统一协议
        - 移除尾部斜杠
        """
        try:
            parsed = urlparse(url)

            # 移除常见 tracking 参数
            tracking_params = {
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
                "ref",
                "source",
                "fbclid",
                "gclid",
                "msclkid",
            }

            query_params = parse_qs(parsed.query)
            filtered_params = {k: v for k, v in query_params.items() if k.lower() not in tracking_params}

            # 重建 URL
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".lower().rstrip("/")

            if filtered_params:
                param_str = "&".join(f"{k}={v[0]}" for k, v in sorted(filtered_params.items()))
                normalized += f"?{param_str}"

            return normalized
        except Exception:
            return url.lower()

    def _dedup_by_content(self, results: list[SearchResult]) -> list[SearchResult]:
        """
        基于内容相似度去重

        使用标题+摘要的相似度判断
        """
        if not results:
            return results

        deduped = [results[0]]

        for result in results[1:]:
            is_duplicate = False

            for i, existing in enumerate(deduped):
                similarity = self._calculate_similarity(result, existing)

                if similarity >= self.config.content_similarity_threshold:
                    is_duplicate = True
                    # 保留分数更高的
                    if result.score > existing.score:
                        deduped[i] = result
                    break

            if not is_duplicate:
                deduped.append(result)

        return deduped

    def _calculate_similarity(self, a: SearchResult, b: SearchResult) -> float:
        """
        计算两个结果的相似度

        使用标题和摘要的组合相似度
        """
        # 标题相似度（权重 0.4）
        title_sim = SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio()

        # 摘要相似度（权重 0.6）
        snippet_sim = SequenceMatcher(None, a.snippet.lower(), b.snippet.lower()).ratio()

        return title_sim * 0.4 + snippet_sim * 0.6

    def _rank_results(self, results: list[SearchResult], query: str) -> list[SearchResult]:
        """
        对结果进行排序

        综合考虑：
        - 查询相关性
        - 内容质量
        - 来源可信度
        """
        for result in results:
            relevance_score = self._calculate_relevance(result, query)
            quality_score = self._calculate_quality(result)

            result.score = (
                relevance_score * self.config.query_relevance_weight + quality_score * self.config.quality_weight
            )

        # 按分数降序排序
        return sorted(results, key=lambda r: r.score, reverse=True)

    def _calculate_relevance(self, result: SearchResult, query: str) -> float:
        """
        计算查询相关性分数

        基于关键词匹配和位置
        """
        if not query:
            return 0.5

        query_lower = query.lower()
        query_words = set(query_lower.split())

        title_lower = result.title.lower()
        snippet_lower = result.snippet.lower()

        score = 0.0

        # 完整查询匹配
        if query_lower in title_lower:
            score += 0.4
        if query_lower in snippet_lower:
            score += 0.2

        # 关键词匹配
        title_words = set(title_lower.split())
        snippet_words = set(snippet_lower.split())

        title_match = len(query_words & title_words) / len(query_words) if query_words else 0
        snippet_match = len(query_words & snippet_words) / len(query_words) if query_words else 0

        score += title_match * 0.25
        score += snippet_match * 0.15

        return min(score, 1.0)

    def _calculate_quality(self, result: SearchResult) -> float:
        """
        计算内容质量分数

        基于：
        - 内容长度
        - 域名可信度
        - 结构完整性
        """
        score = 0.5  # 基础分

        # 内容长度评分
        snippet_len = len(result.snippet)
        if 50 <= snippet_len <= 300:
            score += 0.2
        elif snippet_len > 300:
            score += 0.1

        # 标题质量
        if result.title and len(result.title) > 10:
            score += 0.1

        # 域名可信度
        domain_score = self._get_domain_trust_score(result.url)
        score += domain_score * 0.2

        return min(score, 1.0)

    def _get_domain_trust_score(self, url: str) -> float:
        """
        获取域名可信度分数

        知名域名给予更高分数
        """
        trusted_domains = {
            "github.com": 0.9,
            "stackoverflow.com": 0.9,
            "docs.python.org": 0.95,
            "developer.mozilla.org": 0.95,
            "wikipedia.org": 0.8,
            "medium.com": 0.6,
            "dev.to": 0.7,
        }

        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            # 检查完整域名
            if domain in trusted_domains:
                return trusted_domains[domain]

            # 检查主域名
            for trusted, score in trusted_domains.items():
                if domain.endswith(trusted):
                    return score

            # 检查是否为官方文档域名
            if "docs." in domain or "doc." in domain:
                return 0.7

            return 0.5
        except Exception:
            return 0.3


class SearchResultDeduplicator:
    """
    搜索结果去重器

    提供多种去重策略的独立组件
    """

    def __init__(self, similarity_threshold: float = 0.85):
        self.similarity_threshold = similarity_threshold
        self._seen_hashes: set = set()

    def reset(self):
        """重置状态"""
        self._seen_hashes.clear()

    def is_duplicate(self, result: dict[str, Any]) -> bool:
        """检查是否为重复结果"""
        content_hash = self._compute_hash(result)

        if content_hash in self._seen_hashes:
            return True

        self._seen_hashes.add(content_hash)
        return False

    def _compute_hash(self, result: dict[str, Any]) -> str:
        """计算结果的内容哈希"""
        # 使用标题和摘要的前100字符
        content = f"{result.get('title', '')}|{result.get('snippet', '')[:100]}"
        return hashlib.md5(content.lower().encode(), usedforsecurity=False).hexdigest()

    def deduplicate_batch(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量去重"""
        self.reset()
        return [r for r in results if not self.is_duplicate(r)]


class SearchResultFilter:
    """
    搜索结果过滤器

    提供可组合的过滤规则
    """

    def __init__(self):
        self._filters: list[Callable[[dict[str, Any]], bool]] = []

    def add_domain_filter(self, blocked_domains: list[str]) -> "SearchResultFilter":
        """添加域名过滤"""
        blocked_set = {d.lower() for d in blocked_domains}

        def filter_func(result: dict[str, Any]) -> bool:
            url = result.get("url", "")
            try:
                domain = urlparse(url).netloc.lower()
                domain_parts = domain.split(".")
                return not any(b in domain_parts for b in blocked_set)
            except Exception:
                return False

        self._filters.append(filter_func)
        return self

    def add_keyword_filter(self, blocked_keywords: list[str]) -> "SearchResultFilter":
        """添加关键词过滤"""
        blocked_set = {k.lower() for k in blocked_keywords}

        def filter_func(result: dict[str, Any]) -> bool:
            text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
            return not any(k in text for k in blocked_set)

        self._filters.append(filter_func)
        return self

    def add_length_filter(self, min_length: int = 20, max_length: int = 1000) -> "SearchResultFilter":
        """添加长度过滤"""

        def filter_func(result: dict[str, Any]) -> bool:
            snippet = result.get("snippet", "")
            return min_length <= len(snippet) <= max_length

        self._filters.append(filter_func)
        return self

    def add_custom_filter(self, filter_func: Callable[[dict[str, Any]], bool]) -> "SearchResultFilter":
        """添加自定义过滤器"""
        self._filters.append(filter_func)
        return self

    def apply(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """应用所有过滤器"""
        filtered = results

        for filter_func in self._filters:
            filtered = [r for r in filtered if filter_func(r)]

        return filtered


# 便捷函数
def process_search_results(
    results: list[dict[str, Any]],
    query: str = "",
    config: ProcessorConfig | None = None,
) -> list[dict[str, Any]]:
    """处理搜索结果的便捷函数"""
    processor = SearchResultProcessor(config)
    return processor.process(results, query)


def deduplicate_results(
    results: list[dict[str, Any]],
    similarity_threshold: float = 0.85,
) -> list[dict[str, Any]]:
    """去重搜索结果的便捷函数"""
    deduplicator = SearchResultDeduplicator(similarity_threshold)
    return deduplicator.deduplicate_batch(results)


def filter_results(
    results: list[dict[str, Any]],
    blocked_domains: list[str] | None = None,
    blocked_keywords: list[str] | None = None,
    min_length: int = 20,
) -> list[dict[str, Any]]:
    """过滤搜索结果的便捷函数"""
    filter_builder = SearchResultFilter()

    if blocked_domains:
        filter_builder.add_domain_filter(blocked_domains)

    if blocked_keywords:
        filter_builder.add_keyword_filter(blocked_keywords)

    filter_builder.add_length_filter(min_length=min_length)

    return filter_builder.apply(results)
