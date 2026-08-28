"""长期记忆后端端口与实现。

定义统一的记忆后端接口（IMemoryBackend），上层（压缩/复盘/沉淀/注入）通过
此接口落库/检索记忆。唯一后端：
- HindsightBackend：经 tool-executor 调 hindsight sidecar 工具（向量检索）

0.1 的内核记忆表后端（KernelMemoryBackend，关键词检索降级）已随内核 memory 表
DROP 一并退役（2026-08-19 用户裁定：不留两套真值、禁用备用后端糊弄）。

设计要点：
- 唯一外部依赖是注入的 capability_caller（async fn `(method, params) -> Any`），
  构造时传入，便于测试 mock；解耦插件全局状态。
- 异常策略分两档（现行为，与工具层假成功防线对齐）：写读判定面
  add/search/get_documents 能力失败诚实上抛 RuntimeError——吞错降级会让上层
  把失败包装成 success:true 的假成功；低风险面 delete/import_document 记告警后
  降级返回（False / {chunks_imported: 0, error}），不阻断调用方主流程。
- 不导入 hindsight 包或任何重依赖；仅用 stdlib + typing。

[来源: docs/tasks Step 3 IMemoryBackend 端口 + 工厂]
"""

from __future__ import annotations

import json
import logging
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
        document_id: str = "",
        update_mode: str | None = None,
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
            document_id: 可选文档 id（服务端原样落库为 document id，返回即
                该 id——delete/update 的定向通路，2026-08-22 真机实证）
            update_mode: 可选更新语义（'replace' 替换同 document_id 文档 /
                'append' 追加），服务端文档级操作

        Returns:
            memory id；能力失败（调用失败/服务端报错/未返回 id）上抛
            RuntimeError，不返回空串假成功。
        """
        raise NotImplementedError

    @abstractmethod
    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
        session_id: str | None = None,
        knowledge_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """检索相关记忆。

        Args:
            query: 检索查询文本
            user_id: 租户/用户隔离 key
            top_k: 返回条数上限
            memory_type: 可选按类型过滤
            tags: 可选标签过滤（hindsight 服务端 tags 精确过滤面）
            tags_match: tag 匹配模式（any/all/any_strict/all_strict/exact）
            session_id: 可选会话隔离（映射到 hindsight bank）
            knowledge_name: 可选知识库过滤（服务端 metadata.knowledge_name 过滤）

        Returns:
            统一形态列表 [{id, content, score, memory_type, metadata}]；
            无结果返回 []；能力失败上抛 RuntimeError。
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
    异常策略见模块 docstring：add/search/get_documents 失败诚实上抛
    RuntimeError（降级决策归工具层）；delete/import_document 告警后降级
    （False / {chunks_imported: 0, error}），不向上抛。

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
        document_id: str = "",
        update_mode: str | None = None,
    ) -> str:
        """写入记忆，经 tool-executor.invoke 调用 hindsight.retain。

        wire metadata 键值必须全 str：hindsight-client 0.9.x aretain 的
        metadata 是 ``dict[str, str]`` pydantic 校验——tags 不能以 list
        塞入（否则所有带 tags 的写入失败）。tags 序列化为 JSON 串；
        sidecar retain 会解析并提升为 hindsight 真实 tags（供
        list_documents/recall 服务端 tag 过滤）。
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
        args: dict[str, Any] = {
            "bank_id": user_id,
            "content": content,
            "memory_type": memory_type,
            "metadata": wire_meta,
        }
        if document_id:
            args["document_id"] = document_id
        if update_mode:
            args["update_mode"] = update_mode
        params = {
            "tool_name": "hindsight.retain",
            "plugin_id": "hindsight_memory_service",
            "args": args,
        }
        try:
            result = await self._call("tool-executor.invoke", params)
        except Exception as e:
            # 诚实上抛：吞错降级会让 memory 工具层把失败包装成 success:true
            # 的假成功。降级决策归工具层。
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
        tags: list[str] | None = None,
        tags_match: str = "any",
        session_id: str | None = None,
        knowledge_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """检索记忆，经 tool-executor.invoke 调用 hindsight.recall，
        结果映射为统一形态 {id, content, score, memory_type, metadata}。

        - tags: 服务端 tags 精确过滤（hindsight tags 面，比语义召回可靠）
        - session_id: 会话过滤——转换为 ``session:<id>`` 标签过滤（store 侧
          同款注入，见 HindsightBackend.add；隔离键始终是 user_id 对应
          bank，session 只是内容维度标签，2026-08-22 语义修正）
        - knowledge_name: 客户端 metadata 过滤（recall 结果按
          metadata.knowledge_name 精确匹配；不投给服务端——服务端无该字段面）
        """
        args: dict[str, Any] = {
            "bank_id": user_id,
            "query": query,
            "top_k": top_k,
        }
        if memory_type:
            args["memory_type"] = memory_type
        session_tags = [f"session:{session_id}"] if session_id else None
        if session_tags or tags:
            args["tags"] = list(tags or []) + list(session_tags or [])
            args["tags_match"] = tags_match or "any"
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
        mapped = self._map_hindsight_results(result)
        if knowledge_name:
            mapped = [
                item for item in mapped
                if (item.get("metadata") or {}).get("knowledge_name") == knowledge_name
            ]
        return mapped

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
        """删除记忆，经 tool-executor.invoke 调用 hindsight.delete。

        memory_id 是 store 回传的 document_id（真删除通路）；None 表示删除
        整个 bank（2026-08-22 语义修正：旧实现把 memory_id 传成
        hindsight.delete 不认识的参数，真机 "tool execution failed"）。
        """
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
            # 解 tool-executor.invoke 的 data 信封（import_document 返回纯业务
            # dict {chunks_imported, knowledge_name}）
            inner = result.get("data") if "data" in result else result
            if isinstance(inner, dict):
                result = inner
            out = dict(result)
            out.setdefault("name", name)
            return out
        return {"chunks_imported": 0, "name": name}

    @staticmethod
    def _map_hindsight_results(result: Any) -> list[dict[str, Any]]:
        """把 hindsight recall 原始结果映射为统一形态。

        原始条目可能含 id/content/score/metadata.memory_type；映射后统一为
        {id, content, score, memory_type, metadata, tags}。tags 优先取条目
        顶层 tags（hindsight 0.9.x RecallResult 原生字段）；缺失时回退解析
        metadata["tags"] JSON 串（retain 侧 list 序列化为 str 落库）——调用方
        （prompt_build 压缩块过滤等）按 list 消费，JSON 串直透必被拒。
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
            meta = dict(item.get("metadata") or {})
            tags = item.get("tags")
            if not isinstance(tags, list):
                raw = meta.get("tags")
                parsed: Any = raw
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except (ValueError, TypeError):
                        parsed = None
                tags = parsed if isinstance(parsed, list) else []
            meta["tags"] = [str(t) for t in tags if t]
            mapped.append(
                {
                    "id": str(item.get("id", "")),
                    "content": item.get("content", ""),
                    "score": float(item.get("score", 0.0) or 0.0),
                    "memory_type": meta.get("memory_type")
                    or item.get("memory_type")
                    or "semantic",
                    "metadata": meta,
                    "tags": meta["tags"],
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
