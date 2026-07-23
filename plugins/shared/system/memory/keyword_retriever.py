"""关键词检索器。

朴素子串匹配（TF 比例打分），从 SQLite 存储读取记忆。
实现 IRetriever 接口。无外部依赖，作为 vector/tagwave 不可用时的保底检索。

暴露接口：
- KeywordRetriever: 关键词检索器
"""

from __future__ import annotations

import logging
from typing import Any

from models import MemoryType, SearchResult
from ports import IRetriever

logger = logging.getLogger(__name__)


class KeywordRetriever(IRetriever):
    """关键词检索器。

    从 SqliteVectorStore 读取记忆内容，做子串匹配 + TF 比例打分。

    Attributes:
        _store: SQLite 存储
    """

    def __init__(self, store: Any) -> None:
        """初始化关键词检索器。

        Args:
            store: SqliteVectorStore 实例
        """
        self._store = store

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """关键词检索相关记忆。

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量
            memory_type: 记忆类型（"episode"/"semantic" 逐个检索，"all" 检索全部）
            filters: 额外过滤条件（当前未使用）

        Returns:
            搜索结果列表
        """
        if not query:
            return []

        query_lower = query.lower()
        types = ["episode", "semantic"] if memory_type == "all" else [memory_type]

        scored: list[SearchResult] = []
        for mtype in types:
            entries = self._store.list_memories(memory_type=mtype, user_id=user_id, limit=1000)
            for mem in entries:
                content = str(mem.get("content", ""))
                content_lower = content.lower()
                if query_lower not in content_lower:
                    continue
                score = content_lower.count(query_lower) / max(len(content_lower), 1)
                scored.append(
                    SearchResult(
                        id=mem["id"],
                        content=content,
                        score=round(score, 4),
                        memory_type=MemoryType.EPISODE if mtype == "episode" else MemoryType.SEMANTIC,
                        metadata=mem.get("metadata", {}),
                    )
                )

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]
