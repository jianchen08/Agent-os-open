#!/usr/bin/env python3
"""Task Submit 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 兼容 shim 目录加入 sys.path，使老代码的 from core.* / tools.* / utils.* /
# triggers.* / tasks.* / agents.* 导入解析到 legacy_0_1_compat 下的精简副本。
#详见 plugins/shared/legacy_0_1_compat/__init__.py。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_COMPAT_ROOT = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'legacy_0_1_compat')
if os.path.isdir(_COMPAT_ROOT):
    sys.path.insert(0, _COMPAT_ROOT)

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
