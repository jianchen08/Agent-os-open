#!/usr/bin/env python3
"""project_create 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

# 工具目录入列（tool.py 平铺 import）
sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("project_create_tool")

# schema/description 单一事实源：tool.ProjectCreateTool.get_tool_definition()
from tool import ProjectCreateTool  # noqa: E402

_PROJECT_CREATE_DEF = ProjectCreateTool.get_tool_definition()


@plugin.tool(
    name="project_create",
    schema=_PROJECT_CREATE_DEF.input_schema,
    description=_PROJECT_CREATE_DEF.description,
)
async def project_create(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """创建项目。"""
    tool = ProjectCreateTool()
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
