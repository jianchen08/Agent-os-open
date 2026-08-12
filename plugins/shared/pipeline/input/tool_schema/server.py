#!/usr/bin/env python3
"""tool_schema input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/tool_schema/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache

# 设置 sys.path：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from plugin import ToolSchemaPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("tool_schema_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> ToolSchemaPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return ToolSchemaPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize tool_schema plugin."""
    get_instance()  # 启动时预热，保持原 on_load 构造时机


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup tool_schema plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="tool_schema.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Tool Schema pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the tool_schema pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)

    # Core 插件返回 dict，Input/Output 返回 PluginResult/OutputResult
    if isinstance(result, dict):
        return result

    data: dict = {"state_updates": result.state_updates}
    route_sig = getattr(result, "route_signal", None)
    if route_sig:
        data["route_signal"] = {
            "route_type": route_sig.route_type,
            "target": route_sig.target,
            "reason": route_sig.reason,
        }
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
