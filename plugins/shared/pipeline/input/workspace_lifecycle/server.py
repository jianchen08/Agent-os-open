#!/usr/bin/env python3
"""workspace_lifecycle input pipeline plugin MCP 服务端——纯接口适配层。"""
from __future__ import annotations

import logging
from typing import Any
import os
import sys
from functools import lru_cache

# 设置 sys.path：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from plugin import WorkspaceLifecyclePlugin, set_state_reader, set_task_state_writer  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("workspace_lifecycle_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> WorkspaceLifecyclePlugin:
    """懒构建并缓存插件单例。"""
    config = plugin.get_config()
    return WorkspaceLifecyclePlugin(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    # 注入 state 聚合读取器（task_tree 直读实现的数据源）
    try:
        async def _read_state_rows() -> list[dict[str, Any]]:
            handle = plugin.get_capability("pipeline-state")
            rows = await handle.call("list", {})
            return rows if isinstance(rows, list) else []

        set_state_reader(_read_state_rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workspace_lifecycle] state reader 注入失败: %s", exc)

    # 注入 task.* 写面（pipeline-state.update）：init 创建工作空间后把 ws_meta
    # 以 task.ws_meta 镜像进任务域键空间——update 写直入 state 注册表，运行中
    # 即时可见（state_updates 合并的 ws_meta 键要等引擎回写快照），供
    # task_evaluate 合并门控等运行中读面即时消费。
    try:
        async def _write_task_state(pipeline_id: str, fields: dict[str, Any]) -> None:
            handle = plugin.get_capability("pipeline-state")
            await handle.call("update", {"pipeline_id": pipeline_id, "fields": fields})

        set_task_state_writer(_write_task_state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workspace_lifecycle] task state writer 注入失败: %s", exc)
    """Initialize workspace_lifecycle plugin."""
    get_instance()


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup workspace_lifecycle plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="workspace_lifecycle.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute Workspace Lifecycle pipeline plugin (init bootstrap / exit finalize)",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the workspace_lifecycle pipeline plugin."""
    from pipeline.plugin import PluginContext

    instance = get_instance()
    ctx = PluginContext(state=state, config=config or {})
    result = await instance.execute(ctx)
    # error 必须是结构化 PluginError（{message}）：裸字符串令内核 invoker
    # 反序列化 PluginResult 报 PARSE_ERROR，整个 state_updates 连带丢失
    payload: dict = {"state_updates": result.state_updates}
    if result.error:
        payload["error"] = {"message": str(result.error)}
    return payload


if __name__ == "__main__":
    plugin.run()
