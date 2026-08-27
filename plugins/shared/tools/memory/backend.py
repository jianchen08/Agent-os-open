"""记忆后端客户端——本插件自持，经 tool-executor capability 调用 hindsight 服务。

插件边界契约：memory 工具面不 import 其它插件的源码模块；对
hindsight_memory_service 的全部读写经 ``tool-executor.invoke`` 能力调用
（params 形如 ``{"tool_name": ..., "plugin_id": ..., "args": ...}``，
media/server 插件同款模式）。invoke 信封两层解包：

- 内核 invoker 归一把纯业务 dict 包成 ``{success, data}`` 信封 → 先解
  ``data``；
- 业务 dict（hindsight sidecar 返回）含 ``error`` / ``initialized: false``
  即降级签名 → 明确失败，杜绝空 id 假成功。

异常策略两档：写读判定面 add/search 失败诚实上抛 RuntimeError（吞错会让
工具层包装成 success:true 假成功）；低风险面 delete/import_document 记告警
后降级返回（False / {chunks_imported: 0, error}），不阻断调用方主流程。

暴露接口：
- HindsightBackend(caller)：add / search / delete / import_document 四方法
- get_memory_backend(config, caller)：工厂（唯一后端 = hindsight）
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# capability_caller 类型：(method: str, params: dict) -> Awaitable[Any]
CapabilityCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]

_PLUGIN_ID = "hindsight_memory_service"


def _unwrap_envelope(result: Any) -> tuple[dict[str, Any] | None, bool]:
    """解 tool-executor.invoke 信封。

    Returns:
        (业务 dict | 原样结果, 是否命中降级签名)。降级签名 = 业务层带
        error 或 initialized:false。
    """
    if isinstance(result, dict):
        inner = result.get("data")
        if "data" in result and isinstance(inner, dict):
            result = inner
        degraded = bool(result.get("error")) or result.get("initialized") is False
        return result, degraded
    return None, False


class HindsightBackend:
    """Hindsight sidecar 后端——经 tool-executor 调用 hindsight 服务。

    capability_caller 在构造时注入（async fn ``(method, params) -> Any``）：
    生产环境由插件把 tool-executor 能力句柄的 call 方法包一层剥前缀注入，
    测试环境传 AsyncMock。
    """

    def __init__(self, capability_caller: CapabilityCaller) -> None:
        self._call = capability_caller

    async def _invoke(self, tool_name: str, args: dict[str, Any]) -> Any:
        """统一 invoke 出口：失败上抛 RuntimeError（上层按各方法分档处理）。"""
        params = {"tool_name": tool_name, "plugin_id": _PLUGIN_ID, "args": args}
        try:
            return await self._call("tool-executor.invoke", params)
        except Exception as e:
            raise RuntimeError(f"{tool_name} 调用失败: {e}") from e

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
        """写入一条记忆（→ hindsight.retain），返回 memory id。

        wire metadata 键值必须全 str：hindsight-client 的 retain metadata 是
        ``dict[str, str]`` 校验面——tags 不能以 list 塞入，序列化为 JSON 串；
        sidecar retain 解析并提升为真实 tags。tags/source 语义键由本方法
        装配，不接受调用方 metadata 覆盖。
        """
        wire_meta: dict[str, str] = {}
        for key, value in (metadata or {}).items():
            if key in ("tags", "source"):
                continue
            wire_meta[str(key)] = (
                value if isinstance(value, str)
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

        raw = await self._invoke("hindsight.retain", args)
        mapped, degraded = _unwrap_envelope(raw)
        if not isinstance(mapped, dict):
            raise RuntimeError(f"hindsight.retain 返回非预期类型: {type(raw).__name__}")
        if degraded:
            raise RuntimeError(
                f"hindsight 后端降级: {mapped.get('error') or 'not initialized'}"
            )
        memory_id = str(mapped.get("id", "") or "")
        if not memory_id:
            raise RuntimeError("hindsight.retain 未返回 memory id（写入未确认）")
        return memory_id

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
        """检索相关记忆（→ hindsight.recall），统一映射为
        {id, content, score, memory_type, metadata} 列表。

        - session_id: 会话过滤转 ``session:<id>`` 标签（会话是内容维度标签，
          隔离键始终是 user_id 对应 bank）
        - knowledge_name: 客户端 metadata 过滤（服务端无该字段面）
        """
        args: dict[str, Any] = {"bank_id": user_id, "query": query, "top_k": top_k}
        if memory_type:
            args["memory_type"] = memory_type
        session_tags = [f"session:{session_id}"] if session_id else None
        if session_tags or tags:
            args["tags"] = list(tags or []) + list(session_tags or [])
            args["tags_match"] = tags_match or "any"

        raw = await self._invoke("hindsight.recall", args)
        mapped, degraded = _unwrap_envelope(raw)
        if degraded and isinstance(mapped, dict):
            raise RuntimeError(
                f"hindsight 后端降级: {mapped.get('error') or 'not initialized'}"
            )

        # recall 原始结果形态：envelope 解包后的 {results:[...]} 或裸 list
        if isinstance(mapped, dict):
            items = mapped.get("results") or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        results: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            results.append(
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
        if knowledge_name:
            results = [
                item
                for item in results
                if (item.get("metadata") or {}).get("knowledge_name") == knowledge_name
            ]
        return results

    async def delete(self, user_id: str, memory_id: str | None = None) -> bool:
        """删除记忆（→ hindsight.delete）；失败告警降级返回 False。"""
        args: dict[str, Any] = {"bank_id": user_id}
        if memory_id:
            args["memory_id"] = memory_id
        try:
            raw = await self._invoke("hindsight.delete", args)
        except Exception as e:
            logger.warning("[HindsightBackend.delete] 调用失败降级 | error=%s", e)
            return False
        mapped, _ = _unwrap_envelope(raw)
        if isinstance(mapped, dict):
            return bool(mapped.get("deleted", False))
        return True

    async def import_document(
        self,
        user_id: str,
        text: str | None = None,
        file_path: str | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """导入文档切块入库（→ hindsight.import_document）；失败告警降级返回
        {chunks_imported: 0, error}。"""
        args: dict[str, Any] = {"bank_id": user_id}
        if text is not None:
            args["text"] = text
        if file_path:
            args["file_path"] = file_path
        if name:
            args["knowledge_name"] = name
        try:
            raw = await self._invoke("hindsight.import_document", args)
        except Exception as e:
            logger.warning(
                "[HindsightBackend.import_document] 调用失败降级 | error=%s", e
            )
            return {"chunks_imported": 0, "name": name, "error": str(e)}
        mapped, _ = _unwrap_envelope(raw)
        out = dict(mapped) if isinstance(mapped, dict) else {}
        out.setdefault("name", name)
        return out


def get_memory_backend(
    config: dict[str, Any] | None = None,
    capability_caller: CapabilityCaller | None = None,
) -> HindsightBackend:
    """构建记忆后端（唯一后端 = hindsight）。

    Raises:
        ValueError: capability_caller 为 None；或 config 指定已退役的 kernel
            后端（fail loudly，不留备用真值糊弄）。
    """
    if capability_caller is None:
        raise ValueError(
            "capability_caller 必须注入（生产环境由插件传入 tool-executor "
            "能力句柄的 call 方法）"
        )
    cfg = config or {}
    backend = (cfg.get("backend") or "hindsight").lower()
    if backend == "kernel":
        raise ValueError(
            'memory backend "kernel" 已退役（内核记忆表 DROP），唯一后端 = hindsight'
        )
    return HindsightBackend(capability_caller)
