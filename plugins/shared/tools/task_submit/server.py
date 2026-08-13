#!/usr/bin/env python3
"""Task Submit 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 任务领域模块以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块：service_access /
# task_types / agents_types …）。将其注入 sys.path 以便 tool.py 内懒加载的
# `from service_access import …` / `from task_types import …` 直接解析到该权威位置。
# 另需 system/ 入列——service_access.get_task_service() 内部用
# `from tasks.service import TaskService` 限定导入（M3 防误解析）。
# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_TASKS_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system', 'tasks')
_SYSTEM_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system')
for _d in (_TASKS_DIR, _SYSTEM_DIR):
    if os.path.isdir(_d):
        sys.path.insert(0, _d)

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
def _make_pipeline_caller() -> Any | None:
    """从内核注入的能力句柄构造 pipeline-executor caller（async fn `(method, params)`）。

    桥接说明：caller 约定传入完整 wire method（如 "pipeline-executor.start_run"），
    而 SDK CapabilityHandle.call 会拼接 ``f"{cap}.{method}"``，因此这里剥掉已含的
    能力前缀，避免双命名空间。能力未注入时返回 None——提交降级为仅落库不执行。
    """
    try:
        handle = plugin.get_capability("pipeline-executor")
    except KeyError:
        return None
    prefix = "pipeline-executor."

    async def _call(method: str, params: dict[str, Any]) -> Any:
        stripped = method[len(prefix):] if method.startswith(prefix) else method
        return await handle.call(stripped, params)

    return _call


async def task_submit(**kwargs):
    """任务提交。"""
    from tool import TaskSubmitTool, _configure_launcher  # noqa: PLC0415

    _configure_launcher(_make_pipeline_caller())
    tool = TaskSubmitTool()
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
