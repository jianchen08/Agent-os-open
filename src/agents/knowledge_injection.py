"""
知识库注入服务

负责从知识库中检索相关内容并注入到提示词中
支持多种注入模式：完整、压缩、提示
"""

import logging
from typing import Any, Protocol

from src.agents.types import KnowledgeConfig, KnowledgeMode
from src.core.tokenizer import get_token_counter

logger = logging.getLogger(__name__)


# 定义 SemanticMemory 接口（Protocol）
class ISemanticMemory(Protocol):
    """语义记忆接口"""

    async def retrieve(
        self, query: str, top_k: int = 5, score_threshold: float = 0.7
    ) -> list[Any]:
        """检索相关内容"""
        ...


class KnowledgeInjectionService:
    """
    知识库注入服务

    根据配置从知识库中检索相关内容，支持多种注入模式
    """

    def __init__(
        self,
        semantic_memory: ISemanticMemory,
        config: KnowledgeConfig | None = None,
    ):
        """
        初始化服务

        Args:
            semantic_memory: 语义记忆服务
            config: 知识库配置
        """
        self.semantic_memory = semantic_memory
        self.config = config or KnowledgeConfig()

        logger.debug(
            f"[KnowledgeInjection] 初始化完成 | "
            f"mode={self.config.mode.value} | "
            f"max_tokens={self.config.max_tokens}"
        )

    async def get_injection_content(
        self,
        query: str,
        mode: KnowledgeMode | None = None,
        top_k: int | None = None,
    ) -> str:
        """
        获取注入内容

        Args:
            query: 查询文本
            mode: 注入模式（可选，默认使用配置）
            top_k: 返回数量（可选，默认使用配置）

        Returns:
            注入内容字符串
        """
        mode = mode or self.config.mode
        top_k = top_k or self.config.top_k

        # 如果禁用，返回空
        if mode == KnowledgeMode.DISABLED:
            logger.debug("[KnowledgeInjection] 知识库注入已禁用")
            return ""

        # 根据模式选择注入方式
        if mode == KnowledgeMode.FULL:
            return await self._get_full_content(query, top_k)
        elif mode == KnowledgeMode.COMPRESSED:
            return await self._get_compressed_content(query, top_k)
        elif mode == KnowledgeMode.HINT:
            return await self._get_retrieval_hint(query, top_k)
        else:
            logger.warning(f"[KnowledgeInjection] 未知的注入模式: {mode}")
            return ""

    async def _get_full_content(self, query: str, top_k: int) -> str:
        """
        获取完整内容

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            完整内容字符串
        """
        try:
            # 从语义记忆中检索
            results = await self.semantic_memory.retrieve(
                query=query,
                top_k=top_k,
                score_threshold=self.config.score_threshold,
            )

            if not results:
                logger.debug("[KnowledgeInjection] 未找到相关知识")
                return ""

            # 构建完整内容
            content_parts = []
            total_tokens = 0
            token_counter = get_token_counter()

            for i, result in enumerate(results, 1):
                # 使用 token 计数器计算 token 数
                content_tokens = token_counter.count_tokens(result.content)

                # 检查是否超出预算
                if total_tokens + content_tokens > self.config.max_tokens:
                    logger.debug(
                        f"[KnowledgeInjection] 达到最大 token 限制 | "
                        f"max={self.config.max_tokens} | "
                        f"current={total_tokens}"
                    )
                    break

                content_parts.append(f"{i}. {result.content}")
                total_tokens += content_tokens

            full_content = "\n".join(content_parts)

            logger.info(
                f"[KnowledgeInjection] 完整内容注入 | "
                f"items={len(content_parts)} | "
                f"tokens={total_tokens}"
            )

            return full_content

        except Exception as e:
            logger.error(f"[KnowledgeInjection] 获取完整内容失败: {e}")
            return ""

    async def _get_compressed_content(self, query: str, top_k: int) -> str:
        """
        获取压缩内容

        使用摘要或关键词形式返回

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            压缩内容字符串
        """
        try:
            # 从语义记忆中检索
            results = await self.semantic_memory.retrieve(
                query=query,
                top_k=top_k,
                score_threshold=self.config.score_threshold,
            )

            if not results:
                logger.debug("[KnowledgeInjection] 未找到相关知识")
                return ""

            # 构建压缩内容（摘要形式）
            content_parts = []
            total_tokens = 0

            for i, result in enumerate(results, 1):
                token_counter = get_token_counter()
                # 提取摘要或使用前 N 个字符
                summary = getattr(result, "summary", None)
                if not summary:
                    # 使用内容的前 200 字符作为摘要
                    summary = (
                        result.content[:200] + "..."
                        if token_counter.count_tokens(result.content) > 200
                        else result.content
                    )

                # 使用 token 计数器计算 token 数
                summary_tokens = token_counter.count_tokens(summary)

                # 检查是否超出预算
                if total_tokens + summary_tokens > self.config.max_tokens:
                    break

                content_parts.append(f"{i}. {summary}")
                total_tokens += summary_tokens

            compressed_content = "\n".join(content_parts)

            logger.info(
                f"[KnowledgeInjection] 压缩内容注入 | "
                f"items={len(content_parts)} | "
                f"tokens={total_tokens}"
            )

            return compressed_content

        except Exception as e:
            logger.error(f"[KnowledgeInjection] 获取压缩内容失败: {e}")
            return ""

    async def _get_retrieval_hint(self, query: str, top_k: int) -> str:
        """
        获取检索提示

        仅返回知识库中存在相关内容的提示

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            提示字符串
        """
        try:
            # 从语义记忆中检索
            results = await self.semantic_memory.retrieve(
                query=query,
                top_k=top_k,
                score_threshold=self.config.score_threshold,
            )

            if not results:
                return ""

            # 构建提示信息
            count = len(results)
            topics = []

            for result in results[:5]:  # 最多显示 5 个主题
                token_counter = get_token_counter()
                # 尝试提取主题或使用前 50 字符
                topic = getattr(result, "topic", None)
                if not topic:
                    topic = (
                        result.content[:50] + "..."
                        if token_counter.count_tokens(result.content) > 50
                        else result.content
                    )
                topics.append(f"- {topic}")

            hint = f"知识库中找到 {count} 条相关内容：\n" + "\n".join(topics)

            logger.info(f"[KnowledgeInjection] 检索提示注入 | count={count}")

            return hint

        except Exception as e:
            logger.error(f"[KnowledgeInjection] 获取检索提示失败: {e}")
            return ""

    def update_config(self, config: KnowledgeConfig) -> None:
        """
        更新配置

        Args:
            config: 新配置
        """
        self.config = config
        logger.debug(
            f"[KnowledgeInjection] 配置已更新 | "
            f"mode={config.mode.value} | "
            f"max_tokens={config.max_tokens}"
        )
