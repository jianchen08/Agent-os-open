#!/usr/bin/env python3
"""godot_mcp 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

# 工具目录入列（tool.py 平铺 import）
sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("godot_mcp_tool")

# schema/description 单一事实源：tool.GodotRunTool.get_tool_definition()
from tool import GodotRunTool  # noqa: E402

_GODOT_RUN_DEF = GodotRunTool.get_tool_definition()


@plugin.tool(
    name="godot_run",
    schema=_GODOT_RUN_DEF.input_schema,
    description=_GODOT_RUN_DEF.description,
)
async def godot_run(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """执行 Godot 编辑器命令。"""
    tool = GodotRunTool()
    result = await tool.execute(kwargs)
    if result.success and isinstance(result.output, dict):
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
