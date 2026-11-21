#!/usr/bin/env python3
"""isolation_guard input pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/input/isolation_guard/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import IsolationGuard, set_env_caller  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from agentos_plugin_sdk.capability import bind_capability_caller  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("isolation_guard_pipeline")

# 容器落地直达 isolation_service 正门（系统插件工具不在 LLM 工具注册表，
# tool-executor.invoke 反查必失败，显式带 plugin_id——同 environment_lifecycle
# 的销毁调用方惯例）。
_ISOLATION_PLUGIN_ID = "isolation_service"


@lru_cache(maxsize=1)
def get_instance() -> IsolationGuard:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return IsolationGuard(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize isolation_guard plugin + 注入环境服务调用方。

    实例构造内含后端探测（docker/wsl 子进程，timeout 3s）：下放线程池，
    避免 on_load 期间冻住合宿宿主事件循环心跳。
    """
    await asyncio.to_thread(get_instance)
    try:
        handle = plugin.get_capability("tool-executor")
    except KeyError:
        handle = None
    if handle is None:
        logger.warning(
            "[isolation_guard_pipeline] tool-executor 能力未注入，容器落地降级 blocked"
        )
        set_env_caller(None)
        return
    invoke = bind_capability_caller(handle, "tool-executor")

    async def _env_caller(tool_name: str, args: dict) -> Any:
        return await invoke(
            "tool-executor.invoke",
            {"tool_name": tool_name, "plugin_id": _ISOLATION_PLUGIN_ID, "args": args},
        )

    set_env_caller(_env_caller)


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup isolation_guard plugin（调用方一并摘除，防卸载窗口期残留句柄）。"""
    set_env_caller(None)
    get_instance.cache_clear()


@plugin.tool(
    name="isolation_guard.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Isolation Guard pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the isolation_guard pipeline plugin.

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
