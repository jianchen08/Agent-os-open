#!/usr/bin/env python3
"""Memory 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 源码目录加入 sys.path，使老代码的 from tools.* 导入可用
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("memory_tool")

@plugin.tool(
    name="memory",
    schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["store", "retrieve", "import_text", "update", "delete"]}, "content": {"type": "string"}, "query": {"type": "string"}, "name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]},
    description="存储和检索知识、情景记忆",
)
async def memory(**kwargs):
    from tool import MemoryTool  # noqa: PLC0415
    t = MemoryTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
