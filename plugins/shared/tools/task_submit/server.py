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


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动：注入 chat.send_message 派发器（GAP-1 任务执行驱动）。

    tool.py 模块级注入点 set_chat_sender——提交成功后经 chat capability 的
    send_message（create 分支，引擎生成 pipeline_id）创建任务执行管道。
    能力句柄懒解析（协程内 get_capability），on_load 早于 capability 注入
    完成也能在真正派发时拿到。
    """
    import tool as tool_mod  # noqa: PLC0415

    async def _send(params: dict[str, Any]) -> dict[str, Any]:
        handle = plugin.get_capability("chat")
        return await handle.call("send_message", params)

    tool_mod.set_chat_sender(_send)

    async def _read_state_rows() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    tool_mod.set_state_reader(_read_state_rows)

    # agent_registry 查询接 agent_manager 插件的 agent.get 服务
    # （tool-executor 显式 plugin_id 跨插件通道）。
    # 结果信封：{"success": bool, "data": {"found": bool, "config": dict}}；
    # 服务不可用/未启用 → 异常或 success=false → lookup 返回 None → 磁盘回退。
    async def _agent_lookup(agent_id: str) -> dict[str, Any] | None:
        handle = plugin.get_capability("tool-executor")
        result = await handle.call(
            "invoke",
            {
                "plugin_id": "agent_manager",
                "tool_name": "agent.get",
                "args": {"agent_id": agent_id},
            },
        )
        if not isinstance(result, dict) or not result.get("success"):
            return None
        data = result.get("data")
        if not isinstance(data, dict) or not data.get("found"):
            return None
        config = data.get("config")
        return config if isinstance(config, dict) else None

    tool_mod.set_agent_registry_lookup(_agent_lookup)


# schema/description 单一事实源：tool.TaskSubmitTool.get_tool_definition()——
# 与 manifest 声明同源同形（平铺 goal_title + allOf 容器约束）。tool.py 模块级
# 仅依赖 SDK（领域模块均函数内懒加载），模块级 import 安全。
from tool import TaskSubmitTool  # noqa: E402

_TASK_SUBMIT_DEF = TaskSubmitTool.get_tool_definition()


@plugin.tool(
    name="task_submit",
    schema=_TASK_SUBMIT_DEF.input_schema,
    description=_TASK_SUBMIT_DEF.description,
)
async def task_submit(**kwargs):
    """任务提交。"""
    tool = TaskSubmitTool()
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
