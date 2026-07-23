#!/usr/bin/env python3
"""记忆系统 MCP 服务端。

使用 AgentOS Plugin SDK 封装记忆系统的检索/存储/摘要功能。
存储从进程内 list 改为 SQLite（持久化），支持 keyword/vector/tagwave 三种真检索。

检索分派：
- keyword: KeywordRetriever（子串匹配，无外部依赖）
- vector: VectorRetriever（GLM embedding-3 + numpy 余弦）
- tagwave: TagNetworkRetriever（三阶段 Tag 增强后再向量检索）
embedding key 未配置时 vector/tagwave 自动降级为 keyword（有日志）。

[来源: docs/tasks/task_10_system_plugins.md AC-09-1]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

# 本地模块可达性：插件目录加入 sys.path
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

from embedding_client import EmbeddingUnavailableError, LLMClient  # noqa: E402
from keyword_retriever import KeywordRetriever  # noqa: E402
from models import RetrievalMethod  # noqa: E402
from ports import IRetriever  # noqa: E402
from tag_network_retriever import TagNetworkRetriever  # noqa: E402
from vector_retriever import SqliteVectorStore, VectorRetriever  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("memory_service")

_DB_PATH = os.path.join(_THIS_DIR, "data", "memory.db")
_store: SqliteVectorStore | None = None
_llm: LLMClient | None = None
_retrievers: dict[str, IRetriever] = {}
# vector/tagwave 是否真正可用（False=已降级 keyword）
_retriever_status: dict[str, str] = {}


def _ensure_db_dir() -> None:
    """确保 SQLite 数据目录存在。"""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)


def _embed_fn(texts: list[str]) -> list[list[float]]:
    """同步嵌入函数（供 VectorRetriever/TagNetworkRetriever 使用）。

    Args:
        texts: 待嵌入文本列表

    Returns:
        向量列表

    Raises:
        EmbeddingUnavailableError: LLMClient 未就绪或 key 缺失
        RuntimeError: HTTP 调用失败
    """
    if _llm is None:
        raise EmbeddingUnavailableError("LLMClient 未初始化")
    return _llm.embed_texts(texts)


def _index_memory(entry_id: str, content: str, metadata: dict[str, Any], memory_type: str) -> None:
    """对存储的记忆生成 embedding 并回写（失败仅告警，不阻断存储）。

    Args:
        entry_id: 条目 ID
        content: 内容文本
        metadata: 元数据（从中抽取 tags）
        memory_type: 记忆类型
    """
    if _llm is None or not _llm.embedding_available or _store is None:
        return
    try:
        vectors = _llm.embed_texts([content])
        if not vectors:
            return
        # 回写 embedding（save_memory 的 UPSERT 语义）
        _store.save_memory(
            entry_id=entry_id,
            memory_type=memory_type,
            content=content,
            metadata=metadata,
            embedding=vectors[0],
        )
        _index_tags(metadata.get("tags", []), vectors[0])
    except EmbeddingUnavailableError:
        # key 缺失：静默，已在外层记录降级
        pass
    except Exception as e:
        logger.warning("[memory] 记忆向量索引失败 | id=%s | error=%s", entry_id, e)


def _index_tags(tags: list[str], vector: list[float]) -> None:
    """把记忆的 tags 写入 Tag 索引并更新共现矩阵。

    Args:
        tags: 标签列表
        vector: 复用记忆向量作为 tag 向量（首次出现时）
    """
    if not tags or _store is None:
        return
    tag_ids: list[int] = []
    for name in tags:
        if not name:
            continue
        try:
            tid = _store.save_tag(name, vector, frequency=1)
            tag_ids.append(tid)
        except Exception as e:
            logger.warning("[memory] Tag 索引失败 | tag=%s | error=%s", name, e)

    # 更新两两共现（双向）
    for i in range(len(tag_ids)):
        for j in range(i + 1, len(tag_ids)):
            try:
                _store.update_cooccurrence(tag_ids[i], tag_ids[j])
                _store.update_cooccurrence(tag_ids[j], tag_ids[i])
            except Exception as e:
                logger.warning("[memory] 共现更新失败 | error=%s", e)


async def _dispatch_retrieve(
    retrieval_method: str,
    query: str,
    top_k: int,
    memory_type: str,
) -> list[dict[str, Any]]:
    """按 retrieval_method 分派检索，并处理降级。

    Args:
        retrieval_method: 检索方法
        query: 查询文本
        top_k: 返回数量
        memory_type: 记忆类型

    Returns:
        结果字典列表
    """
    method = (retrieval_method or RetrievalMethod.KEYWORD.value).lower()
    if method not in (RetrievalMethod.KEYWORD.value, RetrievalMethod.VECTOR.value, RetrievalMethod.TAGWAVE.value):
        method = RetrievalMethod.KEYWORD.value

    if _store is None:
        return []

    retriever = _retrievers.get(method)
    status = _retriever_status.get(method, "unknown")

    # 降级：vector/tagwave 未就绪时回退 keyword
    if method in (RetrievalMethod.VECTOR.value, RetrievalMethod.TAGWAVE.value) and status != "ready":
        logger.info(
            "[memory.search] %s 检索不可用（%s），降级为 keyword", method, status
        )
        retriever = _retrievers[RetrievalMethod.KEYWORD.value]

    if retriever is None:
        return []

    results = await retriever.retrieve(query=query, top_k=top_k, memory_type=memory_type)
    return [r.to_dict() for r in results]


@plugin.tool(
    name="memory.search",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "retrieval_method": {
                "type": "string",
                "enum": ["keyword", "vector", "tagwave"],
                "default": "keyword",
            },
            "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
            "memory_type": {
                "type": "string",
                "enum": ["episode", "semantic"],
                "default": "semantic",
            },
        },
        "required": ["query"],
    },
    description="Search episode and semantic memories by keyword/vector/tagwave",
)
async def memory_search(
    query: str,
    retrieval_method: str = "keyword",
    top_k: int = 5,
    memory_type: str = "semantic",
) -> dict[str, Any]:
    """Search memories matching the query.

    Supports keyword, vector, and tagwave retrieval methods.
    Vector/tagwave auto-degrade to keyword when embedding key is unconfigured.
    Returns up to top_k results sorted by relevance.
    """
    results = await _dispatch_retrieve(retrieval_method, query, top_k, memory_type)
    return {"results": results, "total": len(results)}


@plugin.tool(
    name="memory.store",
    schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["episode", "semantic"]},
            "content": {"type": "string"},
            "metadata": {"type": "object", "default": {}},
            "user_id": {"type": "string", "default": ""},
        },
        "required": ["type", "content"],
    },
    description="Store a new memory (episode or semantic) with embedding index",
)
async def memory_store(
    type: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """Store a memory entry.

    Args:
        type: Memory type (episode or semantic).
        content: Memory content text.
        metadata: Optional metadata (tags, source, etc).
        user_id: Optional user id scoping.
    """
    if _store is None:
        return {"id": "", "stored": False, "error": "store not initialized"}

    entry_id = f"mem_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
    meta = metadata or {}
    _store.save_memory(
        entry_id=entry_id,
        memory_type=type,
        content=content,
        user_id=user_id,
        metadata=meta,
        embedding=None,
        created_at=time.time(),
    )
    # 异步生成 embedding（不阻断存储返回）
    await asyncio.to_thread(_index_memory, entry_id, content, meta, type)
    return {"id": entry_id, "stored": True}


@plugin.tool(
    name="memory.summarize",
    schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["episode", "semantic"]},
            "top_k": {"type": "integer", "default": 10},
            "user_id": {"type": "string", "default": ""},
        },
    },
    description="Summarize recent memories for context injection via LLM",
)
async def memory_summarize(
    type: str = "episode", top_k: int = 10, user_id: str = ""
) -> dict[str, Any]:
    """Summarize recent memories for pipeline context injection.

    Uses GLM chat completions to generate a real summary. When the chat API
    key is unconfigured, degrades to returning concatenated retrieval results
    (with a TODO note in the output).
    """
    if _store is None:
        return {"summary": "", "count": 0}

    recent = _store.list_memories(memory_type=type, user_id=user_id or None, limit=top_k)
    if not recent:
        return {"summary": "", "count": 0}

    if _llm is None or not _llm.chat_available:
        # TODO: 配置 GLM chat key 后启用真实摘要；当前降级返回检索结果拼接
        logger.info("[memory.summarize] chat key 未配置，降级为检索结果拼接")
        joined = "\n".join(m.get("content", "") for m in recent if m.get("content"))
        return {"summary": joined, "count": len(recent), "degraded": True}

    joined = "\n".join(f"- {m.get('content', '')}" for m in recent if m.get("content"))
    prompt = f"总结以下记忆，提炼关键信息，用简洁的中文要点输出：\n{joined}"
    try:
        summary = await asyncio.to_thread(_llm.chat_completion, prompt)
    except Exception as e:
        logger.warning("[memory.summarize] LLM 摘要失败，降级拼接: %s", e)
        return {"summary": joined, "count": len(recent), "degraded": True, "error": str(e)}

    return {"summary": summary.strip(), "count": len(recent)}


# 资源暴露（函数式调用——SDK register_resource 签名要求 handler 必填）
def _episode_resource() -> dict[str, Any]:
    """Expose recent episode memories as MCP resource."""
    if _store is None:
        return {"count": 0, "recent": []}
    return {
        "count": _store.count_memories(memory_type="episode"),
        "recent": _store.list_memories(memory_type="episode", limit=5),
    }


def _semantic_resource() -> dict[str, Any]:
    """Expose recent semantic memories as MCP resource."""
    if _store is None:
        return {"count": 0, "recent": []}
    return {
        "count": _store.count_memories(memory_type="semantic"),
        "recent": _store.list_memories(memory_type="semantic", limit=5),
    }


plugin.register_resource(
    "memory://episode/recent", _episode_resource, name="Recent Episode Memories"
)
plugin.register_resource(
    "memory://semantic/recent", _semantic_resource, name="Recent Semantic Memories"
)


def _init_retrievers() -> None:
    """初始化三个检索器并记录就绪状态。

    - keyword 始终就绪
    - vector/tagwave 依赖 embedding key，缺失则标记降级
    """
    global _llm
    assert _store is not None

    config = plugin.get_config()
    _llm = LLMClient(config)

    keyword = KeywordRetriever(_store)
    _retrievers[RetrievalMethod.KEYWORD.value] = keyword
    _retriever_status[RetrievalMethod.KEYWORD.value] = "ready"

    if _llm.embedding_available:
        vector = VectorRetriever(_store, _embed_fn)
        _retrievers[RetrievalMethod.VECTOR.value] = vector
        _retriever_status[RetrievalMethod.VECTOR.value] = "ready"

        tagwave = TagNetworkRetriever(vector, _embed_fn)
        tagwave.init_from_store()
        _retrievers[RetrievalMethod.TAGWAVE.value] = tagwave
        _retriever_status[RetrievalMethod.TAGWAVE.value] = "ready"
        logger.info("[memory] vector/tagwave 检索已就绪")
    else:
        logger.warning(
            "[memory] embedding key 未配置，vector/tagwave 降级为 keyword。"
            "配置 config/models/llm.yaml 的 embedding-3 与 ZHIPU_API_KEY 后启用。"
        )


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize memory service on load: SQLite store + retrievers."""
    global _store
    _ensure_db_dir()
    _store = SqliteVectorStore(_DB_PATH)
    _init_retrievers()
    logger.info(
        "[memory] on_load 完成 | retrievers=%s", json.dumps(_retriever_status, ensure_ascii=False)
    )


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup memory stores on unload."""
    global _store
    if _store is not None:
        _store.close()
        _store = None


if __name__ == "__main__":
    plugin.run()
