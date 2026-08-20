#!/usr/bin/env python3
"""Hindsight 记忆 MCP sidecar 服务端。

使用 AgentOS Plugin SDK 封装 hindsight-all-slim（嵌入式 agent 记忆库，
内部 pg0，无 Docker/外部 DB）。hindsight 通过环境变量读取 LLM/embedding 配置。

工具：
- hindsight.retain — 存入记忆（memory_type 进 metadata 以便 recall 客户端过滤）
- hindsight.recall — 检索记忆（memory_type 可选客户端过滤）
- hindsight.reflect — 触发反思/巩固
- hindsight.delete — 删除记忆
- hindsight.import_document — 文档切块导入（~2000 字符/块）

韧性设计：hindsight 包可能未安装——on_load 内 try/except 懒导入，
失败时 _client=None，所有工具检测 None 后返回降级字典，sidecar 永不崩溃。

bank_id 是多租户隔离 key（来自内核的 tenant_id），缺省回落到默认值。

[来源: docs/tasks Step 2 Hindsight memory sidecar]
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from typing import Any

# 本地模块可达性：插件目录加入 sys.path（与 memory/server.py 同款做法）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("hindsight_memory_service")

# ── 运行时状态 ──────────────────────────────────────────
# hindsight client 句柄。None 表示未初始化（包未安装 / on_load 失败），
# 所有工具据此降级。模块级变量便于测试 monkeypatch。
_client: Any = None
# hindsight-api 服务器子进程（on_load 启动,on_unload 终止）
_api_process: Any = None
# hindsight-api 监听端口
_HINDSIGHT_PORT = "8420"

# bank_id 缺省值（多租户隔离 key 缺省；运行时由内核注入 tenant_id）
_DEFAULT_BANK_ID = os.environ.get("HINDSIGHT_DEFAULT_BANK_ID", "default")

# 缺省回落 "default" 的一次性告警开关（租户隔离未生效必须可观测）
_bank_default_warned = False

# 文档切块大小（字符）
_CHUNK_SIZE = 2000

# 允许导入的文件扩展名
_ALLOWED_DOC_EXTS = (".txt", ".md")


def _degrade_dict(operation: str) -> dict[str, Any]:
    """构造统一的降级返回（_client 未就绪时）。"""
    return {
        "error": "hindsight not initialized",
        "initialized": False,
        "operation": operation,
    }


def _resolve_bank_id(bank_id: str | None) -> str:
    """bank_id 缺省时回落到默认值（多租户隔离 key）。

    Args:
        bank_id: 调用方传入的 bank_id（可能为 None/空串）

    Returns:
        非空 bank_id
    """
    if bank_id:
        return bank_id
    # 运行时可被 on_load 从 config 覆盖
    fallback = getattr(sys.modules[__name__], "_DEFAULT_BANK_ID", "default")
    # 未配置缺省库而回落字面 "default" = 租户隔离未生效，一次性告警
    if fallback == "default":
        global _bank_default_warned
        if not _bank_default_warned:
            _bank_default_warned = True
            logger.warning(
                "[hindsight] bank_id 未提供且未配置缺省库"
                "（config default_bank_id / env HINDSIGHT_DEFAULT_BANK_ID），"
                "回落 'default'——多租户隔离未生效"
            )
    return fallback


def _filter_by_memory_type(
    results: list[dict[str, Any]], memory_type: str | None
) -> list[dict[str, Any]]:
    """按 memory_type 客户端过滤 recall 结果。

    匹配字段：metadata.memory_type（写侧固定存放位置）。

    Args:
        results: hindsight recall 原始结果列表
        memory_type: 过滤类型；None/空 表示不过滤

    Returns:
        过滤后列表
    """
    if not memory_type:
        return results
    out: list[dict[str, Any]] = []
    for r in results:
        meta = r.get("metadata") or {}
        # 写侧固定存 metadata.memory_type（本模块 _store 的 setdefault）。
        if meta.get("memory_type") == memory_type:
            out.append(r)
    return out


def _chunk_text(text: str, chunk_size: int = _CHUNK_SIZE) -> list[str]:
    """按字符数切分文本（朴素滑窗，~chunk_size 字符/块）。

    Args:
        text: 原始文本
        chunk_size: 每块字符数上限

    Returns:
        文本块列表（空文本返回 []）
    """
    if not text:
        return []
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


# ═══════════════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════════════


@plugin.tool(
    name="hindsight.retain",
    schema={
        "type": "object",
        "properties": {
            "bank_id": {
                "type": "string",
                "description": "Memory bank id (tenant isolation key)",
            },
            "content": {"type": "string", "description": "Memory content text"},
            "memory_type": {
                "type": "string",
                "default": "semantic",
                "description": "Memory type tag (goes into metadata for recall filter)",
            },
            "metadata": {
                "type": "object",
                "default": {},
                "description": "Optional extra metadata",
            },
        },
        "required": ["content"],
    },
    description="Store a memory into a hindsight bank",
)
async def hindsight_retain(
    content: str,
    bank_id: str = "",
    memory_type: str = "semantic",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store a memory entry via hindsight client.aretain.

    memory_type 同时写入 tags(服务端过滤)和 metadata(客户端读取)。
    """
    if _client is None:
        return _degrade_dict("retain")

    try:
        bank = _resolve_bank_id(bank_id)
        meta = dict(metadata or {})
        meta.setdefault("memory_type", memory_type)
        # tags：type tag（recall/reflect 服务端过滤）+ 调用方 tags 提升。
        # HindsightBackend.add 把 IMemoryBackend.add 的 tags 序列化进
        # metadata["tags"]（hindsight aretain 的 metadata 是 dict[str,str]
        # pydantic 校验，list 值必炸——2026-08-19 批 C 取证）；此处还原为
        # hindsight 真实 tags，供 list_documents/recall 服务端精确过滤
        # （review 冷读按 review_id:<id> tag 定向的前提）。
        tags = [f"type:{memory_type}"]
        raw_tags = meta.get("tags")
        if isinstance(raw_tags, list):
            # 直调工具的调用方把 list 放进 metadata——同样提升，并回写
            # JSON 串保 wire 校验安全
            tags.extend(str(t) for t in raw_tags if t)
            meta["tags"] = json.dumps(raw_tags, ensure_ascii=False)
        elif isinstance(raw_tags, str):
            try:
                parsed_tags = json.loads(raw_tags)
            except (json.JSONDecodeError, ValueError):
                parsed_tags = None
            if isinstance(parsed_tags, list):
                tags.extend(str(t) for t in parsed_tags if t)

        # 同步 retain 的 RetainResponse 不带单条 id（operation_id 恒 None）——
        # memory 工具层要求 memory_id 确认写入，故用 retain_async + 调用方
        # 生成 operation_id（幂等 id），服务器接受后原样回传即 memory_id
        # （2026-08-19 e2e 实测：sync 模式取 id 恒空→诚变化误判"未确认"）。
        import uuid  # noqa: PLC0415

        operation_id = str(uuid.uuid4())  # 服务器校验标准 UUID（secrets.token_hex 24hex 被 422）
        result = await _client.aretain(
            bank_id=bank, content=content, metadata=meta, tags=tags,
            retain_async=True, operation_id=operation_id,
        )
        # RetainResponse 对象:优先 operation_id(调用方幂等 id)，兜底旧字段
        mem_id = ""
        if hasattr(result, "accepted") or result is not None:
            mem_id = str(getattr(result, "operation_id", "") or "")
        elif isinstance(result, dict):
            mem_id = result.get("id", result.get("operation_id", ""))
        if not mem_id and getattr(result, "success", False):
            mem_id = operation_id
        return {"id": mem_id, "stored": True, "metadata": meta}
    except Exception as e:
        logger.warning("[hindsight.retain] 调用失败 | error=%s", e)
        return {"id": "", "stored": False, "error": str(e)}


@plugin.tool(
    name="hindsight.recall",
    schema={
        "type": "object",
        "properties": {
            "bank_id": {
                "type": "string",
                "description": "Memory bank id (tenant isolation key)",
            },
            "query": {"type": "string", "description": "Recall query"},
            "top_k": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 100,
            },
            "memory_type": {
                "type": "string",
                "description": "Optional client-side filter by memory_type",
            },
        },
        "required": ["query"],
    },
    description="Recall relevant memories from a hindsight bank",
)
async def hindsight_recall(
    query: str,
    bank_id: str = "",
    top_k: int = 5,
    memory_type: str | None = None,
) -> dict[str, Any]:
    """Recall memories via hindsight client.arecall.

    memory_type 用作 tags 服务端过滤(type:<memory_type>)。
    """
    if _client is None:
        return _degrade_dict("recall")

    try:
        bank = _resolve_bank_id(bank_id)
        kwargs: dict[str, Any] = {"bank_id": bank, "query": query}
        if memory_type:
            kwargs["tags"] = [f"type:{memory_type}"]
            kwargs["tags_match"] = "any"

        result = await _client.arecall(**kwargs)
        # RecallResponse (hindsight 0.9.0) 把所有召回结果放在 results 字段
        # (每条含 id/text/type/score 等)。results 是主字段。
        # chunks/source_facts 是可选的辅助字段(需 include_chunks=True 等)。
        items: list[dict[str, Any]] = []
        # 优先取 results(0.9.0 主字段);兼容旧版 memories/facts
        for attr in ("results", "memories", "facts"):
            collection = getattr(result, attr, None)
            if collection:
                for item in collection:
                    if hasattr(item, "model_dump"):
                        items.append(item.model_dump())
                    elif isinstance(item, dict):
                        items.append(item)
                    else:
                        items.append({"content": str(item)})
                break
        # 统一字段名:results 里的 text → content(上层期望 content 字段)
        for item in items:
            if "text" in item and "content" not in item:
                item["content"] = item.pop("text")
        return {"results": items, "total": len(items)}
    except Exception as e:
        logger.warning("[hindsight.recall] 调用失败 | error=%s", e)
        return {"results": [], "total": 0, "error": str(e)}


@plugin.tool(
    name="hindsight.reflect",
    schema={
        "type": "object",
        "properties": {
            "bank_id": {
                "type": "string",
                "description": "Memory bank id (tenant isolation key)",
            },
        },
    },
    description="Trigger hindsight reflection/consolidation on a bank",
)
async def hindsight_reflect(bank_id: str = "", query: str = "") -> dict[str, Any]:
    """Trigger hindsight reflection via areflect.

    query 缺省时用一个通用查询触发反思/巩固。
    """
    if _client is None:
        return _degrade_dict("reflect")

    try:
        bank = _resolve_bank_id(bank_id)
        result = await _client.areflect(bank_id=bank, query=query or "总结最近的记忆和经验")
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        return {"result": str(result)}
    except Exception as e:
        logger.warning("[hindsight.reflect] 调用失败 | error=%s", e)
        return {"error": str(e)}


def _extract_summary_text(result: Any) -> str:
    """从容错提取 reflect 输出中的摘要文本（字段名多样，逐个尝试）。

    Returns:
        摘要文本；无法提取时返回空串（调用方降级）。
    """
    # pydantic 响应（ReflectResponse 等）先 model_dump 再提取——直接
    # str(obj) 会得到对象 repr（summarize 摘要面预存缺陷，与 arecall
    # 传参 TypeError 同路径）
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("summary", "text", "content", "result", "reflection", "response"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val
        parts = [str(v) for v in result.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        return "\n".join(parts)
    return str(result) if result is not None else ""


@plugin.tool(
    name="hindsight.summarize",
    schema={
        "type": "object",
        "properties": {
            "bank_id": {
                "type": "string",
                "description": "Memory bank id (tenant isolation key)",
            },
            "query": {"type": "string", "description": "摘要聚焦的查询（缺省总结最近记忆）"},
            "top_k": {"type": "integer", "default": 20, "description": "recall 检索条数"},
            "memory_type": {"type": "string", "description": "按记忆类型过滤检索"},
        },
    },
    description="Summarize relevant memories via recall + reflection (SUMMARY injection)",
)
async def hindsight_summarize(
    bank_id: str = "", query: str = "", top_k: int = 20, memory_type: str = ""
) -> dict[str, Any]:
    """摘要注入：recall 检索相关记忆 → reflect 反思整合 → 返回摘要文本。

    供 memory_read 的 SUMMARY 注入经 tool-executor.invoke 跨进程调用；
    失败返回降级 dict（含 error），不抛异常。
    """
    if _client is None:
        return _degrade_dict("summarize")

    try:
        bank = _resolve_bank_id(bank_id)
        # arecall 无 top_k/memory_type 形参（透传曾致 TypeError 被 except 降级
        # 吞掉——2026-08-19 批 C 取证）：memory_type 走 tags 服务端过滤（与
        # hindsight_recall 同款）；检索量由 arecall 的 token 预算驱动，top_k
        # 仅作召回计数的上报上限，不再透传。
        recall_kwargs: dict[str, Any] = {"bank_id": bank, "query": query or ""}
        if memory_type:
            recall_kwargs["tags"] = [f"type:{memory_type}"]
            recall_kwargs["tags_match"] = "any"
        recall = await _client.arecall(**recall_kwargs)
        recalled_count = 0
        recall_results = getattr(recall, "results", None)
        if isinstance(recall_results, list):
            recalled_count = min(len(recall_results), max(0, top_k))
        elif isinstance(recall, dict):
            items = recall.get("results")
            recalled_count = len(items) if isinstance(items, list) else 0

        reflect = await _client.areflect(bank_id=bank, query=query or "总结最近的记忆和经验")
        summary_text = _extract_summary_text(reflect)
        if not summary_text:
            summary_text = "（无摘要内容）"
        return {
            "summary": summary_text,
            "recalled": recalled_count,
            "operation": "summarize",
        }
    except Exception as e:
        logger.warning("[hindsight.summarize] 调用失败 | error=%s", e)
        return {"error": str(e), "operation": "summarize"}


@plugin.tool(
    name="hindsight.delete",
    schema={
        "type": "object",
        "properties": {
            "bank_id": {
                "type": "string",
                "description": "Memory bank id (tenant isolation key)",
            },
            "memory_id": {
                "type": "string",
                "description": "Optional specific memory id to delete; "
                "if omitted deletes the whole bank",
            },
        },
    },
    description="Delete memories from a hindsight bank",
)
async def hindsight_delete(bank_id: str = "", memory_id: str = "") -> dict[str, Any]:
    """Delete a whole bank via adelete_bank (per-memory delete 见 hindsight API)。"""
    if _client is None:
        return _degrade_dict("delete")

    try:
        bank = _resolve_bank_id(bank_id)
        # hindsight_client 暴露 adelete_bank(删整个 bank)
        deleter = getattr(_client, "adelete_bank", None) or getattr(_client, "adelete", None)
        if deleter is None:
            return {"deleted": False, "error": "client has no delete method"}
        result = deleter(bank_id=bank)
        # async 方法调用返回协程——必须 await（callable(coro) 恒 False 的旧
        # 守卫从不 await，删库假成功 + "coroutine never awaited"，2026-08-19
        # 批 C 取证）
        if asyncio.iscoroutine(result):
            result = await result
        return {"deleted": True}
    except Exception as e:
        logger.warning("[hindsight.delete] 调用失败 | error=%s", e)
        return {"deleted": False, "error": str(e)}


@plugin.tool(
    name="hindsight.import_document",
    schema={
        "type": "object",
        "properties": {
            "bank_id": {
                "type": "string",
                "description": "Memory bank id (tenant isolation key)",
            },
            "text": {
                "type": "string",
                "description": "Document text to import (mutually exclusive with file_path)",
            },
            "file_path": {
                "type": "string",
                "description": "Path to a .txt/.md file to import",
            },
            "knowledge_name": {
                "type": "string",
                "description": "Optional knowledge label for the imported doc",
            },
        },
    },
    description="Chunk and import a text document into a hindsight bank",
)
async def hindsight_import_document(
    bank_id: str = "",
    text: str | None = None,
    file_path: str | None = None,
    knowledge_name: str | None = None,
) -> dict[str, Any]:
    """Import a document: read file (txt/md only) or use text, chunk, retain each.

    Rejects non-txt/md file paths with an error dict (no retain).
    """
    if _client is None:
        return _degrade_dict("import_document")

    # 解析文本来源
    raw_text = text
    if file_path:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in _ALLOWED_DOC_EXTS:
            return {
                "error": f"unsupported file type: {ext or '(none)'}. "
                f"Only {_ALLOWED_DOC_EXTS} are allowed.",
                "chunks_imported": 0,
            }
        try:
            with open(file_path, encoding="utf-8") as fh:
                raw_text = fh.read()
        except Exception as e:
            return {"error": f"failed to read file: {e}", "chunks_imported": 0}

    if not raw_text:
        return {
            "error": "no text provided (pass text or file_path)",
            "chunks_imported": 0,
        }

    try:
        bank = _resolve_bank_id(bank_id)
        chunks = _chunk_text(raw_text)
        name = knowledge_name or "document"
        for idx, chunk in enumerate(chunks):
            meta = {
                "memory_type": "semantic",
                "knowledge_name": name,
                "chunk_index": idx,
                "chunk_total": len(chunks),
                "source": "import_document",
            }
            await _client.aretain(bank_id=bank, content=chunk, metadata=meta, tags=["type:semantic"])
        return {"chunks_imported": len(chunks), "knowledge_name": name}
    except Exception as e:
        logger.warning("[hindsight.import_document] 导入失败 | error=%s", e)
        return {"chunks_imported": 0, "knowledge_name": knowledge_name, "error": str(e)}


@plugin.tool(
    name="hindsight.get_documents",
    schema={
        "type": "object",
        "properties": {
            "bank_id": {
                "type": "string",
                "description": "Memory bank id (tenant isolation key)",
            },
            "document_id": {
                "type": "string",
                "description": "Exact document id for single-document fetch "
                "(when given, tags/q are ignored)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Server-side tag filter (e.g. ['review_id:<id>'])",
            },
            "tags_match": {
                "type": "string",
                "default": "any_strict",
                "description": "Tag match mode: any/all/any_strict/all_strict/exact",
            },
            "q": {
                "type": "string",
                "description": "Case-insensitive substring filter on document id",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 100,
            },
        },
    },
    description="Get raw documents (original_text + metadata) from a hindsight bank "
    "(read-only; cold-read exact recovery, not extracted facts)",
)
async def hindsight_get_documents(
    bank_id: str = "",
    document_id: str = "",
    tags: list[str] | None = None,
    tags_match: str = "any_strict",
    q: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """按 bank/tags/document_id 取回文档原文（只读）。

    冷读定向面：recall 返回抽取后事实（world/observation/experience），
    原文 JSON 永不命中、types=['memory'] 422（2026-08-19 真实 API 取证）；
    documents API 才保有 original_text。list_documents 条目不含原文
    （仅 metadata/tags/计数），故逐条 get_document 补齐。

    - document_id 给定 → get_document 直查；404 → 空结果（not found 语义）。
    - 否则 list_documents（服务端 tags 过滤，any_strict = OR 且排除无 tag）
      → 逐条 get_document 补 original_text（单条失败降级返回条目本身）。
    """
    if _client is None:
        return _degrade_dict("get_documents")

    def _to_dict(doc: Any) -> dict[str, Any]:
        if hasattr(doc, "model_dump"):
            doc = doc.model_dump()
        elif not isinstance(doc, dict) and hasattr(doc, "__dict__"):
            doc = vars(doc)
        return dict(doc) if isinstance(doc, dict) else {}

    try:
        bank = _resolve_bank_id(bank_id)
        documents: list[dict[str, Any]] = []
        if document_id:
            try:
                doc = await _client.documents.get_document(
                    bank_id=bank, document_id=document_id
                )
            except Exception as e:
                if getattr(e, "status", None) == 404:
                    return {"documents": [], "total": 0}
                raise
            documents.append(_to_dict(doc))
        else:
            kwargs: dict[str, Any] = {"bank_id": bank, "limit": max(1, limit)}
            if tags:
                kwargs["tags"] = tags
                kwargs["tags_match"] = tags_match or "any_strict"
            if q:
                kwargs["q"] = q
            listing = await _client.documents.list_documents(**kwargs)
            items = list(getattr(listing, "items", None) or [])
            for item in items:
                doc_dict = _to_dict(item)
                doc_id = str(doc_dict.get("id", "") or "")
                if not doc_id:
                    documents.append(doc_dict)
                    continue
                try:
                    full = await _client.documents.get_document(
                        bank_id=bank, document_id=doc_id
                    )
                except Exception:
                    # 单条原文取失败不炸整个列举（降级返回条目本身）
                    documents.append(doc_dict)
                    continue
                merged = _to_dict(full)
                # list 条目字段补缺（full 未提供的键不丢）
                for key, value in doc_dict.items():
                    merged.setdefault(key, value)
                documents.append(merged)
        return {"documents": documents, "total": len(documents)}
    except Exception as e:
        logger.warning("[hindsight.get_documents] 调用失败 | error=%s", e)
        return {"documents": [], "total": 0, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════════════════════


def _load_env_file_keys() -> dict[str, str]:
    """从项目根 .env 直读所需 key（sidecar 自足，不依赖内核 env 覆盖）。

    invoker 的 env_delta_overlay 可能未把 ZHIPU/SILICONFLOW key 泡进 sidecar
    进程环境（实测 memory 曾因 key 缺失报 hindsight not initialized）；
    此处仅补读取，不改写任何内核/全局配置，未找到 key 返回空继续（health
    server 照起，向量写入时才失败）。
    """
    # 插件目录 plugins/shared/system/hindsight_memory → 项目根上溯 4 级
    root = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", ".."))
    env_path = os.path.join(root, ".env")
    out: dict[str, str] = {}
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k in ("ZHIPU_API_KEY", "SILICONFLOW_API_KEY"):
                    out[k] = v
    except OSError:
        pass
    return out


def _apply_llm_env() -> None:
    """把 HINDSIGHT_API_* 配置写入环境变量（hindsight-api 从 env 读取配置）。

    注意:hindsight-api 用的是 HINDSIGHT_API_ 前缀(不是 HINDSIGHT_)。

    默认配置:
      - LLM(事实抽取/反思):GLM glm-5.2 @ 智谱 OpenAI 兼容端点(复用 ZHIPU_API_KEY)
      - Embedding(向量检索):BAAI/bge-m3 @ 硅基流动(免费, 国内直连, OpenAI 兼容)
        → 用 SILICONFLOW_API_KEY(用户需在 .env 配置)
      - Reranker:rrf(Reciprocal Rank Fusion, 无需下载模型, 避免 HF 被墙)
    """
    file_keys = _load_env_file_keys()
    if not os.environ.get("ZHIPU_API_KEY") and file_keys.get("ZHIPU_API_KEY"):
        os.environ["ZHIPU_API_KEY"] = file_keys["ZHIPU_API_KEY"]
    if not os.environ.get("SILICONFLOW_API_KEY") and file_keys.get("SILICONFLOW_API_KEY"):
        os.environ["SILICONFLOW_API_KEY"] = file_keys["SILICONFLOW_API_KEY"]
    # ── LLM(GLM, 复用智谱 key)──
    llm_defaults = {
        "HINDSIGHT_API_LLM_PROVIDER": "openai",
        "HINDSIGHT_API_LLM_BASE_URL": "https://open.bigmodel.cn/api/coding/paas/v4/",
        "HINDSIGHT_API_LLM_MODEL": "glm-5.2",
    }
    for key, default in llm_defaults.items():
        if not os.environ.get(key):
            os.environ[key] = default
    zhipu_key = os.environ.get("ZHIPU_API_KEY", "")
    if zhipu_key and not os.environ.get("HINDSIGHT_API_LLM_API_KEY"):
        os.environ["HINDSIGHT_API_LLM_API_KEY"] = zhipu_key

    # ── Embedding(硅基流动 bge-m3, 免费)──
    # 硅基流动是 OpenAI 兼容端点, Hindsight 的 openai provider 直连
    # 注意:
    # - model 名的 env 是 _OPENAI_MODEL(不是 _MODEL)
    # - bge-m3 固定 1024 维, 不传 dimensions 参数(SiliconFlow 传 dimensions 会 400)
    emb_defaults = {
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": "https://api.siliconflow.cn/v1/",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "BAAI/bge-m3",
    }
    for key, default in emb_defaults.items():
        if not os.environ.get(key):
            os.environ[key] = default
    sf_key = os.environ.get("SILICONFLOW_API_KEY", "")
    if sf_key and not os.environ.get("HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"):
        os.environ["HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"] = sf_key

    # ── Reranker(rrf, 无需模型, 避免 HF 下载)──
    if not os.environ.get("HINDSIGHT_API_RERANKER_PROVIDER"):
        os.environ["HINDSIGHT_API_RERANKER_PROVIDER"] = "rrf"


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """启动 hindsight-api 服务器(pg0 嵌入式 PG)并创建 HTTP 客户端。

    真实架构:hindsight-all-slim 是「客户端 + 服务器」分离设计——
    - hindsight-api(main 入口)启动 HTTP 服务器,内部用 pg0 嵌入式 PostgreSQL
    - hindsight_client.Hindsight(base_url) 是 HTTP 客户端,aretain/arecall 调服务器

    本 on_load:
    1. 配置 LLM/embedding env(GLM OpenAI 兼容端点)
    2. 启动 hindsight-api 子进程(后台,监听 _HINDSIGHT_PORT)
    3. 创建 Hindsight(base_url) 客户端,确保 bank 存在
    4. 任一步失败 → _client=None,所有工具降级,sidecar 不崩
    """
    global _client, _DEFAULT_BANK_ID, _api_process

    config = plugin.get_config() or {}
    # 单键：default_bank_id（与 env HINDSIGHT_DEFAULT_BANK_ID 同名）。
    # 不做 bank_id/tenant_id 别名猜测——三键全仓无配置定义，
    # 猜测链只会掩盖"配置没接上"的事实。
    cfg_default_bank = config.get("default_bank_id")
    if cfg_default_bank:
        _DEFAULT_BANK_ID = str(cfg_default_bank)

    # 数据目录(pg0 数据存放)
    data_dir = (
        config.get("data_dir")
        or os.environ.get("HINDSIGHT_DATA_DIR")
        or os.path.join(_THIS_DIR, "data", "hindsight")
    )
    os.makedirs(data_dir, exist_ok=True)

    # LLM/embedding 环境变量(hindsight-api 从 env 读取配置)
    _apply_llm_env()

    port = int(config.get("port") or os.environ.get("HINDSIGHT_PORT") or _HINDSIGHT_PORT)
    base_url = f"http://127.0.0.1:{port}"

    try:
        import subprocess  # noqa: PLC0415
        import urllib.request as _ur  # noqa: PLC0415

        # 幂等连接既有服务：插件重载/重启时 8420 可能已有健康 hindsight-api
        # （外部/上次实例常驻），直接复用而非再 spawn（端口冲突 + 首启 pg0 建库
        # 慢导致 on_load 轮询 60s 超时判死——2026-08-19 e2e 实测）。
        _already_up = False
        try:
            with _ur.urlopen(f"{base_url}/health", timeout=2) as _resp:
                _already_up = _resp.status == 200
        except Exception:
            _already_up = False
        if _already_up:
            logger.info("[hindsight] 复用既有 hindsight-api 服务 %s", base_url)
        else:
            # 启动 hindsight-api 服务器子进程(pg0 嵌入式 PG + uvicorn HTTP)
            # 用 hindsight 专用 venv（.venv-hindsight）的 python 起子进程：venv 内
            # fastmcp 解析到其匹配的 mcp 1.x（request_ctx 等），与宿主 sidecar 的
            # AgentOS SDK（mcp>=2.0,<3）完全隔离——mcp 1.x/2.0 生态互斥问题正解
            # 在此，而非切内核记忆表保底或 shim 系统环境（2026-08-19 用户纠正）。
            # 2026-08-19 批 C 后为「双 venv」：.venv=SDK 轨（invoker 启动 server.py
            # 用），.venv-hindsight=API 服务器栈（requirements.txt 锁版本）。
            _venv_python = os.path.join(_THIS_DIR, ".venv-hindsight", "Scripts", "python.exe")
            if not os.path.isfile(_venv_python):
                # Unix 布局回退探测（与 invoker resolve_sidecar_command 同款双布局）
                _unix_python = os.path.join(_THIS_DIR, ".venv-hindsight", "bin", "python")
                if os.path.isfile(_unix_python):
                    _venv_python = _unix_python
            if not os.path.isfile(_venv_python):
                logger.error(
                    "[hindsight] API 服务器 venv python 缺失（%s），hindsight-api 无法"
                    "启动。创建方式：uv venv .venv-hindsight --python 3.12 && "
                    "uv pip install -r requirements.txt（依赖清单见 requirements.txt）",
                    _venv_python,
                )
                raise RuntimeError("hindsight venv 未初始化")
            # 子进程 stderr 落盘不 DEVNULL（F6，2026-08-20）：今晚 hindsight-api
            # 连续 exit code=1（13:19/13:27/13:41/13:59 四次全灭），stderr 进
            # DEVNULL 导致崩溃原因完全不可诊断（手动复现才知是 env/pg0 锁类
            # 问题）。追加写 data 目录，崩溃时带 tail 进错误消息。
            _stderr_path = os.path.join(data_dir, "hindsight_api_stderr.log")
            _stderr_file = open(_stderr_path, "ab")  # noqa: SIM115
            _api_process = subprocess.Popen(
                [_venv_python, "-m", "hindsight_api.main",
                 "--port", str(port), "--host", "127.0.0.1"],
                stdout=subprocess.DEVNULL,
                stderr=_stderr_file,
                env=os.environ.copy(),
            )
            logger.info(
                "[hindsight] hindsight-api 子进程已启动 PID=%s port=%s stderr_log=%s",
                _api_process.pid, port, _stderr_path,
            )

            # 等待服务器就绪(轮询 /health,最多 60s)
            import asyncio as _aio  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            for _attempt in range(60):
                await _aio.sleep(1)
                try:
                    with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                        if resp.status == 200:
                            logger.info("[hindsight] 服务器就绪 (attempt=%d)", _attempt + 1)
                            break
                except Exception:
                    # 检查子进程是否已退出：带上 stderr tail（落盘日志最后 800
                    # 字符）——崩溃原因可见，不再只有裸 exit code。
                    if _api_process.poll() is not None:
                        _tail = ""
                        try:
                            _stderr_file.flush()
                            with open(_stderr_path, "rb") as _f:
                                _raw = _f.read()
                            _tail = _raw[-800:].decode("utf-8", errors="replace")
                        except Exception:  # noqa: BLE001
                            pass
                        raise RuntimeError(
                            f"hindsight-api 子进程已退出 code={_api_process.returncode}"
                            f" stderr_tail={_tail!r}"
                        )
            else:
                raise RuntimeError("hindsight-api 服务器 60s 内未就绪")

        # 创建 HTTP 客户端
        from hindsight_client import Hindsight  # type: ignore

        _client = Hindsight(base_url=base_url)

        # 确保默认 bank 存在(幂等)
        try:
            await _client.acreate_bank(bank_id=_DEFAULT_BANK_ID)
        except Exception as be:  # noqa: BLE001
            logger.debug("[hindsight] 创建默认 bank(可能已存在): %s", be)

        logger.info(
            "[hindsight] on_load 完成 | base_url=%s bank=%s model=%s",
            base_url, _DEFAULT_BANK_ID, os.environ.get("HINDSIGHT_API_LLM_MODEL"),
        )
    except Exception as e:
        _client = None
        logger.warning(
            "[hindsight] 初始化失败,sidecar 进入降级模式 | error=%s", e,
        )


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup hindsight client and stop api server on unload."""
    global _client, _api_process
    if _client is not None:
        try:
            aclose = getattr(_client, "aclose", None)
            if callable(aclose):
                await aclose()
            else:
                close = getattr(_client, "close", None)
                if callable(close):
                    close()
        except Exception as e:
            logger.warning("[hindsight] on_unload client 清理失败 | error=%s", e)
        finally:
            _client = None
    # 终止 hindsight-api 子进程
    if _api_process is not None:
        try:
            _api_process.terminate()
            _api_process.wait(timeout=10)
        except Exception as e:
            logger.warning("[hindsight] on_unload 终止 api 子进程失败 | error=%s", e)
            try:
                _api_process.kill()
            except Exception:
                pass
        finally:
            _api_process = None




# ═══════════════════════════════════════════════════════════
# HTTP 展示面（前端记忆页消费，B3 收口：记忆数据接成熟 Hindsight）
# ═══════════════════════════════════════════════════════════

def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/hindsight_memory_service/** (memory frontend)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：recall（记忆回顾）/ stats（降级状态）。"""
    q = query or {}
    try:
        if path == "/ext/hindsight_memory_service/recall" and method == "GET":
            result = await hindsight_recall(
                query=q.get("query", ""),
                top_k=max(1, min(100, int(q.get("limit", 10)))),
            )
            return _ok(_json_response(result))

        if path == "/ext/hindsight_memory_service/stats" and method == "GET":
            initialized = _client is not None
            return _ok(
                _json_response(
                    {
                        "bank_id": _resolve_bank_id(q.get("bank_id")),
                        "initialized": initialized,
                        "backend": "hindsight",
                    }
                )
            )

        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:
        logger.exception("hindsight http.handle failed: %s", exc)
        return {"success": False, "error": str(exc), "data": _json_response({"error": str(exc)}, 500)}


if __name__ == "__main__":
    plugin.run()
