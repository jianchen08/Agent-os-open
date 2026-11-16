#!/usr/bin/env python3
"""mode_material_inject input pipeline plugin MCP 服务端——纯接口适配层。

业务逻辑在 plugin.py/mode_material.py；本文件只做接口适配：
通过 MCP SDK 暴露为工具（职责终局三件：mode 观测回写 + persona 接管 +
组装器物料追加，设计 D10）。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import ModeMaterialInjectPlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("mode_material_inject_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> ModeMaterialInjectPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return ModeMaterialInjectPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize mode_material_inject plugin."""
    get_instance()  # 启动时预热，保持原 on_load 构造时机


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup mode_material_inject plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="mode_material_inject.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Mode Material Inject pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the mode_material_inject pipeline plugin.

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
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
