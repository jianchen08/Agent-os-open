"""记忆管理 API 路由（memory 域），由 hindsight_memory_service http.handle 分发。

- 业务函数响应形态逐项对齐（前端 services/api/memory.ts 直接消费）；
- 剥离 FastAPI 依赖：无 APIRouter/Depends/Query/pydantic 模型，返回纯 dict；
- 出错抛 :class:`MemoryAPIError`（status_code/error_code/message），由 server.py
  http.handle 统一捕获转对应 HTTP 状态（404 形态与旧版一致：body `{"detail": ...}`）；
- 后端注入机制：模块级 ``set_memory_backend`` / ``_get_memory_backend``，
  server.py 分发时懒构建注入（幂等；能力缺失保持 None → 空结果降级），测试直接传 mock；
- user_id 恒 "default"——与 channel_api 分发时省略 ``_user``（Depends 缺省 → "default"）
  的行为逐位对齐；鉴权由内核 dispatcher 按 http_endpoints.auth=user 完成，handler 不读身份。

数据结构（IMemoryBackend 统一形态）：{id, content, score, memory_type, metadata}。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── 模块级依赖注入 ──────────────────────────────────────────────────────────
# 长期记忆后端（IMemoryBackend：search/delete 等），None 时所有端点空结果降级。
# 由 server.py http.handle 懒构建注入，测试直接赋值。
_memory_backend: Any | None = None

# 单条查询走 search 时的检索上限（后端 search top_k 语义）
_FETCH_TOP_K = 1000
# 列表面（documents 通路）单次取回上限＝sidecar hindsight.get_documents 工具
# limit 的 schema maximum；列表无相关性排序语义，超出部分属分页范畴不做假分页
_LISTING_LIMIT_CAP = 100

# 后端缺省 user_id（分发时不传 _user → Depends 缺省 → "default"）
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


def _type_tag_filter(memory_type: str | None) -> list[str] | None:
    """把 memory_type 过滤转换为服务端 type:* 标签过滤（retain 落库时注入）。"""
    if not memory_type:
        return None
    return [f"type:{memory_type}"]


def _document_to_memory(doc: dict[str, Any]) -> dict[str, Any]:
    """把 documents 通路条目映射为统一记忆形态 {id, content, score, memory_type, tags, created_at}。

    - content ← original_text（documents 面保有原文；recall 返回的是抽取后事实）；
    - score 恒 0——列举无相关性排序语义，不伪造评分；
    - memory_type 取自服务端 type:* 标签（retain 注入），缺失回落
      document_metadata.memory_type；
    - tags 剔除内部 type:* 前缀，避免实现细节外泄到前端。
    """
    raw_tags = [str(t) for t in (doc.get("tags") or []) if t]
    resolved_type = next((t[len("type:"):] for t in raw_tags if t.startswith("type:")), "")
    if not resolved_type:
        meta = doc.get("document_metadata") or {}
        resolved_type = str(meta.get("memory_type", "") or "")
    return {
        "id": str(doc.get("id", "")),
        "content": doc.get("original_text", ""),
        "score": 0.0,
        "memory_type": resolved_type,
        "tags": [t for t in raw_tags if not t.startswith("type:")],
        "created_at": doc.get("created_at", ""),
    }


async def _list_bank_documents(
    backend: Any, memory_type: str | None, limit: int
) -> list[dict[str, Any]]:
    """经 documents/list 通路取回 bank 文档（列表/统计的既定数据面）。

    recall 是带 query 的检索 API（空 query 服务端必拒），列表面必须走本通路，
    否则链路断裂。能力失败（RuntimeError）诚实上抛由 http.handle 转 500。
    """
    docs = await backend.get_documents(
        user_id=_resolve_user_id(),
        tags=_type_tag_filter(memory_type),
        tags_match="any_strict",
        limit=limit,
    )
    return [d for d in docs or [] if isinstance(d, dict)]


async def list_memories(
    memory_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """获取记忆条目列表。

    支持按记忆类型筛选，经 documents/list 通路返回（后端无 offset 语义）。

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
    docs = await _list_bank_documents(backend, memory_type, limit)
    items = [_document_to_memory(d) for d in docs]
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
    """获取情景记忆列表（documents/list 通路，按 type:episode 服务端过滤）。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    docs = await _list_bank_documents(backend, "episode", page_size)
    items = [
        {
            "id": m["id"],
            "intent_text": m["content"],
            "tags": m["tags"],
            "created_at": m["created_at"],
        }
        for m in (_document_to_memory(d) for d in docs)
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
    """获取语义记忆列表（documents/list 通路，按 type:semantic 服务端过滤）。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    docs = await _list_bank_documents(backend, "semantic", _LISTING_LIMIT_CAP)
    items = [
        {
            "id": m["id"],
            "content": m["content"],
            "source_type": "memory_backend",
            "extra_data": {},
            "created_at": m["created_at"],
        }
        for m in (_document_to_memory(d) for d in docs)
    ]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 记忆整合与统计
# ---------------------------------------------------------------------------


async def consolidate_memory() -> dict[str, Any]:
    """触发记忆整合操作。

    后端若提供 reflect 方法则调用并返回整合条数；否则保持空操作 stub。
    整合失败（reflect 抛错/返回非契约类型）fail-closed：success=false +
    error 说明，失败不伪装成功（前端按 success 布尔消费，响应键面不变）。
    """
    backend = _get_memory_backend()
    consolidated = 0
    if backend is not None and hasattr(backend, "reflect"):
        try:
            result = await backend.reflect()
        except Exception as e:  # noqa: BLE001 — 失败转为错误信封上抛展示面
            return {
                "success": False,
                "message": f"记忆整合失败: {e}",
                "error": str(e),
                "consolidated_count": 0,
            }
        if isinstance(result, dict):
            consolidated = int(result.get("consolidated_count", result.get("count", 0)) or 0)
        elif isinstance(result, int):
            consolidated = result
        else:
            return {
                "success": False,
                "message": f"记忆整合失败: reflect 返回非预期类型 {type(result).__name__}",
                "error": f"reflect 返回非预期类型: {type(result).__name__}",
                "consolidated_count": 0,
            }
    return {"success": True, "message": "记忆整合完成", "consolidated_count": consolidated}


async def get_memory_stats() -> dict[str, Any]:
    """获取记忆统计信息（documents/list 通路按 type 标签聚合计数）。"""
    backend = _get_memory_backend()
    if backend is None:
        return {
            "episode_count": 0,
            "knowledge_count": 0,
            "total_count": 0,
            "last_updated": "",
        }
    episode_count = len(
        await _list_bank_documents(backend, "episode", _LISTING_LIMIT_CAP)
    )
    semantic_count = len(
        await _list_bank_documents(backend, "semantic", _LISTING_LIMIT_CAP)
    )
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
