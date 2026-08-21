"""知识库服务（knowledge-base 域）——channel_api 退役方案批次 4 功能扩展。

原 channel_api 的 knowledge-base 10 端点是纯 stub（routes_missing.py:1037-1092，
恒空列表/假成功）。本模块实现真实链路：

    上传（multipart）→ 原文落盘（uploads/kb/，三方对齐 plugins/shared/uploads_path.py）
    → 切块（~2000 字符/块，与 hindsight.import_document 同款朴素滑窗）
    → 向量化入库（hindsight aretain → 嵌入式 PG pgvector 存储，
      复用本插件的 pgvector embedded 基础设施；独立 **kb** bank，与记忆域
      "default" bank 物理隔离，知识块不会污染记忆检索）
    → 分类/标签元数据（插件本地 JSON 元数据仓 + hindsight tags 服务端过滤）
    → 检索（hindsight arecall + tags 过滤 + include_chunks 原文召回，
      chunk 归属经 chunk_id→item_id 索引回连元数据仓）
    → 删除（元数据 + 落盘文件 + 尽力清理 hindsight 侧 chunk）

设计要点：
- 韧性对齐本插件 server.py：hindsight client 未初始化时所有端点降级不崩溃
  （check 端点如实报告 available=false）。
- 元数据仓为插件本地 JSON（``data/kb/kb_meta.json``，模块级 ``_kb_data_dir()``
  可被测试 monkeypatch / env ``HINDSIGHT_KB_DATA_DIR`` 覆盖）；原子写
  （临时文件 + os.replace），损坏时按空仓重建（只读路径永不炸）。
- 环规：本模块所有 hindsight client 调用均为 async，且**必须在 MCP 事件循环内
  await**（aiohttp 会话 loop 绑定，另起新循环会炸）——server.py http.handle
  直接 await，不复用 sync 桥。
- 检索结果只返回元数据仓仍注册条目的 chunk（已删条目残留 chunk 不回显）；
  hindsight 侧 chunk 清理为尽力而为（documents.delete_document 存在时逐 chunk 删）。

元数据 schema（JSON）::

    {
      "version": 1,
      "categories": [{"name": str, "created_at": str}],
      "items": [
        {
          "id": str, "name": str, "size": int, "mime_type": str,
          "categories": [str], "tags": [str],
          "chunk_count": int, "chunk_ids": [str],
          "source_file": str, "created_at": str, "updated_at": str,
        }
      ]
    }
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# ── 常量 ───────────────────────────────────────────────────────────────────
# 知识块 hindsight bank（与记忆 "default" bank 隔离，见模块 docstring）
_KB_BANK_ID = "kb"

# 知识块切块大小（字符，与 server.py _CHUNK_SIZE 同款朴素滑窗）
_KB_CHUNK_SIZE = 2000

# 允许入库的文档扩展名（无外部解析依赖，纯文本直读）
_KB_ALLOWED_EXTS = (".txt", ".md")

# 单文件大小上限（10MB，防误拖大文件打爆切块/向量化）
_KB_MAX_FILE_BYTES = 10 * 1024 * 1024

# 检索默认/上限
_KB_SEARCH_DEFAULT_TOP_K = 10
_KB_SEARCH_MAX_TOP_K = 50

# 元数据文件名
_KB_META_FILENAME = "kb_meta.json"

# 知识块 tag 前缀（hindsight tags 服务端过滤）
_TAG_TYPE = "type:knowledge"
_TAG_ITEM_PREFIX = "kb_item:"
_TAG_CAT_PREFIX = "kb_cat:"
_TAG_TAG_PREFIX = "kb_tag:"


class KBError(Exception):
    """知识库业务异常（server.py 捕获转 HTTP 状态）。"""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


# ── 运行时状态 ─────────────────────────────────────────────────────────────
# hindsight HTTP client（由 server.py http.handle 分发时注入；None = 未初始化）
_client: Any = None

# 元数据仓目录（缺省插件 data/kb；测试 monkeypatch / env 覆盖）
_KB_DATA_DIR = os.environ.get("HINDSIGHT_KB_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "kb"
)


def set_client(client: Any | None) -> None:
    """注入 hindsight HTTP client 引用（server.py 分发时调用；测试直接传 mock）。"""
    global _client
    _client = client


def set_data_dir(data_dir: str) -> None:
    """覆盖元数据仓目录（测试隔离用）。"""
    global _KB_DATA_DIR
    _KB_DATA_DIR = data_dir


def _require_client() -> Any:
    """返回 hindsight client；未初始化时抛降级异常（check 端点除外）。"""
    if _client is None:
        raise KBError(503, "KNB_INIT_5001", "知识库后端未初始化（hindsight not initialized）")
    return _client


# ── 元数据仓（JSON，原子写）────────────────────────────────────────────────


def _meta_path() -> str:
    return os.path.join(_KB_DATA_DIR, _KB_META_FILENAME)


def _empty_meta() -> dict[str, Any]:
    return {"version": 1, "categories": [], "items": []}


def _load_meta() -> dict[str, Any]:
    """读取元数据仓；文件缺失/损坏时返回空仓（只读路径永不炸）。"""
    path = _meta_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return _empty_meta()


def _save_meta(meta: dict[str, Any]) -> None:
    """原子写元数据仓（临时文件 + os.replace）。"""
    os.makedirs(_KB_DATA_DIR, exist_ok=True)
    tmp = _meta_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _meta_path())  # noqa: PTH105 —— 与仓库 os.* 风格一致


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── 条目结构 ───────────────────────────────────────────────────────────────
# item: {id, name, size, mime_type, categories: [str], tags: [str],
#        chunk_count, chunk_ids: [str], source_file: str, created_at, updated_at}


def _item_public(item: dict[str, Any]) -> dict[str, Any]:
    """条目对外形态（前端 KnowledgeBasePage 字段对齐：id/name/size/categories/tags/created_at/updated_at）。"""
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "size": int(item.get("size", 0) or 0),
        "mime_type": item.get("mime_type", ""),
        "categories": list(item.get("categories", []) or []),
        "tags": list(item.get("tags", []) or []),
        "chunk_count": int(item.get("chunk_count", 0) or 0),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
    }


def _chunk_text(text: str, chunk_size: int = _KB_CHUNK_SIZE) -> list[str]:
    """按字符数朴素滑窗切块（与 server.py _chunk_text 同款）。"""
    if not text:
        return []
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


# ── 业务函数（元数据面，纯本地 JSON，同步）────────────────────────────────


def list_items() -> list[dict[str, Any]]:
    """知识库条目列表（按创建时间倒序，前端页面直接消费数组）。"""
    meta = _load_meta()
    items = sorted(meta.get("items", []), key=lambda it: str(it.get("created_at", "")), reverse=True)
    return [_item_public(it) for it in items]


def get_stats() -> dict[str, Any]:
    """知识库统计：{total, categories_count, tags_count, total_chunks, total_size}。"""
    meta = _load_meta()
    items = meta.get("items", [])
    categories = {c for it in items for c in (it.get("categories") or [])}
    tags = {t for it in items for t in (it.get("tags") or [])}
    return {
        "total": len(items),
        "categories_count": len(categories),
        "tags_count": len(tags),
        "total_chunks": sum(int(it.get("chunk_count", 0) or 0) for it in items),
        "total_size": sum(int(it.get("size", 0) or 0) for it in items),
    }


def list_categories() -> list[dict[str, Any]]:
    """分类列表：[{name, count}]，count = 该分类下的条目数（空分类 count 0）。"""
    meta = _load_meta()
    counts: dict[str, int] = {}
    for it in meta.get("items", []):
        for cat in it.get("categories") or []:
            counts[cat] = counts.get(cat, 0) + 1
    out: list[dict[str, Any]] = []
    for cat in meta.get("categories", []):
        name = str(cat.get("name", ""))
        if name:
            out.append({"name": name, "count": counts.get(name, 0)})
    return out


def create_category(name: str) -> dict[str, Any]:
    """创建分类（同名已存在 → 409）。"""
    name = (name or "").strip()
    if not name:
        raise KBError(400, "KNB_VAL_7001", "分类名称不能为空")
    meta = _load_meta()
    for cat in meta.get("categories", []):
        if str(cat.get("name", "")) == name:
            raise KBError(409, "KNB_CAT_6001", f"分类 '{name}' 已存在")
    cat = {"name": name, "created_at": _now_iso()}
    meta.setdefault("categories", []).append(cat)
    _save_meta(meta)
    return {"message": f"分类 '{name}' 创建成功", "name": name, "created_at": cat["created_at"]}


def delete_category(name: str) -> dict[str, Any]:
    """删除分类：从分类表移除，并解除该分类与全部条目的关联。"""
    name = (name or "").strip()
    meta = _load_meta()
    meta["categories"] = [c for c in meta.get("categories", []) if str(c.get("name", "")) != name]
    for it in meta.get("items", []):
        cats = list(it.get("categories", []) or [])
        if name in cats:
            it["categories"] = [c for c in cats if c != name]
            it["updated_at"] = _now_iso()
    _save_meta(meta)
    return {"message": f"分类 '{name}' 已删除"}


def list_tags() -> list[str]:
    """标签列表（全条目去重，字典序）。"""
    meta = _load_meta()
    tags = {t for it in meta.get("items", []) for t in (it.get("tags") or [])}
    return sorted(tags)


def get_item(item_id: str) -> dict[str, Any]:
    """条目详情（含 chunk_count；chunk 正文经检索端点按相关度取回，不整篇返回）。"""
    meta = _load_meta()
    for it in meta.get("items", []):
        if it.get("id") == item_id:
            return _item_public(it)
    raise KBError(404, "KNB_NOTF_5002", "未找到知识库条目")


async def delete_item(item_id: str) -> dict[str, Any]:
    """删除条目：元数据仓移除 + 落盘源文件删除 + 尽力清理 hindsight 侧 chunk。"""
    meta = _load_meta()
    target = None
    for it in meta.get("items", []):
        if it.get("id") == item_id:
            target = it
            break
    if target is None:
        raise KBError(404, "KNB_NOTF_5002", "未找到知识库条目")
    meta["items"] = [it for it in meta.get("items", []) if it.get("id") != item_id]
    _save_meta(meta)

    # 落盘源文件尽力删除（源文件缺失不视为失败）
    source_file = target.get("source_file") or ""
    if source_file:
        try:
            if os.path.isfile(source_file):
                os.remove(source_file)
        except OSError as exc:  # noqa: BLE001
            logger.warning("[kb] 删除源文件失败 source=%s | %s", source_file, exc)

    # 尽力清理 hindsight 侧 chunk（documents.delete_document 存在时逐 chunk 删；
    # 失败只告警不炸——检索面已按元数据仓客户端过滤，脏数据不会回显）
    chunk_ids = target.get("chunk_ids") or []
    if chunk_ids and _client is not None:
        deleter = getattr(getattr(_client, "documents", None), "delete_document", None)
        if callable(deleter):
            for cid in chunk_ids:
                try:
                    await deleter(bank_id=_KB_BANK_ID, document_id=cid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[kb] chunk 清理失败 id=%s | %s", cid, exc)
    logger.info("[kb] 条目已删除 id=%s chunk_count=%d", item_id, len(chunk_ids))
    return {"message": "条目已删除", "id": item_id}


# ── 上传链路 ───────────────────────────────────────────────────────────────


async def upload_document(
    filename: str,
    content: bytes,
    mime_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """上传文档：落盘 → 切块 → 向量化入库（hindsight aretain）→ 注册元数据。

    Args:
        filename: 原始文件名
        content: 文件字节
        mime_type: 文件 MIME

    Returns:
        {item_id, file_id, filename, size, chunks_imported, name, message}

    Raises:
        KBError: 扩展名不支持 / 文件过大 / 文本解码为空 / client 未初始化
    """
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
    # 落盘源文件（uploads/kb/ 子目录，避让内核 /uploads/{basename} 静态面）
    uploads_dir = _resolve_kb_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)
    source_file = os.path.join(uploads_dir, f"{item_id}{ext}")
    with open(source_file, "wb") as fh:
        fh.write(content)

    # 切块 → 向量化入库（独立 kb bank，type:knowledge tag 隔离）。
    # 在 MCP 事件循环内 await（aiohttp 会话 loop 绑定，禁止另起循环）。
    try:
        await _ensure_bank()
        chunk_ids: list[str] = []
        for idx, chunk in enumerate(chunks):
            chunk_meta = {
                "memory_type": "semantic",
                "kb_id": item_id,
                "kb_name": filename,
                "kb_chunk_index": str(idx),
                "kb_chunk_total": str(len(chunks)),
                "source": "knowledge_base",
                "created_at": _now_iso(),
            }
            tags = [_TAG_TYPE, f"{_TAG_ITEM_PREFIX}{item_id}"]
            result = await client.aretain(
                bank_id=_KB_BANK_ID,
                content=chunk,
                metadata=chunk_meta,
                tags=tags,
            )
            cid = str(getattr(result, "operation_id", "") or "")
            if cid:
                chunk_ids.append(cid)
    except KBError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 入库失败：回滚落盘文件（不留半成品）
        try:
            if os.path.isfile(source_file):
                os.remove(source_file)
        except OSError:  # noqa: BLE001
            pass
        raise KBError(500, "KNB_ING_8001", f"知识块入库失败: {exc}") from exc

    meta: dict[str, Any] = _load_meta()
    item = {
        "id": item_id,
        "name": filename,
        "size": len(content),
        "mime_type": mime_type,
        "categories": [],
        "tags": [_auto_tag_from_ext(ext)],
        "chunk_count": len(chunks),
        "chunk_ids": chunk_ids,
        "source_file": source_file,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    meta.setdefault("items", []).append(item)
    _save_meta(meta)
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


async def _ensure_bank() -> None:
    """确保 kb bank 存在（acreate_bank 幂等）。"""
    client = _require_client()
    create = getattr(client, "acreate_bank", None)
    if callable(create):
        await create(bank_id=_KB_BANK_ID)


def _auto_tag_from_ext(ext: str) -> str:
    """自动标签：文件扩展名（如 ".md" → "filetype:md"）。"""
    return f"filetype:{ext.lstrip('.')}" if ext else "filetype:unknown"


def _resolve_kb_uploads_dir() -> str:
    """KB 源文件落盘目录：uploads 三方对齐（plugins/shared/uploads_path.py）下 kb/ 子目录。

    ``UPLOADS_DIR`` env 覆盖（三方对齐协议原生支持）便于测试注入 tmp 目录。
    """
    try:
        import sys  # noqa: PLC0415

        shared_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
        if shared_root not in sys.path:
            sys.path.insert(0, shared_root)
        from uploads_path import resolve_uploads_dir  # noqa: PLC0415

        return str(resolve_uploads_dir() / "kb")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kb] uploads_path 解析失败，回退插件 data/kb/uploads | %s", exc)
        return os.path.join(_KB_DATA_DIR, "uploads")


# ── 检索 ───────────────────────────────────────────────────────────────────


async def search(
    query: str,
    top_k: int = _KB_SEARCH_DEFAULT_TOP_K,
    category: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """知识库检索：hindsight arecall（语义向量 + 关键词，pgvector embedded）。

    - tags 服务端过滤：type:knowledge 恒带；category/tag 过滤转前缀 tag
      （tags_match="all"，AND 语义）。
    - include_chunks=True 取原文（recall 的 results 是抽取后事实，知识库检索要原文）。
    - 客户端按元数据仓过滤：chunk_id → item_id 归属索引，仅返回仍注册条目的
      chunk，并附条目上下文（item_id/name/categories/tags）。

    Returns:
        {results: [{item_id, name, categories, tags, chunk_count, content, score}], total}
    """
    if not query or not query.strip():
        raise KBError(400, "KNB_VAL_7005", "检索词不能为空")
    top_k = max(1, min(_KB_SEARCH_MAX_TOP_K, int(top_k or _KB_SEARCH_DEFAULT_TOP_K)))
    client = _require_client()
    tags = [_TAG_TYPE]
    if category:
        tags.append(f"{_TAG_CAT_PREFIX}{category}")
    if tag:
        tags.append(f"{_TAG_TAG_PREFIX}{tag}")

    args: dict[str, Any] = {
        "bank_id": _KB_BANK_ID,
        "query": query,
        "include_chunks": True,
        "tags": tags,
        "tags_match": "all",
    }
    try:
        response = await client.arecall(**args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kb] recall 调用失败降级 | error=%s", exc)
        return {"results": [], "total": 0, "error": str(exc)}

    meta = _load_meta()
    items_by_id = {str(it.get("id", "")): it for it in meta.get("items", [])}
    # chunk 归属索引：chunk_id（aretain operation_id = memory unit id，2026-08-19
    # e2e 实测）→ item_id。召回条目 id 命中索引即带可靠出处；未命中的 chunk
    # （异源 / 已删条目残留）一律不回显。
    chunk_owner: dict[str, str] = {}
    for it in meta.get("items", []):
        for cid in it.get("chunk_ids") or []:
            chunk_owner[str(cid)] = str(it.get("id", ""))

    def _to_dict(obj: Any) -> dict[str, Any]:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj if isinstance(obj, dict) else {}

    # 原文优先（recall chunks，include_chunks=True），事实结果兜底
    raw: list[tuple[str, float, str]] = []
    chunks_collection = getattr(response, "chunks", None) or []
    if isinstance(chunks_collection, list) and chunks_collection:
        for chunk in chunks_collection:
            d = _to_dict(chunk)
            text = d.get("text") or d.get("content") or ""
            if not text:
                continue
            raw.append((str(d.get("id", "")), float(d.get("score", 0.0) or 0.0), text))
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
            raw.append((str(d.get("id", "")), float(d.get("score", 0.0) or 0.0), text))

    out: list[dict[str, Any]] = []
    for cid, score, text in raw[:top_k]:
        item_id = chunk_owner.get(cid, "")
        if not item_id or item_id not in items_by_id:
            # 出处不可考（大体是已删条目的残留 chunk）——不回显，保证检索面诚实
            continue
        item = items_by_id[item_id]
        out.append(
            {
                "item_id": item["id"],
                "name": item.get("name", ""),
                "categories": list(item.get("categories", []) or []),
                "tags": list(item.get("tags", []) or []),
                "chunk_count": int(item.get("chunk_count", 0) or 0),
                "content": text,
                "score": score,
            }
        )

    return {"results": out, "total": len(out)}


async def check_available() -> dict[str, Any]:
    """知识库可用性检查（降级端点，不抛错）。"""
    if _client is None:
        return {
            "available": False,
            "message": "知识库服务未初始化（hindsight not initialized）",
            "backend": "hindsight",
            "bank": _KB_BANK_ID,
        }
    try:
        await _ensure_bank()
        return {
            "available": True,
            "message": "知识库服务可用（hindsight + pgvector embedded）",
            "backend": "hindsight",
            "bank": _KB_BANK_ID,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "message": f"知识库服务异常: {exc}",
            "backend": "hindsight",
            "bank": _KB_BANK_ID,
        }
