"""
标签提取器模块

提供从 L3 压缩内容和知识库内容中提取标签/关键词的功能
"""

import logging
import re
from typing import Any

from src.core.embeddings import EmbeddingService
from src.llm.base import LLMClient, Message

logger = logging.getLogger(__name__)


class TagExtractor:
    """
    标签提取器

    负责从 L3 压缩内容和知识库内容中提取关键词和主题标签
    支持两种提取方式：
    1. 基于规则的提取（解析预定义格式）
    2. 基于 LLM 的提取（智能分析内容主题）

    使用示例:
        >>> extractor = TagExtractor(llm_client)
        >>> tags = await extractor.extract_from_l3("关键词：Python, 异步, FastAPI")
        >>> tags = await extractor.extract_from_knowledge("FastAPI 是一个现代 Web 框架...")
    """

    # 从知识库内容提取主题标签的提示词模板
    KNOWLEDGE_TAG_PROMPT = """请分析以下知识内容，提取 3-5 个主题标签。

要求：
1. 标签应准确概括内容的核心主题
2. 使用简洁的中文或英文词汇/短语
3. 标签之间应有一定的区分度
4. 按重要性排序（最重要的在前）

格式要求：
请直接返回标签列表，每行一个标签，不要添加序号或额外说明。

示例输出：
Python
异步编程
Web框架
性能优化

---

知识内容：
{content}

请提取主题标签："""

    # 从 L3 内容提取关键词的备用提示词（当规则解析失败时使用）
    L3_FALLBACK_PROMPT = """请从以下 L3 压缩内容中提取核心关键词。

要求：
1. 提取 5-10 个最重要的关键词/短语
2. 关键词应能代表内容的核心概念
3. 使用简洁的中文或英文词汇

格式要求：
请直接返回关键词列表，每行一个关键词，不要添加序号或额外说明。

---

L3 内容：
{content}

请提取关键词："""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        embedding_service: EmbeddingService | None = None,
    ):
        """
        初始化标签提取器

        Args:
            llm_client: LLM 客户端，用于智能提取标签
            embedding_service: 嵌入服务，可选，用于语义分析
        """
        self.llm_client = llm_client
        self.embedding_service = embedding_service

    async def extract_from_l3(self, compressed_content: str) -> list[str]:
        """
        从 L3 压缩内容中提取关键词

        首先尝试解析 "关键词：词1, 词2, 词3" 格式，
        如果解析失败且配置了 LLM 客户端，则使用 LLM 提取核心概念。

        Args:
            compressed_content: L3 压缩内容，可能包含 "关键词：xxx" 格式

        Returns:
            提取的关键词列表

        示例:
            >>> content = "关键词：Python, 异步编程, FastAPI\\n核心概念：Web框架"
            >>> tags = await extractor.extract_from_l3(content)
            >>> print(tags)
            ['Python', '异步编程', 'FastAPI']
        """
        if not compressed_content or not compressed_content.strip():
            return []

        content = compressed_content.strip()

        # 步骤 1: 尝试使用规则解析 "关键词：" 格式
        keywords = self._parse_keyword_format(content)
        if keywords:
            logger.debug(f"[TagExtractor] 从 L3 内容解析到 {len(keywords)} 个关键词")
            return keywords

        # 步骤 2: 如果规则解析失败且有 LLM 客户端，使用 LLM 提取
        if self.llm_client:
            try:
                keywords = await self._extract_with_llm(content, self.L3_FALLBACK_PROMPT)
                logger.debug(f"[TagExtractor] 使用 LLM 从 L3 内容提取到 {len(keywords)} 个关键词")
                return keywords
            except Exception as e:
                logger.warning(f"[TagExtractor] LLM 提取 L3 关键词失败: {e}")

        # 步骤 3: 降级方案 - 提取内容中的高频词汇
        logger.debug("[TagExtractor] 使用降级方案提取 L3 关键词")
        return self._extract_fallback_keywords(content)

    async def extract_from_knowledge(self, content: str) -> list[str]:
        """
        从知识库内容中提取主题标签

        使用 LLM 分析内容主题，返回 3-5 个主题标签。
        如果没有配置 LLM 客户端，则使用基于词频的提取方法。

        Args:
            content: 知识库内容文本

        Returns:
            主题标签列表（3-5 个）

        示例:
            >>> content = "FastAPI 是一个现代、高性能的 Python Web 框架..."
            >>> tags = await extractor.extract_from_knowledge(content)
            >>> print(tags)
            ['FastAPI', 'Python', 'Web框架', '异步编程']
        """
        if not content or not content.strip():
            return []

        content = content.strip()

        # 使用 LLM 提取主题标签
        if self.llm_client:
            try:
                tags = await self._extract_with_llm(content, self.KNOWLEDGE_TAG_PROMPT)
                # 限制返回数量在 3-5 个
                if len(tags) > 5:
                    tags = tags[:5]
                logger.debug(f"[TagExtractor] 从知识内容提取到 {len(tags)} 个主题标签")
                return tags
            except Exception as e:
                logger.warning(f"[TagExtractor] LLM 提取知识标签失败: {e}")

        # 降级方案：使用基于词频的提取
        logger.debug("[TagExtractor] 使用降级方案提取知识标签")
        return self._extract_fallback_keywords(content, max_tags=5)

    def _parse_keyword_format(self, content: str) -> list[str]:
        """
        解析 "关键词：词1, 词2, 词3" 格式

        Args:
            content: 输入内容

        Returns:
            解析出的关键词列表，如果未匹配到格式则返回空列表
        """
        # 匹配 "关键词：" 或 "关键词:" 开头的行
        patterns = [
            r"关键词[：:]\s*([^\n]+)",  # 关键词：xxx, yyy
            r"(?:^|\n)关键词[：:]\s*([^\n]+)",  # 行首的关键词
        ]

        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if match:
                keyword_str = match.group(1).strip()
                # 按逗号、顿号或分号分割
                keywords = re.split(r"[,，;；、]", keyword_str)
                # 清理并过滤空值
                keywords = [kw.strip() for kw in keywords if kw.strip()]
                return keywords

        return []

    async def _extract_with_llm(self, content: str, prompt_template: str) -> list[str]:
        """
        使用 LLM 提取标签

        Args:
            content: 输入内容
            prompt_template: 提示词模板

        Returns:
            提取的标签列表

        Raises:
            RuntimeError: LLM 调用失败时抛出
        """
        if not self.llm_client:
            raise RuntimeError("未配置 LLM 客户端")

        prompt = prompt_template.format(content=content)
        messages = [Message(role="user", content=prompt)]

        response = await self.llm_client.generate(
            messages=messages,
            temperature=0.3,  # 较低温度以获得更稳定的结果
        )

        if not response.content:
            return []

        # 解析响应内容
        tags = self._parse_llm_response(response.content)
        return tags

    def _parse_llm_response(self, response: str) -> list[str]:
        """
        解析 LLM 返回的标签列表

        处理多种常见格式：
        - 每行一个标签
        - 逗号/顿号分隔
        - 带序号的形式（1. xxx, 2. yyy）

        Args:
            response: LLM 响应文本

        Returns:
            清理后的标签列表
        """
        if not response:
            return []

        lines = response.strip().split("\n")
        tags = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 移除常见的列表标记（- * 1. 2. 等）
            line = re.sub(r"^[-*•・]\s*", "", line)  # 移除列表符号
            line = re.sub(r"^\d+[.．、]\s*", "", line)  # 移除序号

            # 如果清理后还有内容
            if line:
                # 检查是否包含分隔符（可能是单行多个标签）
                if any(sep in line for sep in [",", "，", "、", ";", "；"]):
                    parts = re.split(r"[,，;；、]", line)
                    for part in parts:
                        part = part.strip()
                        if part and len(part) <= 50:  # 限制单个标签长度
                            tags.append(part)
                else:
                    # 单标签，限制长度
                    if len(line) <= 50:
                        tags.append(line)

        # 去重并保持顺序
        seen = set()
        unique_tags = []
        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower not in seen:
                seen.add(tag_lower)
                unique_tags.append(tag)

        return unique_tags

    def _extract_fallback_keywords(self, content: str, max_tags: int = 10) -> list[str]:
        """
        降级方案：基于词频提取关键词

        当 LLM 不可用时使用简单的启发式方法提取关键词。

        Args:
            content: 输入内容
            max_tags: 最大返回标签数量

        Returns:
            提取的关键词列表
        """
        if not content:
            return []

        # 简单的文本清理
        text = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9\s]", " ", content)

        # 中文分词（简单实现：按字符和常见词提取）
        words = []

        # 提取英文单词
        english_words = re.findall(r"[a-zA-Z]{3,}", text)
        words.extend(english_words)

        # 提取中文字符组合（2-4 个字符）
        chinese_chars = re.findall(r"[\u4e00-\u9fa5]{2,4}", text)
        words.extend(chinese_chars)

        # 统计词频
        word_freq: dict[str, int] = {}
        for word in words:
            word = word.lower() if word.isascii() else word
            if len(word) >= 2:  # 至少 2 个字符
                word_freq[word] = word_freq.get(word, 0) + 1

        # 过滤停用词（简单列表）
        stopwords = {
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "her", "was", "one", "our", "out", "day", "get", "has", "him",
            "his", "how", "its", "may", "new", "now", "old", "see", "two",
            "who", "boy", "did", "she", "use", "way", "many", "oil",
            "sit", "set", "run", "eat", "far", "sea", "eye", "ago", "off",
            "too", "any", "say", "man", "try", "ask", "end", "why", "let",
            "put", "own", "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这",
        }

        # 过滤并排序
        filtered_words = [
            (word, freq)
            for word, freq in word_freq.items()
            if word.lower() not in stopwords and len(word) >= 2
        ]

        # 按频率排序
        filtered_words.sort(key=lambda x: x[1], reverse=True)

        # 返回前 N 个
        return [word for word, freq in filtered_words[:max_tags]]

    async def extract_with_embedding(
        self,
        content: str,
        candidate_tags: list[str],
        top_k: int = 5,
    ) -> list[str]:
        """
        使用 Embedding 语义相似度提取最相关的标签

        将内容与候选标签进行语义匹配，返回最相关的标签。

        Args:
            content: 输入内容
            candidate_tags: 候选标签列表
            top_k: 返回最相关的 K 个标签

        Returns:
            最相关的标签列表

        Raises:
            RuntimeError: 未配置 Embedding 服务时抛出
        """
        if not self.embedding_service:
            raise RuntimeError("未配置 Embedding 服务")

        if not content or not candidate_tags:
            return []

        # 生成内容向量
        content_embedding = await self.embedding_service.embed_text(content)

        # 生成候选标签向量
        tag_embeddings = await self.embedding_service.embed_texts(candidate_tags)

        # 计算相似度并排序
        similarities: list[tuple[str, float]] = []
        for tag, tag_embedding in zip(candidate_tags, tag_embeddings, strict=False):
            similarity = EmbeddingService.cosine_similarity(
                content_embedding, tag_embedding
            )
            similarities.append((tag, similarity))

        # 按相似度排序
        similarities.sort(key=lambda x: x[1], reverse=True)

        # 返回前 K 个
        return [tag for tag, score in similarities[:top_k]]

    def get_stats(self) -> dict[str, Any]:
        """
        获取提取器状态信息

        Returns:
            包含配置信息的状态字典
        """
        return {
            "has_llm_client": self.llm_client is not None,
            "has_embedding_service": self.embedding_service is not None,
        }
