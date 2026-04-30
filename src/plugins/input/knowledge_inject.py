"""知识注入 Input 插件（memory 模块版本）。

包装 KnowledgeService → IInputPlugin，在管道输入阶段
从知识库检索相关内容写入 state。

依赖注入：通过 ctx.get_service("semantic_storage") 获取 ISemanticStorage 实例，
构造函数只接受 config，由 build_plugin_registry 统一实例化。

注意：此插件位于 memory/plugins/ 下，与 M6 的
plugins/input/knowledge_inject.py 是不同的文件，不修改 M6 的 Mock 实现。

State 命名空间：
    - knowledge.context : 本插件写入的知识内容
"""

from __future__ import annotations

import logging
from typing import Any

from memory.knowledge_service import KnowledgeService
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class KnowledgeInjectPlugin(IInputPlugin):
    """知识注入 Input 插件（memory 模块版本）。

    从知识库检索相关内容，将结果写入 state["knowledge.context"]。
    支持四种注入模式：FULL、COMPRESSED、HINT、DISABLED。

    通过 ctx.get_service("semantic_storage") 获取 ISemanticStorage 实例，
    无需在构造时注入。

    优先级：30（数据级，在 context_build 之后、prompt_build 之前）
    错误策略：FALLBACK（降级为无知识对话）

    Attributes:
        _config: 插件配置
        _mode: 注入模式
        _top_k: 检索结果数量
        _max_tokens: 最大 token 数
    """

    error_policy = ErrorPolicy.FALLBACK
    fallback_state: dict[str, Any] = {"knowledge.context": ""}

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
        self._knowledge_service: KnowledgeService | None = None

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "knowledge_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 30

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """从知识库检索内容并写入 state。

        通过 ctx.get_service("semantic_storage") 获取语义存储实例，
        无 semantic_storage 服务时降级为无知识对话。

        优先使用 KnowledgeService.search() 进行相关性检索（结合关键词
        匹配和语义相似度），search 不可用时回退到 list + 客户端过滤。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含知识内容状态更新的插件执行结果
        """
        if self._mode == "disabled":
            return PluginResult(state_updates={"knowledge.context": ""})

        query = ctx.state.get("user_message", "")
        user_id = ctx.state.get("user_id", "")
        if not query:
            return PluginResult(state_updates={"knowledge.context": ""})

        try:
            # 从服务注册表获取 semantic_storage（延迟初始化并缓存 KnowledgeService）
            try:
                semantic_storage = ctx.get_service("semantic_storage")
            except KeyError:
                logger.debug("[%s] No semantic_storage service, skipping", self.name)
                return PluginResult(state_updates={"knowledge.context": ""})

            if self._knowledge_service is None:
                # 尝试获取 embedding_fn 以支持语义检索
                embedding_fn = None
                try:
                    embedding_service = ctx.get_service("embedding_service")
                    if hasattr(embedding_service, "embed_text"):
                        embedding_fn = embedding_service.embed_text
                    elif hasattr(embedding_service, "embed"):
                        embedding_fn = embedding_service.embed
                except KeyError:
                    pass

                self._knowledge_service = KnowledgeService(
                    semantic_storage=semantic_storage,
                    embedding_fn=embedding_fn,
                )
            knowledge_service = self._knowledge_service

            # 优先使用相关性搜索（关键词 + 语义）
            items: list[dict[str, Any]] = []
            try:
                search_results = await knowledge_service.search(
                    user_id=user_id,
                    query=query,
                    top_k=self._top_k,
                )
                items = [r.to_dict() for r in search_results]
            except Exception as search_err:
                # 搜索失败时回退到 list + 客户端过滤
                logger.debug(
                    "[%s] 相关性搜索失败，回退到列表模式: %s",
                    self.name, search_err,
                )
                result = await knowledge_service.list_semantic_memory(user_id)
                items = result.get("items", [])

                if items and query:
                    items = self._filter_by_relevance(items, query)

            if not items:
                return PluginResult(state_updates={"knowledge.context": ""})

            # 根据模式格式化
            if self._mode == "full":
                content = self._format_full(items)
            elif self._mode == "compressed":
                content = self._format_compressed(items)
            elif self._mode == "hint":
                content = self._format_hint(items)
            else:
                content = ""

            logger.debug(
                "[%s] 知识注入完成 | mode=%s | items=%d",
                self.name, self._mode, len(items),
            )

            return PluginResult(state_updates={"knowledge.context": content})

        except Exception as e:
            logger.warning("[%s] 知识注入失败: %s", self.name, e)
            return PluginResult(
                state_updates={"knowledge.context": ""},
                error=e,
            )

    def _format_full(self, items: list[dict[str, Any]]) -> str:
        """格式化完整知识内容。

        Args:
            items: 知识条目列表

        Returns:
            格式化后的文本
        """
        parts: list[str] = []
        total_tokens = 0

        for i, item in enumerate(items[:self._top_k], 1):
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

        for i, item in enumerate(items[:self._top_k], 1):
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
        self, items: list[dict[str, Any]], query: str,
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
            return items[:self._top_k]

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
        return filtered[:self._top_k] if filtered else items[:self._top_k]
