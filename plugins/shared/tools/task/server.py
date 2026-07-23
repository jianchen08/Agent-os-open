#!/usr/bin/env python3
"""Task Management 工具 MCP 服务端——接口适配层。"""
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

plugin = AgentOSPlugin("task_manage_tool")


@plugin.tool(
    name="task_manage",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "continue", "stop", "delete", "change"],
            },
            "task_id": {"type": "string"},
            "task_ids": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string"},
            "reason": {"type": "string"},
            "message": {"type": "string"},
            "container_reason": {"type": "string"},
            "include_details": {"type": "boolean", "default": False},
            "include_agent_calls": {"type": "boolean", "default": False},
            "parent_task_id": {"type": "string"},
            "project_id": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
            "show_all": {"type": "boolean", "default": False},
            "task_scope": {"type": "string", "enum": ["all", "container", "non_container"]},
        },
        "required": ["action"],
    },
    description="任务管理工具",
)
async def task_manage(**kwargs):
    """任务管理。"""
    from tool import TaskTool  # noqa: PLC0415

    task_tool = TaskTool()
    result = await task_tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
