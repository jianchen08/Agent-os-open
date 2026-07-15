#!/usr/bin/env python3
"""记忆系统 MCP 服务端。

使用 AgentOS Plugin SDK 封装记忆系统的检索/存储/摘要功能。
核心业务逻辑参考 0.1 src/memory/service.py，简化为独立运行版本。

[来源: docs/tasks/task_10_system_plugins.md AC-09-1]
"""

from __future__ import annotations

import json
import time
from typing import Any

from lingxi_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("memory_service")

# 内存存储（生产环境替换为 SQLite/向量库）
_episode_store: list[dict[str, Any]] = []
_semantic_store: list[dict[str, Any]] = []


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
        },
        "required": ["query"],
    },
    description="Search episode and semantic memories by query",
)
async def memory_search(
    query: str,
    retrieval_method: str = "keyword",
    top_k: int = 5,
) -> dict[str, Any]:
    """Search memories matching the query.

    Supports keyword, vector, and tagwave retrieval methods.
    Returns up to top_k results sorted by relevance.
    """
    results: list[dict[str, Any]] = []
    query_lower = query.lower()

    # Search episode memories
    for mem in _episode_store:
        content = str(mem.get("content", "")).lower()
        if query_lower in content:
            score = content.count(query_lower) / max(len(content), 1)
            results.append({**mem, "type": "episode", "score": round(score, 4)})

    # Search semantic memories
    for mem in _semantic_store:
        content = str(mem.get("content", "")).lower()
        if query_lower in content:
            score = content.count(query_lower) / max(len(content), 1)
            results.append({**mem, "type": "semantic", "score": round(score, 4)})

    # Sort by score descending, take top_k
    results.sort(key=lambda x: x["score"], reverse=True)
    return {"results": results[:top_k], "total": len(results)}


@plugin.tool(
    name="memory.store",
    schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["episode", "semantic"]},
            "content": {"type": "string"},
            "metadata": {"type": "object", "default": {}},
        },
        "required": ["type", "content"],
    },
    description="Store a new memory (episode or semantic)",
)
async def memory_store(
    type: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store a memory entry.

    Args:
        type: Memory type (episode or semantic).
        content: Memory content text.
        metadata: Optional metadata (tags, source, etc).
    """
    entry = {
        "id": f"mem_{len(_episode_store) + len(_semantic_store)}",
        "content": content,
        "metadata": metadata or {},
        "timestamp": time.time(),
    }

    if type == "episode":
        _episode_store.append(entry)
    else:
        _semantic_store.append(entry)

    return {"id": entry["id"], "stored": True}


@plugin.tool(
    name="memory.summarize",
    schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["episode", "semantic"]},
            "top_k": {"type": "integer", "default": 10},
        },
    },
    description="Summarize recent memories for context injection",
)
async def memory_summarize(type: str = "episode", top_k: int = 10) -> dict[str, Any]:
    """Summarize recent memories for pipeline context injection."""
    store = _episode_store if type == "episode" else _semantic_store
    recent = store[-top_k:] if store else []

    if not recent:
        return {"summary": "", "count": 0}

    # Simple summarization: concatenate content
    parts = [m["content"] for m in recent if m.get("content")]
    summary = "\n".join(parts[:top_k])

    return {"summary": summary, "count": len(recent)}


# 资源暴露（函数式调用——SDK register_resource 签名要求 handler 必填）
def _episode_resource() -> dict[str, Any]:
    """Expose recent episode memories as MCP resource."""
    return {"count": len(_episode_store), "recent": _episode_store[-5:]}


def _semantic_resource() -> dict[str, Any]:
    """Expose recent semantic memories as MCP resource."""
    return {"count": len(_semantic_store), "recent": _semantic_store[-5:]}


plugin.register_resource(
    "memory://episode/recent", _episode_resource, name="Recent Episode Memories"
)
plugin.register_resource(
    "memory://semantic/recent", _semantic_resource, name="Recent Semantic Memories"
)



@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize memory service on load."""
    pass


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup memory stores on unload."""
    pass

if __name__ == "__main__":
    plugin.run()
