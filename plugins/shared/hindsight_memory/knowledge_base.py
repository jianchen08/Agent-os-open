"""知识库服务（knowledge-base 域）——bank 唯一存储 + 租户分 bank（X10-F1）。

    上传（multipart）→ 整文档单次 aretain 写记忆 bank（**客户端零切块**，
    条目边界交上游 hindsight 原生摄取管线：递归分隔符切块 `\n\n` 段落优先 +
    delta retain 增量；ADR 2026-11-19-KB原生摄取管线改造）→ 列表/详情/统计/
    标签从 bank documents 面派生（分页拉全 + document_metadata.kb_* 聚合）→
    检索（arecall 原文，results 事实面携带 kb_* 归属）→ 删除（按 kb_item
    标签双 bank 删除，主路径；定位 0 文档=404）

**双 bank 模式分治（用户裁定 2026-11-19）**：主 bank（``kb``/``kb_<tenant>``）
bank 级配置 `retain_extraction_mode=verbatim`（LLM 只抽元数据标签，原文原样
存）+ `entity_labels` 受控词表；降级 bank（``kb_chunks``/``kb_chunks_<tenant>``）
chunks 模式（零 LLM 原文直存）。verbatim 落库失败（LLM provider 未配置 /
bank 解析模式被上游强制非 verbatim / aretain 调用失败）→ 降级落 kb_chunks
bank，响应字段（ingestion_mode/bank_id/degraded/degradation_reason）+ 告警
日志**如实标注实际落库模式**，禁静默降级。recall/list 删双 bank 扇出，检索
面统一。

**标题双通道硬约束（ADR 决策5）**：上传把文档标题双通道传入摄取——①内容
头部前置标题行 ``# {title}``（首 chunk 向量化输入=fact_text，标题随文入向量）
②aretain context 字段=标题（每条 fact 记录携带归属 + verbatim 抽取获得文档
上下文）。向量库=唯一存储：无目录、无副本、无对账、不进消息面、不经内核。

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
# 的 bank，零数据迁移。主 bank bank 级配置 verbatim 抽取模式（LLM 只抽元数据
# 标签、原文原样存；ADR 2026-11-19 用户裁定主模式）。
_KB_BANK_ID = "kb"

# 降级 bank（chunks 模式，零 LLM 原文直存）：verbatim 落库失败时的降级位。
# default 种子租户 ↔ "kb_chunks"，其余租户 kb_chunks_<tenant>。
_KB_CHUNKS_BANK_ID = "kb_chunks"
_KB_CHUNKS_BANK_PREFIX = "kb_chunks"

# 降级 bank 前缀（admin bank 发现面区分主/降级；非 admin 恒双 bank 精确对）
_CHUNKS_BANK_PREFIX = "kb_chunks"

# 内核 dispatcher 权威注入的身份头（匿名请求剥离伪造头，handler 可见即可信）
_TENANT_HEADER = "x-agentos-tenant"
_USER_HEADER = "x-agentos-user"
_ROLE_HEADER = "x-agentos-role"

# admin 种子租户（一用户一租户模型；tenant_id="default" ↔ bank "kb"）
_DEFAULT_TENANT = "default"

# 写面越权/无身份统一文案（403）
_FORBIDDEN_MSG = "无权操作该知识条目（非上传租户且非 admin 角色）"

# sidecar LLM provider 注入的环境变量（server.py _apply_llm_env 写入；缺省或
# "none" = verbatim 抽取天然不可用，上游对 provider=none 全局强制 chunks 模式）
_LLM_PROVIDER_ENV = "HINDSIGHT_API_LLM_PROVIDER"

# verbatim 主 bank 的实体域受控词表（ADR 2026-11-19：健康/金钱/法律/安全/学习；
# entity_labels 为 bank 级可配清单字段，形态=上游 LabelGroup 列表，key:value
# 分类标签抽取对齐域词表）。消费方扩展词表 = 改此清单（bank config 幂等覆盖）。
_VERBATIM_ENTITY_LABELS: list[dict[str, Any]] = [
    {
        "key": "domain",
        "description": "知识条目内容域（kb bank verbatim 抽取受控词表）",
        "type": "multi-values",
        "values": [{"value": v} for v in ("健康", "金钱", "法律", "安全", "学习")],
    }
]

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
    """租户 → kb 主 bank id（X10-F1 租户分 bank）。

    default 种子租户沿用存量 bank ``kb``（旧 kb bank 即 default 租户的 bank，
    存量文档零迁移）；其余租户 ``kb_<tenant>`` 从空开始。
    """
    return _KB_BANK_ID if tenant_id == _DEFAULT_TENANT else f"kb_{tenant_id}"


def _kb_chunks_bank_for_tenant(tenant_id: str) -> str:
    """租户 → kb 降级 bank id（chunks 模式降级位，与主 bank 同租户划分）。"""
    if tenant_id == _DEFAULT_TENANT:
        return _KB_CHUNKS_BANK_ID
    return f"{_KB_CHUNKS_BANK_PREFIX}_{tenant_id}"


def _is_chunks_bank(bank_id: str) -> bool:
    """bank id 是否降级 bank（kb_chunks 前缀；主 bank 形如 kb / kb_<tenant>）。"""
    return bank_id.startswith(_CHUNKS_BANK_PREFIX)


def _bank_extraction_mode(bank_id: str) -> str:
    """bank → 摄取模式：主 bank verbatim（LLM 抽元数据、原文原样存）；
    降级 bank chunks（零 LLM 原文直存）。"""
    return "chunks" if _is_chunks_bank(bank_id) else "verbatim"


async def _discover_kb_banks() -> list[str]:
    """实例内 kb 域 bank id 枚举（banks.list_banks 过滤 ``kb``/``kb_*``，含降级 bank）。

    client 缺能力/发现失败 → 空集（跨 bank 面 fail-closed 只收窄：调用方
    仍恒见自身租户双 bank，仅失去 admin 全域视野与 403/404 精判）。
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
    """调用方可见的 kb bank 集合（fail-closed 归属过滤，X10-F1；主+降级双 bank）。

    - 身份头缺失（非内核认证分发/匿名剥离面）→ 空集，绝不返回全量；
    - 非 admin：恰自身租户主 bank + 降级 bank（租户内共享、跨租户隔离；
      verbatim 降级位落 kb_chunks bank，列表/检索/删除三面同权可见）；
    - admin 角色：实例 kb 域全 bank ∪ 自身双 bank（运维面；发现失败降级自身
      双 bank，只收窄不放大）。
    """
    tenant_id, user_id, role = _caller_identity(headers)
    if not tenant_id or not user_id:
        logger.warning(
            "[kb] 请求缺身份头（x-agentos-tenant/x-agentos-user），fail-closed 空集"
        )
        return []
    own = {_kb_bank_for_tenant(tenant_id), _kb_chunks_bank_for_tenant(tenant_id)}
    if role != "admin":
        return sorted(own)
    discovered = await _discover_kb_banks()
    return sorted({*discovered, *own})


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
    """bank 文档 → 条目聚合（document_metadata.kb_* 分组；created_at 倒序）。

    chunk_count 兜底链：kb_chunk_total 元数据（旧逐块形态）→ memory_unit_count
    （单文档形态，上游 chunk/单元数）→ 实见文档数。
    """
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
            "chunk_count": (
                int(meta.get("kb_chunk_total", 0) or 0)
                or int(doc.get("memory_unit_count", 0) or 0)
                or seen[kb_id]
            ),
            "created_at": created,
            "updated_at": created,
        }
    # kb_chunk_total 缺失的条目以实际见到的块数兜底（分页截断面诚实计数）
    for kb_id, item in items.items():
        if not item["chunk_count"]:
            item["chunk_count"] = seen[kb_id]
    return sorted(items.values(), key=lambda it: iso_sort_key(str(it["created_at"])), reverse=True)


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

    双 bank 覆盖：条目可能落主 bank（verbatim）或降级 bank（kb_chunks，verbatim
    失败降级形态），两者都删。授权（X10-F1，写面真实状态到前端）：缺身份头 →
    403；非 admin 且条目落在他租户 bank（含他租户降级 bank）→ 403（同租户共享
    删除）；admin 跨租户运维删除放行；全域定位 0 文档 → 404。
    """
    tenant_id, user_id, role = _caller_identity(headers)
    if not tenant_id or not user_id:
        raise KBError(403, "KNB_FORB_7006", "知识库删除缺身份头（fail-closed）")
    client = _require_client()
    banks = await _caller_banks(headers)
    found = False
    if role != "admin":
        own = {_kb_bank_for_tenant(tenant_id), _kb_chunks_bank_for_tenant(tenant_id)}
        foreign = [
            b for b in await _discover_kb_banks()
            if b not in own and await _bank_has_item(client, b, item_id)
        ]
        if foreign:
            raise KBError(403, "KNB_FORB_7006", _FORBIDDEN_MSG)
        for bank in sorted(own):
            if await _delete_item_in_bank(client, bank, item_id):
                found = True
    else:
        for bank in banks:
            if await _delete_item_in_bank(client, bank, item_id):
                found = True
    if not found:
        raise KBError(404, "KNB_NOTF_5002", "未找到知识库条目")
    logger.info("[kb] 条目已删除 id=%s tenant=%s role=%s", item_id, tenant_id, role or "user")
    return {"message": "条目已删除", "id": item_id}


# ── 上传链路（bank 唯一写入面；整文档单次 aretain + verbatim 主模式）────────


def _llm_provider_configured() -> bool:
    """sidecar LLM provider 是否可用（verbatim 抽取前提）。

    信号源 = server.py `_apply_llm_env` 注入的 ``HINDSIGHT_API_LLM_PROVIDER``；
    缺失或 "none"（上游对 provider=none 全局强制 chunks 模式）→ verbatim 主
    模式天然不可用，上传直落 kb_chunks 降级 bank。
    """
    provider = os.environ.get(_LLM_PROVIDER_ENV, "").strip()
    return bool(provider) and provider.lower() != "none"


def _title_of(filename: str) -> str:
    """文档标题 = 文件名去扩展名（kb_name 元数据保留完整文件名）。"""
    return os.path.splitext(filename)[0] or filename


def _title_primed_content(title: str, text: str) -> str:
    """内容头部前置标题行 ``# {title}``（标题双通道之一：首 chunk 向量化输入
    含标题语义——向量化输入=fact_text，context 不参与 embedding）。"""
    return f"# {title}\n\n{text}"


async def _resolved_extraction_mode(bank_id: str) -> str:
    """bank 解析后的实际摄取模式（banks.get_bank_config 权威读）。

    上游在 LLM provider=none 时全局强制 chunks——即使 bank 请求 verbatim，
    解析值也是 chunks；此读是降级判定的配置面权威信号。读取失败（bank config
    API 被服务端禁用等）返回空串 = 未知，调用方乐观走主链路。
    """
    getter = getattr(getattr(_client, "banks", None), "get_bank_config", None)
    if not callable(getter):
        return ""
    try:
        config = await getter(bank_id=bank_id)
    except Exception as exc:  # noqa: BLE001 — 探测失败按未知处理（乐观走主链路）
        logger.debug("[kb] bank 解析模式探测失败（按未知处理）| bank=%s error=%s", bank_id, exc)
        return ""
    if hasattr(config, "model_dump"):
        config = config.model_dump()
    if not isinstance(config, dict):
        return ""
    resolved = config.get("config") or {}
    return str(resolved.get("retain_extraction_mode", "") or "") if isinstance(resolved, dict) else ""


async def _ensure_bank(bank_id: str = _KB_BANK_ID) -> None:
    """确保 kb bank 存在且摄取模式/词表就位（幂等；X10-F1 起按租户 bank 调用）。

    模式经 acreate_bank 的 create-or-update 通道（retain_extraction_mode 直传，
    幂等覆盖）；受控词表（entity_labels）仅 banks config API 通道承载——该 API
    被服务端禁用时 warn 留痕继续（模式已由 acreate_bank 承载，缺词表仅降级
    verbatim 实体的域标签增强面）。
    """
    if _client is None:
        raise KBError(503, "KNB_INIT_5001", "知识库后端未初始化（hindsight not initialized）")
    create = getattr(_client, "acreate_bank", None)
    if callable(create):
        await create(bank_id=bank_id, retain_extraction_mode=_bank_extraction_mode(bank_id))
    if _is_chunks_bank(bank_id):
        return
    updater = getattr(getattr(_client, "banks", None), "update_bank_config", None)
    if callable(updater):
        try:
            await updater(bank_id=bank_id, entity_labels=_VERBATIM_ENTITY_LABELS)
        except Exception as exc:  # noqa: BLE001 — 词表缺席 warn 降级（模式面不受影响）
            logger.warning(
                "[kb] bank entity_labels 受控词表设置失败（verbatim 实体域标签增强缺席）"
                "| bank=%s error=%s",
                bank_id,
                exc,
            )


def _auto_tag_from_ext(ext: str) -> str:
    """自动标签：文件扩展名（如 ".md" → "filetype:md"，随 chunk 落 bank tags）。"""
    return f"filetype:{ext.lstrip('.')}" if ext else "filetype:unknown"


async def _retain_document(bank_id: str, content: str, title: str, metadata: dict[str, str],
                           tags: list[str]) -> None:
    """整文档单次 aretain（标题双通道：内容前置标题行 + context=标题）。

    retain_async=True：抽取后台完成（不阻塞上传；recall 逐步可见，ADR 验证点③
    时点语义）。在 MCP 事件循环内 await（aiohttp 会话 loop 绑定，禁止另起循环）。
    """
    client = _require_client()
    await client.aretain(
        bank_id=bank_id,
        content=content,
        context=title,
        metadata=metadata,
        tags=tags,
        retain_async=True,
    )


async def upload_document(
    filename: str,
    content: bytes,
    mime_type: str = "application/octet-stream",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """上传文档：整文档单次 aretain 写调用方租户 bank（唯一存储，X10-F1）。

    主链路 verbatim 模式（主 bank bank 级配置）；verbatim 落库失败（LLM
    provider 未配置 / bank 解析模式被上游强制非 verbatim / aretain 调用失败）
    → 降级落 kb_chunks bank（chunks 模式零 LLM），响应字段 + 告警日志如实
    标注实际落库模式（禁静默降级）；双 bank 皆失败 → 上传如实 500。

    Args:
        filename: 原始文件名
        content: 文件字节
        mime_type: 文件 MIME
        headers: 入站请求头（内核权威身份头；缺失 → 403 不落写）

    Returns:
        {item_id, file_id, filename, size, chunks_imported, name, message,
         ingestion_mode, bank_id, degraded, degradation_reason}
        （chunks_imported=入库文档数，恒 1；条目内切块归上游原生管线）

    Raises:
        KBError: 扩展名不支持 / 文件过大 / 文本解码为空 / 缺身份头(403) /
            client 未初始化(503) / 双 bank 入库失败(500)
    """
    tenant_id, user_id, _role = _caller_identity(headers)
    if not tenant_id or not user_id:
        raise KBError(403, "KNB_FORB_7006", "知识库上传缺身份头（fail-closed，不落写）")
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

    item_id = uuid.uuid4().hex[:12]
    created_at = _now_iso()
    tags = [_TAG_TYPE, f"{_TAG_ITEM_PREFIX}{item_id}", _auto_tag_from_ext(ext)]
    title = _title_of(filename)
    metadata = {
        "memory_type": "semantic",
        "kb_id": item_id,
        "kb_name": filename,
        "kb_size": str(len(content)),
        "kb_mime": mime_type,
        "kb_owner": user_id,
        "kb_tenant": tenant_id,
        "source": "knowledge_base",
        "created_at": created_at,
    }
    doc_content = _title_primed_content(title, text)

    primary_bank = _kb_bank_for_tenant(tenant_id)
    fallback_bank = _kb_chunks_bank_for_tenant(tenant_id)
    landed_bank = ""
    degradation_reason = ""

    if _llm_provider_configured():
        try:
            await _ensure_bank(primary_bank)
            resolved = await _resolved_extraction_mode(primary_bank)
            if resolved and resolved != "verbatim":
                # 上游强制非 verbatim（provider=none 全局强制 chunks）→ 主 bank
                # 假 verbatim 不做，直落降级 bank
                degradation_reason = (
                    f"bank {primary_bank} 解析摄取模式={resolved}（LLM provider 未配置，"
                    "上游强制 chunks）"
                )
            else:
                await _retain_document(primary_bank, doc_content, title, metadata, tags)
                landed_bank = primary_bank
        except Exception as exc:  # noqa: BLE001 — verbatim 落库失败 → 降级（如实上报）
            degradation_reason = f"verbatim 落库失败: {exc}"
    else:
        degradation_reason = (
            f"LLM provider 未配置（{_LLM_PROVIDER_ENV} 缺失或 none，verbatim 不可用）"
        )

    if landed_bank:
        ingestion_mode = "verbatim"
    else:
        # 降级落 kb_chunks bank（chunks 模式零 LLM）；降级也失败 = 唯一存储面
        # 真实故障，如实 500
        ingestion_mode = "chunks"
        try:
            await _ensure_bank(fallback_bank)
            await _retain_document(fallback_bank, doc_content, title, metadata, tags)
            landed_bank = fallback_bank
        except Exception as exc:  # noqa: BLE001 — 双 bank 皆败 = 上传如实失败
            raise KBError(500, "KNB_ING_8001", f"知识块入库失败: {exc}") from exc
        logger.warning(
            "[kb] verbatim 降级落 chunks 模式（如实上报，禁静默）| item_id=%s bank=%s reason=%s",
            item_id,
            fallback_bank,
            degradation_reason,
        )

    logger.info(
        "[kb] 上传入库成功 item_id=%s filename=%s bank=%s mode=%s",
        item_id, filename, landed_bank, ingestion_mode,
    )
    return {
        "item_id": item_id,
        "file_id": item_id,
        "filename": filename,
        "name": filename,
        "size": len(content),
        "chunks_imported": 1,
        "message": "文件上传成功" if not degradation_reason else "文件上传成功（已降级 chunks 模式入库）",
        "ingestion_mode": ingestion_mode,
        "bank_id": landed_bank,
        "degraded": bool(degradation_reason),
        "degradation_reason": degradation_reason,
    }


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
