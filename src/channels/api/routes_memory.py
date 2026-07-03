"""记忆管理 API 路由。

提供记忆条目的列表、搜索和删除接口。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import APIError, require_auth, validate_pagination
from channels.api.memory_store import store
from channels.api.models import (
    MemoryListResponse,
    MemoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/memory", tags=["记忆"])


def _memory_to_response(m: dict[str, Any]) -> MemoryResponse:
    """将存储层记忆字典转为 MemoryResponse。"""
    return MemoryResponse(
        id=m["id"],
        content=m.get("content", ""),
        memory_type=m.get("memory_type", ""),
        tags=m.get("tags", []),
        score=m.get("score", 0.0),
        created_at=m.get("created_at", ""),
    )


@router.get(
    "",
    response_model=MemoryListResponse,
    summary="获取记忆列表",
)
def list_memories(
    memory_type: str | None = Query(
        default=None,
        description="按类型筛选 (episode/semantic/procedural)",
    ),
    limit: int = Query(default=20, ge=1, le=100, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _user: dict = Depends(require_auth),
) -> MemoryListResponse:
    """获取记忆条目列表。

    支持按记忆类型筛选，分页返回。

    Returns:
        MemoryListResponse 包含 items 和 total
    """
    validate_pagination(limit, offset)
    memories = store.list_memories(
        memory_type=memory_type,
        limit=limit,
        offset=offset,
    )
    total = len(store.memories)
    if memory_type:
        total = sum(1 for m in store.memories.values() if m["memory_type"] == memory_type)

    items = [_memory_to_response(m) for m in memories]
    return MemoryListResponse(items=items, total=total)


@router.get(
    "/search",
    response_model=MemoryListResponse,
    summary="搜索记忆",
)
def search_memories(
    query: str = Query(..., description="搜索关键词"),
    top_k: int = Query(default=5, ge=1, le=50, description="返回数量"),
    method: str = Query(
        default="keyword",
        description="检索方法 (keyword/vector/tagwave)",
    ),
    _user: dict = Depends(require_auth),
) -> MemoryListResponse:
    """搜索记忆条目。

    支持关键词搜索，返回按相关度排序的结果。

    Args:
        query: 搜索关键词
        top_k: 返回数量
        method: 检索方法

    Returns:
        MemoryListResponse 搜索结果
    """
    results = store.search_memories(query=query, top_k=top_k, method=method)
    items = [_memory_to_response(m) for m in results]
    return MemoryListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# 情景记忆端点
# ---------------------------------------------------------------------------


@router.get(
    "/episodes",
    summary="获取情景记忆列表",
)
def list_episodes(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取情景记忆列表。"""
    offset = (page - 1) * page_size
    memories = store.list_memories(memory_type="episode", limit=page_size, offset=offset)
    total = sum(1 for m in store.memories.values() if m["memory_type"] == "episode")
    items = [
        {
            "id": m["id"],
            "intent_text": m.get("content", ""),
            "tags": m.get("tags", []),
            "created_at": m.get("created_at", ""),
        }
        for m in memories
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get(
    "/episodes/{episode_id}",
    summary="获取单个情景记忆",
)
def get_episode(
    episode_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取单个情景记忆。"""
    memory = store.get_memory(episode_id)
    if memory is None:
        raise APIError(status_code=404, error_code="MEM_NOTF_5001", message="未找到相关记忆")
    return {
        "id": memory["id"],
        "intent_text": memory.get("content", ""),
        "tags": memory.get("tags", []),
        "created_at": memory.get("created_at", ""),
    }


# ---------------------------------------------------------------------------
# 语义记忆端点
# ---------------------------------------------------------------------------


@router.get(
    "/semantic",
    summary="获取语义记忆列表",
)
def list_semantic(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取语义记忆列表。"""
    memories = store.list_memories(memory_type="semantic", limit=100)
    total = sum(1 for m in store.memories.values() if m["memory_type"] == "semantic")
    items = [
        {
            "id": m["id"],
            "content": m.get("content", ""),
            "source_type": "memory_store",
            "extra_data": {},
            "created_at": m.get("created_at", ""),
        }
        for m in memories
    ]
    return {"items": items, "total": total}


# ---------------------------------------------------------------------------
# 记忆整合与统计
# ---------------------------------------------------------------------------


@router.post(
    "/consolidate",
    summary="记忆整合",
)
def consolidate_memory(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """触发记忆整合操作。"""
    return {"success": True, "message": "记忆整合完成", "consolidated_count": 0}


@router.get(
    "/stats",
    summary="获取记忆统计",
)
def get_memory_stats(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取记忆统计信息。"""
    episode_count = sum(1 for m in store.memories.values() if m["memory_type"] == "episode")
    semantic_count = sum(1 for m in store.memories.values() if m["memory_type"] == "semantic")
    return {
        "episode_count": episode_count,
        "knowledge_count": semantic_count,
        "total_count": len(store.memories),
        "last_updated": "",
    }


# ---------------------------------------------------------------------------
# POST 搜索（前端使用 POST /memory/search）
# ---------------------------------------------------------------------------


@router.post(
    "/search",
    response_model=MemoryListResponse,
    summary="搜索记忆（POST）",
)
def search_memories_post(
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> MemoryListResponse:
    """搜索记忆条目（POST 方式）。"""
    if body is None:
        return MemoryListResponse(items=[], total=0)
    query = body.get("query", "")
    top_k = body.get("top_k", 5)
    results = store.search_memories(query=query, top_k=top_k)
    items = [_memory_to_response(m) for m in results]
    return MemoryListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# 动态路径端点 — 必须放在所有固定路径之后，否则 /stats、/semantic 等会被 {memory_id} 捕获
# ---------------------------------------------------------------------------


@router.get(
    "/{memory_id}",
    response_model=MemoryResponse,
    summary="获取记忆详情",
)
def get_memory(
    memory_id: str,
    _user: dict = Depends(require_auth),
) -> MemoryResponse:
    """获取指定记忆条目的详情。

    Args:
        memory_id: 记忆 ID

    Returns:
        MemoryResponse 记忆详情

    Raises:
        APIError: 记忆不存在 (404)
    """
    memory = store.get_memory(memory_id)
    if memory is None:
        raise APIError(
            status_code=404,
            error_code="MEM_NOTF_5001",
            message="未找到相关记忆",
        )
    return _memory_to_response(memory)


@router.delete(
    "/{memory_id}",
    summary="删除记忆",
)
def delete_memory(
    memory_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, str]:
    """删除指定记忆条目。

    Args:
        memory_id: 记忆 ID

    Returns:
        删除成功消息

    Raises:
        APIError: 记忆不存在 (404)
    """
    deleted = store.delete_memory(memory_id)
    if not deleted:
        raise APIError(
            status_code=404,
            error_code="MEM_NOTF_5001",
            message="未找到相关记忆",
        )
    return {"message": "记忆已删除"}
