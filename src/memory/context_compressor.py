"""上下文压缩器。

从旧代码 src/memory/compressor/core.py 搬迁。
移除 LLMClient/langchain 硬依赖和 cachetools 依赖，
token 计数使用简化估算，LLM 调用通过注入的可调用对象实现。

暴露接口：
- CompressionConfig: 压缩配置
- ContextCompressor: 上下文压缩器
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# 层级名称映射（向后兼容）
LAYER_NAME_MAP = {
    "DSL": "L1",
    "CSL": "L2",
    "KIL": "L2",
}


def normalize_layer_name(layer: str) -> str:
    """标准化层级名称。

    Args:
        layer: 层级名称

    Returns:
        标准化后的层级名称
    """
    return LAYER_NAME_MAP.get(layer.upper(), layer.upper())


@dataclass
class CompressionConfig:
    """压缩配置。

    所有比例基于 context_window 计算实际 token 数。

    Attributes:
        context_window: 模型上下文窗口大小
        compress_trigger_ratio: 压缩触发比例
        l1_ratio: L1 预算比例
        l2_ratio: L2 预算比例
        recent_ratio: 最近原文预算比例
        retrieval_ratio: 检索召回预算比例
        max_turn_ratio: 单轮次最大比例
    """

    context_window: int = 128000
    compress_trigger_ratio: float = 0.5
    l1_ratio: float = 0.15
    l2_ratio: float = 0.05
    recent_ratio: float = 0.3
    retrieval_ratio: float = 0.1
    max_turn_ratio: float = 0.5

    def get_budgets(self) -> dict[str, int]:
        """计算各部分实际 token 预算。

        Returns:
            各层 token 预算字典
        """
        recent_budget = int(self.context_window * self.recent_ratio)
        return {
            "recent": recent_budget,
            "L1": int(self.context_window * self.l1_ratio),
            "L2": int(self.context_window * self.l2_ratio),
            "retrieval": int(self.context_window * self.retrieval_ratio),
            "max_turn": int(recent_budget * self.max_turn_ratio),
        }

    def get_trigger_threshold(self) -> int:
        """获取触发压缩的 token 阈值。

        Returns:
            触发阈值
        """
        return int(self.context_window * self.compress_trigger_ratio)


class ContextCompressor:
    """上下文压缩器。

    负责将长对话历史压缩成结构化摘要。
    支持分层递进压缩：L0(原文) → L1(十模块) → L2(三元组)。

    设计原则：
    - 纯函数设计，无状态管理
    - 输入输出都是字符串
    - 不操作数据库

    LLM 调用通过注入的 llm_call_fn 实现而非硬依赖。

    Attributes:
        config: 压缩配置
        budgets: 各层 token 预算
        _llm_call_fn: LLM 调用函数
        _cache: 压缩结果缓存
        _stats: 统计信息
    """

    # L0 → L1：十模块压缩模板
    TEN_SECTION_PROMPT = """你正在做一次梦，对记忆文件做一次反思性的回顾。把你最近学到的东西整合成持久的、组织良好的记忆，方便未来的会话快速定位。

请将以下对话历史压缩成结构化的记忆文件。

要求：严格按照10个模块格式输出，每段简洁精炼，总长度控制在 {max_tokens} tokens 以内。

## Session Title
（简短描述这个会话的主题/标题）

## Current State
（当前的工作状态、进度、悬而未决的问题）

## Task Specification
（用户要求完成的具体任务）

## Files and Functions
（涉及的主要文件和函数）

## Workflow
（执行的主要步骤和工作流程）

## Errors & Corrections
（遇到的错误和解决方案）

## Codebase Documentation
（代码库相关的重要信息）

## Learnings
（从这次会话中学到的新知识、技巧）

## Key Results
（取得的关键结果、完成的里程碑）

## Worklog
（未完成的事项、待跟进的问题）

---

对话历史：
{messages}

请开始压缩（严格按10模块格式）："""

    # L1 → L2：三模块压缩模板
    TRIPLET_PROMPT = """请将以下十模块摘要进一步压缩成核心三要素。

要求：只保留最核心的信息，总长度控制在 {max_tokens} tokens 以内。

格式：
## 意图
（用户最终要达成什么）

## 过程
（关键步骤和决策）

## 结果
（完成了什么，还剩什么）

---

十模块摘要：
{summary}

请压缩成三要素："""

    # 关键词提取模板
    KEYWORD_PROMPT = """请从以下内容中提取 5-10 个最重要的关键词。

要求：
1. 关键词应反映核心主题、实体、操作和概念
2. 优先提取专有名词、技术术语、关键动作
3. 每个关键词 1-4 个字

输出格式（JSON 数组）：
["关键词1", "关键词2", "关键词3", ...]

---

内容：
{content}

请提取关键词（只输出 JSON 数组）："""

    def __init__(
        self,
        llm_call_fn: Callable[[str], Awaitable[str]] | None = None,
        config: CompressionConfig | None = None,
    ) -> None:
        """初始化上下文压缩器。

        Args:
            llm_call_fn: LLM 调用函数，接收 prompt 返回 response 文本
            config: 压缩配置
        """
        self._llm_call_fn = llm_call_fn
        self.config = config or CompressionConfig()
        self.budgets = self.config.get_budgets()

        # 缓存
        self._cache: dict[str, str] = {}
        self._cache_max_size = 1000

        # 统计信息
        self._stats: dict[str, Any] = {
            "l0_to_l1_count": 0,
            "l1_to_l2_count": 0,
            "total_tokens_compressed": 0,
        }

    async def compress(
        self, messages: list[dict[str, Any]], preserve_structure: bool = True,
    ) -> str:
        """压缩对话历史（兼容旧接口）。

        Args:
            messages: 对话消息列表
            preserve_structure: 是否保留结构

        Returns:
            压缩后的文本
        """
        return await self.compress_to_l1(messages)

    async def compress_to_l1(self, messages: list[dict[str, Any]]) -> str:
        """L0 → L1：压缩成十模块摘要。

        Args:
            messages: 对话消息列表

        Returns:
            十模块摘要文本

        Raises:
            RuntimeError: LLM 调用失败时
        """
        if not messages:
            return ""

        # 生成缓存键
        cache_key = self._generate_cache_key(messages, "L1")
        if cache_key in self._cache:
            return self._cache[cache_key]

        messages_text = self._format_messages(messages)
        max_tokens = self.budgets.get("L1", 1000)

        prompt = self.TEN_SECTION_PROMPT.format(
            messages=messages_text, max_tokens=max_tokens,
        )

        try:
            summary = await self._call_llm(prompt)
            summary = self._truncate_to_budget(summary, max_tokens)

            self._cache_put(cache_key, summary)
            self._stats["l0_to_l1_count"] += 1
            self._stats["total_tokens_compressed"] += self._estimate_tokens(messages_text)

            return summary

        except Exception as e:
            logger.error("[ContextCompressor] L0→L1 压缩失败 | error=%s", e)
            raise RuntimeError(f"L1 压缩失败: {e}") from e

    async def compress_to_l2(self, l1_summary: str) -> str:
        """L1 → L2：压缩成三元组摘要。

        Args:
            l1_summary: L1 摘要文本

        Returns:
            三元组摘要文本

        Raises:
            RuntimeError: LLM 调用失败时
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

            self._cache_put(cache_key, summary)
            self._stats["l1_to_l2_count"] += 1

            return summary

        except Exception as e:
            logger.error("[ContextCompressor] L1→L2 压缩失败 | error=%s", e)
            raise RuntimeError(f"L2 压缩失败: {e}") from e

    async def extract_keywords(self, content: str) -> list[str]:
        """从内容中提取关键词。

        Args:
            content: 内容文本

        Returns:
            关键词列表
        """
        import json
        import re

        if not content:
            return []

        prompt = self.KEYWORD_PROMPT.format(content=content[:2000])

        try:
            response = await self._call_llm(prompt)
            json_match = re.search(r'\[.*?\]', response, re.DOTALL)
            if json_match:
                keywords = json.loads(json_match.group())
                if isinstance(keywords, list):
                    return [
                        kw.strip() for kw in keywords
                        if isinstance(kw, str) and kw.strip()
                    ][:10]
            return []
        except Exception as e:
            logger.warning("[ContextCompressor] 关键词提取失败: %s", e)
            return []

    async def progressive_compress(
        self,
        l0: str,
        l1: str,
        l2: str,
        budgets: dict[str, int],
        executor_id: str | None = None,
    ) -> tuple[str, str]:
        """递进压缩主逻辑。

        Args:
            l0: L0 原文
            l1: L1 摘要
            l2: L2 三元组
            budgets: 各层预算
            executor_id: 执行器 ID

        Returns:
            (新 L1, 新 L2) 元组
        """
        l0_tokens = self._estimate_tokens(l0) if l0 else 0
        l1_tokens = self._estimate_tokens(l1) if l1 else 0
        l2_tokens = self._estimate_tokens(l2) if l2 else 0

        l1_budget = budgets.get("L1", budgets.get("DSL", 1000))
        l2_budget = budgets.get("L2", budgets.get("CSL", 500))

        new_l1, new_l2 = l1, l2

        # 步骤1：L0 → L1
        if l0_tokens > 0:
            messages = [{"role": "user", "content": l0}]
            compressed_l1 = await self.compress_to_l1(messages)

            if new_l1:
                new_l1 = new_l1 + "\n\n---\n\n" + compressed_l1
            else:
                new_l1 = compressed_l1

            l1_tokens = self._estimate_tokens(new_l1)

        # 步骤2：L1 → L2
        if l1_tokens > l1_budget:
            overflow = self._extract_overflow(new_l1, l1_budget)
            if overflow:
                compressed_l2 = await self.compress_to_l2(overflow)

                if new_l2:
                    new_l2 = new_l2 + "\n\n---\n\n" + compressed_l2
                else:
                    new_l2 = compressed_l2

                new_l1 = self._keep_within_budget(new_l1, l1_budget)
                l2_tokens = self._estimate_tokens(new_l2)

        # 步骤3：L2 超预算
        if l2_tokens > l2_budget:
            new_l2 = self._keep_within_budget(new_l2, l2_budget)

        logger.debug(
            "[ContextCompressor] 递进压缩完成: L1≈%dtokens, L2≈%dtokens",
            self._estimate_tokens(new_l1), self._estimate_tokens(new_l2),
        )

        return new_l1, new_l2

    def _extract_overflow(self, content: str, budget: int) -> str:
        """提取超出预算的内容（最旧的部分）。

        Args:
            content: 内容文本
            budget: token 预算

        Returns:
            溢出内容
        """
        if not content:
            return ""

        total_tokens = self._estimate_tokens(content)
        if total_tokens <= budget:
            return ""

        parts = content.split("\n\n---\n\n")
        if len(parts) <= 1:
            return content

        overflow_parts: list[str] = []
        remaining_tokens = 0

        for part in parts:
            part_tokens = self._estimate_tokens(part)
            if remaining_tokens + part_tokens <= budget:
                remaining_tokens += part_tokens
            else:
                overflow_parts.append(part)

        return "\n\n---\n\n".join(overflow_parts) if overflow_parts else ""

    def _keep_within_budget(self, content: str, budget: int) -> str:
        """保留预算内的内容（最新的部分）。

        Args:
            content: 内容文本
            budget: token 预算

        Returns:
            预算内的内容
        """
        if not content:
            return ""

        total_tokens = self._estimate_tokens(content)
        if total_tokens <= budget:
            return content

        parts = content.split("\n\n---\n\n")
        if len(parts) <= 1:
            return self._truncate_to_budget(content, budget)

        remaining_parts: list[str] = []
        current_tokens = 0

        for part in reversed(parts):
            part_tokens = self._estimate_tokens(part)
            if current_tokens + part_tokens <= budget:
                remaining_parts.insert(0, part)
                current_tokens += part_tokens
            else:
                break

        return "\n\n---\n\n".join(remaining_parts) if remaining_parts else ""

    def _truncate_to_budget(self, text: str, max_tokens: int) -> str:
        """截断文本到预算内。

        简化估算：1 个中文字 ≈ 1.5 token，1 个英文词 ≈ 1 token。

        Args:
            text: 文本
            max_tokens: 最大 token 数

        Returns:
            截断后的文本
        """
        estimated = self._estimate_tokens(text)
        if estimated <= max_tokens:
            return text

        # 粗略截断：按字符数估算
        max_chars = int(max_tokens * 1.5)
        return text[:max_chars]

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        """格式化消息为文本。

        Args:
            messages: 消息列表

        Returns:
            格式化后的文本
        """
        lines: list[str] = []

        for i, msg in enumerate(messages, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if not content:
                continue

            if role == "user":
                lines.append(f"【用户 {i}】\n{content}")
            elif role == "assistant":
                lines.append(f"【助手 {i}】\n{content}")
            elif role == "system":
                lines.append(f"【系统 {i}】\n{content}")
            elif role == "tool":
                tool_name = msg.get("name", "unknown_tool")
                content_preview = content[:200] + "..." if len(content) > 200 else content
                lines.append(f"【工具 {i}: {tool_name}】\n{content_preview}")
            else:
                lines.append(f"【{role.upper()} {i}】\n{content}")

            lines.append("")

        return "\n".join(lines)

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 生成摘要。

        Args:
            prompt: 提示词

        Returns:
            LLM 响应文本

        Raises:
            RuntimeError: 无 LLM 调用函数或调用失败时
        """
        if not self._llm_call_fn:
            raise RuntimeError("未提供 LLM 调用函数，无法执行压缩")

        return await self._llm_call_fn(prompt)

    def _estimate_tokens(self, text: str | list[dict[str, Any]]) -> int:
        """估算 token 数（简化版）。

        1 个中文字 ≈ 1.5 token，1 个英文词 ≈ 1 token。

        Args:
            text: 文本或消息列表

        Returns:
            估算的 token 数
        """
        if isinstance(text, list):
            total = 0
            for msg in text:
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                total += self._estimate_tokens(content)
            return total

        if not text:
            return 0

        # 简化估算：字符数 / 2
        return max(1, len(text) // 2)

    def _generate_cache_key(
        self, messages: list[dict[str, Any]], layer: str = "",
    ) -> str:
        """生成缓存键。

        Args:
            messages: 消息列表
            layer: 层级标识

        Returns:
            缓存键
        """
        msg_count = len(messages)
        last_content = messages[-1].get("content", "") if messages else ""
        key_str = f"{layer}_{msg_count}_{last_content[:100]}"
        return hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()

    def _cache_put(self, key: str, value: str) -> None:
        """放入缓存，超限时清理。

        Args:
            key: 缓存键
            value: 缓存值
        """
        if len(self._cache) >= self._cache_max_size:
            # 简单清理：删除最早的 10%
            remove_count = self._cache_max_size // 10
            keys_to_remove = list(self._cache.keys())[:remove_count]
            for k in keys_to_remove:
                del self._cache[k]

        self._cache[key] = value

    def clear_cache(self) -> None:
        """清空缓存。"""
        self._cache.clear()

    def get_cache_size(self) -> int:
        """获取缓存大小。

        Returns:
            缓存条目数
        """
        return len(self._cache)

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。

        Returns:
            统计信息字典
        """
        return {
            **self._stats,
            "budgets": self.budgets,
            "cache_size": len(self._cache),
        }

    def update_config(self, config: CompressionConfig) -> None:
        """更新配置。

        Args:
            config: 新的压缩配置
        """
        self.config = config
        self.budgets = config.get_budgets()
