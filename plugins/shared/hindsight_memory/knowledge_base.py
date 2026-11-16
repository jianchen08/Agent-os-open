"""知识库服务（knowledge-base 域）——bank 唯一存储 + 租户分 bank（X10-F1）。

    上传（multipart）→ 切块（~2000 字符/块，朴素滑窗）→ aretain 逐块写记忆
    bank（kb bank，独立于记忆 "default" bank；**向量库=唯一存储**：无目录、
    无副本、无对账、不进消息面、不经内核）→ 中途失败如实报错并尽力清理已写
    chunk → 列表/详情/统计/标签从 bank documents 面派生（分页拉全 +
    document_metadata.kb_* 聚合）→ 检索（arecall include_chunks 原文，
    chunk_id=document_id 逐条解析归属上下文）→ 删除（按 kb_item 标签删 bank，
    主路径；定位 0 文档=404）

租户归属（X10-F1，复用 X4-F1/X6-F1 身份过滤模式）：bank 按租户划分——
default 种子租户沿用存量 bank ``kb``（旧 kb bank 即 default 租户的 bank，
存量文档零迁移），其余租户 ``kb_<tenant>`` 从空开始；租户内共享（读/删），
跨租户互不可见。身份取内核 dispatcher 权威注入的 ``x-agentos-tenant`` /
``x-agentos-user`` / ``x-agentos-role`` 头（匿名剥离，可见即可信）：读面
缺头 fail-closed 空集/404；写面缺头 403、跨租户 DELETE 403（真实状态到
前端）；admin 角色跨租户可见可操作（运维面）；上传落章属主
（kb_owner/kb_tenant 进 chunk metadata）。

- kb_meta.json / uploads/kb 源文件 / categories 已删除（bank 承载不了的功能
  不保留；删除无残留账本可失配，孤儿问题从根上消失）。条目聚合元数据随
  chunk 落 bank（metadata: kb_id/kb_name/kb_size/kb_mime/kb_chunk_total/
  created_at；tags: type:knowledge + kb_item:{id} + filetype:*）。
- 故障语义（与压缩块路径不同）：读面 client 缺席降级空结果（check 如实报告），
  bank 调用失败诚实上抛；写面（上传/删除）bank 故障=如实失败（唯一存储，
  无兜底）。
- 环规：所有 client 调用均为 async，且必须在 MCP 事件循环内 await（aiohttp
  会话 loop 绑定）——server.py http.handle 直接 await。

条目聚合形态（_item_public，前端 KnowledgeBasePage 字段对齐；categories 键
保留恒空数组——响应形态稳定，功能已删）::

    {id, name, size, mime_type, categories: [], tags,
     chunk_count, created_at, updated_at}
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Any

# 共享层时间戳公共模块（plugins/shared 平铺，stdlib-only）裸名导入
# （先例：task_submit/tool.py 的 _SHARED_ROOT 注入）。时间戳是持久化数据
# 格式契约，属模块级依赖而非现场懒加载。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from _root_api import iso_sort_key, now_iso_utc as _now_iso  # noqa: E402

logger = logging.getLogger(__name__)

# ── 常量 ───────────────────────────────────────────────────────────────────
# 知识块 bank（独立 bank，与记忆 "default" 隔离）——default 种子租户的 bank；
# 其余租户 kb_<tenant>（_kb_bank_for_tenant）。存量旧 kb bank 即 default 租户
# 的 bank，零数据迁移。
_KB_BANK_ID = "kb"

# 内核 dispatcher 权威注入的身份头（匿名请求剥离伪造头，handler 可见即可信）
_TENANT_HEADER = "x-agentos-tenant"
_USER_HEADER = "x-agentos-user"
_ROLE_HEADER = "x-agentos-role"

# admin 种子租户（一用户一租户模型；tenant_id="default" ↔ bank "kb"）
_DEFAULT_TENANT = "default"

# 写面越权/无身份统一文案（403）
_FORBIDDEN_MSG = "无权操作该知识条目（非上传租户且非 admin 角色）"

# 知识块切块大小（字符，与 server.py _CHUNK_SIZE 同款朴素滑窗）
_KB_CHUNK_SIZE = 2000

# 允许入库的文档扩展名（无外部解析依赖，纯文本直读）
_KB_ALLOWED_EXTS = (".txt", ".md")

# 单文件大小上限（10MB，防误拖大文件打爆切块/向量化）
_KB_MAX_FILE_BYTES = 10 * 1024 * 1024

# 检索默认/上限
_KB_SEARCH_DEFAULT_TOP_K = 10
_KB_SEARCH_MAX_TOP_K = 50

# bank 列举分页（list_documents 单页上限=documents 面 schema maximum）与页数保护
_KB_LIST_PAGE_SIZE = 100
_KB_LIST_MAX_PAGES = 50

# 知识块 tag（aretain 时写入；列表/删除/检索全按 tags 定位）
_TAG_TYPE = "type:knowledge"
_TAG_ITEM_PREFIX = "kb_item:"

# 内部 tag 前缀（对外 tags 面剔除，不外泄实现细节）
_INTERNAL_TAG_PREFIXES = ("type:", "kb_item:")


class KBError(Exception):
    """知识库业务异常（server.py 捕获转 HTTP 状态）。"""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


# ── 运行时状态 ─────────────────────────────────────────────────────────────
# hindsight HTTP client（由 server.py http.handle 分发时注入；None = 未初始化，
# 读面降级空结果，写面如实失败）
_client: Any = None


def set_client(client: Any | None) -> None:
    """注入 hindsight HTTP client 引用（server.py 分发时调用；测试直接传 mock）。"""
    global _client
    _client = client


def _require_client() -> Any:
    """返回 hindsight client；未初始化时抛降级异常（写面如实失败：唯一存储不可达）。"""
    if _client is None:
        raise KBError(503, "KNB_INIT_5001", "知识库后端未初始化（hindsight not initialized）")
    return _client


# ── 身份与租户归属（X10-F1 跨租户收口，复用 X4-F1/X6-F1 模式）──────────────


def _header_value(headers: dict[str, str] | None, name: str) -> str:
    """取入站请求头值（内核 header key 已小写，大小写防御）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == name and v:
            return str(v)
    return ""


def _caller_identity(headers: dict[str, str] | None) -> tuple[str, str, str]:
    """(tenant_id, user_id, role)；缺身份头 → 空串（fail-closed：无身份即无归属）。"""
    return (
        _header_value(headers, _TENANT_HEADER),
        _header_value(headers, _USER_HEADER),
        _header_value(headers, _ROLE_HEADER),
    )


def _kb_bank_for_tenant(tenant_id: str) -> str:
    """租户 → kb bank id（X10-F1 租户分 bank）。

    default 种子租户沿用存量 bank ``kb``（旧 kb bank 即 default 租户的 bank，
    存量文档零迁移）；其余租户 ``kb_<tenant>`` 从空开始。
    """
    return _KB_BANK_ID if tenant_id == _DEFAULT_TENANT else f"kb_{tenant_id}"


async def _discover_kb_banks() -> list[str]:
    """实例内 kb 域 bank id 枚举（banks.list_banks 过滤 ``kb``/``kb_*``）。

    client 缺能力/发现失败 → 空集（跨 bank 面 fail-closed 只收窄：调用方
    仍恒见自身租户 bank，仅失去 admin 全域视野与 403/404 精判）。
    """
    if _client is None:
        return []
    list_banks = getattr(getattr(_client, "banks", None), "list_banks", None)
    if not callable(list_banks):
        return []
    try:
        listing = await list_banks()
    except Exception as exc:  # noqa: BLE001 — 发现失败收窄（warn 留痕）
        logger.warning("[kb] bank 发现失败，跨 bank 面 fail-closed | error=%s", exc)
        return []
    if hasattr(listing, "model_dump"):
        listing = listing.model_dump()
    items = listing.get("banks") if isinstance(listing, dict) else getattr(listing, "banks", None)
    ids: list[str] = []
    for item in items or []:
        bid = (
            str(item.get("bank_id", "") or "")
            if isinstance(item, dict)
            else str(getattr(item, "bank_id", "") or "")
        )
        if bid:
            ids.append(bid)
    return sorted(b for b in ids if b == _KB_BANK_ID or b.startswith("kb_"))


async def _caller_banks(headers: dict[str, str] | None) -> list[str]:
    """调用方可见的 kb bank 集合（fail-closed 归属过滤，X10-F1）。

    - 身份头缺失（非内核认证分发/匿名剥离面）→ 空集，绝不返回全量；
    - 非 admin：恰自身租户 bank（租户内共享、跨租户隔离）；
    - admin 角色：实例 kb 域全 bank ∪ 自身 bank（运维面；发现失败降级自身
      bank，只收窄不放大）。
    """
    tenant_id, user_id, role = _caller_identity(headers)
    if not tenant_id or not user_id:
        logger.warning(
            "[kb] 请求缺身份头（x-agentos-tenant/x-agentos-user），fail-closed 空集"
        )
        return []
    own = _kb_bank_for_tenant(tenant_id)
    if role != "admin":
        return [own]
    discovered = await _discover_kb_banks()
    return sorted({*discovered, own})


# ── bank 文档面（唯一数据通道）──────────────────────────────────────────────


def _to_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj if isinstance(obj, dict) else {}


def _public_tags(tags: Any) -> list[str]:
    """对外 tags：剔除内部前缀（type:/kb_item:），保持次序去空。"""
    raw = tags if isinstance(tags, list) else []
    return [str(t) for t in raw if t and not str(t).startswith(_INTERNAL_TAG_PREFIXES)]


async def _bank_documents(banks: list[str]) -> list[dict[str, Any]]:
    """可见 bank 集合的全量文档（type:knowledge 标签过滤，updated_at DESC 分页拉全）。

    多 bank（admin 运维面）逐 bank 分页拉取合并；banks 空（fail-closed 匿名）
    → 空集。页数上限保护：单 bank 超大库截断并 warn（读面诚实留痕；条目聚合
    只依赖 document_metadata，页界切割不产生错值）。
    """
    if _client is None or not banks:
        return []
    docs: list[dict[str, Any]] = []
    truncated = False
    for bank_id in banks:
        bank_docs: list[dict[str, Any]] = []
        for _page in range(_KB_LIST_MAX_PAGES):
            listing = await _client.documents.list_documents(
                bank_id=bank_id,
                tags=[_TAG_TYPE],
                tags_match="any_strict",
                limit=_KB_LIST_PAGE_SIZE,
                offset=len(bank_docs),
            )
            page = [_to_dict(d) for d in (getattr(listing, "items", None) or [])]
            page = [d for d in page if d]
            if not page:
                break
            bank_docs.extend(page)
            if len(bank_docs) >= int(getattr(listing, "total", 0) or 0):
                break
        if len(bank_docs) >= _KB_LIST_MAX_PAGES * _KB_LIST_PAGE_SIZE:
            truncated = True
        docs.extend(bank_docs)
    if truncated:
        logger.warning(
            "[kb] 文档分页达上限（列表截断）banks=%d docs=%d",
            len(banks), len(docs),
        )
    return docs


def _aggregate_items(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """bank 文档 → 条目聚合（document_metadata.kb_* 分组；created_at 倒序）。"""
    items: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}
    for doc in docs:
        meta = doc.get("document_metadata") or {}
        kb_id = str(meta.get("kb_id", "") or "")
        if not kb_id:
            continue
        seen[kb_id] = seen.get(kb_id, 0) + 1
        if kb_id in items:
            continue
        created = str(meta.get("created_at", "") or doc.get("created_at", "") or "")
        items[kb_id] = {
            "id": kb_id,
            "name": str(meta.get("kb_name", "") or ""),
            "size": int(meta.get("kb_size", 0) or 0),
            "mime_type": str(meta.get("kb_mime", "") or ""),
            "categories": [],
            "tags": _public_tags(doc.get("tags")),
            "chunk_count": int(meta.get("kb_chunk_total", 0) or 0) or seen[kb_id],
            "created_at": created,
            "updated_at": created,
        }
    # kb_chunk_total 缺失的条目以实际见到的块数兜底（分页截断面诚实计数）
    for kb_id, item in items.items():
        if not item["chunk_count"]:
            item["chunk_count"] = seen[kb_id]
    return sorted(items.values(), key=lambda it: iso_sort_key(str(it["created_at"])), reverse=True)


def _chunk_text(text: str, chunk_size: int = _KB_CHUNK_SIZE) -> list[str]:
    """按字符数朴素滑窗切块——本插件切块算式的单一实现（server 模块转发到此）。"""
    if not text:
        return []
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


# ── 业务函数（bank 派生读面）────────────────────────────────────────────────


async def list_items(headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """知识库条目列表（bank 聚合派生，按创建时间倒序，前端页面直接消费数组）。

    可见面 = 调用方可见 bank 集合（X10-F1：缺身份头空集，租户分 bank）。
    """
    return _aggregate_items(await _bank_documents(await _caller_banks(headers)))


async def get_stats(headers: dict[str, str] | None = None) -> dict[str, Any]:
    """知识库统计：{total, categories_count, tags_count, total_chunks, total_size}。

    categories 功能已随目录删除，键保留恒 0（响应形态稳定）；统计面只覆盖
    调用方可见 bank 集合。
    """
    items = _aggregate_items(await _bank_documents(await _caller_banks(headers)))
    tags = {t for it in items for t in it["tags"]}
    return {
        "total": len(items),
        "categories_count": 0,
        "tags_count": len(tags),
        "total_chunks": sum(int(it["chunk_count"]) for it in items),
        "total_size": sum(int(it["size"]) for it in items),
    }


async def list_tags(headers: dict[str, str] | None = None) -> list[str]:
    """标签列表（可见条目去重，字典序；内部前缀已剔除）。"""
    tags = {t for it in _aggregate_items(await _bank_documents(await _caller_banks(headers))) for t in it["tags"]}
    return sorted(tags)


async def get_item(item_id: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """条目详情（聚合形态；chunk 正文经检索端点按相关度取回，不整篇返回）。

    可见集合外 by-id → 404（不泄露存在性，X6-F1 读面同款）。
    """
    for it in _aggregate_items(await _bank_documents(await _caller_banks(headers))):
        if it["id"] == item_id:
            return it
    raise KBError(404, "KNB_NOTF_5002", "未找到知识库条目")


async def _bank_has_item(client: Any, bank_id: str, item_id: str) -> bool:
    """条目是否落在指定 bank（type:knowledge + kb_item 标签，limit=1 探测）。

    探测故障按「不在」处理（403/404 精判退化为 404，只收窄不放大）。
    """
    try:
        listing = await client.documents.list_documents(
            bank_id=bank_id,
            tags=[_TAG_TYPE, f"{_TAG_ITEM_PREFIX}{item_id}"],
            tags_match="all",
            limit=1,
            offset=0,
        )
    except Exception as exc:  # noqa: BLE001 — 探测失败不放大可见面
        logger.warning("[kb] 条目归属探测失败 bank=%s | error=%s", bank_id, exc)
        return False
    return bool(getattr(listing, "items", None) or [])


async def _delete_item_in_bank(client: Any, bank_id: str, item_id: str) -> bool:
    """在指定 bank 删除条目全部 chunk；定位 0 文档返回 False（每轮列出即删）。

    删除失败如实 500（已删的不回，重试幂等——剩余部分继续删）。
    """
    found = False
    while True:
        listing = await client.documents.list_documents(
            bank_id=bank_id,
            tags=[_TAG_TYPE, f"{_TAG_ITEM_PREFIX}{item_id}"],
            tags_match="all",
            limit=_KB_LIST_PAGE_SIZE,
            offset=0,
        )
        docs = [_to_dict(d) for d in (getattr(listing, "items", None) or [])]
        docs = [d for d in docs if d]
        if not docs:
            break
        found = True
        for doc in docs:
            doc_id = str(doc.get("id", "") or "")
            if not doc_id:
                continue
            try:
                await client.documents.delete_document(bank_id=bank_id, document_id=doc_id)
            except Exception as exc:  # noqa: BLE001 — 删除失败如实上抛（唯一存储面）
                raise KBError(500, "KNB_DEL_8002", f"知识库条目删除失败: {exc}") from exc
    return found


async def delete_item(item_id: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """删除条目：按 ``type:knowledge + kb_item:{id}`` 标签删 bank（主路径）。

    授权（X10-F1，写面真实状态到前端）：缺身份头 → 403；非 admin 且条目落
    在他租户 bank → 403（同租户共享删除）；admin 跨租户运维删除放行；
    全域定位 0 文档 → 404。
    """
    tenant_id, user_id, role = _caller_identity(headers)
    if not tenant_id or not user_id:
        raise KBError(403, "KNB_FORB_7006", "知识库删除缺身份头（fail-closed）")
    client = _require_client()
    banks = await _caller_banks(headers)
    found = False
    if role != "admin":
        own = _kb_bank_for_tenant(tenant_id)
        foreign = [
            b for b in await _discover_kb_banks()
            if b != own and await _bank_has_item(client, b, item_id)
        ]
        if foreign:
            raise KBError(403, "KNB_FORB_7006", _FORBIDDEN_MSG)
        found = await _delete_item_in_bank(client, own, item_id)
    else:
        for bank in banks:
            if await _delete_item_in_bank(client, bank, item_id):
                found = True
    if not found:
        raise KBError(404, "KNB_NOTF_5002", "未找到知识库条目")
    logger.info("[kb] 条目已删除 id=%s tenant=%s role=%s", item_id, tenant_id, role or "user")
    return {"message": "条目已删除", "id": item_id}


# ── 上传链路（bank 唯一写入面）──────────────────────────────────────────────


async def upload_document(
    filename: str,
    content: bytes,
    mime_type: str = "application/octet-stream",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """上传文档：切块 → aretain 逐块写调用方租户 bank（唯一存储，X10-F1）。

    中途失败：上传如实失败（500）+ 已写 chunk 尽力清理（清理失败仅告警——
    残留 chunk 会如实出现在列表面，无账本可失配）。

    Args:
        filename: 原始文件名
        content: 文件字节
        mime_type: 文件 MIME
        headers: 入站请求头（内核权威身份头；缺失 → 403 不落写）

    Returns:
        {item_id, file_id, filename, size, chunks_imported, name, message}

    Raises:
        KBError: 扩展名不支持 / 文件过大 / 文本解码为空 / 缺身份头(403) /
            client 未初始化(503) / bank 入库失败(500)
    """
    tenant_id, user_id, _role = _caller_identity(headers)
    if not tenant_id or not user_id:
        raise KBError(403, "KNB_FORB_7006", "知识库上传缺身份头（fail-closed，不落写）")
    bank_id = _kb_bank_for_tenant(tenant_id)
    client = _require_client()
    ext = os.path.splitext(filename)[1].lower()  # noqa: PTH122 —— 与仓库 os.* 风格一致
    if ext not in _KB_ALLOWED_EXTS:
        raise KBError(
            400,
            "KNB_VAL_7002",
            f"不支持的文件类型: {ext or '(none)'}。仅支持 {list(_KB_ALLOWED_EXTS)}",
        )
    if len(content) > _KB_MAX_FILE_BYTES:
        raise KBError(400, "KNB_VAL_7003", f"文件过大（上限 {_KB_MAX_FILE_BYTES} 字节）")
    if not content:
        raise KBError(400, "KNB_VAL_7004", "文件内容为空")

    # 解码文本（utf-8 容错；解码后为空 → 拒绝入库）
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        raise KBError(400, "KNB_VAL_7004", "文件内容为空（无法提取文本）")

    chunks = _chunk_text(text)
    if not chunks:
        raise KBError(400, "KNB_VAL_7004", "文件内容为空")

    item_id = uuid.uuid4().hex[:12]
    created_at = _now_iso()
    tags = [_TAG_TYPE, f"{_TAG_ITEM_PREFIX}{item_id}", _auto_tag_from_ext(ext)]

    # 切块 → 向量化入库（bank=唯一存储）。在 MCP 事件循环内 await（aiohttp
    # 会话 loop 绑定，禁止另起循环）。
    chunk_ids: list[str] = []
    try:
        await _ensure_bank(bank_id)
        for idx, chunk in enumerate(chunks):
            chunk_meta = {
                "memory_type": "semantic",
                "kb_id": item_id,
                "kb_name": filename,
                "kb_size": str(len(content)),
                "kb_mime": mime_type,
                "kb_chunk_index": str(idx),
                "kb_chunk_total": str(len(chunks)),
                "kb_owner": user_id,
                "kb_tenant": tenant_id,
                "source": "knowledge_base",
                "created_at": created_at,
            }
            result = await client.aretain(
                bank_id=bank_id,
                content=chunk,
                metadata=chunk_meta,
                tags=tags,
            )
            cid = str(getattr(result, "operation_id", "") or "")
            if cid:
                chunk_ids.append(cid)
    except Exception as exc:  # noqa: BLE001 — bank 故障=上传如实失败（唯一存储无兜底）
        await _cleanup_chunks(client, chunk_ids, bank_id)
        raise KBError(500, "KNB_ING_8001", f"知识块入库失败: {exc}") from exc

    logger.info("[kb] 上传入库成功 item_id=%s filename=%s chunks=%d", item_id, filename, len(chunks))
    return {
        "item_id": item_id,
        "file_id": item_id,
        "filename": filename,
        "name": filename,
        "size": len(content),
        "chunks_imported": len(chunks),
        "message": "文件上传成功",
    }


async def _cleanup_chunks(client: Any, chunk_ids: list[str], bank_id: str) -> None:
    """失败清理：已写 chunk 尽力删除（失败只告警——残留会如实出现在列表）。"""
    for cid in chunk_ids:
        try:
            await client.documents.delete_document(bank_id=bank_id, document_id=cid)
        except Exception as exc:  # noqa: BLE001 — 清理失败留痕，不掩盖原始失败
            logger.warning("[kb] 失败清理 chunk 未竟 id=%s | %s", cid, exc)


async def _ensure_bank(bank_id: str = _KB_BANK_ID) -> None:
    """确保 kb bank 存在（acreate_bank 幂等；X10-F1 起按租户 bank 调用）。"""
    if _client is None:
        raise KBError(503, "KNB_INIT_5001", "知识库后端未初始化（hindsight not initialized）")
    create = getattr(_client, "acreate_bank", None)
    if callable(create):
        await create(bank_id=bank_id)


def _auto_tag_from_ext(ext: str) -> str:
    """自动标签：文件扩展名（如 ".md" → "filetype:md"，随 chunk 落 bank tags）。"""
    return f"filetype:{ext.lstrip('.')}" if ext else "filetype:unknown"


# ── 检索（bank 语义召回）────────────────────────────────────────────────────


async def _chunk_context(chunk_id: str, bank_id: str) -> dict[str, Any]:
    """chunk（=document）归属上下文：item_id/name/tags/chunk_count。

    解析失败（已删残留/瞬时故障）→ 空上下文（内容照回显，不炸）。
    """
    try:
        doc = _to_dict(await _client.documents.get_document(bank_id=bank_id, document_id=chunk_id))
    except Exception:  # noqa: BLE001 — 归属缺失只降级上下文字段
        return {"item_id": "", "name": "", "tags": [], "chunk_count": 0}
    meta = doc.get("document_metadata") or {}
    return {
        "item_id": str(meta.get("kb_id", "") or ""),
        "name": str(meta.get("kb_name", "") or ""),
        "tags": _public_tags(doc.get("tags")),
        "chunk_count": int(meta.get("kb_chunk_total", 0) or 0),
    }


async def search(
    query: str,
    top_k: int = _KB_SEARCH_DEFAULT_TOP_K,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """知识库检索：hindsight arecall（语义向量 + 关键词，pgvector embedded）。

    - type:knowledge 服务端 tag 过滤；include_chunks=True 取原文。
    - 可见面 = 调用方可见 bank 集合（X10-F1：缺身份头空集不触 bank；admin
      跨 bank 合并按 score 降序截断，X4-F1 归一语义）。
    - 归属上下文按 chunk_id（=document id）逐条解析；解析失败留空不炸
      （bank 唯一存储，残留/异源 chunk 照实回显）。

    Returns:
        {results: [{item_id, name, categories, tags, chunk_count, content, score}], total}
        （sidecar 缺席/缺身份/recall 故障 → {results: [], total: 0, error} 同形降级）
    """
    if not query or not query.strip():
        raise KBError(400, "KNB_VAL_7005", "检索词不能为空")
    top_k = max(1, min(_KB_SEARCH_MAX_TOP_K, int(top_k or _KB_SEARCH_DEFAULT_TOP_K)))
    banks = await _caller_banks(headers)
    if _client is None:
        return {"results": [], "total": 0, "error": "知识库检索后端未初始化（hindsight not initialized）"}
    if not banks:
        return {"results": [], "total": 0, "error": "知识库检索缺身份头（fail-closed 空集）"}

    raw: list[tuple[str, float, str, dict[str, Any] | None, str]] = []
    try:
        for bank_id in banks:
            response = await _client.arecall(
                bank_id=bank_id,
                query=query,
                include_chunks=True,
                tags=[_TAG_TYPE],
                tags_match="all",
            )
            # 原文优先（recall chunks，include_chunks=True），事实结果兜底
            chunks_collection = getattr(response, "chunks", None) or []
            if isinstance(chunks_collection, list) and chunks_collection:
                for chunk in chunks_collection:
                    d = _to_dict(chunk)
                    text = d.get("text") or d.get("content") or ""
                    if not text:
                        continue
                    raw.append((str(d.get("id", "")), float(d.get("score", 0.0) or 0.0), text, None, bank_id))
            else:
                results: list[Any] = []
                for attr in ("results", "memories"):
                    collection = getattr(response, attr, None)
                    if isinstance(collection, list):
                        results = collection
                        break
                for item in results:
                    d = _to_dict(item)
                    text = d.get("text") or d.get("content") or ""
                    if not text:
                        continue
                    raw.append((str(d.get("document_id", "") or d.get("id", "")),
                                float(d.get("score", 0.0) or 0.0), text, d, bank_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kb] recall 调用失败降级 | error=%s", exc)
        return {"results": [], "total": 0, "error": str(exc)}

    # 跨 bank 合并按 score 降序截断（单 bank 稳定排序不改变召回次序）
    raw.sort(key=lambda t: t[1], reverse=True)

    out: list[dict[str, Any]] = []
    contexts: dict[tuple[str, str], dict[str, Any]] = {}
    for chunk_id, score, text, result_doc, bank_id in raw[:top_k]:
        context: dict[str, Any]
        if result_doc is not None:
            # results 事实面自带归属（metadata/tags），无需再查 documents 面
            meta = result_doc.get("metadata") or {}
            context = {
                "item_id": str(meta.get("kb_id", "") or ""),
                "name": str(meta.get("kb_name", "") or ""),
                "tags": _public_tags(result_doc.get("tags")),
                "chunk_count": 0,
            }
        else:
            if (chunk_id, bank_id) not in contexts:
                contexts[(chunk_id, bank_id)] = await _chunk_context(chunk_id, bank_id)
            context = contexts[(chunk_id, bank_id)]
        out.append(
            {
                "item_id": context["item_id"],
                "name": context["name"],
                "categories": [],
                "tags": list(context["tags"]),
                "chunk_count": int(context["chunk_count"] or 0),
                "content": text,
                "score": score,
            }
        )

    return {"results": out, "total": len(out)}


async def check_available(headers: dict[str, str] | None = None) -> dict[str, Any]:
    """知识库检索面可用性检查（降级端点，不抛错）。

    报告调用方租户 bank（X10-F1）；缺身份头如实报告不可用（fail-closed，
    不预置可用面——匿名可用的可用性报告与写面 403 互相矛盾）。
    """
    tenant_id, user_id, _role = _caller_identity(headers)
    bank = _kb_bank_for_tenant(tenant_id) if tenant_id and user_id else ""
    if not bank:
        return {
            "available": False,
            "message": "知识库请求缺身份头（x-agentos-tenant/x-agentos-user，fail-closed）",
            "backend": "hindsight",
            "bank": "",
        }
    if _client is None:
        return {
            "available": False,
            "message": "知识库服务未初始化（hindsight not initialized）",
            "backend": "hindsight",
            "bank": bank,
        }
    try:
        await _ensure_bank(bank)
        return {
            "available": True,
            "message": "知识库服务可用（hindsight + pgvector embedded）",
            "backend": "hindsight",
            "bank": bank,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "message": f"知识库服务异常: {exc}",
            "backend": "hindsight",
            "bank": bank,
        }
