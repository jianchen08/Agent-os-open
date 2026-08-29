#!/usr/bin/env python3
"""stop_check output pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/output/stop_check/plugin.py 原封不动复制到本目录，
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

from plugin import StopCheckPlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("stop_check_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> StopCheckPlugin:
    """懒构建并缓存插件单例（线程安全；替代模块级可变 `_instance` 全局）。"""
    config = plugin.get_config()
    return StopCheckPlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize stop_check plugin."""
    instance = get_instance()  # 启动时预热，保持原 on_load 构造时机

    # 注入 state 聚合读取器（pipeline-state capability）：任务实时终态检测
    # 的数据源——外部终态写入（task_evaluate）对运行中循环内存态不可见，
    # 聚合是唯一实时来源（用户裁定：任务终态当轮停止）
    async def _read_state_rows():
        handle = plugin.get_capability("pipeline-state")
        resp = await handle.call("list", {})
        return resp if isinstance(resp, list) else None

    # 经单例注入：共宿 sidecar 进程内裸名 plugin 模块可能被同进程其它插件
    # 的同名模块占位（平铺导入冲突），不能按模块名导入
    instance.set_state_reader(_read_state_rows)


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup stop_check plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="stop_check.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Stop Check pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the stop_check pipeline plugin.

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
