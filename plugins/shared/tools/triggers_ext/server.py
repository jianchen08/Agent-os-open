#!/usr/bin/env python3
"""Trigger Setup 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
# 触发器领域代码（triggers/）为本工具自有子包，位于本工具目录下，由上方
# sys.path 注入解析。不再依赖 0.1 兼容 shim。

from tool import TriggerSetupTool  # noqa: E402
from triggers.manager import get_trigger_manager  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("trigger_setup_tool")

# MCP 暴露的 schema 必须与后端 Tool.get_tool_definition() 一致——
# input_schema 本身不含 injected_params（pipeline_id/execution_id），由 param_inject
# 从 pipeline state 注入，故直接复用 input_schema 作为 LLM 可见 schema。
_TD = TriggerSetupTool.get_tool_definition()
_TRIGGER_SETUP_SCHEMA = _TD.input_schema
_TRIGGER_SETUP_DESCRIPTION = _TD.description


def _make_trigger_injector() -> Any:
    """构造 0.2 sidecar 触发消息投递器。

    到期触发时 TriggerManager 经此把消息投给内核 ``chat.send_message`` capability，
    复用前端 WS 派发路径唤醒 agent。能力句柄懒解析（在协程内 get_capability），
    即便 on_load 时机早于 capability 声明完成，也能在真正触发时拿到（或抛错由 manager 记录）。
    """

    async def _inject(pipeline_id: str, message: str, user_id: str) -> Any:
        handle = plugin.get_capability("chat")
        return await handle.call(
            "send_message",
            {"pipeline_id": pipeline_id, "message": message, "user_id": user_id},
        )

    return _inject


def _make_state_provider() -> Any:
    """构造 state 聚合行提供者（GAP-2 CONDITION 求值上下文）。

    经内核 ``pipeline-state`` capability 读管道 state 聚合（与
    /api/v1/pipelines/state 同构：扁平点号键行）。能力句柄懒解析
    （协程内 get_capability），读取失败由 manager 记录并跳过本轮。
    """

    async def _provide() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    return _provide


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动（主事件循环内）：接通触发检查循环 + 注入器 + 双桥。

    1. set_main_loop 注入运行循环 → 触发器到期可调度；
    2. 注入器经内核 chat capability 投递触发消息；
    3. state provider 注入 → CONDITION 触发器有了求值上下文（state 聚合轮询）；
    4. 域事件桥就绪标记 → manifest 声明 domain_event hook + 下方
       ``_on_domain_event`` 处理器注册后，内核终态事件可达 evaluate_event。
    """
    mgr = get_trigger_manager()
    mgr.set_main_loop(asyncio.get_running_loop())
    mgr.set_injector(_make_trigger_injector())
    mgr.set_state_provider(_make_state_provider())
    mgr.set_event_bridge_ready()
    mgr.start_check_loop()


@plugin.on_unload
async def _on_unload(_params: dict[str, Any]) -> None:
    """sidecar 卸载：停止后台检查线程。"""
    get_trigger_manager().stop_check_loop()


@plugin.on_domain_event
async def _on_domain_event(params: dict[str, Any]) -> None:
    """域事件入口（GAP-2 EVENT 接线）。

    内核在 run 终态（completed/failed/suspended）经 broadcast_domain_event
    推送域事件（state 带 ``task.*`` 字段时派生 ``task_completed`` /
    ``task_failed``）；此处转发给 TriggerManager.evaluate_event 匹配触发器。
    """
    event_name = params.get("event") or ""
    if not event_name:
        return
    await get_trigger_manager().handle_domain_event(event_name, params)


@plugin.tool(
    name="trigger_setup",
    schema=_TRIGGER_SETUP_SCHEMA,
    description=_TRIGGER_SETUP_DESCRIPTION,
)
async def trigger_setup(**kwargs):
    t = TriggerSetupTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/trigger_setup_tool/** (trigger management REST)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """REST 分发到 triggers 域 9 端点。"""
    from http_api import handle_http_dispatch  # noqa: PLC0415

    return await handle_http_dispatch(path, method, raw_body, query or {})


if __name__ == "__main__":
    plugin.run()
