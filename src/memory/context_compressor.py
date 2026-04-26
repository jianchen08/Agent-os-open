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

    # 一次性压缩模板：L1 + L2 + 关键词
    COMPRESS_PROMPT = """## 任务
将以下对话历史一次性压缩完成，输出三部分：
1. **十段摘要 (l1)**：结构化记录对话核心信息，保留对继续当前任务至关重要的内容
2. **三要素摘要 (l2)**：从 l1 中提炼最核心的意图、过程、结果
3. **关键词 (keywords)**：5-10 个反映核心主题的关键词

## 对话历史
{messages}

## 输出格式
严格输出以下 JSON，不要输出任何其他内容。每个字段简洁精炼，无内容填 null。

```json
{{
  "l1": {{
    "session_title": "会话主题（一句话）",
    "current_state": "当前工作状态、进度、待解决问题",
    "task_specification": "用户要求完成的具体任务",
    "files_and_functions": "涉及的主要文件和函数",
    "workflow": "执行的主要步骤和工作流程",
    "errors_and_corrections": "遇到的错误和解决方案",
    "codebase_info": "内容的相关的重要信息和关键细节",
    "learnings": "从对话中获得的关键认知",
    "key_results": "已完成的关键成果",
    "worklog": "未完成事项和待跟进问题"
  }},
  "l2": {{
    "intent": "用户最终要达成什么",
    "process": "关键步骤和决策",
    "results": "完成了什么，还剩什么"
  }},
  "keywords": ["关键词1", "关键词2", "关键词3"]
}}
```"""

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

    def set_llm_call_fn(self, llm_call_fn: Callable[[str], Awaitable[str]]) -> None:
        """延迟注入 LLM 调用函数。

        Args:
            llm_call_fn: 异步 LLM 调用函数
        """
        self._llm_call_fn = llm_call_fn

    async def compress_all(
        self, messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """一次性完成 L1 + L2 + 关键词压缩（单次 LLM 调用）。

        Args:
            messages: 对话消息列表

        Returns:
            {"l1": str, "l2": str, "keywords": list[str]}

        Raises:
            RuntimeError: LLM 调用失败时
        """
        if not messages:
            return {"l1": "", "l2": "", "keywords": []}

        messages_text = self._format_messages(messages)

        prompt = self.COMPRESS_PROMPT.format(messages=messages_text)

        try:
            response = await self._call_llm(prompt)
            raw_json = self._extract_json(response)

            import json
            parsed = json.loads(raw_json)

            l1_data = parsed.get("l1", {})
            l1_str = json.dumps(l1_data, ensure_ascii=False, indent=2) if l1_data else ""

            l2_data = parsed.get("l2", {})
            l2_str = json.dumps(l2_data, ensure_ascii=False, indent=2) if l2_data else ""

            raw_keywords = parsed.get("keywords", [])
            keywords = [
                kw.strip() for kw in raw_keywords
                if isinstance(kw, str) and kw.strip()
            ][:10]

            l1_max = self.budgets.get("L1", 1000)
            l2_max = self.budgets.get("L2", 500)
            l1_str = self._truncate_to_budget(l1_str, l1_max)
            l2_str = self._truncate_to_budget(l2_str, l2_max)

            self._stats["l0_to_l1_count"] += 1
            self._stats["l1_to_l2_count"] += 1
            self._stats["total_tokens_compressed"] += self._estimate_tokens(messages_text)

            logger.info(
                "[ContextCompressor] 一次性压缩完成 | L1≈%d字符 L2≈%d字符 keywords=%d",
                len(l1_str), len(l2_str), len(keywords),
            )

            return {"l1": l1_str, "l2": l2_str, "keywords": keywords}

        except Exception as e:
            logger.error("[ContextCompressor] 一次性压缩失败 | error=%s", e)
            raise RuntimeError(f"压缩失败: {e}") from e

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

        # 一次性压缩 L0 → L1 + L2
        if l0_tokens > 0:
            messages = [{"role": "user", "content": l0}]
            result = await self.compress_all(messages)

            compressed_l1 = result.get("l1", "")
            compressed_l2 = result.get("l2", "")

            if compressed_l1:
                if new_l1:
                    new_l1 = new_l1 + "\n\n---\n\n" + compressed_l1
                else:
                    new_l1 = compressed_l1
                l1_tokens = self._estimate_tokens(new_l1)

            if compressed_l2:
                if new_l2:
                    new_l2 = new_l2 + "\n\n---\n\n" + compressed_l2
                else:
                    new_l2 = compressed_l2
                l2_tokens = self._estimate_tokens(new_l2)

        # L1 超预算：溢出部分已有 L2，直接裁剪 L1
        if l1_tokens > l1_budget:
            new_l1 = self._keep_within_budget(new_l1, l1_budget)

        # L2 超预算
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
        """截断文本到预算内，保持 JSON 结构完整。

        Args:
            text: 文本
            max_tokens: 最大 token 数

        Returns:
            截断后的文本
        """
        estimated = self._estimate_tokens(text)
        if estimated <= max_tokens:
            return text

        import json

        max_chars = int(max_tokens * 1.5)
        truncated = text[:max_chars]

        # 如果是 JSON，尝试保持结构完整
        try:
            json.loads(text)
            # 找到最后一个完整的 key-value 对
            last_comma = truncated.rfind(',\n')
            if last_comma > 0:
                truncated = text[:last_comma] + "\n}"
            if json.loads(truncated):
                return truncated
        except (json.JSONDecodeError, ValueError):
            pass

        return truncated

    def _extract_json(self, text: str) -> str:
        """从 LLM 响应中提取 JSON 并格式化。

        处理 LLM 可能包裹代码块或添加额外文本的情况。

        Args:
            text: LLM 原始响应

        Returns:
            格式化后的 JSON 字符串
        """
        import json
        import re

        if not text:
            return text

        # 尝试从 markdown 代码块中提取
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            # 尝试直接匹配 { ... }
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            json_str = json_match.group(0) if json_match else text

        try:
            parsed = json.loads(json_str)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            logger.warning("[ContextCompressor] JSON 解析失败，返回原文")
            return text.strip()

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
