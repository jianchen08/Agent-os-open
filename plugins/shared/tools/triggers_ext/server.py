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
# 原先这里是残桩 schema（仅 action/trigger_type/config），导致 message/interval/
# pipeline_id 等字段在分发层被丢，execute() 报「缺少注入参数: pipeline_id」。
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


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动（主事件循环内）：接通触发检查循环 + 注入器。

    修复两处 0.2 迁移遗留：
    1. set_main_loop 此前无人调用 → _main_loop 恒为 None → 触发器到期直接跳过；
    2. 注入路径此前依赖已删除的 pipeline.message_bus → 现改为内核 chat capability。
    """
    mgr = get_trigger_manager()
    mgr.set_main_loop(asyncio.get_running_loop())
    mgr.set_injector(_make_trigger_injector())
    mgr.start_check_loop()


@plugin.on_unload
async def _on_unload(_params: dict[str, Any]) -> None:
    """sidecar 卸载：停止后台检查线程。"""
    get_trigger_manager().stop_check_loop()


@plugin.tool(
    name="trigger_setup",
    schema=_TRIGGER_SETUP_SCHEMA,
    description=_TRIGGER_SETUP_DESCRIPTION,
)
async def trigger_setup(**kwargs):
    t = TriggerSetupTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}


if __name__ == "__main__":
    plugin.run()
