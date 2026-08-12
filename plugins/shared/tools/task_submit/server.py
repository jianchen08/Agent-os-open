#!/usr/bin/env python3
"""Task Submit 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 任务领域模块以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块：service_access /
# task_types / agents_types …）。将其注入 sys.path 以便 tool.py 内懒加载的
# `from service_access import …` / `from task_types import …` 直接解析到该权威位置。
# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_TASKS_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system', 'tasks')
if os.path.isdir(_TASKS_DIR):
    sys.path.insert(0, _TASKS_DIR)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("task_submit_tool")


@plugin.tool(
    name="task_submit",
    schema={
        "type": "object",
        "properties": {
            "target_type": {"type": "string", "enum": ["agent"]},
            "target_id": {"type": "string"},
            "goal": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}}, "required": ["title"]},
            "acceptance_criteria": {"type": "object"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "max_retries": {"type": "integer", "minimum": 0, "default": 3},
            "task_scope": {"type": "string", "enum": ["non_container", "container"], "default": "non_container"},
            "parent_task_id": {"type": "string"},
            "workspace": {"type": "string"},
            "isolation_level": {"type": "string", "enum": ["non_isolated", "isolated"]},
            "inherit": {"type": "object"},
        },
        "required": ["goal"],
    },
    description="任务提交工具",
)
async def task_submit(**kwargs):
    """任务提交。"""
    from tool import TaskSubmitTool  # noqa: PLC0415
    tool = TaskSubmitTool()
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
