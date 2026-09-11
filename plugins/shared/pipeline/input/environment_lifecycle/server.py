#!/usr/bin/env python3
"""environment_lifecycle input pipeline plugin MCP 服务端——纯接口适配层。"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import EnvironmentLifecyclePlugin, set_destroy_caller  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from agentos_plugin_sdk.capability import bind_capability_caller  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("environment_lifecycle_pipeline")

# 系统插件工具不在 LLM 工具注册表，tool-executor.invoke 反查必失败，
# 显式带 plugin_id 直达（同 hindsight.recall 惯例）。
_ISOLATION_PLUGIN_ID = "isolation_service"


@lru_cache(maxsize=1)
def get_instance() -> EnvironmentLifecyclePlugin:
    """懒构建并缓存插件单例。"""
    config = plugin.get_config()
    return EnvironmentLifecyclePlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize environment_lifecycle plugin + 注入销毁调用方。"""
    get_instance()
    try:
        handle = plugin.get_capability("tool-executor")
    except KeyError:
        handle = None
    if handle is None:
        logger.warning(
            "[environment_lifecycle_pipeline] tool-executor 能力未注入，exit 环境销毁降级不可用"
        )
        set_destroy_caller(None)
        return
    invoke = bind_capability_caller(handle, "tool-executor")

    async def _destroy_caller(tool_name: str, args: dict) -> Any:
        return await invoke(
            "tool-executor.invoke",
            {"tool_name": tool_name, "plugin_id": _ISOLATION_PLUGIN_ID, "args": args},
        )

    set_destroy_caller(_destroy_caller)


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
