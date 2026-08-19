"""长期记忆后端端口与实现。

定义统一的记忆后端接口（IMemoryBackend），上层（压缩/复盘/沉淀/注入）通过
此接口落库/检索记忆，后端可插拔：
- HindsightBackend：通过 tool-executor 调用 hindsight sidecar 工具（向量检索，高质量）
- KernelMemoryBackend：通过 service-registry 调用内核记忆表（关键词检索，永远可用，降级）

工厂 get_memory_backend 按配置选后端，默认 hindsight，降级 kernel。

设计要点：
- 唯一外部依赖是注入的 capability_caller（async fn `(method, params) -> Any`），
  构造时传入，便于测试 mock；解耦插件全局状态。
- 所有方法在能力调用失败时记告警并降级返回（空列表/空串/False），永不崩溃——
  与 hindsight_memory/server.py 的韧性设计一致。
- 不导入 hindsight 包或任何重依赖；仅用 stdlib + typing。

[来源: docs/tasks Step 3 IMemoryBackend 端口 + 工厂]
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# capability_caller 类型：(method: str, params: dict) -> Awaitable[Any]
CapabilityCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]

# 内核记忆表切块大小（字符）——内核无原生文档导入，逐块 create
_KERNEL_CHUNK_SIZE = 2000


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
    ) -> str:
        """写入一条记忆，返回 memory id。

        Args:
            user_id: 租户/用户隔离 key（映射到 bank_id）
            content: 记忆内容文本
            memory_type: 记忆类型（semantic/episode/...）
            tags: 可选标签列表
            source: 可选来源标注

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
    ) -> str:
        """写入记忆，经 tool-executor.invoke 调用 hindsight.retain。"""
        metadata: dict[str, Any] = {}
        if tags:
            metadata["tags"] = tags
        if source:
            metadata["source"] = source
        params = {
            "tool_name": "hindsight.retain",
            "plugin_id": "hindsight_memory_service",
            "args": {
                "bank_id": user_id,
                "content": content,
                "memory_type": memory_type,
                "metadata": metadata,
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
# 内核记忆表后端（降级）
# ═══════════════════════════════════════════════════════════


class KernelMemoryBackend(IMemoryBackend):
    """内核记忆表后端——经 service-registry 调用 memory.* 方法。

    FALLBACK 后端：内核记忆表永远存在，搜索为简易关键词匹配（无向量），
    质量低于 hindsight 但永远可用。无原生文档导入，本类自行切块后逐条 create。

    capability_caller 在构造时注入（async fn `(method, params) -> Any`），
    生产环境由插件把 service-registry 能力句柄的 call 方法注入进来。
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
    ) -> str:
        """写入记忆，经 service-registry 调用 memory.create。

        构造 MemoryRecord 形态 {id, content, memory_type, tags, score, created_at}。
        """
        import secrets

        record = {
            "id": secrets.token_hex(6),  # 12-hex，与内核 MemoryRecord 对齐
            "content": content,
            "memory_type": memory_type,
            "tags": tags or [],
            "score": 0.0,
            "created_at": _now_iso(),
        }
        try:
            await self._call("memory.create", record)
        except Exception as e:
            # 上抛而非静默 ""：空 id 会被 memory 工具层判失败，但吞错让排查
            # 无从下手（真实错误如 capability 超时被藏成"成功但无 id"）。
            raise RuntimeError(f"memory.create 调用失败: {e}") from e
        return record["id"]

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """检索记忆，经 service-registry 调用 memory.search，
        结果映射为统一形态。"""
        try:
            result = await self._call(
                "memory.search", {"query": query, "top_k": top_k}
            )
        except Exception as e:
            # 上抛：search 失败 ≠ 空结果（静默 [] 曾掩盖 capability 超时根因
            # 半天，2026-08-19 e2e 实测）。空结果只来自内核真实返回的空列表。
            raise RuntimeError(f"memory.search 调用失败: {e}") from e
        return self._map_kernel_results(result, memory_type)

    async def delete(self, user_id: str, memory_id: str | None = None) -> bool:
        """删除记忆，经 service-registry 调用 memory.delete。

        kernel memory.delete 只支持按 id 删（无 bank 概念），memory_id 必填。
        """
        if not memory_id:
            # 内核 memory.delete 需要明确 id；未指定 id 时记告警并返回 False
            logger.warning(
                "[KernelMemoryBackend.delete] 缺少 memory_id，内核 memory.delete 无法整库删"
            )
            return False
        try:
            result = await self._call("memory.delete", {"id": memory_id})
        except Exception as e:
            logger.warning("[KernelMemoryBackend.delete] 调用失败降级 | error=%s", e)
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
        """导入文档——内核无原生导入，自行切块后逐条 memory.create。"""
        raw_text = text
        if file_path:
            try:
                with open(file_path, encoding="utf-8") as fh:
                    raw_text = fh.read()
            except Exception as e:
                return {"chunks_imported": 0, "name": name, "error": str(e)}
        if not raw_text:
            return {
                "chunks_imported": 0,
                "name": name,
                "error": "no text provided",
            }

        chunks = _chunk_text(raw_text, _KERNEL_CHUNK_SIZE)
        imported = 0
        for idx, chunk in enumerate(chunks):
            tags = ["import_document"]
            if name:
                tags.append(name)
            mem_id = await self.add(
                user_id=user_id,
                content=chunk,
                memory_type="semantic",
                tags=tags,
                source=f"import_document:{name}:{idx}/{len(chunks)}",
            )
            if mem_id:
                imported += 1
        return {"chunks_imported": imported, "name": name, "total_chunks": len(chunks)}

    @staticmethod
    def _map_kernel_results(
        result: Any, memory_type: str | None
    ) -> list[dict[str, Any]]:
        """把内核 memory.search 返回的 MemoryRecord 列表映射为统一形态，
        并可选按 memory_type 客户端过滤。"""
        if not isinstance(result, list):
            return []
        mapped: list[dict[str, Any]] = []
        for rec in result:
            if not isinstance(rec, dict):
                continue
            mt = rec.get("memory_type", "semantic")
            if memory_type and mt != memory_type:
                continue
            mapped.append(
                {
                    "id": str(rec.get("id", "")),
                    "content": rec.get("content", ""),
                    "score": float(rec.get("score", 0.0) or 0.0),
                    "memory_type": mt,
                    "metadata": {
                        "tags": rec.get("tags", []),
                        "created_at": rec.get("created_at", ""),
                    },
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
    """按配置选记忆后端。默认 hindsight，降级 kernel。

    Args:
        config: 配置字典，键 backend: "hindsight"(默认) | "kernel"
        capability_caller: 注入的能力调用 async 函数 `(method, params) -> Any`。
            生产环境由插件注入（tool-executor / service-registry 句柄的 call 方法）。

    Returns:
        IMemoryBackend 实例。

    Raises:
        ValueError: capability_caller 为 None（必须注入，便于测试与解耦）。
    """
    if capability_caller is None:
        raise ValueError(
            "capability_caller 必须注入（生产环境由插件传入 tool-executor/"
            "service-registry 能力句柄的 call 方法）"
        )

    cfg = config or {}
    backend = (cfg.get("backend") or "hindsight").lower()

    if backend == "kernel":
        return KernelMemoryBackend(capability_caller)
    # 默认 hindsight（含未知值也回落到 hindsight）
    return HindsightBackend(capability_caller)


# ═══════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════


def _now_iso() -> str:
    """当前 UTC 时间 ISO8601 字符串（与内核 MemoryRecord.created_at 对齐）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _chunk_text(text: str, chunk_size: int = _KERNEL_CHUNK_SIZE) -> list[str]:
    """按字符数朴素切块（与 hindsight_memory/server.py._chunk_text 同款）。"""
    if not text:
        return []
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
