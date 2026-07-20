#!/usr/bin/env python3
"""multimodal_preprocessor input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/multimodal_preprocessor/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys

# 设置 sys.path：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from lingxi_plugin_sdk import AgentOSPlugin  # noqa: E402
from plugin import MultimodalPreprocessor  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("multimodal_preprocessor_pipeline")

_instance: MultimodalPreprocessor | None = None


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize multimodal_preprocessor plugin."""
    global _instance
    config = plugin.get_config()
    _instance = MultimodalPreprocessor(config=config)


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup multimodal_preprocessor plugin."""
    global _instance
    _instance = None


@plugin.tool(
    name="multimodal_preprocessor.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Multimodal Preprocessor pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the multimodal_preprocessor pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from pipeline.plugin import PluginContext  # noqa: PLC0415
    from pipeline.types import create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})
    result = await _instance.execute(ctx)

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
