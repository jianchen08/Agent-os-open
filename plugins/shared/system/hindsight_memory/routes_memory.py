"""记忆管理 API 路由（memory 域）——自持版，由 hindsight_memory_service http.handle 分发。

迁移自 channel_api/routes_memory.py（channel_api 退役方案批次 1：memory 域 → 本插件）：

- 业务函数原样保留，响应形态逐项对齐（前端 services/api/memory.ts 直接消费）；
- 剥离 FastAPI 依赖：无 APIRouter/Depends/Query/pydantic 模型，返回纯 dict；
- 出错抛 :class:`MemoryAPIError`（status_code/error_code/message），由 server.py
  http.handle 统一捕获转对应 HTTP 状态（404 形态与旧版一致：body `{"detail": ...}`）；
- 后端注入机制不变：模块级 ``set_memory_backend`` / ``_get_memory_backend``，
  server.py 分发时懒构建注入（幂等；能力缺失保持 None → 空结果降级），测试直接传 mock；
- user_id 恒 "default"——与 channel_api 分发时省略 ``_user``（Depends 缺省 → "default"）
  的行为逐位对齐；鉴权由内核 dispatcher 按 http_endpoints.auth=user 完成，handler 不读身份。

数据结构（IMemoryBackend 统一形态）：{id, content, score, memory_type, metadata}。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── 模块级依赖注入（与 channel_api 同款模式）───────────────────────────────
# 长期记忆后端（IMemoryBackend：search/delete 等），None 时所有端点空结果降级。
# 由 server.py http.handle 懒构建注入，测试直接赋值。
_memory_backend: Any | None = None

# 统计/单条查询的检索上限（后端 search top_k 语义）
_STATS_TOP_K = 1000
_FETCH_TOP_K = 1000

# 后端缺省 user_id（channel_api 分发时不传 _user → FastAPI Depends 缺省 → "default"）
_DEFAULT_USER_ID = "default"


class MemoryAPIError(Exception):
    """记忆域业务异常，携带 HTTP 状态码与错误码（server.py 捕获转 HTTP 响应）。"""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


def set_memory_backend(backend: Any | None) -> None:
    """注入 IMemoryBackend 实例（由 server.py 在分发时注入）。

    Args:
        backend: 实现 search/delete 的后端实例（HindsightBackend 或 duck-type）；
            传 None 清空（端点空结果降级）。
    """
    global _memory_backend
    _memory_backend = backend


def _get_memory_backend() -> Any | None:
    """返回当前注入的记忆后端（None 表示未注入，端点空结果降级）。"""
    return _memory_backend


def _resolve_user_id() -> str:
    """返回用户隔离 key（当前恒 "default"，见模块 docstring 说明）。"""
    return _DEFAULT_USER_ID


def _memory_to_response(m: dict[str, Any]) -> dict[str, Any]:
    """将后端统一形态 {id, content, score, memory_type, metadata} 转响应 dict。"""
    metadata = m.get("metadata") or {}
    return {
        "id": m["id"],
        "content": m.get("content", ""),
        "memory_type": m.get("memory_type", ""),
        "tags": metadata.get("tags", []),
        "score": m.get("score", 0.0),
        "created_at": metadata.get("created_at", ""),
    }


async def list_memories(
    memory_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """获取记忆条目列表。

    支持按记忆类型筛选，分页返回（后端无 offset 语义，top_k 截断）。

    Args:
        memory_type: 按类型筛选 (episode/semantic/procedural)
        limit: 每页数量
        offset: 偏移量

    Returns:
        {items, total}
    """
    if limit < 1 or limit > 100:
        raise MemoryAPIError(400, "VAL_RANGE_7003", "limit 必须在 1-100 之间")
    if offset < 0:
        raise MemoryAPIError(400, "VAL_RANGE_7003", "offset 不能为负数")
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    results = await backend.search(
        query="",
        user_id=_resolve_user_id(),
        top_k=limit,
        memory_type=memory_type,
    )
    items = [_memory_to_response(m) for m in results]
    return {"items": items, "total": len(items)}


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


async def search_memories(
    query: str,
    top_k: int = 5,
    method: str = "keyword",
) -> dict[str, Any]:
    """搜索记忆条目，返回按相关度排序的结果。

    method 为检索方法名；当它本身是记忆类型（episode/semantic/...）时作为
    memory_type 过滤透传，否则（keyword/vector/tagwave）不限定类型。

    Returns:
        {items, total}
    """
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    results = await backend.search(
        query=query,
        user_id=_resolve_user_id(),
        top_k=top_k,
        memory_type=_method_to_memory_type(method),
    )
    items = [_memory_to_response(m) for m in results]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 情景记忆端点
# ---------------------------------------------------------------------------


async def list_episodes(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """获取情景记忆列表。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    results = await backend.search(
        query="",
        user_id=_resolve_user_id(),
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


async def get_episode(episode_id: str) -> dict[str, Any]:
    """获取单个情景记忆（按 id 过滤后端 episode 检索结果）。"""
    backend = _get_memory_backend()
    if backend is not None:
        results = await backend.search(
            query="",
            user_id=_resolve_user_id(),
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
    raise MemoryAPIError(404, "MEM_NOTF_5001", "未找到相关记忆")


# ---------------------------------------------------------------------------
# 语义记忆端点
# ---------------------------------------------------------------------------


async def list_semantic() -> dict[str, Any]:
    """获取语义记忆列表。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    results = await backend.search(
        query="",
        user_id=_resolve_user_id(),
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


async def consolidate_memory() -> dict[str, Any]:
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


async def get_memory_stats() -> dict[str, Any]:
    """获取记忆统计信息（按类型聚合后端检索计数）。"""
    backend = _get_memory_backend()
    if backend is None:
        return {
            "episode_count": 0,
            "knowledge_count": 0,
            "total_count": 0,
            "last_updated": "",
        }
    user_id = _resolve_user_id()
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
# POST 搜索（前端 memory.ts 使用 POST /memory/search）
# ---------------------------------------------------------------------------


async def search_memories_post(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """搜索记忆条目（POST 方式，body: {query, top_k}）。"""
    if body is None:
        return {"items": [], "total": 0}
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    query = body.get("query", "")
    top_k = body.get("top_k", 5)
    results = await backend.search(
        query=query,
        user_id=_resolve_user_id(),
        top_k=int(top_k),
        memory_type=None,
    )
    items = [_memory_to_response(m) for m in results]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 动态路径端点 — 必须放在所有固定路径之后，否则 /stats、/semantic 等会被 {memory_id} 捕获
# （server.py 分发按固定路径先行匹配，此处仅保留函数本身）
# ---------------------------------------------------------------------------


async def get_memory(memory_id: str) -> dict[str, Any]:
    """获取指定记忆条目的详情（按 id 过滤后端检索结果）。

    Args:
        memory_id: 记忆 ID

    Returns:
        MemoryResponse dict 记忆详情

    Raises:
        MemoryAPIError: 记忆不存在 (404)
    """
    backend = _get_memory_backend()
    if backend is not None:
        results = await backend.search(
            query="",
            user_id=_resolve_user_id(),
            top_k=_FETCH_TOP_K,
            memory_type=None,
        )
        for m in results:
            if m["id"] == memory_id:
                return _memory_to_response(m)
    raise MemoryAPIError(404, "MEM_NOTF_5001", "未找到相关记忆")


async def delete_memory(memory_id: str) -> dict[str, str]:
    """删除指定记忆条目。

    Args:
        memory_id: 记忆 ID

    Returns:
        删除成功消息

    Raises:
        MemoryAPIError: 记忆不存在 (404)
    """
    backend = _get_memory_backend()
    deleted = False
    if backend is not None:
        deleted = bool(
            await backend.delete(
                user_id=_resolve_user_id(),
                memory_id=memory_id,
            )
        )
    if not deleted:
        raise MemoryAPIError(404, "MEM_NOTF_5001", "未找到相关记忆")
    return {"message": "记忆已删除"}
