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
import json
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

if TYPE_CHECKING:
    import subprocess

# 本地模块可达性（插件目录）+ 共享层自举（http_json 经 plugins/shared/ 裸名导入）。
_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path
# 测试接缝：test_hindsight_server_lifecycle monkeypatch 此名定位隔离根，
# 测试接缝：test_hindsight_server_lifecycle monkeypatch 此名定位隔离根，
# 路径消费统一走 _THIS_DIR（勿内联为 _paths.plugin_dir）。
_THIS_DIR = _paths.plugin_dir

from http_json import (  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response,
    ok as _ok,
    parse_multipart as _parse_multipart,
)
from proc_tree import kill_process_tree  # noqa: E402  （共享根经 bootstrap 入 path）

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

# 导入切块并发 retain 上限（服务端并行抽取，实测 ~16s/chunk 串行、并发 3 提速
# 2.4x；大文档串行必然撞工具 300s 超时）
_IMPORT_CONCURRENCY = 6

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


def _chunk_text(text: str, chunk_size: int = _CHUNK_SIZE) -> list[str]:
    """按字符数切分文本（朴素滑窗，~chunk_size 字符/块）。

    Args:
        text: 原始文本
        chunk_size: 每块字符数上限

    Returns:
        文本块列表（空文本返回 []）
    """
    # 算式单一来源在 knowledge_base._chunk_text（模块名仓内唯一，裸名导入无抢占面；
    # 懒导入避免装载环——knowledge_base 不反向依赖本模块）
    import knowledge_base as kb  # noqa: PLC0415

    return kb._chunk_text(text, chunk_size)


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
            "document_id": {
                "type": "string",
                "description": "Optional document id (returned as-is for targeted delete/update)",
            },
            "retain_async": {
                "type": "boolean",
                "default": False,
                "description": "Async background retain (slow; recall misses until extraction finishes)",
            },
            "update_mode": {
                "type": "string",
                "enum": ["replace", "append"],
                "description": "Optional update semantics for same document_id (replace/append)",
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
    document_id: str = "",
    retain_async: bool = False,
    operation_id: str | None = None,
    update_mode: str | None = None,
) -> dict[str, Any]:
    """Store a memory entry via hindsight client.aretain.

    memory_type 同时写入 tags(服务端过滤)和 metadata(客户端读取)。

    时序语义（2026-08-22 真机实证修正）：默认**同步 retain**——
    retain_async=True 时服务端后台抽取需数秒完成，期间 recall 检索不到
    刚写入的记忆（LLM 编排下 store→retrieve 间隔远小于抽取耗时，8-22
    测试 4 连 store 后 4 连 retrieve 全空的根因之一）。同步 retain 写入后
    立即可召回。

    document_id：调用方给定时代入作为文档 id 并原样回传（memory 工具层
    delete/update 的定向通路；真机实证 delete_document 级联删除该文档全部
    记忆单元）。operation_id 仅保留给显式 retain_async 的幂等场景。
    """
    import uuid  # noqa: PLC0415

    if _client is None:
        return _degrade_dict("retain")

    try:
        bank = _resolve_bank_id(bank_id)
        meta = dict(metadata or {})
        meta.setdefault("memory_type", memory_type)
        # tags：type tag（recall/reflect 服务端过滤）+ 调用方 tags 提升。
        # HindsightBackend.add 把 IMemoryBackend.add 的 tags 序列化进
        # metadata["tags"]（hindsight aretain 的 metadata 是 dict[str,str]
        # pydantic 校验，list 值必炸）；此处还原为 hindsight 真实 tags，
        # 供 list_documents/recall 服务端精确过滤
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

        call_kwargs: dict[str, Any] = {
            "bank_id": bank,
            "content": content,
            "metadata": meta,
            "tags": tags,
            "retain_async": retain_async,
        }
        if document_id:
            call_kwargs["document_id"] = document_id
            # update_mode 仅在同 document_id 写入时才有意义（文档级替换/追加）
            if update_mode:
                call_kwargs["update_mode"] = update_mode
        if retain_async:
            # 异步写入的幂等 id（同步 retain 下 operation_id 被客户端丢弃，
            # 传了只会造成"原样回传假 id"）
            call_kwargs["operation_id"] = operation_id or str(uuid.uuid4())
        result = await _client.aretain(**call_kwargs)
        # 同步 retain：文档 id（调用方给定/服务端生成）即真实落库锚点；
        # 异步 retain：回传 operation_id（调用方幂等 id）。
        mem_id = ""
        if document_id:
            mem_id = document_id
        elif retain_async:
            mem_id = str(getattr(result, "operation_id", "") or "")
        else:
            # 同步且无 document_id：aretain 给真实 id 则透传；客户端未拿到
            # id 时宁缺毋假——operation_id 是调用方幂等 id 不是落库锚点，
            # 顶替回传会让 delete/update 定向通路打到不存在的锚点。
            mem_id = str(getattr(result, "id", "") or "")
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
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional server-side tag filter",
            },
            "tags_match": {
                "type": "string",
                "default": "any",
                "description": "Tag match mode: any/all/any_strict/all_strict/exact",
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
    tags: list[str] | None = None,
    tags_match: str = "any",
) -> dict[str, Any]:
    """Recall memories via hindsight client.arecall.

    memory_type 用作 tags 服务端过滤(type:<memory_type>)；tags/tags_match
    为调用方（memory 工具层 filter.tags）的显式标签过滤——服务端 tags 过滤
    是精确匹配面，比语义召回可靠（2026-08-22 真机实证）。

    空 query 直接拒绝（hindsight 服务端对空 query 必 422：
    "query must contain at least one word character"——list 工具先于查询判空）。
    """
    if _client is None:
        return _degrade_dict("recall")
    if not query or not query.strip():
        return {"results": [], "total": 0, "error": "query is required (empty query rejected)"}

    try:
        bank = _resolve_bank_id(bank_id)
        kwargs: dict[str, Any] = {"bank_id": bank, "query": query}
        # memory_type（type: 前缀）与调用方 tags 合并后一次投给服务端——
        # 覆盖式赋值会在同传时丢掉 memory_type 过滤（pipeline chunk 检索
        # 同时带 memory_type=chunk + tags=[pipeline:{id}] 的场景）。
        merged_tags: list[str] = []
        if memory_type:
            merged_tags.append(f"type:{memory_type}")
        if tags:
            merged_tags.extend(tags)
        if merged_tags:
            kwargs["tags"] = merged_tags
            kwargs["tags_match"] = tags_match or "any"

        result = await _client.arecall(**kwargs)
        # RecallResponse (hindsight 0.9.1) 召回结果主字段是 results
        # （每条含 id/text/type/score 等）。
        items: list[dict[str, Any]] = []
        collection = getattr(result, "results", None)
        if collection:
            for item in collection:
                if hasattr(item, "model_dump"):
                    items.append(item.model_dump())
                elif isinstance(item, dict):
                    items.append(item)
                else:
                    items.append({"content": str(item)})
        # 统一字段名:results 里的 text → content(上层期望 content 字段)
        for item in items:
            if "text" in item and "content" not in item:
                item["content"] = item.pop("text")
        # 相关度排序 + top_k 截断：RecallResult 的分数在嵌套 scores.final
        # （顶层无 score 键，读取归各消费方映射层），召回条数由 token 预算
        # 驱动与 top_k 无关——按 final 降序排并截断 top_k，「检索数量」契约
        # 才成立。
        limit = max(1, int(top_k or 5))

        def _final_score(item: dict[str, Any]) -> float:
            nested = item.get("scores")
            final = nested.get("final") if isinstance(nested, dict) else None
            return float(final) if isinstance(final, (int, float)) else 0.0

        items.sort(key=_final_score, reverse=True)
        items = items[:limit]
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

    经 tool-executor.invoke 跨进程调用；失败返回降级 dict（含 error），不抛异常。
    """
    if _client is None:
        return _degrade_dict("summarize")

    try:
        bank = _resolve_bank_id(bank_id)
        # arecall 无 top_k/memory_type 形参（透传会 TypeError 被 except 降级
        # 吞掉）：memory_type 走 tags 服务端过滤（与 hindsight_recall 同款）；
        # 检索量由 arecall 的 token 预算驱动，top_k
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
    """Delete memories from a hindsight bank.

    - memory_id 是文档 id：documents.delete_document 级联删除该文档及其全部
      记忆单元（真删除通路）。
    - memory_id 是记忆单元 id（retrieve 返回 RecallResult 的 id，documents
      直删 404）：按单元解析父文档后级联删除——残留条目可清理。
    - memory_id 缺省：删整个 bank（adelete_bank，既有语义保留）。
    """
    if _client is None:
        return _degrade_dict("delete")

    try:
        bank = _resolve_bank_id(bank_id)
        if memory_id:
            documents_api = getattr(_client, "documents", None)
            if documents_api is None or not hasattr(documents_api, "delete_document"):
                return {"deleted": False, "error": "client has no documents.delete_document"}
            # 先按文档直删（delete_document 级联删 document + 全部记忆单元）
            try:
                resp = await documents_api.delete_document(
                    bank_id=bank, document_id=memory_id
                )
                deleted = _deleted_from_response(resp)
                doc_error: str | None = None
            except Exception as e:
                deleted = False
                doc_error = str(e)
            if deleted:
                return {"deleted": True, "memory_id": memory_id}
            # 单元 id 回退：retrieve 给的是记忆单元 id，documents API 只认
            # 文档 id——按单元解析父文档后级联删除（残留条目可清理）。
            doc_id = await _resolve_unit_document_id(bank, memory_id)
            if doc_id:
                resp = await documents_api.delete_document(
                    bank_id=bank, document_id=doc_id
                )
                return {
                    "deleted": _deleted_from_response(resp),
                    "memory_id": memory_id,
                    "document_id": doc_id,
                }
            return {
                "deleted": False,
                "memory_id": memory_id,
                "error": (
                    f"memory id 既非文档也查不到所属记忆单元（不存在或已删除）"
                    f"| detail={doc_error or 'not found'}"
                ),
            }
        # 无 memory_id：删整个 bank
        deleter = getattr(_client, "adelete_bank", None) or getattr(_client, "adelete", None)
        if deleter is None:
            return {"deleted": False, "error": "client has no delete method"}
        result = deleter(bank_id=bank)
        # async 方法调用返回协程——必须 await（callable(coro) 恒 False 的
        # 守卫不会 await，删库会假成功）
        if asyncio.iscoroutine(result):
            result = await result
        return {"deleted": True}
    except Exception as e:
        logger.warning("[hindsight.delete] 调用失败 | error=%s", e)
        return {"deleted": False, "error": str(e)}


def _deleted_from_response(resp: Any) -> bool:
    """从 DeleteDocumentResponse 取语义值 success。

    模型/dict 读 success 键；None 及其它形态按客户端契约视为成功
    （204 风格无体删除响应）。
    """
    if hasattr(resp, "model_dump"):
        payload = resp.model_dump()
        return bool(payload.get("success", True))
    if isinstance(resp, dict):
        return bool(resp.get("success", True))
    return True


async def _resolve_unit_document_id(bank_id: str, memory_id: str) -> str | None:
    """记忆单元 id → 父文档 id（documents API 只认文档 id，删除回退用）。

    get_memory 返回形态 tolerant（pydantic model_dump / dict / __dict__）；
    能力缺失或解析失败返回 None——删除面照常诚实失败，不吞错误。
    """
    memory_api = getattr(_client, "memory", None)
    if memory_api is None:
        return None
    getter = getattr(memory_api, "get_memory", None)
    if not callable(getter):
        return None
    try:
        unit = await getter(bank_id=bank_id, memory_id=memory_id)
    except Exception as exc:  # noqa: BLE001 — 解析失败按"不可解析"降级，删除面照常诚实失败
        logger.debug("[hindsight] unit 文档 id 解析失败（按不可解析处理）| bank=%s memory=%s error=%s", bank_id, memory_id, exc)
        return None
    if hasattr(unit, "model_dump"):
        unit = unit.model_dump()
    elif not isinstance(unit, dict):
        unit = vars(unit) if hasattr(unit, "__dict__") else {}
    doc_id = str((unit or {}).get("document_id", "") or "")
    return doc_id or None


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
        chunks = [c for c in _chunk_text(raw_text) if c.strip()]
        name = knowledge_name or "document"
        total = len(chunks)
        sem = asyncio.Semaphore(_IMPORT_CONCURRENCY)

        async def _retain_chunk(idx: int, chunk: str) -> None:
            meta = {
                "memory_type": "semantic",
                "knowledge_name": name,
                # MemoryItem.metadata 是 dict[str,str] pydantic 校验面——
                # 数字/布尔值必 422，全部字符串化
                "chunk_index": str(idx),
                "chunk_total": str(total),
                "source": "import_document",
            }
            async with sem:
                await _client.aretain(
                    bank_id=bank, content=chunk, metadata=meta,
                    tags=["type:semantic"],
                )

        await asyncio.gather(*(_retain_chunk(i, c) for i, c in enumerate(chunks)))
        return {"chunks_imported": total, "knowledge_name": name}
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


def _project_root() -> str:
    """插件目录 plugins/shared/system/hindsight_memory → 项目根上溯 4 级。"""
    return os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", ".."))


def _load_env_file() -> dict[str, str]:
    """从项目根 .env 直读全量 key=value（sidecar 自足，不依赖内核 env 覆盖）。

    供应商 key 解析链（_resolve_env_ref）：进程环境优先，.env 兜底——invoker 的
    env_delta_overlay 不保证把供应商 key 泡进 sidecar 进程环境。此处仅补读取，
    不改写任何内核/全局配置，未找到 key 返回空继续（health server 照起，
    向量写入时才失败）。
    """
    env_path = os.path.join(_project_root(), ".env")
    out: dict[str, str] = {}
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as exc:
        logger.debug("[hindsight] .env 读取失败（按无附加配置处理）| path=%s error=%s", env_path, exc)
    return out


def _load_llm_yaml() -> dict[str, Any] | None:
    """读系统模型注册真值 config/models/llm.yaml（LLM 设置页写回的单一真值）。

    缺失/损坏返回 None，调用方按段降级（不阻塞启动）。
    """
    import yaml  # noqa: PLC0415  # SDK 传递依赖，插件 venv 必有

    try:
        with open(
            os.path.join(_project_root(), "config", "models", "llm.yaml"), encoding="utf-8"
        ) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "[hindsight] llm.yaml 读取失败（按段降级，本段返回 None）: %s | %s",
            os.path.join(_project_root(), "config", "models", "llm.yaml"),
            exc,
        )
        return None
    return data if isinstance(data, dict) else None


def _resolve_env_ref(ref: str, env_file: dict[str, str]) -> str:
    """解析 ``${VAR}`` 凭证引用：进程 env 优先，项目根 .env 兜底；非引用式原样返回。"""
    if ref.startswith("${") and ref.endswith("}"):
        var = ref[2:-1]
        return os.environ.get(var) or env_file.get(var, "")
    return ref


def _resolve_model_endpoint(
    model_id: str, llm_cfg: dict[str, Any], env_file: dict[str, str]
) -> tuple[str, str, str, str] | None:
    """llm.yaml 模型 id → (api_base, model_name, 已解析 key, provider type)。

    解析链 models[id] → providers[entry.provider]（keys[0].api_key 为 ${VAR}
    引用）；id 或 provider 条目缺失返回 None。api_base 与主 LLM 路径同口径
    （llm/_config_models.py）：模型条目优先，provider 条目回退——只配在
    provider 上的端点（如 ollama-oline）否则解析为空串。
    """
    entry = (llm_cfg.get("models") or {}).get(model_id)
    provider = (llm_cfg.get("providers") or {}).get(str((entry or {}).get("provider")))
    if not isinstance(entry, dict) or not isinstance(provider, dict):
        return None
    keys = provider.get("keys") or []
    key_ref = str(keys[0].get("api_key", "")) if keys and isinstance(keys[0], dict) else ""
    return (
        str(entry.get("api_base", "") or provider.get("api_base", "")),
        str(entry.get("model_name", "")),
        _resolve_env_ref(key_ref, env_file),
        str(provider.get("type", "")),
    )


def _manifest_model_config() -> dict[str, str]:
    """读本插件 manifest 内联配置字段（config_files[].fields 的 name→default）。

    单一真值裁定（2026-09-02）：字段值内联于 manifest，设置页保存写回本文件；
    manifest 变更经 watcher 触发 respawn，下次进程启动即读到新值。
    """
    try:
        with open(os.path.join(_THIS_DIR, "plugin.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # 配置缺省有内置兜底，不阻断启动；但缺配置与"无配置字段"必须可区分排查
        logger.warning("plugin.json 读取失败，配置字段回退内置默认 | error=%s", e)
        return {}
    out: dict[str, str] = {}
    for entry in manifest.get("config_files") or []:
        for field in entry.get("fields") or []:
            name = field.get("name")
            if name:
                out[str(name)] = str(field.get("default") or "")
    return out


# 非 OpenAI 兼容端点的 provider type（协议不同，openai provider 直连必失败）。
# zai/minimax 等 type 虽非 "openai"，端点实测 OpenAI 兼容（探针 2026-09-07）。
_NON_OPENAI_PROVIDER_TYPES = frozenset({"anthropic", "gemini"})


def _apply_llm_env() -> None:
    """把 HINDSIGHT_API_* 配置写入环境变量（hindsight-api 从 env 读取配置）。

    注意:hindsight-api 用的是 HINDSIGHT_API_ 前缀(不是 HINDSIGHT_)。

    配置真值 = 系统 LLM 注册（config/models/llm.yaml）+ 本插件配置字段选型
    （ADR 2026-09-07-hindsight-llm-follow-system）。每段独立解析，优先级：

      显式 HINDSIGHT_API_* env（逃生口，逐键最高优先）
        > 本插件配置 llm_model / embedding_model（非空时）
        > 系统默认 defaults.chat / defaults.embedding

    provider type 为 anthropic/gemini（非 OpenAI 兼容端点）时警告并跳过该段；
    缺 llm.yaml / 模型 id 无效 / key 未配置均警告不阻塞（health server 照起，
    调用期才失败）。Reranker 固定 rrf（Reciprocal Rank Fusion，无需下载模型，
    避免 HF 被墙）。
    """
    log = logging.getLogger(__name__)
    env_file = _load_env_file()
    llm_cfg = _load_llm_yaml()
    if llm_cfg is None:
        log.warning(
            "hindsight 配置: config/models/llm.yaml 缺失或损坏，LLM/嵌入段不注入"
            "（可用 HINDSIGHT_API_* 显式指定）"
        )
    else:
        defaults = llm_cfg.get("defaults") or {}
        model_cfg = _manifest_model_config()
        sections = (
            (
                "LLM",
                "llm_model",
                "chat",
                {
                    "HINDSIGHT_API_LLM_PROVIDER": "openai",
                    "HINDSIGHT_API_LLM_BASE_URL": "{api_base}",
                    "HINDSIGHT_API_LLM_MODEL": "{model_name}",
                    "HINDSIGHT_API_LLM_API_KEY": "{api_key}",
                },
            ),
            (
                "Embedding",
                "embedding_model",
                "embedding",
                {
                    "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
                    "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": "{api_base}",
                    "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "{model_name}",
                    "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "{api_key}",
                },
            ),
        )
        for section, cfg_field, sys_default, envs in sections:
            chosen = (
                model_cfg.get(cfg_field, "").strip()
                or str(defaults.get(sys_default) or "").strip()
            )
            resolved = (
                _resolve_model_endpoint(chosen, llm_cfg, env_file) if chosen else None
            )
            if resolved is None:
                if chosen:
                    log.warning(
                        "hindsight %s段: 模型 id %r 在 llm.yaml 无条目，跳过注入",
                        section,
                        chosen,
                    )
                continue
            api_base, model_name, api_key, provider_type = resolved
            if provider_type in _NON_OPENAI_PROVIDER_TYPES:
                log.warning(
                    "hindsight %s段: 模型 %s 的 provider type=%r 非 OpenAI 兼容端点，跳过注入",
                    section,
                    chosen,
                    provider_type,
                )
                continue
            if not api_base:
                log.warning(
                    "hindsight %s段: 模型 %s 的 api_base 在模型条目与 provider 条目均为空，"
                    "BASE_URL 不注入——OpenAI 兼容客户端将回落公网默认端点，调用期必失败",
                    section,
                    chosen,
                )
            for env_key, tpl in envs.items():
                if os.environ.get(env_key):
                    continue
                value = {"{api_base}": api_base, "{model_name}": model_name, "{api_key}": api_key}.get(tpl, tpl)
                if value:
                    os.environ[env_key] = value
    if not os.environ.get("HINDSIGHT_API_RERANKER_PROVIDER"):
        os.environ["HINDSIGHT_API_RERANKER_PROVIDER"] = "rrf"


def _hindsight_api_up(base_url: str) -> bool:
    """探测既有 hindsight-api /health 是否就绪（幂等复用常驻实例）。"""
    import urllib.request  # noqa: PLC0415

    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


# hindsight-api stderr 轮转上限（10MB × 3 备份）：uvicorn/pg0 日志无界
# 追加是磁盘泄漏面，经父侧排空线程 + RotatingFileHandler 收敛。
_STDERR_ROTATE_MAX_BYTES = 10 * 1024 * 1024
_STDERR_ROTATE_BACKUP_COUNT = 3


def _build_stderr_handler(log_path: str) -> Any:
    """构造 hindsight-api stderr 轮转 handler（10MB × 3 备份）。

    单独成函数便于测试断言轮转配置（maxBytes/backupCount）生效。
    """
    from logging.handlers import RotatingFileHandler  # noqa: PLC0415

    handler = RotatingFileHandler(
        log_path,
        maxBytes=_STDERR_ROTATE_MAX_BYTES,
        backupCount=_STDERR_ROTATE_BACKUP_COUNT,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _spawn_stderr_drain(process: Any, stderr_path: str) -> None:
    """排空子进程 stderr → 父进程侧 RotatingFileHandler（daemon 线程）。

    为什么不把文件 fd 直接交给子进程：子进程持 fd 自写完全绕过父进程
    handler，轮转永不触发、磁盘无界。PIPE + 父侧排空线程是轮转约束
    真实生效的唯一形态；handler 逐条 flush，崩溃 tail 诊断照常可用。
    """
    drain_logger = logging.getLogger("hindsight_api.stderr")
    drain_logger.setLevel(logging.INFO)
    drain_logger.propagate = False

    def _drain() -> None:
        handler = _build_stderr_handler(stderr_path)
        drain_logger.addHandler(handler)
        try:
            stream = process.stderr
            if stream is not None:
                for raw in iter(stream.readline, b""):
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    if text:
                        drain_logger.info("%s", text)
        except Exception as e:  # noqa: BLE001 — 排空线程自终止，失败留痕
            drain_logger.warning("stderr 排空异常终止: %s", e)
        finally:
            drain_logger.removeHandler(handler)
            handler.close()

    import threading  # noqa: PLC0415

    threading.Thread(target=_drain, name="hindsight-stderr-drain", daemon=True).start()


def _start_api_server(port: int, data_dir: str) -> tuple[subprocess.Popen[bytes], str]:
    """以 hindsight 专用 venv 启动 hindsight-api 子进程。

    返回 (进程句柄, stderr 落盘路径)；venv python 缺失时抛 RuntimeError。
    stderr 走 PIPE 由父侧排空线程写入轮转日志（10MB×3），不再交 fd 给子
    进程（自写绕过 handler → 轮转失效、磁盘无界）。
    """
    import subprocess  # noqa: PLC0415

    # 启动 hindsight-api 服务器子进程(pg0 嵌入式 PG + uvicorn HTTP)
    # 用 hindsight 专用 venv（.venv-hindsight）的 python 起子进程：venv 内
    # fastmcp 解析到其匹配的 mcp 1.x（request_ctx 等），与宿主 sidecar 的
    # AgentOS SDK（mcp>=2.0,<3）完全隔离——mcp 1.x/2.0 生态互斥问题正解
    # 在此。当前为「双 venv」布局：.venv=SDK 轨（invoker 启动 server.py
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
    # 子进程 stderr 落盘不 DEVNULL：stderr 进 DEVNULL 会令崩溃原因
    # 完全不可诊断。PIPE → 父侧排空线程写轮转日志，崩溃时带 tail 进错误
    # 消息（handler 逐条 flush，tail 读取无延迟窗口）。
    _stderr_path = os.path.join(data_dir, "hindsight_api_stderr.log")
    process = subprocess.Popen(
        [_venv_python, "-m", "hindsight_api.main",
         "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    _spawn_stderr_drain(process, _stderr_path)
    logger.info(
        "[hindsight] hindsight-api 子进程已启动 PID=%s port=%s stderr_log=%s",
        process.pid, port, _stderr_path,
    )
    return process, _stderr_path


async def _wait_api_ready(
    base_url: str,
    process: subprocess.Popen[bytes],
    stderr_path: str,
) -> None:
    """轮询 /health 直至就绪（最多 60s）；子进程提前退出则带 stderr tail 抛错。

    stderr 由父侧排空线程写轮转日志（逐条 flush），崩溃 tail 直接读落盘
    路径；本函数不持有任何文件句柄。
    """
    import asyncio as _aio  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    for _attempt in range(60):
        await _aio.sleep(1)
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    logger.info("[hindsight] 服务器就绪 (attempt=%d)", _attempt + 1)
                    return
        except Exception:
            # 检查子进程是否已退出：带上 stderr tail（落盘日志最后 800
            # 字符）——崩溃原因可见，不再只有裸 exit code。
            if process.poll() is not None:
                _tail = ""
                try:
                    with open(stderr_path, "rb") as _f:
                        _tail = _f.read()[-800:].decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"hindsight-api 子进程已退出 code={process.returncode}"
                    f" stderr_tail={_tail!r}"
                )
    raise RuntimeError("hindsight-api 服务器 60s 内未就绪")


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
        # 幂等连接既有服务：插件重载/重启时端口可能已有健康 hindsight-api
        # （外部/上次实例常驻），直接复用而非再 spawn（端口冲突 + 首启 pg0
        # 建库慢会拖垮 on_load 轮询超时）。
        if _hindsight_api_up(base_url):
            logger.info("[hindsight] 复用既有 hindsight-api 服务 %s", base_url)
        else:
            _api_process, _stderr_path = _start_api_server(port, data_dir)
            await _wait_api_ready(base_url, _api_process, _stderr_path)

        from hindsight_client import Hindsight  # type: ignore  # 第三方 hindsight_client 无类型 stub

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
    # 终止 hindsight-api 子进程树：pg0 嵌入式 PG 与 uvicorn 的 helper 进程
    # 可能以子进程形态残留，terminate 单进程会留孤儿——共享杀树件整棵清
    # （失败清单非空 = 有残留，warn 留痕可排查）。
    if _api_process is not None:
        failures = kill_process_tree(_api_process.pid)
        if failures:
            logger.warning(
                "[hindsight] on_unload 进程树清理失败（可能残留进程） | pid=%s | failures=%s",
                _api_process.pid,
                failures,
            )
        _api_process = None




# ═══════════════════════════════════════════════════════════
# HTTP 展示面（前端记忆页消费，B3 收口：记忆数据接成熟 Hindsight）
# 响应封装/请求解析助手（json_response/_ok/_decode_body/_parse_multipart）：
# 公共实现 plugins/shared/http_json.py（文件头已导入）。
# ═══════════════════════════════════════════════════════════


# ── memory 域：IMemoryBackend 懒构建注入 ────────────────────────────────────

_memory_backend: Any | None = None
_memory_backend_attempted = False


def _ensure_memory_backend() -> Any | None:
    """构建并缓存 IMemoryBackend（幂等）；能力缺失/构建失败时返回 None。

    与 channel_api server.py 同款懒注入：tool-executor 能力注入经    wiring.build_memory_backend 构造 HindsightBackend（唯一后端），
    None 时 memory 域端点空结果降级。
    """
    global _memory_backend, _memory_backend_attempted
    if not _memory_backend_attempted:
        _memory_backend_attempted = True
        try:
            from wiring import build_memory_backend  # noqa: PLC0415

            _memory_backend = build_memory_backend(plugin)
        except Exception as e:  # noqa: BLE001
            logger.warning("[hindsight] 记忆后端构建失败 | error=%s", e)
    return _memory_backend


def _kberr_response(exc: Exception) -> dict[str, Any]:
    """把知识库/记忆域业务异常（KBError/MemoryAPIError，含 status_code）转 HTTP 响应。"""
    status = int(getattr(exc, "status_code", 500) or 500)
    message = getattr(exc, "message", None) or str(exc)
    return _ok(_json_response({"detail": message}, status))


async def _handle_memory_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """memory 域分发：/ext/hindsight_memory_service/memory/** → routes_memory 业务函数。

    分发前懒注入记忆后端（幂等；无能力时保持 None → 路由空结果降级）。
    路径语义与原 /ext/channel_api/memory/** 逐项对齐（前端 memory.ts
    消费同一响应形态）。
    """
    import routes_memory as rmm  # noqa: PLC0415

    rmm.set_memory_backend(_ensure_memory_backend())

    prefix = "/ext/hindsight_memory_service/memory"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a memory path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/search" / "/episodes" / "/{memory_id}" ...

    def _qint(key: str, default: int) -> int:
        try:
            return int(query[key]) if key in query else default
        except (TypeError, ValueError):
            return default

    try:
        # GET ""（list，query: memory_type/limit/offset）
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(await rmm.list_memories(
                memory_type=query.get("memory_type"),
                limit=_qint("limit", 20),
                offset=_qint("offset", 0),
            )))
        # GET /search（query: query/top_k/method）
        if sub == "/search" and method == "GET":
            return _ok(_json_response(await rmm.search_memories(
                query=query.get("query", ""),
                top_k=_qint("top_k", 5),
                method=query.get("method", "keyword"),
            )))
        # POST /search（body: query/top_k）
        if sub == "/search" and method == "POST":
            body = _decode_body(raw_body) or None
            return _ok(_json_response(await rmm.search_memories_post(body)))
        # GET /episodes（query: page/page_size）
        if sub == "/episodes" and method == "GET":
            return _ok(_json_response(await rmm.list_episodes(
                page=_qint("page", 1), page_size=_qint("page_size", 20),
            )))
        # GET /episodes/{episode_id}
        if sub.startswith("/episodes/") and method == "GET":
            episode_id = sub[len("/episodes/"):]
            return _ok(_json_response(await rmm.get_episode(episode_id)))
        # GET /semantic
        if sub == "/semantic" and method == "GET":
            return _ok(_json_response(await rmm.list_semantic()))
        # POST /consolidate
        if sub == "/consolidate" and method == "POST":
            return _ok(_json_response(await rmm.consolidate_memory()))
        # GET /stats
        if sub == "/stats" and method == "GET":
            return _ok(_json_response(await rmm.get_memory_stats()))
        # GET /{memory_id}（动态路径，放最后）
        if sub.startswith("/") and method == "GET" and "/" not in sub[1:]:
            memory_id = sub[1:]
            return _ok(_json_response(await rmm.get_memory(memory_id)))
        # DELETE /{memory_id}
        if sub.startswith("/") and method == "DELETE" and "/" not in sub[1:]:
            memory_id = sub[1:]
            return _ok(_json_response(await rmm.delete_memory(memory_id)))

        logger.warning("memory http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code"):
            return _kberr_response(exc)
        logger.error("memory http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_kb_domain(
    path: str, method: str, raw_body: str, query: dict[str, str], headers: dict[str, str] | None
) -> dict[str, Any]:
    """knowledge-base 域分发：/ext/hindsight_memory_service/knowledge-base/**。

    真实现（上传/分块/向量化/分类/标签/统计/check/条目/检索）。分发前注入
    hindsight client（_client）——knowledge_base 模块直接复用本 sidecar 的
    客户端（同进程同事件循环）。
    """
    import base64 as _b64  # noqa: PLC0415

    import knowledge_base as kb  # noqa: PLC0415

    kb.set_client(_client)

    prefix = "/ext/hindsight_memory_service/knowledge-base"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a knowledge-base path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/stats" / "/upload" / "/search" / "/{item_id}" ...

    def _qint(key: str, default: int) -> int:
        try:
            return int(query[key]) if key in query else default
        except (TypeError, ValueError):
            return default

    try:
        # GET ""（列表，返回数组——KnowledgeBasePage 消费形态）
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(kb.list_items()))
        # GET /stats
        if sub == "/stats" and method == "GET":
            return _ok(_json_response(kb.get_stats()))
        # GET /check
        if sub == "/check" and method == "GET":
            return _ok(_json_response(await kb.check_available()))
        # GET /search（query: query/top_k/category/tag）
        if sub == "/search" and method == "GET":
            return _ok(_json_response(await kb.search(
                query=query.get("query", ""),
                top_k=_qint("top_k", 10),
                category=query.get("category"),
                tag=query.get("tag"),
            )))
        # POST /upload（multipart/form-data，file 字段）
        if sub == "/upload" and method == "POST":
            try:
                body_bytes = _b64.b64decode(raw_body) if raw_body else b""
            except Exception as exc:  # noqa: BLE001
                return _ok(_json_response({"error": f"invalid upload body: {exc}"}, 400))
            content_type = ""
            for k, v in (headers or {}).items():
                if isinstance(k, str) and k.lower() == "content-type" and v:
                    content_type = str(v)
                    break
            if "multipart/form-data" not in content_type:
                return _ok(_json_response(
                    {"error": "upload requires multipart/form-data", "content_type": content_type}, 400,
                ))
            try:
                fields = _parse_multipart(content_type, body_bytes)
            except Exception as exc:  # noqa: BLE001
                return _ok(_json_response({"error": f"multipart parse failed: {exc}"}, 400))
            file_field = fields.get("file")
            if not isinstance(file_field, dict):
                return _ok(_json_response({"error": "missing 'file' field in multipart"}, 400))
            filename = file_field.get("filename") or "upload"
            mime_type = file_field.get("content_type") or "application/octet-stream"
            data = file_field.get("data") or b""
            return _ok(_json_response(await kb.upload_document(
                filename=str(filename), content=data, mime_type=str(mime_type),
            )))
        # GET/POST /categories（列表 / 创建）
        if sub == "/categories" and method == "GET":
            return _ok(_json_response(kb.list_categories()))
        if sub == "/categories" and method == "POST":
            body = _decode_body(raw_body) or {}
            return _ok(_json_response(kb.create_category(str(body.get("name", "")))))
        # DELETE /categories/{name}
        if sub.startswith("/categories/") and method == "DELETE":
            name = sub[len("/categories/"):]
            return _ok(_json_response(kb.delete_category(name)))
        # GET /tags
        if sub == "/tags" and method == "GET":
            return _ok(_json_response(kb.list_tags()))
        # GET /{item_id}（动态路径，放固定路径之后）
        if sub.startswith("/") and method == "GET" and "/" not in sub[1:]:
            item_id = sub[1:]
            return _ok(_json_response(kb.get_item(item_id)))
        # DELETE /{item_id}
        if sub.startswith("/") and method == "DELETE" and "/" not in sub[1:]:
            item_id = sub[1:]
            return _ok(_json_response(await kb.delete_item(item_id)))

        logger.warning("knowledge-base http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code"):
            return _kberr_response(exc)
        logger.error("knowledge-base http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


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
    """按 path 分发：recall/stats（既有）/ memory 域 / knowledge-base 域。"""
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

        # memory 域（懒注入记忆后端后分发）
        if path.startswith("/ext/hindsight_memory_service/memory"):
            return await _handle_memory_domain(path, method, raw_body, q)

        # knowledge-base 域（注入 hindsight client 后分发）
        if path.startswith("/ext/hindsight_memory_service/knowledge-base"):
            return await _handle_kb_domain(path, method, raw_body, q, headers)

        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:
        logger.exception("hindsight http.handle failed: %s", exc)
        return {"success": False, "error": str(exc), "data": _json_response({"error": str(exc)}, 500)}


if __name__ == "__main__":
    plugin.run()
