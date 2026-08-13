#!/usr/bin/env python3
"""Trigger Setup 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
# 触发器领域代码（triggers/）为本工具自有子包，位于本工具目录下，由上方
# sys.path 注入解析。不再依赖 0.1 兼容 shim。

from tool import TriggerSetupTool  # noqa: E402

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
