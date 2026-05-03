"""语义预处理器模块。

实现 LLM 智能预处理（语义分块 + 多维打标），
基于纯 Python 实现，不依赖外部 NLP 库。

暴露接口：
- SemanticPreprocessorConfig: 语义预处理器配置
- SemanticPreprocessor: 语义预处理器
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# 停用词表
# ============================================================

_ENGLISH_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "am", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "dare", "it", "its", "he", "she", "they", "them", "their", "his",
    "her", "we", "us", "our", "you", "your", "this", "that", "these",
    "those", "which", "who", "whom", "what", "where", "when", "how",
    "not", "no", "nor", "if", "then", "than", "so", "as", "up", "out",
    "just", "over", "about", "into", "through", "during", "before",
    "after", "above", "below", "between", "under", "again", "further",
    "once", "here", "there", "all", "each", "every", "both", "few",
    "more", "most", "other", "some", "such", "only", "own", "same",
    "too", "very", "also",
})

_CHINESE_STOP_WORDS: frozenset[str] = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
    "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
    "会", "着", "没有", "看", "好", "自己", "这", "他", "她", "它",
    "们", "那", "些", "什么", "怎么", "如果", "因为", "所以", "但是",
    "而且", "或者", "以", "及", "等", "吧", "吗", "呢", "啊", "哦",
    "嗯", "呀", "把", "被", "让", "给", "从", "向", "对", "与",
    "而", "又", "还", "则", "虽", "却", "已", "已经", "过", "来",
    "得", "地", "做", "能", "可以", "可", "这个", "那个",
    "其", "之", "中", "里", "下", "后", "前", "时", "年", "月",
    "日", "大", "小", "多", "少",
})

_ALL_STOP_WORDS: frozenset[str] = _ENGLISH_STOP_WORDS | _CHINESE_STOP_WORDS

# 领域关键词映射
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "技术": [
        "python", "java", "javascript", "docker", "kubernetes", "api",
        "database", "server", "client", "framework", "algorithm",
        "编程", "代码", "程序", "架构", "部署", "容器", "微服务",
        "开发", "框架", "库", "接口", "数据库", "服务器", "算法",
    ],
    "商业": [
        "market", "revenue", "profit", "business", "strategy",
        "市场", "收入", "利润", "商业", "策略", "营销", "投资",
        "客户", "销售", "品牌", "竞争",
    ],
    "科学": [
        "research", "experiment", "hypothesis", "theory", "analysis",
        "研究", "实验", "假设", "理论", "分析", "数据", "样本",
        "统计", "概率", "模型",
    ],
    "艺术": [
        "design", "music", "painting", "creative", "art",
        "设计", "音乐", "绘画", "创作", "艺术", "风格", "美学",
    ],
}

# 操作动词映射
_ACTION_VERBS: dict[str, str] = {
    "创建": "create", "建立": "create", "新建": "create", "添加": "create",
    "新增": "create", "生成": "create", "构建": "create",
    "删除": "delete", "移除": "delete", "清除": "delete", "去掉": "delete",
    "丢弃": "delete", "remove": "delete", "delete": "delete",
    "查询": "query", "搜索": "query", "查找": "query", "获取": "query",
    "读取": "query", "find": "query", "search": "query", "get": "query",
    "更新": "update", "修改": "update", "编辑": "update", "变更": "update",
    "调整": "update", "update": "update", "edit": "update", "modify": "update",
    "分析": "analyze", "评估": "analyze", "计算": "analyze", "统计": "analyze",
    "analyze": "analyze",
}

# 情感词表
_POSITIVE_WORDS: frozenset[str] = frozenset({
    "优秀", "好", "棒", "出色", "成功", "完美", "显著", "突出",
    "值得", "推广", "进步", "增长", "提升", "改善", "高效",
    "excellent", "good", "great", "amazing", "wonderful", "perfect",
    "success", "outstanding", "brilliant", "fantastic",
})

_NEGATIVE_WORDS: frozenset[str] = frozenset({
    "差", "糟糕", "失败", "错误", "问题", "缺陷", "不足", "低效",
    "退步", "下降", "损失", "风险", "困难", "恶劣",
    "bad", "terrible", "awful", "fail", "failure", "error", "wrong",
    "poor", "worst", "problem", "issue",
})


@dataclass
class SemanticPreprocessorConfig:
    """语义预处理器配置。

    Attributes:
        max_chunk_size: 最大块大小（字符数）
        min_chunk_size: 最小块大小（字符数）
        overlap_size: 块之间的重叠字符数
        quality_threshold: 最低质量分阈值
        max_tags_per_chunk: 每块最大标签数
    """

    max_chunk_size: int = 500
    min_chunk_size: int = 50
    overlap_size: int = 50
    quality_threshold: float = 0.3
    max_tags_per_chunk: int = 10


class SemanticPreprocessor:
    """语义预处理器。

    实现 LLM 智能预处理：语义分块 + 多维打标 + 质量评估。
    纯 Python 实现，不依赖外部 NLP 库。

    Attributes:
        _config: 预处理器配置
    """

    def __init__(self, config: SemanticPreprocessorConfig | None = None) -> None:
        """初始化语义预处理器。

        Args:
            config: 预处理器配置，为 None 时使用默认配置
        """
        self._config = config or SemanticPreprocessorConfig()

    def semantic_chunk(self, text: str, max_chunk_size: int = 500) -> list[dict[str, Any]]:
        """语义分块：按语义边界分割长文本。

        按以下优先级识别语义边界：
        1. 段落边界（双换行）
        2. 句子边界（句号、问号、感叹号后跟空格或换行）
        3. 强制分割（单个语义单元超过 max_chunk_size）

        Args:
            text: 待分割文本
            max_chunk_size: 最大块大小，0 或负数时使用默认值 500

        Returns:
            分块列表，每项含 content, boundaries, metadata
        """
        if max_chunk_size <= 0:
            max_chunk_size = 500

        text = text.strip()
        if not text:
            return []

        # 步骤 1: 按段落边界分割（段落边界始终保留）
        segments = self._split_by_paragraphs(text)

        # 步骤 2: 对超长段落，先在句子边界处分割
        sentence_split: list[str] = []
        for seg in segments:
            if len(seg) > max_chunk_size:
                sentence_split.extend(self._split_by_sentences(seg, max_chunk_size))
            else:
                sentence_split.append(seg)

        # 步骤 3: 强制分割剩余超长文本
        final_chunks: list[str] = []
        for seg in sentence_split:
            if len(seg) > max_chunk_size:
                final_chunks.extend(self._force_split(seg, max_chunk_size))
            else:
                final_chunks.append(seg)

        # 步骤 5: 添加重叠
        result = self._add_overlap(final_chunks)

        # 步骤 6: 构建返回结构
        offset = 0
        chunks_with_meta: list[dict[str, Any]] = []
        for chunk_data in result:
            content = chunk_data["content"]
            boundaries = chunk_data["boundaries"]
            start = text.find(content, max(0, offset - 10))
            if start == -1:
                start = offset
            end = start + len(content)
            offset = end

            chunks_with_meta.append({
                "content": content,
                "boundaries": boundaries,
                "metadata": {
                    "start": start,
                    "end": end,
                    "length": len(content),
                },
            })

        logger.debug(
            "[SemanticPreprocessor] 分块完成 | input_len=%d | chunks=%d",
            len(text), len(chunks_with_meta),
        )
        return chunks_with_meta

    def multi_dimension_tag(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """多维打标：为内容块提取多维度标签。

        提取维度包括：主题、领域、实体、操作类型、情感。

        Args:
            chunk: 内容块，需含 content 字段

        Returns:
            打标结果，含 tags, dimensions, confidence
        """
        content = chunk.get("content", "")
        if not content or not content.strip():
            return {
                "tags": [],
                "dimensions": {
                    "topic": "",
                    "domain": "",
                    "entities": [],
                    "action_type": "",
                    "sentiment": "neutral",
                },
                "confidence": 0.0,
            }

        # 主题标签：高频词
        topic = self._extract_topic(content)

        # 领域标签
        domain = self._detect_domain(content)

        # 实体标签
        entities = self._extract_entities(content)

        # 操作类型
        action_type = self._detect_action_type(content)

        # 情感
        sentiment = self._detect_sentiment(content)

        # 汇总标签（去重，过滤停用词）
        all_tags: list[str] = []
        seen: set[str] = set()
        tag_sources = [topic, domain] + entities
        if action_type:
            tag_sources.append(action_type)
        if sentiment != "neutral":
            tag_sources.append(sentiment)

        for tag in tag_sources:
            tag_lower = tag.lower()
            if tag and tag_lower not in seen and tag_lower not in _ALL_STOP_WORDS:
                seen.add(tag_lower)
                all_tags.append(tag)

        # 截断
        all_tags = all_tags[: self._config.max_tags_per_chunk]

        # 置信度：基于提取到的标签数量和信息量
        confidence = self._compute_tag_confidence(content, all_tags)

        return {
            "tags": all_tags,
            "dimensions": {
                "topic": topic,
                "domain": domain,
                "entities": entities,
                "action_type": action_type,
                "sentiment": sentiment,
            },
            "confidence": confidence,
        }

    def assess_quality(self, chunk: dict[str, Any]) -> float:
        """块质量评估：评估分块的语义完整性和信息密度。

        综合评估：
        - 信息密度（有效词占比）
        - 语义完整性（句子是否完整）
        - 标签覆盖度（提取的标签数量和置信度）

        Args:
            chunk: 内容块，需含 content 字段

        Returns:
            0-1 质量分，0 为最低质量
        """
        content = chunk.get("content", "")
        if not content or not content.strip():
            return 0.0

        score_info_density = self._compute_info_density(content)
        score_completeness = self._compute_completeness(content)

        # 标签覆盖度（如果 metadata 中已有 tags）
        tags = chunk.get("metadata", {}).get("tags", [])
        if not tags:
            tag_result = self.multi_dimension_tag(chunk)
            tags = tag_result["tags"]
        score_tag_coverage = min(1.0, len(tags) / 5.0) if tags else 0.0

        # 加权综合
        quality = (
            score_info_density * 0.4
            + score_completeness * 0.4
            + score_tag_coverage * 0.2
        )
        return round(min(1.0, max(0.0, quality)), 4)

    def process(self, text: str, max_chunk_size: int = 500) -> list[dict[str, Any]]:
        """完整的预处理流程：分块 → 打标 → 质量评估。

        Args:
            text: 待处理文本
            max_chunk_size: 最大块大小

        Returns:
            处理结果列表，每项含 content, metadata, tags, quality_score
        """
        chunks = self.semantic_chunk(text, max_chunk_size)
        if not chunks:
            return []

        results: list[dict[str, Any]] = []
        for chunk in chunks:
            # 打标
            tag_result = self.multi_dimension_tag(chunk)

            # 将标签信息注入 metadata
            chunk["metadata"]["tags"] = tag_result["tags"]
            chunk["metadata"]["dimensions"] = tag_result["dimensions"]

            # 质量评估
            quality = self.assess_quality(chunk)

            # 过滤低质量块
            if quality < self._config.quality_threshold:
                logger.debug(
                    "[SemanticPreprocessor] 过滤低质量块 | score=%.3f | content=%.50s",
                    quality, chunk["content"][:50],
                )
                continue

            results.append({
                "content": chunk["content"],
                "metadata": chunk["metadata"],
                "tags": tag_result["tags"],
                "quality_score": quality,
            })

        logger.info(
            "[SemanticPreprocessor] 处理完成 | chunks=%d | filtered=%d",
            len(chunks), len(chunks) - len(results),
        )
        return results

    # ============================================================
    # 私有方法：分块相关
    # ============================================================

    def _split_by_paragraphs(self, text: str) -> list[str]:
        """按段落边界（双换行）分割文本。

        Args:
            text: 待分割文本

        Returns:
            段落列表
        """
        parts = re.split(r"\n\s*\n", text)
        return [p.strip() for p in parts if p.strip()]

    def _merge_segments(self, segments: list[str], max_size: int) -> list[str]:
        """合并过小段落，保持段落边界。

        仅当段落长度低于 min_chunk_size 时才与相邻段落合并，
        保持正常的段落分割边界。

        Args:
            segments: 段落列表
            max_size: 最大合并大小

        Returns:
            合并后的段落列表
        """
        if not segments:
            return []

        min_size = self._config.min_chunk_size
        merged: list[str] = []
        current = segments[0]

        for seg in segments[1:]:
            # 仅当前段过短时才合并，保持正常段落边界
            if len(current) < min_size or len(seg) < min_size:
                candidate = current + "\n\n" + seg
                if len(candidate) <= max_size:
                    current = candidate
                    continue

            merged.append(current)
            current = seg

        merged.append(current)
        return merged

    def _split_by_sentences(self, text: str, max_size: int) -> list[str]:
        """按句子边界分割超长段落。

        句子边界：句号、问号、感叹号后跟空格或换行。

        Args:
            text: 待分割文本
            max_size: 最大块大小

        Returns:
            句子块列表
        """
        # 匹配句子边界：中英文标点后跟空格/换行/结束
        sentence_boundaries = list(
            re.finditer(r"[。！？.!]\s+", text)
        )

        # 也匹配末尾标点
        if text and text[-1] in "。！？.!":
            sentence_boundaries.append(type(
                "Match", (), {"end": lambda self: len(text)}
            )())

        if not sentence_boundaries:
            return [text]

        # 按边界位置分割
        parts: list[str] = []
        last_pos = 0
        current_chunk = ""

        for match in sentence_boundaries:
            end_pos = match.end()
            sentence = text[last_pos:end_pos].strip()
            last_pos = end_pos

            if not sentence:
                continue

            if len(current_chunk) + len(sentence) + 1 <= max_size:
                current_chunk = (current_chunk + " " + sentence).strip()
            else:
                if current_chunk:
                    parts.append(current_chunk)
                current_chunk = sentence

        if current_chunk:
            parts.append(current_chunk)

        return parts if parts else [text]

    def _force_split(self, text: str, max_size: int) -> list[str]:
        """强制按固定长度分割超长文本。

        Args:
            text: 待分割文本
            max_size: 最大块大小

        Returns:
            分割后的文本列表
        """
        if len(text) <= max_size:
            return [text]

        parts: list[str] = []
        for i in range(0, len(text), max_size):
            part = text[i : i + max_size].strip()
            if part:
                parts.append(part)
        return parts

    def _add_overlap(self, chunks: list[str]) -> list[dict[str, Any]]:
        """为相邻块添加重叠内容。

        Args:
            chunks: 文本块列表

        Returns:
            含重叠信息的块列表
        """
        overlap = self._config.overlap_size
        result: list[dict[str, Any]] = []

        for i, chunk in enumerate(chunks):
            boundaries: list[int] = []

            if i > 0 and overlap > 0:
                prev_chunk = chunks[i - 1]
                overlap_text = prev_chunk[-overlap:]
                # 避免完全重复前一块
                if overlap_text and not chunk.startswith(overlap_text):
                    chunk = overlap_text + chunk
                boundaries.append(len(overlap_text))

            result.append({
                "content": chunk,
                "boundaries": boundaries,
            })

        return result

    # ============================================================
    # 私有方法：打标相关
    # ============================================================

    def _extract_topic(self, content: str) -> str:
        """提取主题标签（高频有效词）。

        Args:
            content: 文本内容

        Returns:
            主题标签字符串
        """
        words = self._tokenize(content)
        if not words:
            return ""

        # 统计词频
        counter = Counter(w.lower() for w in words if len(w) > 1)
        if not counter:
            return ""

        most_common = counter.most_common(1)
        return most_common[0][0] if most_common else ""

    def _detect_domain(self, content: str) -> str:
        """检测领域标签。

        Args:
            content: 文本内容

        Returns:
            领域标签
        """
        content_lower = content.lower()
        domain_scores: dict[str, int] = {}

        for domain, keywords in _DOMAIN_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.lower() in content_lower)
            if score > 0:
                domain_scores[domain] = score

        if not domain_scores:
            return ""

        return max(domain_scores, key=lambda d: domain_scores[d])

    def _extract_entities(self, content: str) -> list[str]:
        """提取实体标签。

        识别引号内的术语和大写开头的英文专有名词。

        Args:
            content: 文本内容

        Returns:
            实体列表
        """
        entities: list[str] = []
        seen: set[str] = set()

        # 1. 提取引号内的术语（中英文引号）
        quoted = re.findall(r'["""](.*?)["""]', content)
        for term in quoted:
            term = term.strip()
            if term and term not in seen and len(term) <= 30:
                seen.add(term)
                entities.append(term)

        # 2. 提取连续大写开头的英文专有名词（2+ 单词）
        proper_nouns = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", content)
        for noun in proper_nouns:
            noun = noun.strip()
            if noun not in seen and len(noun) <= 50:
                seen.add(noun)
                entities.append(noun)

        return entities

    def _detect_action_type(self, content: str) -> str:
        """检测操作类型。

        Args:
            content: 文本内容

        Returns:
            操作类型字符串
        """
        detected: set[str] = set()
        for verb, action in _ACTION_VERBS.items():
            if verb in content:
                detected.add(action)

        if not detected:
            return ""

        # 返回最常出现的操作类型
        return sorted(detected)[0]

    def _detect_sentiment(self, content: str) -> str:
        """检测情感倾向。

        Args:
            content: 文本内容

        Returns:
            情感标签：positive / negative / neutral
        """
        content_lower = content.lower()
        pos_count = sum(1 for w in _POSITIVE_WORDS if w in content_lower)
        neg_count = sum(1 for w in _NEGATIVE_WORDS if w in content_lower)

        if pos_count > neg_count:
            return "positive"
        if neg_count > pos_count:
            return "negative"
        return "neutral"

    def _compute_tag_confidence(self, content: str, tags: list[str]) -> float:
        """计算标签置信度。

        基于标签数量和文本长度的比值。

        Args:
            content: 文本内容
            tags: 标签列表

        Returns:
            0-1 置信度
        """
        if not tags or not content:
            return 0.0

        # 标签覆盖率
        tag_coverage = min(1.0, len(tags) / 3.0)

        # 标签在原文中的出现率
        content_lower = content.lower()
        matched = sum(1 for t in tags if t.lower() in content_lower)
        tag_match_rate = matched / len(tags) if tags else 0.0

        confidence = tag_coverage * 0.5 + tag_match_rate * 0.5
        return round(min(1.0, max(0.0, confidence)), 4)

    # ============================================================
    # 私有方法：质量评估相关
    # ============================================================

    def _compute_info_density(self, content: str) -> float:
        """计算信息密度（有效词占比）。

        Args:
            content: 文本内容

        Returns:
            0-1 信息密度分
        """
        words = self._tokenize(content)
        if not words:
            return 0.0

        meaningful = [w for w in words if w.lower() not in _ALL_STOP_WORDS]
        density = len(meaningful) / len(words) if words else 0.0
        return round(min(1.0, max(0.0, density)), 4)

    def _compute_completeness(self, content: str) -> float:
        """计算语义完整性（句子是否完整）。

        完整句子以标点结尾。

        Args:
            content: 文本内容

        Returns:
            0-1 完整性分
        """
        stripped = content.strip()
        if not stripped:
            return 0.0

        # 检查是否以标点结尾
        ends_with_punct = stripped[-1] in "。！？.!;；"
        punct_score = 0.5 if ends_with_punct else 0.1

        # 计算完整句子比例
        sentence_ends = len(re.findall(r"[。！？.!]", stripped))
        char_count = len(stripped)

        # 每句平均长度（合理的句子长度在 10-100 字）
        if sentence_ends > 0:
            avg_len = char_count / sentence_ends
            if 10 <= avg_len <= 100:
                length_score = 0.5
            elif 5 <= avg_len <= 150:
                length_score = 0.3
            else:
                length_score = 0.1
        else:
            length_score = 0.1

        return round(min(1.0, max(0.0, punct_score + length_score)), 4)

    # ============================================================
    # 私有工具方法
    # ============================================================

    def _tokenize(self, text: str) -> list[str]:
        """简单分词：英文按空格分割，中文按字符分割。

        Args:
            text: 文本内容

        Returns:
            词列表
        """
        tokens: list[str] = []

        # 英文分词：提取连续的字母数字组合
        english_words = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text)
        tokens.extend(english_words)

        # 中文分词：提取连续中文字符（2 字以上作为短语）
        chinese_segments = re.findall(r"[\u4e00-\u9fff]{2,}", text)
        for seg in chinese_segments:
            if len(seg) <= 4:
                tokens.append(seg)
            else:
                # 长中文串按 2 字拆分（bigram）
                for i in range(len(seg) - 1):
                    tokens.append(seg[i : i + 2])

        return tokens
