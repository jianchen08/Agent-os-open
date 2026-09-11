#!/usr/bin/env python3
"""prompt_build input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/prompt_build/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import sys
from functools import lru_cache

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

# pipeline/input 目录：plugin.py 压缩预算配置经 context_window_guard.plugin 复用
# （兄弟插件互相导入的路径前提；缺它则单实现收敛的 import 不可达——组根不随
# bootstrap 注入，见 ADR 2026-09-08-plugin-bootstrap-sink 决策 1）
if _paths.group_root not in sys.path:
    sys.path.insert(0, _paths.group_root)

from plugin import PromptBuildPlugin, set_memory_backend  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

from wiring import build_memory_backend  # noqa: E402  （共享裸名模块，共享根经 bootstrap 入 path）

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
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
