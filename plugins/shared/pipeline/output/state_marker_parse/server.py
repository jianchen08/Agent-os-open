#!/usr/bin/env python3
"""state_marker_parse output pipeline plugin MCP 服务端——纯接口适配层。

业务逻辑在 plugin.py；本文件只做接口适配：通过 MCP SDK 暴露为工具 +
注入 tool-executor 调用句柄（mode_evolution 先例同通道）。
"""
from __future__ import annotations

import base64
import json
import logging
from functools import lru_cache
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import StateMarkerParsePlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from agentos_plugin_sdk.capability import bind_capability_caller  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("state_marker_parse_pipeline")


async def _tool_executor_caller(
    tool_name: str, plugin_id: str, args: dict[str, Any]
) -> Any:
    """tool-executor 跨插件调用通道：(tool, plugin_id, args) → invoke 信封。

    句柄延迟到调用时解析——on_load 预热不依赖能力注入顺序（该能力可能晚于
    本插件注册，探针/生产同险）。
    """
    te = plugin.get_capability("tool-executor")
    caller = bind_capability_caller(te, "tool-executor")
    return await caller(
        "tool-executor.invoke",
        {"tool_name": tool_name, "plugin_id": plugin_id, "args": args},
    )


@lru_cache(maxsize=1)
def get_instance() -> StateMarkerParsePlugin:
    """懒构建并缓存插件单例（线程安全）。"""
    config = plugin.get_config()
    return StateMarkerParsePlugin(config=config, invoke_caller=_tool_executor_caller)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize state_marker_parse plugin."""
    get_instance()  # 启动时预热，保持原 on_load 构造时机


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup state_marker_parse plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="state_marker_parse.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute State Marker Parse pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the state_marker_parse pipeline plugin.

    Args:
        state: Pipeline state dictionary (messages 尾条 assistant 为解析输入).
        config: Plugin config overrides.

    Returns:
        {"state_updates": {...}}（context.state_updates / context.character_state_text）
    """
    instance = get_instance()
    ctx_state = dict(state or {})
    if config:
        merged = dict(instance._config)
        merged.update(config)
        instance = StateMarkerParsePlugin(
            config=merged, invoke_caller=instance._invoke
        )
    result = await instance.execute(type("Ctx", (), {"state": ctx_state, "config": {}})())
    return {"state_updates": result.state_updates}


# ── http.handle 分发（/ext/state_marker_parse/** 入口） ────────────────────


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
    description="HTTP endpoint handler for /ext/state_marker_parse/** (latest marker payload)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict:
    """按 path 分发到 state_marker_parse 端点（latest 载荷拉取，前端状态小卡源）。"""
    q = query or {}
    if path == "/ext/pipeline_state_marker_parse/latest" and method == "GET":
        pipeline_id = q.get("pipeline_id", "")
        payload = get_instance().latest_payload(pipeline_id) if pipeline_id else None
        body = json.dumps({"state_updates": payload}, ensure_ascii=False)
        return {
            "success": True,
            "data": {
                "status": 200,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                "body_encoding": "base64",
            },
        }
    return {
        "success": False,
        "error": f"未知端点: {path}",
        "data": {
            "status": 404,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": base64.b64encode(b'{"error": "not found"}').decode("ascii"),
            "body_encoding": "base64",
        },
    }


if __name__ == "__main__":
    plugin.run()
