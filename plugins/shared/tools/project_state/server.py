#!/usr/bin/env python3
"""project_state 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

# 工具目录入列（tool.py 平铺 import）
sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("project_state_tool")

# schema/description 单一事实源：tool.ProjectStateTool.get_tool_definition()
from tool import ProjectStateTool  # noqa: E402

_PROJECT_STATE_DEF = ProjectStateTool.get_tool_definition()


@plugin.tool(
    name="project_state",
    schema=_PROJECT_STATE_DEF.input_schema,
    description=_PROJECT_STATE_DEF.description,
)
async def project_state(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """查询或迁移项目方案工作流状态。"""
    tool = ProjectStateTool()
    result = await tool.execute(kwargs)
    if result.success and isinstance(result.output, dict):
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
