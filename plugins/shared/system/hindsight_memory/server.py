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

# bank_id 缺省值（多租户隔离 key 缺省；运行时由内核注入 tenant_id）
_DEFAULT_BANK_ID = os.environ.get("HINDSIGHT_DEFAULT_BANK_ID", "default")

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
    return getattr(sys.modules[__name__], "_DEFAULT_BANK_ID", "default")


def _filter_by_memory_type(
    results: list[dict[str, Any]], memory_type: str | None
) -> list[dict[str, Any]]:
    """按 memory_type 客户端过滤 recall 结果。

    匹配优先级：metadata.memory_type → 顶层 memory_type 字段。

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
        mt = meta.get("memory_type") or r.get("memory_type")
        if mt == memory_type:
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
    """Store a memory entry via hindsight client.retain.

    The memory_type is merged into metadata so recall can filter client-side.
    """
    if _client is None:
        return _degrade_dict("retain")

    try:
        bank = _resolve_bank_id(bank_id)
        # memory_type 进 metadata 以便 recall 过滤
        meta = dict(metadata or {})
        meta.setdefault("memory_type", memory_type)

        result = _client.retain(bank_id=bank, content=content, metadata=meta)
        # 兼容 hindsight 返回 id 字符串或 dict 的两种形态
        if isinstance(result, dict):
            mem_id = result.get("id", result.get("memory_id", ""))
            ret_meta = result.get("metadata", meta)
        else:
            mem_id = str(result) if result is not None else ""
            ret_meta = meta
        return {"id": mem_id, "stored": True, "metadata": ret_meta}
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
    """Recall memories via hindsight client.recall.

    If memory_type is given, results are filtered client-side.
    """
    if _client is None:
        return _degrade_dict("recall")

    try:
        bank = _resolve_bank_id(bank_id)
        result = _client.recall(bank_id=bank, query=query, top_k=top_k)
        # 统一成 list
        if isinstance(result, dict) and "results" in result:
            items = result.get("results", [])
        elif isinstance(result, list):
            items = result
        else:
            items = []
        items = _filter_by_memory_type(items, memory_type)
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
async def hindsight_reflect(bank_id: str = "") -> dict[str, Any]:
    """Trigger hindsight reflection (consolidation) on a bank."""
    if _client is None:
        return _degrade_dict("reflect")

    try:
        bank = _resolve_bank_id(bank_id)
        result = _client.reflect(bank_id=bank)
        if isinstance(result, dict):
            return result
        return {"result": result}
    except Exception as e:
        logger.warning("[hindsight.reflect] 调用失败 | error=%s", e)
        return {"error": str(e)}


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
    """Delete memories from a bank (specific id or whole bank)."""
    if _client is None:
        return _degrade_dict("delete")

    try:
        bank = _resolve_bank_id(bank_id)
        kwargs: dict[str, Any] = {"bank_id": bank}
        if memory_id:
            kwargs["memory_id"] = memory_id
        # hindsight delete API 形态多样，尽力调用
        deleter = getattr(_client, "delete", None) or getattr(
            _client, "delete_memory", None
        )
        if deleter is None:
            return {"deleted": False, "error": "client has no delete method"}
        deleter(**kwargs)
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
            with open(file_path, "r", encoding="utf-8") as fh:
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
            _client.retain(bank_id=bank, content=chunk, metadata=meta)
        return {"chunks_imported": len(chunks), "knowledge_name": name}
    except Exception as e:
        logger.warning("[hindsight.import_document] 导入失败 | error=%s", e)
        return {"chunks_imported": 0, "knowledge_name": knowledge_name, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════════════════════


def _apply_llm_env() -> None:
    """把 HINDSIGHT_* 配置写入环境变量（hindsight 从 env 读取 LLM/embedding）。

    默认值复用项目的 GLM (Zhipu) OpenAI-compatible 端点：
      - base_url: https://open.bigmodel.cn/api/coding/paas/v4/ （llm.yaml providers.zhipu_coding）
      - model: glm-5.2 （llm.yaml models.glm-5.2）
      - embeddings_provider: zhipu （embedding.yaml default_provider）
      - api_key: ${ZHIPU_API_KEY}
    """
    defaults = {
        "HINDSIGHT_LLM_BASE_URL": "https://open.bigmodel.cn/api/coding/paas/v4/",
        "HINDSIGHT_LLM_MODEL": "glm-5.2",
        "HINDSIGHT_EMBEDDINGS_PROVIDER": "zhipu",
        "HINDSIGHT_EMBEDDINGS_MODEL": "embedding-3",
    }
    for key, default in defaults.items():
        if not os.environ.get(key):
            os.environ[key] = default

    # API key：优先 ZHIPU_API_KEY（与 llm.yaml providers.zhipu_coding 对齐）
    zhipu_key = os.environ.get("ZHIPU_API_KEY", "")
    if zhipu_key and not os.environ.get("HINDSIGHT_LLM_API_KEY"):
        os.environ["HINDSIGHT_LLM_API_KEY"] = zhipu_key


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """懒导入 hindsight 并初始化嵌入式 client。

    包未安装或初始化失败时 _client 保持 None，所有工具降级——sidecar 永不崩溃。
    """
    global _client, _DEFAULT_BANK_ID

    # 从内核注入的 config 读取默认 bank_id（运行时通常为 tenant_id）
    config = plugin.get_config() or {}
    cfg_default_bank = (
        config.get("default_bank_id")
        or config.get("bank_id")
        or config.get("tenant_id")
    )
    if cfg_default_bank:
        _DEFAULT_BANK_ID = str(cfg_default_bank)

    # 数据目录
    data_dir = (
        config.get("data_dir")
        or os.environ.get("HINDSIGHT_DATA_DIR")
        or os.path.join(_THIS_DIR, "data", "hindsight")
    )
    os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("HINDSIGHT_DATA_DIR", data_dir)

    # LLM/embedding 环境变量（hindsight 从 env 读取配置）
    _apply_llm_env()

    # 懒导入 hindsight（包可能未安装）
    try:
        # hindsight-all-slim 的嵌入式入口；按其文档使用 internal pg0
        from hindsight import Hindsight  # type: ignore

        _client = Hindsight(embedded=True, data_dir=data_dir)
        logger.info(
            "[hindsight] on_load 完成 | data_dir=%s | model=%s",
            data_dir,
            os.environ.get("HINDSIGHT_LLM_MODEL"),
        )
    except Exception as e:
        # 包未安装 / 初始化失败：降级，sidecar 继续运行
        _client = None
        logger.warning(
            "[hindsight] 初始化失败，sidecar 进入降级模式（所有工具返回 error）| error=%s",
            e,
        )


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup hindsight client on unload."""
    global _client
    if _client is not None:
        try:
            close = getattr(_client, "close", None)
            if callable(close):
                close()
        except Exception as e:
            logger.warning("[hindsight] on_unload 清理失败 | error=%s", e)
        finally:
            _client = None


if __name__ == "__main__":
    plugin.run()
