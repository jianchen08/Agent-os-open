#!/usr/bin/env python3
"""Human Interaction 工具 MCP 服务端——接口适配层。"""
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

plugin = AgentOSPlugin("human_interaction_tool")

@plugin.tool(
    name="human_interaction",
    schema={"type": "object", "properties": {"mode": {"type": "string", "enum": ["choice", "conversation", "notification"]}, "title": {"type": "string"}, "description": {"type": "string"}, "options": {"type": "array"}, "questions": {"type": "array"}, "initial_message": {"type": "string"}, "file_paths": {"type": "array"}, "timeout_seconds": {"type": "number", "default": 86400}, "priority": {"type": "string", "default": "normal"}}, "required": ["mode", "title"]},
    description="与用户交互",
)
async def human_interaction(**kwargs):
    from tool import HumanInteractionTool  # noqa: PLC0415
    t = HumanInteractionTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
