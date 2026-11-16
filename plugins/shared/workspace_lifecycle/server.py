#!/usr/bin/env python3
"""workspace_lifecycle input pipeline plugin MCP 服务端——纯接口适配层。"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

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

    # 注入 task.* 写面（pipeline-state.update）：init 创建工作空间后把坐标
    # 写进单键 task.ws_meta——update 写直入 state 注册表，运行中即时可见
    # （state_updates 合并的 task.ws_meta 要等引擎 step 投影），供
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
    from _libs.pipeline_base.plugin import PluginContext

    instance = get_instance()
    ctx = PluginContext(state=state, config=config or {})
    result = await instance.execute(ctx)
    # error 必须是结构化 PluginError（{message}）：裸字符串令内核 invoker
    # 反序列化 PluginResult 报 PARSE_ERROR，整个 state_updates 连带丢失
    payload: dict = {"state_updates": result.state_updates}
    if result.error:
        payload["error"] = {"message": str(result.error)}
    return payload


@plugin.tool(
    name="workspace.prepare_task",
    schema={
        "type": "object",
        "properties": {
            "pipeline_id": {"type": "string", "description": "已登记的任务管道 id（= task.id）"},
            "state": {"type": "object", "description": "身份登记之后的管道 state"},
            "execution_context": {
                "type": "object",
                "description": "工作区/隔离声明（管道 init 时才随 dispatch 注入，此处按同形传入）",
            },
        },
        "required": ["pipeline_id", "state"],
    },
    description=(
        "出生期同步创建任务工作空间（ADR 2026-10-17 四段式出生第三段）："
        "复用 init 体同一段创建逻辑，失败即失败（无降级）"
    ),
)
async def prepare_task(
    pipeline_id: str,
    state: dict,
    execution_context: dict | None = None,
) -> dict:
    """出生期同步准备任务工作空间：成功返回 ok=true + 坐标，失败 ok=false + 原因。"""
    result = await get_instance().prepare_task_workspace(
        pipeline_id=str(pipeline_id or ""),
        state=dict(state or {}),
        execution_context=execution_context or {},
    )
    payload: dict = {
        "ok": result.error is None,
        "state_updates": result.state_updates or {},
    }
    if result.error is not None:
        payload["error"] = str(result.error)
    return payload


if __name__ == "__main__":
    plugin.run()
