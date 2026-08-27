#!/usr/bin/env python3
"""Web Operate 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("web_operate_tool")

@plugin.tool(
    name="web_operate",
    schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["get", "post", "fetch"], "default": "get"}, "url": {"type": "string"}, "headers": {"type": "object"}, "data": {"type": "object"}, "params": {"type": "object"}, "timeout": {"type": "integer", "default": 30}, "extract_text": {"type": "boolean", "default": True}}, "required": ["action", "url"]},
    description="Web 操作",
)
async def web_operate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    from tool import WebTool  # noqa: PLC0415
    t = WebTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
