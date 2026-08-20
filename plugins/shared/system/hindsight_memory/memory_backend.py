"""长期记忆后端端口与实现。

定义统一的记忆后端接口（IMemoryBackend），上层（压缩/复盘/沉淀/注入）通过
此接口落库/检索记忆。唯一后端：
- HindsightBackend：经 tool-executor 调 hindsight sidecar 工具（向量检索）

0.1 的内核记忆表后端（KernelMemoryBackend，关键词检索降级）已随内核 memory 表
DROP 一并退役（2026-08-19 用户裁定：不留两套真值、禁用备用后端糊弄）。

设计要点：
- 唯一外部依赖是注入的 capability_caller（async fn `(method, params) -> Any`），
  构造时传入，便于测试 mock；解耦插件全局状态。
- 所有方法在能力调用失败时记告警并降级返回（空列表/空串/False），永不崩溃——
  与 hindsight_memory/server.py 的韧性设计一致。
- 不导入 hindsight 包或任何重依赖；仅用 stdlib + typing。

[来源: docs/tasks Step 3 IMemoryBackend 端口 + 工厂]
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# capability_caller 类型：(method: str, params: dict) -> Awaitable[Any]
CapabilityCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]

# ═══════════════════════════════════════════════════════════
# 端口
# ═══════════════════════════════════════════════════════════


class IMemoryBackend(ABC):
    """长期记忆后端端口 — 压缩/复盘/沉淀产出的记忆落库入口。

    上层只依赖此接口，后端可换（hindsight / mem0 / 内核降级）。
    所有方法为 async——记忆检索/落库涉及跨进程能力调用。
    """

    @abstractmethod
    async def add(
        self,
        user_id: str,
        content: str,
        memory_type: str = "semantic",
        tags: list[str] | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """写入一条记忆，返回 memory id。

        Args:
            user_id: 租户/用户隔离 key（映射到 bank_id）
            content: 记忆内容文本
            memory_type: 记忆类型（semantic/episode/...）
            tags: 可选标签列表
            source: 可选来源标注
            metadata: 可选定向键值（如 review_id）——键值会被序列化为 str
                合入 wire metadata（hindsight pydantic dict[str,str] 校验面）。

        Returns:
            memory id；失败时返回空串（降级，不抛异常）。
        """
        raise NotImplementedError

    @abstractmethod
    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """检索相关记忆。

        Args:
            query: 检索查询文本
            user_id: 租户/用户隔离 key
            top_k: 返回条数上限
            memory_type: 可选按类型过滤

        Returns:
            统一形态列表 [{id, content, score, memory_type, metadata}]；
            失败/无结果返回 []（降级，不抛异常）。
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(self, user_id: str, memory_id: str | None = None) -> bool:
        """删除记忆。

        Args:
            user_id: 租户/用户隔离 key
            memory_id: 指定记忆 id；None 表示删除整个 bank

        Returns:
            是否成功；失败返回 False（降级，不抛异常）。
        """
        raise NotImplementedError

    @abstractmethod
    async def import_document(
        self,
        user_id: str,
        text: str | None = None,
        file_path: str | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """导入文档（切块后逐条落库）。

        Args:
            user_id: 租户/用户隔离 key
            text: 文档文本（与 file_path 二选一）
            file_path: 文档文件路径
            name: 知识标签

        Returns:
            {chunks_imported, name, ...}；失败返回 {chunks_imported: 0, error}
            （降级，不抛异常）。
        """
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════
# Hindsight 后端
# ═══════════════════════════════════════════════════════════


class HindsightBackend(IMemoryBackend):
    """Hindsight sidecar 后端——经 tool-executor 调用 hindsight 工具。

    高质量向量检索；依赖 hindsight sidecar 进程在线且 hindsight 包可用。
    所有能力调用失败时记告警并降级（空/False），不向上抛——与上层韧性约定一致。

    capability_caller 在构造时注入（async fn `(method, params) -> Any`），
    实际生产环境由插件把 tool-executor 能力句柄的 call 方法注入进来，
    测试环境传 AsyncMock。
    """

    def __init__(self, capability_caller: CapabilityCaller) -> None:
        self._call = capability_caller

    async def add(
        self,
        user_id: str,
        content: str,
        memory_type: str = "semantic",
        tags: list[str] | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """写入记忆，经 tool-executor.invoke 调用 hindsight.retain。

        wire metadata 键值必须全 str：hindsight-client 0.9.x aretain 的
        metadata 是 ``dict[str, str]`` pydantic 校验——tags 以 list 塞入曾致
        所有带 tags 的写入必炸（复盘报告从未真正持久化，2026-08-19 批 C
        真实 API A/B 取证）。tags 序列化为 JSON 串；sidecar retain 会解析并
        提升为 hindsight 真实 tags（供 list_documents/recall 服务端 tag 过滤）。
        """
        wire_meta: dict[str, str] = {}
        if metadata:
            # 调用方定向键（如 review_id）——值强转 str；tags/source 语义键
            # 由本方法装配，不接受调用方覆盖
            for key, value in metadata.items():
                if key in ("tags", "source"):
                    continue
                wire_meta[str(key)] = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, default=str)
                )
        if tags:
            wire_meta["tags"] = json.dumps(list(tags), ensure_ascii=False)
        if source:
            wire_meta["source"] = source
        params = {
            "tool_name": "hindsight.retain",
            "plugin_id": "hindsight_memory_service",
            "args": {
                "bank_id": user_id,
                "content": content,
                "memory_type": memory_type,
                "metadata": wire_meta,
            },
        }
        try:
            result = await self._call("tool-executor.invoke", params)
        except Exception as e:
            # 诚实上抛：吞错降级会让 memory 工具层把失败包装成 success:true
            # 的假成功（2026-08-19 e2e 实测）。降级决策归工具层。
            raise RuntimeError(f"hindsight.retain 调用失败: {e}") from e
        if isinstance(result, dict):
            # tool-executor.invoke 经内核 invoker 归一：纯业务 dict（无
            # success/error）被包成 {success:true, data:<业务>} 信封（invoker.rs
            # 决策树 ③）。侧边 tools/call 直连无信封，故此处两层都解。
            if "data" in result:
                inner = result.get("data")
                if isinstance(inner, dict):
                    result = inner
            # 业务 dict（hindsight sidecar 的 hindsight.retain 返回）里有
            # error/降级签名或无 id → 明确失败，杜绝"空 id 报成功"。
            if result.get("error") or result.get("initialized") is False:
                raise RuntimeError(
                    f"hindsight 后端降级: {result.get('error') or 'not initialized'}"
                )
            memory_id = str(result.get("id", "") or "")
            if not memory_id:
                raise RuntimeError("hindsight.retain 未返回 memory id（写入未确认）")
            return memory_id
        raise RuntimeError(f"hindsight.retain 返回非预期类型: {type(result).__name__}")

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """检索记忆，经 tool-executor.invoke 调用 hindsight.recall，
        结果映射为统一形态 {id, content, score, memory_type, metadata}。"""
        args: dict[str, Any] = {
            "bank_id": user_id,
            "query": query,
            "top_k": top_k,
        }
        if memory_type:
            args["memory_type"] = memory_type
        params = {"tool_name": "hindsight.recall", "plugin_id": "hindsight_memory_service", "args": args}
        try:
            result = await self._call("tool-executor.invoke", params)
        except Exception as e:
            raise RuntimeError(f"hindsight.recall 调用失败: {e}") from e
        if isinstance(result, dict):
            # 同 add：解 tool-executor.invoke 的 ToolExecutionResult data 信封
            if "data" in result:
                inner = result.get("data")
                if isinstance(inner, dict):
                    result = inner
            if result.get("error") or result.get("initialized") is False:
                raise RuntimeError(
                    f"hindsight 后端降级: {result.get('error') or 'not initialized'}"
                )
        return self._map_hindsight_results(result)

    async def get_documents(
        self,
        user_id: str,
        document_id: str = "",
        tags: list[str] | None = None,
        tags_match: str = "any_strict",
        q: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按 bank/tags/document_id 取回文档原文（original_text）。

        冷读定向面（IMemoryBackend 端口之外的 HindsightBackend 扩展方法）：
        recall 返回抽取后事实（world/observation/experience），原文永不命中；
        本方法走 sidecar ``hindsight.get_documents`` 工具（hindsight documents
        API）按 tags 服务端过滤精确取回原始文档。

        Args:
            user_id: bank 隔离 key（review 报告固定 "review" bank）
            document_id: 精确取单文档（给定时忽略 tags/q）
            tags: 服务端 tag 过滤（如 ["review_id:<id>"]）
            tags_match: tag 匹配模式（any_strict = OR 且排除无 tag）
            q: document id 子串过滤（可选）
            limit: 返回条数上限

        Returns:
            文档 dict 列表（含 original_text/document_metadata/tags）；
            空结果/形态异常降级 []；sidecar 降级/能力失败诚实上抛 RuntimeError
            （与 search 同风格，降级决策归调用方）。
        """
        args: dict[str, Any] = {
            "bank_id": user_id,
            "limit": limit,
            "tags_match": tags_match or "any_strict",
        }
        if document_id:
            args["document_id"] = document_id
        if tags:
            args["tags"] = list(tags)
        if q:
            args["q"] = q
        params = {
            "tool_name": "hindsight.get_documents",
            "plugin_id": "hindsight_memory_service",
            "args": args,
        }
        try:
            result = await self._call("tool-executor.invoke", params)
        except Exception as e:
            raise RuntimeError(f"hindsight.get_documents 调用失败: {e}") from e
        if isinstance(result, dict):
            if "data" in result:
                inner = result.get("data")
                if isinstance(inner, dict):
                    result = inner
            if result.get("error") or result.get("initialized") is False:
                raise RuntimeError(
                    f"hindsight 后端降级: {result.get('error') or 'not initialized'}"
                )
            docs = result.get("documents")
            if isinstance(docs, list):
                return [d for d in docs if isinstance(d, dict)]
        return []

    async def delete(self, user_id: str, memory_id: str | None = None) -> bool:
        """删除记忆，经 tool-executor.invoke 调用 hindsight.delete。"""
        args: dict[str, Any] = {"bank_id": user_id}
        if memory_id:
            args["memory_id"] = memory_id
        params = {"tool_name": "hindsight.delete", "plugin_id": "hindsight_memory_service", "args": args}
        try:
            result = await self._call("tool-executor.invoke", params)
        except Exception as e:
            logger.warning("[HindsightBackend.delete] 调用失败降级 | error=%s", e)
            return False
        if isinstance(result, dict):
            return bool(result.get("deleted", False))
        return True

    async def import_document(
        self,
        user_id: str,
        text: str | None = None,
        file_path: str | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """导入文档，经 tool-executor.invoke 调用 hindsight.import_document。"""
        args: dict[str, Any] = {"bank_id": user_id}
        if text is not None:
            args["text"] = text
        if file_path:
            args["file_path"] = file_path
        if name:
            args["knowledge_name"] = name
        params = {"tool_name": "hindsight.import_document", "plugin_id": "hindsight_memory_service", "args": args}
        try:
            result = await self._call("tool-executor.invoke", params)
        except Exception as e:
            logger.warning(
                "[HindsightBackend.import_document] 调用失败降级 | error=%s", e
            )
            return {"chunks_imported": 0, "name": name, "error": str(e)}
        if isinstance(result, dict):
            out = dict(result)
            out.setdefault("name", name)
            return out
        return {"chunks_imported": 0, "name": name}

    @staticmethod
    def _map_hindsight_results(result: Any) -> list[dict[str, Any]]:
        """把 hindsight recall 原始结果映射为统一形态。

        原始条目可能含 id/content/score/metadata.memory_type；映射后统一为
        {id, content, score, memory_type, metadata}。
        """
        if isinstance(result, dict) and "results" in result:
            items = result.get("results") or []
        elif isinstance(result, list):
            items = result
        else:
            return []
        mapped: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            mapped.append(
                {
                    "id": str(item.get("id", "")),
                    "content": item.get("content", ""),
                    "score": float(item.get("score", 0.0) or 0.0),
                    "memory_type": meta.get("memory_type")
                    or item.get("memory_type")
                    or "semantic",
                    "metadata": meta,
                }
            )
        return mapped


# ═══════════════════════════════════════════════════════════
# 工厂
# ═══════════════════════════════════════════════════════════


def get_memory_backend(
    config: dict[str, Any] | None = None,
    capability_caller: CapabilityCaller | None = None,
) -> IMemoryBackend:
    """构建记忆后端（唯一后端 = HindsightBackend）。

    Args:
        config: 配置字典（历史键 backend 已无 "kernel" 选项，见下）。
        capability_caller: 注入的能力调用 async 函数 `(method, params) -> Any`。
            生产环境由插件注入 tool-executor 句柄的 call 方法。

    Returns:
        IMemoryBackend 实例。

    Raises:
        ValueError: capability_caller 为 None；或 config 指定已退役的 kernel 后端
            （fail loudly——内核记忆表已 DROP，禁止静默回落糊弄）。
    """
    if capability_caller is None:
        raise ValueError(
            "capability_caller 必须注入（生产环境由插件传入 tool-executor 能力句柄的 call 方法）"
        )

    cfg = config or {}
    backend = (cfg.get("backend") or "hindsight").lower()
    if backend == "kernel":
        raise ValueError(
            'memory backend "kernel" 已退役（2026-08-19 内核记忆表 DROP，'
            "不留两套真值）；唯一后端 = hindsight"
        )
    return HindsightBackend(capability_caller)
