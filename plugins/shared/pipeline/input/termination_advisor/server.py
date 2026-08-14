#!/usr/bin/env python3
"""termination_advisor input pipeline plugin MCP 服务端——纯接口适配层。

仿 cost_control/server.py：通过 MCP SDK 暴露为工具。
frontend.emit 桥接（task_observability 1c）：把 FrontendEmitter 注入
PluginContext._services，插件经 ctx.get_service("frontend") 推送
termination_status 事件。
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

from agentos_plugin_sdk import AgentOSPlugin, FrontendEmitter  # noqa: E402
from plugin import TerminationAdvisorPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("termination_advisor_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> TerminationAdvisorPlugin:
    """懒构建并缓存插件单例。"""
    config = plugin.get_config()
    return TerminationAdvisorPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize termination advisor plugin."""
    get_instance()


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup termination advisor plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="termination_advisor.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute TerminationAdvisor pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the termination advisor pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})

    # frontend.emit 桥接：旧内核未声明 frontend capability 时优雅降级
    _emitter = FrontendEmitter.from_plugin(plugin)
    if _emitter is not None:
        ctx._services["frontend"] = _emitter

    result = await get_instance().execute(ctx)

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
