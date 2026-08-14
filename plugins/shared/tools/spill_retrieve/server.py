#!/usr/bin/env python3
# @feature: FP-0.2.spill_guard 取回工具 | @vision: V1 可进化
"""spill_retrieve MCP 服务端——按 tool_call_id 读回 spill 存档原文。

职责（大输出兜底闭环的取回半环）：
- spill_guard（Rust in_process）把超阈值工具结果存文件并替换为提取摘要 +
  定位符；agent 需要细节时调用本工具按 tool_call_id 取回完整原文。
- ``on_pipeline_end`` 生命周期钩子：管道结束整目录清理 ``{base}/{pipeline_id}``
  （复盘基于压缩摘要 + 轨迹，不依赖 spill 原文；生命周期 = 管道）。

pipeline_id 解析优先级：显式参数（param_inject 从 state 注入）>
``_call_context.pipeline_id``（tool_core 注入的前端路由键）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

from spill_store import cleanup_pipeline, read_spill, resolve_base_path

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("spill_retrieve_tool")

SPILL_RETRIEVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_call_id": {
            "type": "string",
            "description": "要取回的工具调用 ID（spill_guard 替换文本中定位符携带的 tool_call_id）",
        },
        "pipeline_id": {
            "type": "string",
            "description": "管道 ID（通常自动注入，无需手动提供）",
        },
    },
    "required": ["tool_call_id"],
}


def _spill_config() -> dict[str, Any]:
    """读内核注入配置（config_files → spill 命名空间），缺省回退默认值。"""
    cfg = plugin.get_config().get("spill", {})
    if not isinstance(cfg, dict):
        return {}
    return cfg


def _resolve_pipeline_id(explicit: str, kwargs: dict[str, Any]) -> str:
    """pipeline_id：显式参数 > _call_context > default。"""
    if explicit:
        return explicit
    ctx = kwargs.get("_call_context")
    if isinstance(ctx, dict) and ctx.get("pipeline_id"):
        return str(ctx["pipeline_id"])
    return "default"


def spill_retrieve(
    tool_call_id: str = "",
    pipeline_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """取回 spill 存档的完整原文。

    kwargs 接收内核注入字段（``_call_context`` 等）；测试可用 ``_spill_base``
    覆盖基准目录（生产路径由配置解析，见 spill_store.resolve_base_path）。
    """
    if not tool_call_id:
        return {"success": False, "error": "tool_call_id 不能为空"}

    cfg = _spill_config()
    base_override = kwargs.get("_spill_base")
    base = Path(base_override) if base_override else resolve_base_path(
        cfg.get("base_path", "./data/spill")
    )
    pid = _resolve_pipeline_id(pipeline_id, kwargs)

    r = read_spill(base, pid, tool_call_id)
    if not r.get("found"):
        return {
            "success": False,
            "error": r.get("error", "spill 原文不存在"),
            "data": {"tool_call_id": tool_call_id, "pipeline_id": pid},
        }
    return {
        "success": True,
        "data": {
            "tool_call_id": r["tool_call_id"],
            "pipeline_id": pid,
            "content": r["content"],
            "encoding": r["encoding"],
            "size_bytes": r["size_bytes"],
        },
    }


def _handle_pipeline_end(params: dict[str, Any]) -> None:
    """on_pipeline_end 钩子：清理该管道的 spill 目录（best-effort）。

    params 由内核 send_lifecycle_hook 注入（pipeline_id 等标签）；测试可用
    ``_spill_base`` 覆盖基准目录。
    """
    cfg = _spill_config()
    if cfg.get("cleanup_on_pipeline_end") is False:
        return
    pid = str(params.get("pipeline_id", "")).strip()
    if not pid:
        return
    base_override = params.get("_spill_base")
    base = Path(base_override) if base_override else resolve_base_path(
        cfg.get("base_path", "./data/spill")
    )
    removed = cleanup_pipeline(base, pid)
    logger.info("[spill_retrieve] on_pipeline_end 清理 %s：%d 个文件", pid, removed)


plugin.register_tool(
    "spill_retrieve",
    SPILL_RETRIEVE_SCHEMA,
    spill_retrieve,
    "取回 spill_guard 存档的工具大输出原文（按 tool_call_id）",
)
plugin.on_lifecycle("on_pipeline_end", _handle_pipeline_end)


def main() -> None:
    """MCP stdio 启动入口。"""
    plugin.run()


if __name__ == "__main__":
    main()
