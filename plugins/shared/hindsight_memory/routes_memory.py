"""记忆管理 API 路由（memory 域），由 hindsight_memory_service http.handle 分发。

- 业务函数响应形态逐项对齐（前端 services/api/memory.ts 直接消费）；
- 剥离 FastAPI 依赖：无 APIRouter/Depends/Query/pydantic 模型，返回纯 dict；
- 出错抛 :class:`MemoryAPIError`（status_code/error_code/message），由 server.py
  http.handle 统一捕获转对应 HTTP 状态（404 响应 body 形态：`{"detail": ...}`）；
- 后端注入机制：模块级 ``set_memory_backend`` / ``_get_memory_backend``，
  server.py 分发时懒构建注入（幂等；能力缺失保持 None → 空结果降级），测试直接传 mock；
- 身份归属（X4-F1）：鉴权由内核 dispatcher 按 http_endpoints.auth=user 完成后，
  权威注入 ``x-agentos-tenant`` / ``x-agentos-user`` 头（匿名请求剥离伪造头，
  handler 可见即可信）。可见 bank 集合由 :func:`resolve_caller_banks` fail-closed
  判定：会话 bank（thread-*）经 db-read sessions 面（租户内建收窄，Authorization
  原头交内核复权）判定归属；默认 bank 仅 admin 种子租户（tenant_id="default"）
  可见；身份头缺失/归属不可判 → 空集（宁可少显示不可跨户）。业务函数以 ``banks``
  参数接收该集合，绝不自行扩大探查面。

数据结构（IMemoryBackend 统一形态）：{id, content, score, memory_type, metadata}。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from _root_api import DbReadQueryError, db_read_query  # 共享根显式出口（E2 闸）
from memory_backend import THREAD_BANK_PREFIX  # 同插件单源（tag/bank 前缀契约，B11-S5）

logger = logging.getLogger(__name__)

# ── 模块级依赖注入 ──────────────────────────────────────────────────────────
# 长期记忆后端（IMemoryBackend：search/delete 等），None 时所有端点空结果降级。
# 由 server.py http.handle 懒构建注入，测试直接赋值。
_memory_backend: Any | None = None

# 列表面（documents 通路）单次取回上限＝sidecar hindsight.get_documents 工具
# limit 的 schema maximum；列表无相关性排序语义，超出部分属分页范畴不做假分页
_LISTING_LIMIT_CAP = 100

# 默认 bank id（＝admin 种子租户 id：一用户一租户模型下 default bank 即 admin
# 的家底 bank；其他租户不可见）
_DEFAULT_BANK_ID = "default"

# 内核 dispatcher 权威注入的身份头 + 入站 Authorization 原头（db-read 复权锚）
_TENANT_HEADER = "x-agentos-tenant"
_USER_HEADER = "x-agentos-user"
_AUTHORIZATION_HEADER = "authorization"

# 会话归属查询的 db-read 数据面（内核 read_faces.yaml 的 sessions 面，scope=tenant）
_SESSIONS_FACE = "sessions"
# IN 绑定分片上限（SQLite 变量数防御；超片循环查询）
_DB_READ_CHUNK = 500


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


def _header_value(headers: dict[str, str] | None, name: str) -> str:
    """取入站请求头值（内核 header key 已小写，大小写防御）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == name and v:
            return v
    return ""


async def _session_banks(backend: Any) -> list[str]:
    """hindsight 实例发现的会话 bank 集合（thread-*，字典序）。

    memory 工具按可信 caller 身份（会话 id）分 bank 落库。后端无 bank 发现
    能力（list_banks 缺失或非协程方法）时返回空集（consolidate_memory 的
    reflect 能力探测同款约定）；归属判定由 :func:`resolve_caller_banks` 在
    本集合之上收敛。
    """
    if backend is None:
        return []
    discover = getattr(backend, "list_banks", None)
    if not callable(discover) or not inspect.iscoroutinefunction(discover):
        return []
    bank_ids = await discover()
    return sorted({str(b) for b in bank_ids or [] if str(b).startswith(THREAD_BANK_PREFIX)})


async def _owned_session_banks(
    plugin: Any, session_banks: list[str], authorization: str
) -> list[str]:
    """会话 bank 归属判定：经 db-read sessions 面（租户内建收窄）过滤。

    归属判定本体在内核：SELECT 交收窄包装强制追加
    ``WHERE tenant_id = <身份链租户>``——查回来的 thread_id 即调用方租户的
    会话（一用户一租户，tenant_id = user_id）。无凭证（内核复权 401）/
    查询失败 → 空集（fail-closed，宁可少显示不可跨户）。
    """
    if not session_banks:
        return []
    if not authorization:
        logger.warning(
            "memory 会话归属查询缺 Authorization 凭证，会话 bank fail-closed 不可见"
        )
        return []
    owned: set[str] = set()
    for start in range(0, len(session_banks), _DB_READ_CHUNK):
        chunk = session_banks[start : start + _DB_READ_CHUNK]
        bind = {f"b{i}": b for i, b in enumerate(chunk)}
        sql = (
            "SELECT thread_id, tenant_id FROM sessions WHERE thread_id IN ("
            + ", ".join(f":b{i}" for i in range(len(chunk)))
            + ")"
        )
        try:
            rows = await db_read_query(plugin, _SESSIONS_FACE, sql, bind, authorization)
        except DbReadQueryError as exc:
            logger.warning(
                "memory 会话归属查询失败，会话 bank fail-closed 不可见 | status=%s", exc.status
            )
            return []
        for row in rows:
            if row:
                owned.add(str(row[0]))
    return [b for b in session_banks if b in owned]


async def resolve_caller_banks(
    plugin: Any, headers: dict[str, str] | None, backend: Any
) -> list[str]:
    """调用方可见的对话 bank 集合（fail-closed 归属过滤，X4-F1）。

    - 身份头缺失（非内核认证分发/匿名剥离面）→ 空集，绝不返回全量；
    - 会话 bank（thread-*）：db-read sessions 面按调用方租户收窄判定归属；
    - 默认 bank：仅 admin 种子租户（tenant_id="default"，与 bank id 同名）
      可见——恒查 default 的 M-9 问题按调用方身份收敛。

    Args:
        plugin: AgentOSPlugin 实例（db-read 能力句柄来源；能力缺失由
            db_read_query 归一为契约错误 → 会话 bank fail-closed 不可见）。
        headers: http.handle 收到的请求头（内核已注入权威身份头）。
        backend: IMemoryBackend（bank 发现通路；None → 无会话 bank）。

    Returns:
        调用方可见 bank id 列表（会话 bank 字典序 + 默认 bank 殿后）。
    """
    tenant_id = _header_value(headers, _TENANT_HEADER)
    user_id = _header_value(headers, _USER_HEADER)
    if not tenant_id or not user_id:
        logger.warning(
            "memory 请求缺身份头（x-agentos-tenant/x-agentos-user），fail-closed 空集"
        )
        return []
    owned = await _owned_session_banks(
        plugin, await _session_banks(backend), _header_value(headers, _AUTHORIZATION_HEADER)
    )
    if tenant_id == _DEFAULT_BANK_ID:
        owned.append(_DEFAULT_BANK_ID)
    return owned


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
    backend: Any, banks: list[str], memory_type: str | None, limit: int
) -> list[dict[str, Any]]:
    """经 documents/list 通路取回 banks 覆盖的文档（列表/统计的既定数据面）。

    recall 是带 query 的检索 API（空 query 服务端必拒），列表面必须走本通路，
    否则链路断裂。逐 bank 取回后按 created_at 降序合并（hindsight list 无全局
    排序语义，bank 名序迭代会把新记忆排进截断区），超 limit 截断（延续单
    bank 时代的列表面上限语义）。能力失败（RuntimeError）诚实上抛由
    http.handle 转 500。banks 由分发面经 :func:`resolve_caller_banks` 归属过滤
    后传入——本函数不扩大探查面。
    """
    docs: list[dict[str, Any]] = []
    for bank in banks:
        fetched = await backend.get_documents(
            user_id=bank,
            tags=_type_tag_filter(memory_type),
            tags_match="any_strict",
            limit=limit,
        )
        docs.extend(d for d in fetched or [] if isinstance(d, dict))
    # ISO-8601 UTC 时间戳字符串序＝时间序；缺失 created_at 稳定排尾
    docs.sort(key=lambda d: str(d.get("created_at", "") or ""), reverse=True)
    return docs[:limit]


async def list_memories(
    banks: list[str],
    memory_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """获取记忆条目列表。

    支持按记忆类型筛选，经 documents/list 通路返回（后端无 offset 语义）。

    Args:
        banks: 调用方可见 bank 集合（resolve_caller_banks 归属过滤产物）。
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
    docs = await _list_bank_documents(backend, banks, memory_type, limit)
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


async def _search_across_banks(
    backend: Any, banks: list[str], query: str, top_k: int, memory_type: str | None
) -> list[dict[str, Any]]:
    """逐 bank 检索后按相关度合并（按 id 去重，score 降序截断 top_k）。

    与 server.py 召回归一面同款语义：跨 bank 按 id 去重（同一记忆单元只落
    一个 bank，防御合并重复）；排序按统一形态 score 降序，检索数量契约成立。
    """
    merged: dict[str, dict[str, Any]] = {}
    for bank in banks:
        results = await backend.search(
            query=query,
            user_id=bank,
            top_k=top_k,
            memory_type=memory_type,
        )
        for m in results or []:
            if isinstance(m, dict):
                merged.setdefault(str(m.get("id", "")), m)

    def _score(m: dict[str, Any]) -> float:
        raw = m.get("score", 0.0)
        return float(raw) if isinstance(raw, (int, float)) else 0.0

    items = sorted(merged.values(), key=_score, reverse=True)
    return items[: max(0, top_k)]


async def search_memories(
    banks: list[str],
    query: str,
    top_k: int = 5,
    method: str = "keyword",
) -> dict[str, Any]:
    """搜索记忆条目，返回按相关度排序的结果。

    method 为检索方法名；当它本身是记忆类型（episode/semantic/...）时作为
    memory_type 过滤透传，否则（keyword/vector/tagwave）不限定类型。
    检索面 = 调用方可见 bank 集合（归属过滤产物，见模块 docstring）。

    Returns:
        {items, total}
    """
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    results = await _search_across_banks(
        backend, banks, query, top_k, _method_to_memory_type(method)
    )
    items = [_memory_to_response(m) for m in results]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 情景记忆端点
# ---------------------------------------------------------------------------


async def list_episodes(banks: list[str], page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """获取情景记忆列表（documents/list 通路，按 type:episode 服务端过滤）。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    docs = await _list_bank_documents(backend, banks, "episode", page_size)
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


async def get_episode(episode_id: str, banks: list[str]) -> dict[str, Any]:
    """获取单个情景记忆（documents 直查通路，document_id 精确取回）。

    不经 recall：sidecar 对空 query 直接拒错，且"拉 top N 线性过滤 id"在第
    N 条之外存在伪 404——直查按 id 定位，与列表面同数据源、无截断。
    探查面 = 调用方可见 bank 集合（归属过滤产物），集合外 id 一律 404。
    """
    backend = _get_memory_backend()
    if backend is not None:
        for bank in banks:
            docs = await backend.get_documents(
                user_id=bank,
                document_id=episode_id,
                limit=1,
            )
            for d in docs:
                if str(d.get("id", "")) == episode_id:
                    m = _document_to_memory(d)
                    return {
                        "id": m["id"],
                        "intent_text": m["content"],
                        "tags": m["tags"],
                        "created_at": m["created_at"],
                    }
    raise MemoryAPIError(404, "MEM_NOTF_5001", "未找到相关记忆")


# ---------------------------------------------------------------------------
# 语义记忆端点
# ---------------------------------------------------------------------------


async def list_semantic(banks: list[str]) -> dict[str, Any]:
    """获取语义记忆列表（documents/list 通路，按 type:semantic 服务端过滤）。"""
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    docs = await _list_bank_documents(backend, banks, "semantic", _LISTING_LIMIT_CAP)
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


async def get_memory_stats(banks: list[str]) -> dict[str, Any]:
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
        await _list_bank_documents(backend, banks, "episode", _LISTING_LIMIT_CAP)
    )
    semantic_count = len(
        await _list_bank_documents(backend, banks, "semantic", _LISTING_LIMIT_CAP)
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


async def search_memories_post(banks: list[str], body: dict[str, Any] | None = None) -> dict[str, Any]:
    """搜索记忆条目（POST 方式，body: {query, top_k}）。"""
    if body is None:
        return {"items": [], "total": 0}
    backend = _get_memory_backend()
    if backend is None:
        return {"items": [], "total": 0}
    query = body.get("query", "")
    top_k = int(body.get("top_k", 5))
    results = await _search_across_banks(backend, banks, query, top_k, None)
    items = [_memory_to_response(m) for m in results]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 动态路径端点 — 必须放在所有固定路径之后，否则 /stats、/semantic 等会被 {memory_id} 捕获
# （server.py 分发按固定路径先行匹配，此处仅保留函数本身）
# ---------------------------------------------------------------------------


async def get_memory(memory_id: str, banks: list[str]) -> dict[str, Any]:
    """获取指定记忆条目的详情（documents 直查通路，document_id 精确取回）。

    逐调用方可见 bank（归属过滤产物，与列表面同集合）探测 document_id，
    记忆页列出的条目按 id 同样可查看——集合外 bank 的 id 一律 404
    （X4-F1：跨户读写删全通面收口，fail-closed）。

    content 取 documents 面原文 original_text（recall 返回的是抽取后事实）；
    直查无相关性排序语义，score 恒 0（不伪造评分）。

    Args:
        memory_id: 记忆 ID
        banks: 调用方可见 bank 集合（resolve_caller_banks 归属过滤产物）

    Returns:
        MemoryResponse dict 记忆详情

    Raises:
        MemoryAPIError: 记忆不存在 (404)
    """
    backend = _get_memory_backend()
    if backend is not None:
        for bank in banks:
            docs = await backend.get_documents(
                user_id=bank,
                document_id=memory_id,
                limit=1,
            )
            for d in docs:
                if str(d.get("id", "")) == memory_id:
                    m = _document_to_memory(d)
                    return {
                        "id": m["id"],
                        "content": m["content"],
                        "memory_type": m["memory_type"],
                        "tags": m["tags"],
                        "score": m["score"],
                        "created_at": m["created_at"],
                    }
    raise MemoryAPIError(404, "MEM_NOTF_5001", "未找到相关记忆")


async def delete_memory(memory_id: str, banks: list[str]) -> dict[str, str]:
    """删除指定记忆条目（可见 bank 内定位，语义与 get_memory 同）。

    逐调用方可见 bank 调 delete 直至命中（backend.delete 兼容文档 id 与
    记忆单元 id 的回退解析，bank 探测序与列表面一致）；集合外 bank 的 id
    一律 404（X4-F1：删除面同刀收口）。

    Args:
        memory_id: 记忆 ID
        banks: 调用方可见 bank 集合（resolve_caller_banks 归属过滤产物）

    Returns:
        删除成功消息

    Raises:
        MemoryAPIError: 记忆不存在 (404)
    """
    backend = _get_memory_backend()
    if backend is not None:
        for bank in banks:
            deleted = bool(
                await backend.delete(
                    user_id=bank,
                    memory_id=memory_id,
                )
            )
            if deleted:
                return {"message": "记忆已删除"}
    raise MemoryAPIError(404, "MEM_NOTF_5001", "未找到相关记忆")
