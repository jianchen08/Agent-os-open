#!/usr/bin/env python3
"""Trigger Setup 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 源码目录加入 sys.path，使老代码的 from tools.* 导入可用
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from lingxi_plugin_sdk import AgentOSPlugin  # noqa: E402

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
