#!/usr/bin/env python3
"""pause_guard input pipeline plugin MCP 服务端——纯接口适配层。

本目录为实现模块，本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

# extra 显式注入任务域依赖面：system/（tasks 包目录，plugin.py 懒加载
# `from tasks.types import TaskStatus` 的解析前提），不靠同宿插件副作用。
bootstrap_plugin(__file__, extra=('system',))

from plugin import PauseGuardPlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("pause_guard_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> PauseGuardPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return PauseGuardPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize pause_guard plugin."""
    get_instance()  # 启动时预热，保持原 on_load 构造时机


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup pause_guard plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="pause_guard.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Pause Guard pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the pause_guard pipeline plugin.

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
