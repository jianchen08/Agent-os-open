#!/usr/bin/env python3
"""context_window_guard input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/context_window_guard/plugin.py 原封不动复制到本目录，
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

from plugin import (
    ContextWindowGuardPlugin,
    set_capability_caller,
    set_frontend_emit,
    set_memory_backend,
)  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

# hindsight_memory 插件目录（wiring.py 所在处）加入 sys.path
_HINDSIGHT_MEMORY_DIR = os.path.join(_shared_dir, "system", "hindsight_memory")
if _HINDSIGHT_MEMORY_DIR not in sys.path:
    sys.path.insert(0, _HINDSIGHT_MEMORY_DIR)

from wiring import build_memory_backend, make_capability_caller  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("context_window_guard_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> ContextWindowGuardPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return ContextWindowGuardPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize context_window_guard plugin + 注入记忆后端 + capability_caller。"""
    get_instance()  # 预热：构建插件单例（保持原 on_load 构造时机）
    backend = build_memory_backend(plugin)
    if backend:
        set_memory_backend(backend)
    else:
        logger.warning("[context_window_guard_pipeline] 记忆后端未注入，功能降级")
    caller = make_capability_caller(plugin)
    if caller:
        set_capability_caller(caller)
    else:
        logger.warning("[context_window_guard_pipeline] capability_caller 未注入")
    # 前端一次性事件通道（压缩失败透传用；内核内置能力，缺失只降级日志）
    try:
        frontend_handle = plugin.get_capability("frontend")
    except KeyError:
        frontend_handle = None
    if frontend_handle is not None:

        async def _frontend_emit(event: str, payload: dict, thread_id: str) -> None:
            await frontend_handle.call(
                "emit",
                {"event": event, "payload": payload, "thread_id": thread_id},
            )

        set_frontend_emit(_frontend_emit)
    else:
        set_frontend_emit(None)
        logger.warning(
            "[context_window_guard_pipeline] frontend 能力未注入，压缩失败不推前端"
        )


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup context_window_guard plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="context_window_guard.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Context Window Guard pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the context_window_guard pipeline plugin.

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
