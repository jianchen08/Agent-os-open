#!/usr/bin/env python3
"""Task Management 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 任务领域模块以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块：service /
# state_machine / task_types / agents_types / service_access …）。将其注入
# sys.path 以便 tool.py 顶部的 `from service import …` / `from task_types import …`
# 直接解析到该权威位置。跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_TASKS_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system', 'tasks')
if os.path.isdir(_TASKS_DIR):
    sys.path.insert(0, _TASKS_DIR)

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
