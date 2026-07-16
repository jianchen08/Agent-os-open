"""知识注入 Input 插件。

通过 MemoryService 统一检索知识内容，在管道输入阶段
将检索结果写入 state["knowledge.context"]。

依赖注入：通过 ctx.get_service("memory_service") 获取 MemoryService 实例，
不再自行创建 KnowledgeService。

State 命名空间：
    - knowledge.context : 本插件写入的知识内容
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class KnowledgeInjectPlugin(IInputPlugin):
    """知识注入 Input 插件。

    通过 MemoryService 统一检索知识内容，将结果写入 state["knowledge.context"]。
    支持四种注入模式：full、compressed、hint、disabled。

    通过 ctx.get_service("memory_service") 获取 MemoryService 实例，
    不再自行创建 KnowledgeService。

    优先级：30（数据级，在 context_build 之后、prompt_build 之前）
    错误策略：FALLBACK（降级为无知识对话）

    Attributes:
        _config: 插件配置
        _mode: 注入模式
        _top_k: 检索结果数量
        _max_tokens: 最大 token 数
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化知识注入插件。

        Args:
            config: 插件配置，支持以下键：
                - mode: 注入模式 (full/compressed/hint/disabled)，默认 disabled
                - top_k: 检索结果数量，默认 5
                - max_tokens: 最大 token 数，默认 2000
        """
        self._config = config or {}
        self._mode = self._config.get("mode", "disabled")
        self._top_k = self._config.get("top_k", 5)
        self._max_tokens = self._config.get("max_tokens", 2000)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "knowledge_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 30

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """通过 MemoryService 从知识库检索内容并写入 state。

        通过 ctx.get_service("memory_service") 获取 MemoryService 实例，
        调用其 retrieve() 方法进行知识检索。
        memory_service 不可用时降级为无知识对话。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含知识内容状态更新的插件执行结果
        """
        if self._mode == "disabled":
            return PluginResult(state_updates={"knowledge.context": ""})

        try:
            memory_service = ctx.get_service("memory_service")
        except KeyError:
            return PluginResult(state_updates={"knowledge.context": ""})

        user_id = ctx.state.get("user_id", "")
        query = ctx.state.get("current_query", "")

        if not query:
            return PluginResult(state_updates={"knowledge.context": ""})

        try:
            results = await memory_service.retrieve(
                user_id=user_id,
                filter={"memory_type": "semantic"},
                inject_type=self._mode,
                retrieval_method="keyword",
                query=query,
                top_k=self._top_k,
            )
        except Exception as e:
            logger.warning("[KnowledgeInjectPlugin] 检索失败: %s", e)
            return PluginResult(
                state_updates={"knowledge.context": ""},
                error=str(e),
            )

        if not results:
            return PluginResult(state_updates={"knowledge.context": ""})

        context = "\n\n".join(r.content for r in results)
        return PluginResult(state_updates={"knowledge.context": context})

    def _format_full(self, items: list[dict[str, Any]]) -> str:
        """格式化完整知识内容。

        Args:
            items: 知识条目列表

        Returns:
            格式化后的文本
        """
        parts: list[str] = []
        total_tokens = 0

        for i, item in enumerate(items[: self._top_k], 1):
            content = item.get("content", "")
            estimated_tokens = len(content) // 2
            if total_tokens + estimated_tokens > self._max_tokens:
                break
            parts.append(f"{i}. {content}")
            total_tokens += estimated_tokens

        return "\n".join(parts)

    def _format_compressed(self, items: list[dict[str, Any]]) -> str:
        """格式化压缩知识内容。

        Args:
            items: 知识条目列表

        Returns:
            格式化后的文本
        """
        parts: list[str] = []
        total_tokens = 0

        for i, item in enumerate(items[: self._top_k], 1):
            content = item.get("content", "")
            summary = content[:200] + "..." if len(content) > 200 else content
            estimated_tokens = len(summary) // 2
            if total_tokens + estimated_tokens > self._max_tokens:
                break
            parts.append(f"{i}. {summary}")
            total_tokens += estimated_tokens

        return "\n".join(parts)

    def _format_hint(self, items: list[dict[str, Any]]) -> str:
        """格式化知识提示。

        Args:
            items: 知识条目列表

        Returns:
            格式化后的提示文本
        """
        count = len(items)
        topics: list[str] = []
        for item in items[:5]:
            content = item.get("content", "")
            topic = content[:50] + "..." if len(content) > 50 else content
            topics.append(f"- {topic}")

        return f"知识库中找到 {count} 条相关内容：\n" + "\n".join(topics)

    def _filter_by_relevance(
        self,
        items: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        """按 query 关键词对知识条目做基础相关性排序和筛选。

        将 query 分词后，统计每个 item 的 content 中命中的关键词数量，
        按命中数降序排序，过滤掉零命中的条目。保留原始顺序作为同分时的稳定排序。

        Args:
            items: 知识条目列表
            query: 用户查询文本

        Returns:
            按相关性排序后的知识条目列表
        """
        # 简单分词：按空白字符拆分，过滤短词
        query_words = {w.lower() for w in query.split() if len(w) > 1}
        if not query_words:
            return items[: self._top_k]

        scored: list[tuple[int, int, dict[str, Any]]] = []
        for idx, item in enumerate(items):
            content = item.get("content", "").lower()
            tags = " ".join(item.get("tags", [])).lower()
            combined = f"{content} {tags}"
            hit_count = sum(1 for w in query_words if w in combined)
            # (命中数, 原始索引, item) — 按命中数降序，索引升序保持稳定
            scored.append((hit_count, idx, item))

        # 过滤零命中，按命中数降序排序
        scored.sort(key=lambda x: (-x[0], x[1]))
        filtered = [item for hit, _, item in scored if hit > 0]

        # 若全部零命中，回退到原始列表（保证有内容可用）
        return filtered[: self._top_k] if filtered else items[: self._top_k]
