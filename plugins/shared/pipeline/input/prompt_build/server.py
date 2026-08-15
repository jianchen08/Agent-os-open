#!/usr/bin/env python3
"""prompt_build input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/prompt_build/plugin.py 原封不动复制到本目录，
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

from plugin import PromptBuildPlugin, set_memory_backend  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

# hindsight_memory 插件目录（wiring.py 所在处）加入 sys.path
_HINDSIGHT_MEMORY_DIR = os.path.join(_shared_dir, "system", "hindsight_memory")
if _HINDSIGHT_MEMORY_DIR not in sys.path:
    sys.path.insert(0, _HINDSIGHT_MEMORY_DIR)

from wiring import build_memory_backend  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("prompt_build_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> PromptBuildPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return PromptBuildPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize prompt_build plugin + 注入记忆后端。"""
    get_instance()  # 预热：构建插件单例（保持原 on_load 构造时机）
    backend = build_memory_backend(plugin)
    if backend:
        set_memory_backend(backend)
    else:
        logger.warning("[prompt_build_pipeline] 记忆后端未注入，功能降级")


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup prompt_build plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="prompt_build.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Prompt Build pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the prompt_build pipeline plugin.

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
