#!/usr/bin/env python3
"""Task Evaluate 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 任务领域模块以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块：
# service_access / task_types / agents_types …）。注入 sys.path 供 tool.py 的
# `from task_types import TaskStatus` / `from service_access import …` 直接解析。
# 评估类型面（_eval_core.py）位于本目录，已由上方 sys.path.insert 覆盖。
# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_TASKS_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system', 'tasks')
_SYSTEM_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system')
for _d in (_TASKS_DIR, _SYSTEM_DIR):
    if os.path.isdir(_d):
        sys.path.insert(0, _d)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("task_evaluate_tool")


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动：注入 state 聚合读取器（GAP-1 统一——评估输入从 state 读）。"""
    import tool as tool_mod  # noqa: PLC0415

    async def _read_state_rows() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    tool_mod.set_state_reader(_read_state_rows)


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
