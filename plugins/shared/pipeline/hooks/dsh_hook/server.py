#!/usr/bin/env python3
"""dsh_hook pipeline plugin MCP 服务端——纯接口适配层。

dsh_adapter.translator.translate_hooks_config 产出的链位步骤条目引用本插件
（invoke_entry=dsh_hook.execute）；内核经 per-plugin inputs 通道传参
（config["inputs"]，见 plugin.py 模块 docstring 契约）。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import DshHookPlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("dsh_hook_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> DshHookPlugin:
    """懒构建并缓存插件单例（线程安全）。"""
    config = plugin.get_config()
    return DshHookPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize dsh_hook plugin."""
    get_instance()  # 启动时预热，保持构造时机一致


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup dsh_hook plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="dsh_hook.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {
                "type": "object",
                "default": {},
                "description": "Plugin config; per-step inputs at config.inputs",
            },
        },
        "required": ["state"],
    },
    description="Execute DSH hook pipeline step",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the dsh_hook pipeline step.

    Args:
        state: Pipeline state dictionary.
        config: Plugin config; per-step inputs at config["inputs"].

    Returns:
        Execution result containing state updates.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await get_instance().execute(ctx)
    return {"state_updates": result.state_updates}
