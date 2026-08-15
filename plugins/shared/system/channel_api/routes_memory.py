"""记忆管理 API 路由。

提供记忆条目的列表、搜索、导入和删除接口。

Step 7 重建：数据源由 memory_store（进程内 dict，第三存储，恒空）切换到
IMemoryBackend（Hindsight / Kernel 后端，见 hindsight_memory/memory_backend.py）。
- 后端经模块级 `set_memory_backend()` 注入（由 server.py 在 http.handle 分发生成时
  懒构建注入；测试直接传 AsyncMock）。
- 未注入后端时所有端点空结果降级（等价旧 memory_store 恒空 dict 的行为），不崩溃。
- 路由为 async：后端 search/import_document/delete 均为 async 能力调用；
  server.py 的 memory 域分发器已同步为 async 并 await 各路由。
"""

from __future__ import annotations

import logging
from typing import Any

from deps import APIError, require_auth, validate_pagination
from fastapi import APIRouter, Depends, Query
from fastapi.params import Query as QueryParam
from models import (
    MemoryListResponse,
    MemoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/memory", tags=["记忆"])

# ── 模块级依赖注入（与 experience_consolidator / review 插件同款模式）──────────
# 长期记忆后端（IMemoryBackend：search/delete/import_document 等），None 时
# 所有端点空结果降级。由 server.py 懒构建注入，测试直接赋值。
_memory_backend: Any | None = None

# 统计/单条查询的检索上限（后端 search top_k 语义）
_STATS_TOP_K = 1000
_FETCH_TOP_K = 1000


def set_memory_backend(backend: Any | None) -> None:
    """注入 IMemoryBackend 实例（由 server.py 在分发时注入）。

    Args:
        backend: 实现 search/delete/import_document 的后端实例
            （HindsightBackend / KernelMemoryBackend 或 duck-type）；传 None 清空
    """
    global _memory_backend
    _memory_backend = backend


def _get_memory_backend() -> Any | None:
    """返回当前注入的记忆后端（None 表示未注入，端点空结果降级）。"""
    return _memory_backend


def _resolve_user_id(_user: Any) -> str:
    """从 FastAPI 注入的 _user 提取 user_id。

    dispatcher 直调时不传 _user（默认值是 Depends 对象而非 dict），此时回退
    "default" 用户，保证后端调用始终有合法的隔离 key。
    """
    if isinstance(_user, dict):
        return str(_user.get("sub") or _user.get("id") or "default")
    return "default"


def _memory_to_response(m: dict[str, Any]) -> MemoryResponse:
    """将后端统一形态 {id, content, score, memory_type, metadata} 转 MemoryResponse。"""
    metadata = m.get("metadata") or {}
    return MemoryResponse(
        id=m["id"],
        content=m.get("content", ""),
        memory_type=m.get("memory_type", ""),
        tags=metadata.get("tags", []),
        score=m.get("score", 0.0),
        created_at=metadata.get("created_at", ""),
    )


@router.get(
    "",
    response_model=MemoryListResponse,
    summary="获取记忆列表",
)
async def list_memories(
    memory_type: str | None = Query(
        default=None,
        description="按类型筛选 (episode/semantic/procedural)",
    ),
    limit: int = Query(default=20, ge=1, le=100, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _user: dict = Depends(require_auth),
) -> MemoryListResponse:
    """获取记忆条目列表。

    支持按记忆类型筛选，分页返回（后端无 offset 语义，top_k 截断）。

    Returns:
        MemoryListResponse 包含 items 和 total
    """
    validate_pagination(limit, offset)
    backend = _get_memory_backend()
    if backend is None:
        return MemoryListResponse(items=[], total=0)
    results = await backend.search(
        query="",
        user_id=_resolve_user_id(_user),
        top_k=limit,
        memory_type=memory_type,
    )
    items = [_memory_to_response(m) for m in results]
    return MemoryListResponse(items=items, total=len(items))


@router.get(
    "/search",
    response_model=MemoryListResponse,
    summary="搜索记忆",
)
async def search_memories(
    query: str = Query(..., description="搜索关键词"),
    top_k: int = Query(default=5, ge=1, le=50, description="返回数量"),
    method: str = Query(
        default="keyword",
        description="检索方法 (keyword/vector/tagwave)",
    ),
    _user: dict = Depends(require_auth),
) -> MemoryListResponse:
    """搜索记忆条目。

    支持关键词搜索，返回按相关度排序的结果。method 为检索方法名，
    当它本身是记忆类型（episode/semantic/...）时作为 memory_type 过滤透传，
    否则（keyword/vector/tagwave）不限定类型。

    Args:
        query: 搜索关键词
        top_k: 返回数量
        method: 检索方法

    Returns:
        MemoryListResponse 搜索结果
    """
    backend = _get_memory_backend()
    if backend is None:
        return MemoryListResponse(items=[], total=0)
    results = await backend.search(
        query=query,
        user_id=_resolve_user_id(_user),
        top_k=top_k,
        memory_type=_method_to_memory_type(method),
    )
    items = [_memory_to_response(m) for m in results]
    return MemoryListResponse(items=items, total=len(items))


def _method_to_memory_type(method: str | None) -> str | None:
    """把检索方法名映射为 memory_type 过滤。

    仅当 method 本身是记忆类型名（episode/semantic/procedural 等）时透传，
    通用检索方法（keyword/vector/tagwave/空）返回 None（不限类型）。
    """
    if not method:
        return None
    m = method.strip().lower()
    if m in ("episode", "semantic", "procedural", "experience", "chunk"):
        return m
    return None


# ---------------------------------------------------------------------------
# 情景记忆端点
# ---------------------------------------------------------------------------


@router.get(
    "/episodes",
    summary="获取情景记忆列表",
)
async def list_episodes(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取情景记忆列表。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    results = await backend.search(
        query="",
        user_id=_resolve_user_id(_user),
        top_k=page_size,
        memory_type="episode",
    )
    items = [
        {
            "id": m["id"],
            "intent_text": m.get("content", ""),
            "tags": (m.get("metadata") or {}).get("tags", []),
            "created_at": (m.get("metadata") or {}).get("created_at", ""),
        }
        for m in results
    ]
    return {"items": items, "total": len(items), "page": page, "page_size": page_size}


@router.get(
    "/episodes/{episode_id}",
    summary="获取单个情景记忆",
)
async def get_episode(
    episode_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取单个情景记忆（按 id 过滤后端 episode 检索结果）。"""
    backend = _get_memory_backend()
    if backend is not None:
        results = await backend.search(
            query="",
            user_id=_resolve_user_id(_user),
            top_k=_FETCH_TOP_K,
            memory_type="episode",
        )
        for m in results:
            if m["id"] == episode_id:
                metadata = m.get("metadata") or {}
                return {
                    "id": m["id"],
                    "intent_text": m.get("content", ""),
                    "tags": metadata.get("tags", []),
                    "created_at": metadata.get("created_at", ""),
                }
    raise APIError(status_code=404, error_code="MEM_NOTF_5001", message="未找到相关记忆")


# ---------------------------------------------------------------------------
# 语义记忆端点
# ---------------------------------------------------------------------------


@router.get(
    "/semantic",
    summary="获取语义记忆列表",
)
async def list_semantic(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取语义记忆列表。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    results = await backend.search(
        query="",
        user_id=_resolve_user_id(_user),
        top_k=100,
        memory_type="semantic",
    )
    items = [
        {
            "id": m["id"],
            "content": m.get("content", ""),
            "source_type": "memory_backend",
            "extra_data": {},
            "created_at": (m.get("metadata") or {}).get("created_at", ""),
        }
        for m in results
    ]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 记忆整合与统计
# ---------------------------------------------------------------------------


@router.post(
    "/consolidate",
    summary="记忆整合",
)
async def consolidate_memory(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """触发记忆整合操作。

    后端若提供 reflect 方法则调用并返回整合条数；否则保持空操作 stub。
    """
    backend = _get_memory_backend()
    consolidated = 0
    if backend is not None and hasattr(backend, "reflect"):
        try:
            result = await backend.reflect()
            if isinstance(result, dict):
                consolidated = int(result.get("consolidated_count", result.get("count", 0)) or 0)
            elif isinstance(result, int):
                consolidated = result
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆整合失败（降级为 0）: %s", e)
    return {"success": True, "message": "记忆整合完成", "consolidated_count": consolidated}


@router.get(
    "/stats",
    summary="获取记忆统计",
)
async def get_memory_stats(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取记忆统计信息（按类型聚合后端检索计数）。"""
    backend = _get_memory_backend()
    if backend is None:
        return {
            "episode_count": 0,
            "knowledge_count": 0,
            "total_count": 0,
            "last_updated": "",
        }
    user_id = _resolve_user_id(_user)
    episodes = await backend.search(
        query="", user_id=user_id, top_k=_STATS_TOP_K, memory_type="episode"
    )
    semantic = await backend.search(
        query="", user_id=user_id, top_k=_STATS_TOP_K, memory_type="semantic"
    )
    episode_count = len(episodes)
    semantic_count = len(semantic)
    return {
        "episode_count": episode_count,
        "knowledge_count": semantic_count,
        "total_count": episode_count + semantic_count,
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
async def search_memories_post(
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> MemoryListResponse:
    """搜索记忆条目（POST 方式）。"""
    if body is None:
        return MemoryListResponse(items=[], total=0)
    backend = _get_memory_backend()
    if backend is None:
        return MemoryListResponse(items=[], total=0)
    query = body.get("query", "")
    top_k = body.get("top_k", 5)
    results = await backend.search(
        query=query,
        user_id=_resolve_user_id(_user),
        top_k=int(top_k),
        memory_type=None,
    )
    items = [_memory_to_response(m) for m in results]
    return MemoryListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# 文档导入端点（Step 7 新增）
# ---------------------------------------------------------------------------


@router.post(
    "/import",
    summary="导入文档到记忆",
)
async def import_document(
    text: str | None = Query(default=None, description="文档文本（与 file_path 二选一）"),
    file_path: str | None = Query(default=None, description="文档文件路径"),
    name: str = Query(default="", description="知识标签/名称"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """导入文档（切块后落库为语义记忆）。

    Args:
        text: 文档文本
        file_path: 文档文件路径
        name: 知识标签

    Returns:
        {"imported": N, "name": ...}；未注入后端时返回 0（降级，不抛异常）。
    """
    # dispatcher 直调省略参数时默认值是 FastAPI Query 包装对象，需还原为裸值
    text = None if isinstance(text, QueryParam) else text
    file_path = None if isinstance(file_path, QueryParam) else file_path
    name = "" if isinstance(name, QueryParam) else (name or "")
    backend = _get_memory_backend()
    if backend is None:
        return {"imported": 0, "name": name}
    result = await backend.import_document(
        user_id=_resolve_user_id(_user),
        text=text,
        file_path=file_path,
        name=name,
    )
    if not isinstance(result, dict):
        return {"imported": 0, "name": name}
    return {
        "imported": int(result.get("chunks_imported", 0) or 0),
        "name": result.get("name") or name,
    }


# ---------------------------------------------------------------------------
# 动态路径端点 — 必须放在所有固定路径之后，否则 /stats、/semantic 等会被 {memory_id} 捕获
# ---------------------------------------------------------------------------


@router.get(
    "/{memory_id}",
    response_model=MemoryResponse,
    summary="获取记忆详情",
)
async def get_memory(
    memory_id: str,
    _user: dict = Depends(require_auth),
) -> MemoryResponse:
    """获取指定记忆条目的详情（按 id 过滤后端检索结果）。

    Args:
        memory_id: 记忆 ID

    Returns:
        MemoryResponse 记忆详情

    Raises:
        APIError: 记忆不存在 (404)
    """
    backend = _get_memory_backend()
    if backend is not None:
        results = await backend.search(
            query="",
            user_id=_resolve_user_id(_user),
            top_k=_FETCH_TOP_K,
            memory_type=None,
        )
        for m in results:
            if m["id"] == memory_id:
                return _memory_to_response(m)
    raise APIError(
        status_code=404,
        error_code="MEM_NOTF_5001",
        message="未找到相关记忆",
    )


@router.delete(
    "/{memory_id}",
    summary="删除记忆",
)
async def delete_memory(
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
    backend = _get_memory_backend()
    deleted = False
    if backend is not None:
        deleted = bool(await backend.delete(
            user_id=_resolve_user_id(_user),
            memory_id=memory_id,
        ))
    if not deleted:
        raise APIError(
            status_code=404,
            error_code="MEM_NOTF_5001",
            message="未找到相关记忆",
        )
    return {"message": "记忆已删除"}
