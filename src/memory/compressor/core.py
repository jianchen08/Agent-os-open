"""
核心压缩逻辑模块

包含 ContextCompressor 类，负责将长对话历史压缩成结构化摘要
"""

import hashlib
import logging
from typing import Any

import cachetools

from src.core.tokenizer import get_token_counter
from src.llm.base import LLMClient

from .config import CompressionConfig

logger = logging.getLogger(__name__)


# 记忆层级常量（新架构）
# 层级名称映射（向后兼容）
LAYER_NAME_MAP = {
    "DSL": "L1",  # Detailed Summary Layer → L1
    "CSL": "L2",  # Compressed Summary Layer → L2
    "KIL": "L3",  # Keyword Index Layer → L3
}

# 反向映射
LAYER_NAME_MAP_REVERSE = {v: k for k, v in LAYER_NAME_MAP.items()}


def normalize_layer_name(layer: str) -> str:
    """
    标准化层级名称

    将旧架构的层级名称（DSL/CSL/KIL）映射到新架构（L1/L2/L3）

    Args:
        layer: 层级名称

    Returns:
        标准化后的层级名称
    """
    return LAYER_NAME_MAP.get(layer.upper(), layer.upper())


class ContextCompressor:
    """
    上下文压缩器

    负责将长对话历史压缩成结构化摘要
    支持分层递进压缩：L0(原文) → L1(八段) → L2(三元组) → L3(关键词)

    设计原则：
    - 纯函数设计，无状态管理
    - 输入输出都是字符串
    - 不操作数据库

    触发流程：
    1. 用户发送消息
    2. 检查 L0 是否超出预算
    3. 如果超出，将最旧的消息压缩成八段摘要，追加到 L1
    4. 检查 L1 是否超出预算
    5. 如果超出，将最旧的八段摘要压缩成三元组，追加到 L2
    6. 依此类推...

    拼接流程（发送给 LLM 时）：
    [系统提示] + [工具描述] + [L3关键词] + [L2摘要] + [L1详细] + [检索召回] + [L0原文] + [用户消息]
    """

    # L0 → L1：八段压缩模板
    EIGHT_SECTION_PROMPT = """请将以下对话历史压缩成结构化摘要。

要求：严格按照八段格式输出，每段简洁精炼，总长度控制在 {max_tokens} tokens 以内。

## 1. 核心事件线索
（对话中发生的关键事件，按时间顺序）

## 2. 用户意图演进
（用户目标如何变化/细化）

## 3. 关键决策节点
（做了哪些重要决定，为什么）

## 4. 知识引用记录
（引用了哪些文档/代码/外部信息）

## 5. 执行结果摘要
（工具调用和操作的结果）

## 6. 问题与解决方案
（遇到什么问题，如何解决）

## 7. 重要上下文
（影响后续对话的关键信息）

## 8. 待续事项
（未完成的任务、待确认的问题）

---

对话历史：
{messages}

请开始压缩（严格按八段格式）："""

    # L1 → L2：三元组压缩模板
    TRIPLET_PROMPT = """请将以下八段摘要进一步压缩成核心三元组。

要求：只保留最核心的信息，总长度控制在 {max_tokens} tokens 以内。

格式：
## 意图
（用户最终要达成什么）

## 过程
（关键步骤和决策）

## 结果
（完成了什么，还剩什么）

---

八段摘要：
{summary}

请压缩成三元组："""

    # L2 → L3：关键词压缩模板
    KEYWORD_PROMPT = """请从以下摘要中提取关键词和核心概念。

要求：
1. 提取 5-10 个最重要的关键词/短语
2. 用于后续向量检索
3. 总长度控制在 {max_tokens} tokens 以内

格式：
关键词：词1, 词2, 词3, ...
核心概念：一句话概括

---

摘要：
{summary}

请提取关键词："""

    def __init__(
        self,
        llm_client: LLMClient,
        config: CompressionConfig | None = None,
        model: str = None,
    ):
        """
        初始化上下文压缩器

        Args:
            llm_client: LLM 客户端
            config: 压缩配置
            model: 使用的模型名称（可选）
        """
        self.llm_client = llm_client
        self.config = config or CompressionConfig()
        self.model = model
        self.token_counter = get_token_counter()

        # 计算各层预算
        self.budgets = self.config.get_budgets()

        # 缓存（使用 LRU 缓存，避免重复压缩导致内存无限增长）
        # maxsize=1000 表示最多缓存 1000 个压缩结果
        self._cache: cachetools.LRUCache[str, str] = cachetools.LRUCache(maxsize=1000)

        # 统计信息
        self._stats = {
            "l0_to_l1_count": 0,
            "l1_to_l2_count": 0,
            "l2_to_l3_count": 0,
            "total_tokens_compressed": 0,
        }

    async def compress(
        self, messages: list[dict[str, Any]], preserve_structure: bool = True
    ) -> str:
        """
        压缩对话历史（兼容旧接口）

        Args:
            messages: 消息列表
            preserve_structure: 是否保留结构化格式

        Returns:
            压缩后的摘要
        """
        return await self.compress_to_l1(messages)

    async def compress_to_l1(self, messages: list[dict[str, Any]]) -> str:
        """
        L0 → L1：压缩成八段摘要

        Args:
            messages: 原始消息列表

        Returns:
            八段格式的摘要
        """
        if not messages:
            return ""

        # 生成缓存键
        cache_key = self._generate_cache_key(messages, "L1")
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 格式化消息
        messages_text = self._format_messages(messages)
        max_tokens = self.budgets.get("L1", 1000)

        # 构建提示词
        prompt = self.EIGHT_SECTION_PROMPT.format(
            messages=messages_text, max_tokens=max_tokens
        )

        try:
            summary = await self._call_llm(prompt)

            # 截断到预算内
            summary = self._truncate_to_budget(summary, max_tokens)

            # 缓存
            self._cache[cache_key] = summary
            self._stats["l0_to_l1_count"] += 1
            model = self.model or self.llm_client.model_name
            self._stats["total_tokens_compressed"] += self.token_counter.count_messages(
                messages, model
            )

            return summary

        except Exception as e:
            logger.error(f"[ContextCompressor] L0→L1 压缩失败，拒绝降级 | error={e}")
            raise RuntimeError(f"L1 压缩失败: {e}") from e

    async def compress_to_l2(self, l1_summary: str) -> str:
        """
        L1 → L2：压缩成三元组摘要

        Args:
            l1_summary: 八段摘要

        Returns:
            三元组格式的摘要
        """
        if not l1_summary:
            return ""

        cache_key = self._generate_cache_key([{"content": l1_summary}], "L2")
        if cache_key in self._cache:
            return self._cache[cache_key]

        max_tokens = self.budgets.get("L2", 500)
        prompt = self.TRIPLET_PROMPT.format(summary=l1_summary, max_tokens=max_tokens)

        try:
            summary = await self._call_llm(prompt)
            summary = self._truncate_to_budget(summary, max_tokens)

            self._cache[cache_key] = summary
            self._stats["l1_to_l2_count"] += 1

            return summary

        except Exception as e:
            logger.error(f"[ContextCompressor] L1→L2 压缩失败，拒绝降级 | error={e}")
            raise RuntimeError(f"L2 压缩失败: {e}") from e

    async def compress_to_l3(self, l2_summary: str) -> str:
        """
        L2 → L3：压缩成关键词索引

        Args:
            l2_summary: 三元组摘要

        Returns:
            关键词索引
        """
        if not l2_summary:
            return ""

        cache_key = self._generate_cache_key([{"content": l2_summary}], "L3")
        if cache_key in self._cache:
            return self._cache[cache_key]

        max_tokens = self.budgets.get("L3", 200)
        prompt = self.KEYWORD_PROMPT.format(summary=l2_summary, max_tokens=max_tokens)

        try:
            keywords = await self._call_llm(prompt)
            keywords = self._truncate_to_budget(keywords, max_tokens)

            self._cache[cache_key] = keywords
            self._stats["l2_to_l3_count"] += 1

            return keywords

        except Exception as e:
            logger.error(f"[ContextCompressor] L2→L3 压缩失败，拒绝降级 | error={e}")
            raise RuntimeError(f"L3 压缩失败: {e}") from e

    async def progressive_compress(
        self,
        l0: str,
        l1: str,
        l2: str,
        budgets: dict[str, int],
        executor_type: str | None = None,
        executor_id: str | None = None,
    ) -> tuple[str, str, str]:
        """
        递进压缩主逻辑

        根据预算限制，将内容从低层向高层递进压缩。
        如果某层内容超出预算，则将其最旧部分压缩到下一层。

        按执行者隔离压缩，确保同一 Agent 的上下文一起压缩。

        Args:
            l0: L0层内容（最近对话）
            l1: L1层现有内容（八段摘要）
            l2: L2层现有内容（三元组摘要）
            budgets: 各层预算配置，包含 L1, L2, L3 的token限制
            executor_type: 执行者类型（用于隔离标识）
            executor_id: 执行者ID（用于隔离标识）

        Returns:
            压缩后的 (l1, l2, l3) 三层内容
        """
        # 计算各层当前token数
        l0_tokens = self.token_counter.count_tokens(l0) if l0 else 0
        l1_tokens = self.token_counter.count_tokens(l1) if l1 else 0
        l2_tokens = self.token_counter.count_tokens(l2) if l2 else 0

        l1_budget = budgets.get("L1", budgets.get("DSL", 1000))
        l2_budget = budgets.get("L2", budgets.get("CSL", 500))
        l3_budget = budgets.get("L3", budgets.get("KIL", 200))

        new_l1, new_l2, new_l3 = l1, l2, ""

        # 步骤1：检查 L0 是否需要压缩到 L1
        # 如果 L0 有内容且 L1 即将超预算，触发 L0→L1 压缩
        if l0_tokens > 0:
            # 将 L0 内容压缩成 L1 格式
            messages = [{"role": "user", "content": l0}]
            compressed_l1 = await self.compress_to_l1(messages)

            # 追加到现有 L1
            if new_l1:
                new_l1 = new_l1 + "\n\n---\n\n" + compressed_l1
            else:
                new_l1 = compressed_l1

            # 重新计算 L1 token数
            l1_tokens = self.token_counter.count_tokens(new_l1)

        # 步骤2：检查 L1 是否超出预算，需要压缩到 L2
        if l1_tokens > l1_budget:
            # 提取超出预算的部分
            overflow = self._extract_overflow(new_l1, l1_budget)
            if overflow:
                # 压缩溢出部分到 L2
                compressed_l2 = await self.compress_to_l2(overflow)

                # 追加到现有 L2
                if new_l2:
                    new_l2 = new_l2 + "\n\n---\n\n" + compressed_l2
                else:
                    new_l2 = compressed_l2

                # 保留预算内的 L1 内容
                new_l1 = self._keep_within_budget(new_l1, l1_budget)
                l2_tokens = self.token_counter.count_tokens(new_l2)

        # 步骤3：检查 L2 是否超出预算，需要压缩到 L3
        if l2_tokens > l2_budget:
            # 提取超出预算的部分
            overflow = self._extract_overflow(new_l2, l2_budget)
            if overflow:
                # 压缩溢出部分到 L3
                new_l3 = await self.compress_to_l3(overflow)

                # 保留预算内的 L2 内容
                new_l2 = self._keep_within_budget(new_l2, l2_budget)

        # 步骤4：检查 L3 是否超出预算（丢弃最旧的部分）
        l3_tokens = self.token_counter.count_tokens(new_l3) if new_l3 else 0
        if l3_tokens > l3_budget:
            # L3 超预算，只保留最新的内容
            new_l3 = self._keep_within_budget(new_l3, l3_budget)

        logger.debug(
            f"[ContextCompressor] 递进压缩完成: "
            f"L1={self.token_counter.count_tokens(new_l1)}tokens, "
            f"L2={self.token_counter.count_tokens(new_l2)}tokens, "
            f"L3={self.token_counter.count_tokens(new_l3)}tokens, "
            f"executor={executor_type}:{executor_id}"
        )

        return new_l1, new_l2, new_l3

    def _extract_overflow(self, content: str, budget: int) -> str:
        """
        提取超出预算的内容（最旧的部分）

        Args:
            content: 层内容
            budget: token预算

        Returns:
            溢出的内容（最旧部分），如果没有溢出返回空字符串
        """
        if not content:
            return ""

        total_tokens = self.token_counter.count_tokens(content)
        if total_tokens <= budget:
            return ""

        # 按分隔符分割
        parts = content.split("\n\n---\n\n")

        if len(parts) <= 1:
            # 只有一部分，无法分割，返回全部内容
            return content

        # 从前往后累加，找出溢出的部分
        overflow_parts = []
        remaining_tokens = 0

        for part in parts:
            part_tokens = self.token_counter.count_tokens(part)
            if remaining_tokens + part_tokens <= budget:
                remaining_tokens += part_tokens
            else:
                overflow_parts.append(part)

        return "\n\n---\n\n".join(overflow_parts) if overflow_parts else ""

    def _keep_within_budget(self, content: str, budget: int) -> str:
        """
        保留预算内的内容（最新的部分）

        Args:
            content: 层内容
            budget: token预算

        Returns:
            预算内的内容（最新部分）
        """
        if not content:
            return ""

        total_tokens = self.token_counter.count_tokens(content)
        if total_tokens <= budget:
            return content

        # 按分隔符分割
        parts = content.split("\n\n---\n\n")

        if len(parts) <= 1:
            # 只有一部分，直接截断
            return self._truncate_to_budget(content, budget)

        # 从后往前保留（保留最新的）
        remaining_parts = []
        current_tokens = 0

        for part in reversed(parts):
            part_tokens = self.token_counter.count_tokens(part)
            if current_tokens + part_tokens <= budget:
                remaining_parts.insert(0, part)
                current_tokens += part_tokens
            else:
                break

        return "\n\n---\n\n".join(remaining_parts) if remaining_parts else ""

    def _truncate_to_budget(self, text: str, max_tokens: int) -> str:
        """截断文本到预算内"""
        current_tokens = self.token_counter.count_tokens(text)
        if current_tokens <= max_tokens:
            return text
        return self.token_counter.truncate_text(text, max_tokens)

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        """
        格式化消息为文本

        Args:
            messages: 消息列表

        Returns:
            格式化后的文本
        """
        lines = []

        for i, msg in enumerate(messages, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            # 跳过空消息
            if not content:
                continue

            # 格式化
            if role == "user":
                lines.append(f"【用户 {i}】\n{content}")
            elif role == "assistant":
                lines.append(f"【助手 {i}】\n{content}")
            elif role == "system":
                lines.append(f"【系统 {i}】\n{content}")
            elif role == "tool":
                # 工具调用结果，只显示简要信息
                tool_name = msg.get("name", "unknown_tool")
                content_preview = (
                    content[:200] + "..." if len(content) > 200 else content
                )
                lines.append(f"【工具 {i}: {tool_name}】\n{content_preview}")
            else:
                lines.append(f"【{role.upper()} {i}】\n{content}")

            lines.append("")  # 空行分隔

        return "\n".join(lines)

    async def _call_llm(self, prompt: str) -> str:
        """
        调用 LLM 生成摘要

        使用禁用流式传输的独立 LLM 客户端，避免响应被 LangGraph 的 astream 捕获。

        BUG-FIX-fix_20260225_120000_compress_context:
        问题根因: LangChain 的回调机制通过 Python contextvars 继承，即使创建新的客户端实例
                  也会继承父上下文中的回调处理器，导致压缩响应被 LangGraph 的 astream 捕获。
        修复方案: 在 ainvoke 调用时显式传入空的回调列表 config={"callbacks": []}，
                  切断与父上下文的回调继承，确保压缩调用完全隔离。
        影响范围: 压缩时的 LLM 调用不再被发送到前端

        Args:
            prompt: 提示词

        Returns:
            生成的摘要
        """
        try:
            from langchain_core.messages import HumanMessage
            from langchain_openai import ChatOpenAI

            if not hasattr(self.llm_client, "_chat_model"):
                raise RuntimeError("LLM 客户端不支持 _chat_model 属性，无法创建禁用流式传输的客户端")

            original_model = self.llm_client._chat_model

            api_key = original_model.openai_api_key
            if hasattr(api_key, 'get_secret_value'):
                api_key = api_key.get_secret_value()

            disable_streaming_model = ChatOpenAI(
                model=original_model.model_name,
                api_key=api_key,
                base_url=original_model.openai_api_base,
                temperature=0.3,
                disable_streaming=True,
            )

            lc_messages = [HumanMessage(content=prompt)]
            response = await disable_streaming_model.ainvoke(
                lc_messages,
                config={"callbacks": []}
            )

            return response.content.strip()

        except Exception as e:
            raise RuntimeError(f"LLM 压缩失败: {e}")

    def _generate_cache_key(
        self, messages: list[dict[str, Any]], layer: str = ""
    ) -> str:
        """
        生成缓存键

        Args:
            messages: 消息列表
            layer: 层标识

        Returns:
            缓存键
        """
        msg_count = len(messages)
        last_content = messages[-1].get("content", "") if messages else ""
        key_str = f"{layer}_{msg_count}_{last_content[:100]}"
        return hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()

    def get_cache_size(self) -> int:
        """获取缓存大小"""
        return len(self._cache)

    def cleanup_memory(self, aggressive: bool = False):
        """
        清理内存，释放不必要的数据

        Args:
            aggressive: 是否启用激进模式（清空所有缓存）
        """
        if aggressive:
            # 激进模式：清空所有缓存
            self._cache.clear()
        else:
            # 温和模式：LRU 缓存会自动清理最久未使用的条目
            pass

    def get_memory_stats(self) -> dict[str, Any]:
        """获取内存使用统计"""
        return {
            "cache_size": len(self._cache),
            "cache_maxsize": self._cache.maxsize
            if hasattr(self._cache, "maxsize")
            else None,
            "cache_usage_percent": len(self._cache) / self._cache.maxsize * 100
            if hasattr(self._cache, "maxsize") and self._cache.maxsize > 0
            else 0,
        }

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            **self._stats,
            "budgets": self.budgets,
            "cache_size": len(self._cache),
        }

    def update_config(self, config: CompressionConfig):
        """更新配置"""
        self.config = config
        self.budgets = config.get_budgets()
