#!/usr/bin/env python3
"""Task Evaluate 工具 MCP 服务端——接口适配层。"""
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

plugin = AgentOSPlugin("task_evaluate_tool")


@plugin.tool(
    name="task_evaluate",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["evaluate_single", "auto_complete"], "default": "auto_complete"},
            "metric_id": {"type": "string"},
            "summary": {"type": "string"},
        },
    },
    description="任务评估工具",
)
async def task_evaluate(**kwargs):
    """任务评估。"""
    from tool import TaskEvaluateTool  # noqa: PLC0415
    tool = TaskEvaluateTool()
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
