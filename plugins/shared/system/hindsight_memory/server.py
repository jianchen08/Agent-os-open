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
# hindsight-api 服务器子进程（on_load 启动,on_unload 终止）
_api_process: Any = None
# hindsight-api 监听端口
_HINDSIGHT_PORT = "8420"

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
    """Store a memory entry via hindsight client.aretain.

    memory_type 同时写入 tags(服务端过滤)和 metadata(客户端读取)。
    """
    if _client is None:
        return _degrade_dict("retain")

    try:
        bank = _resolve_bank_id(bank_id)
        meta = dict(metadata or {})
        meta.setdefault("memory_type", memory_type)
        # tags 用于 arecall 服务端过滤(memory_type 作为 tag)
        tags = [f"type:{memory_type}"]

        result = await _client.aretain(
            bank_id=bank, content=content, metadata=meta, tags=tags,
        )
        # RetainResponse 对象:取 id 字段
        mem_id = ""
        if hasattr(result, "accepted"):
            mem_id = str(getattr(result, "operation_id", "") or "")
        elif isinstance(result, dict):
            mem_id = result.get("id", result.get("operation_id", ""))
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
        if callable(result) and not isinstance(result, bool):
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
            await _client.aretain(bank_id=bank, content=chunk, metadata=meta, tags=["type:semantic"])
        return {"chunks_imported": len(chunks), "knowledge_name": name}
    except Exception as e:
        logger.warning("[hindsight.import_document] 导入失败 | error=%s", e)
        return {"chunks_imported": 0, "knowledge_name": knowledge_name, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════════════════════


def _apply_llm_env() -> None:
    """把 HINDSIGHT_API_* 配置写入环境变量（hindsight-api 从 env 读取配置）。

    注意:hindsight-api 用的是 HINDSIGHT_API_ 前缀(不是 HINDSIGHT_)。

    默认配置:
      - LLM(事实抽取/反思):GLM glm-5.2 @ 智谱 OpenAI 兼容端点(复用 ZHIPU_API_KEY)
      - Embedding(向量检索):BAAI/bge-m3 @ 硅基流动(免费, 国内直连, OpenAI 兼容)
        → 用 SILICONFLOW_API_KEY(用户需在 .env 配置)
      - Reranker:rrf(Reciprocal Rank Fusion, 无需下载模型, 避免 HF 被墙)
    """
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
    cfg_default_bank = (
        config.get("default_bank_id")
        or config.get("bank_id")
        or config.get("tenant_id")
    )
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

        # 启动 hindsight-api 服务器子进程(pg0 嵌入式 PG + uvicorn HTTP)
        _api_process = subprocess.Popen(
            [sys.executable, "-m", "hindsight_api.main",
             "--port", str(port), "--host", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        logger.info("[hindsight] hindsight-api 子进程已启动 PID=%s port=%s", _api_process.pid, port)

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
                # 检查子进程是否已退出
                if _api_process.poll() is not None:
                    raise RuntimeError(f"hindsight-api 子进程已退出 code={_api_process.returncode}")
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


if __name__ == "__main__":
    plugin.run()
