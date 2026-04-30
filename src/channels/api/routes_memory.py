"""记忆管理 API 路由。

提供记忆条目的列表、搜索和删除接口。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import APIError, require_auth, validate_pagination
from channels.api.models import (
    MemoryListResponse,
    MemoryResponse,
    MemorySearchRequest,
    store,
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
        total = sum(
            1 for m in store.memories.values()
            if m["memory_type"] == memory_type
        )

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
            error_code="MEM_001",
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
            error_code="MEM_001",
            message="未找到相关记忆",
        )
    return {"message": "记忆已删除"}
