#!/usr/bin/env python3
"""environment_lifecycle input pipeline plugin MCP 服务端——纯接口适配层。"""
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

from plugin import EnvironmentLifecyclePlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("environment_lifecycle_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> EnvironmentLifecyclePlugin:
    """懒构建并缓存插件单例。"""
    config = plugin.get_config()
    return EnvironmentLifecyclePlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize environment_lifecycle plugin."""
    get_instance()


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup environment_lifecycle plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="environment_lifecycle.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Environment Lifecycle pipeline plugin (init resolver / exit release)",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the environment_lifecycle pipeline plugin."""
    from pipeline.plugin import PluginContext

    instance = get_instance()
    ctx = PluginContext(state=state, config=config or {})
    result = await instance.execute(ctx)
    # error 必须是结构化 PluginError（{message}）：裸字符串令内核 invoker
    # 反序列化 PluginResult 报 PARSE_ERROR，整个 state_updates 连带丢失
    payload: dict = {"state_updates": result.state_updates}
    if result.error:
        payload["error"] = {"message": str(result.error)}
    return payload


if __name__ == "__main__":
    plugin.run()
