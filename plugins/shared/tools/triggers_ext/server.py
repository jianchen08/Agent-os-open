#!/usr/bin/env python3
"""Trigger Setup 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
# 触发器领域代码（triggers/）为本工具自有子包，位于本工具目录下，由上方
# sys.path 注入解析。不再依赖 0.1 兼容 shim。

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("trigger_setup_tool")

@plugin.tool(
    name="trigger_setup",
    schema={"type": "object", "properties": {"action": {"type": "string"}, "trigger_type": {"type": "string"}, "config": {"type": "object"}}},
    description="触发器配置",
)
async def trigger_setup(**kwargs):
    from tool import TriggerSetupTool  # noqa: PLC0415
    t = TriggerSetupTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
