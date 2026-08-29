#!/usr/bin/env python3
"""Task Management 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 任务领域模块以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块：service /
# state_machine / task_types / agents_types / service_access …）。将其注入
# sys.path 以便 tool.py 顶部的 `from service import …` / `from task_types import …`
# 直接解析到该权威位置。另需 system/ 入列——service_access.get_task_service()
# 内部用 `from tasks.service import TaskService` 限定导入（M3 防误解析），
# 要求 `tasks` 包所在目录（system/）也在搜索路径上。跨插件共享类型走 SDK。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_TASKS_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system', 'tasks')
_SYSTEM_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system')
_SHARED_ROOT = os.path.join(_PROJECT_ROOT, 'plugins', 'shared')
for _d in (_TASKS_DIR, _SYSTEM_DIR, _SHARED_ROOT):
    if os.path.isdir(_d):
        sys.path.insert(0, _d)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("task_manage_tool")
@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动：注入 chat / pipeline-state / pipeline-executor 能力（GAP-1 统一）。"""
    import tool as tool_mod  # noqa: PLC0415

    async def _chat(params: dict[str, Any]) -> dict[str, Any]:
        handle = plugin.get_capability("chat")
        return await handle.call("send_message", params)

    async def _read_state_rows() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    async def _exec(params: dict[str, Any]) -> dict[str, Any]:
        handle = plugin.get_capability("pipeline-executor")
        return await handle.call(params["method"], params["params"])

    async def _list_traces(pipeline_id: str) -> list[dict[str, Any]]:
        # service-registry 约定：handle.call("<域>.<op>")。任务管道无 sessions 行，
        # 其 thread_id 恒等于自身 pipeline_id，故以 pipeline_id 作 thread_id 查。
        handle = plugin.get_capability("service-registry")
        rows = await handle.call("traces.list", {"thread_id": pipeline_id})
        return rows if isinstance(rows, list) else []

    tool_mod.set_chat_sender(_chat)
    tool_mod.set_state_reader(_read_state_rows)
    tool_mod.set_pipeline_executor(_exec)
    tool_mod.set_traces_reader(_list_traces)




# 注意：@plugin.tool 装饰器必须落在真正的 handler（task_manage）上——
# 若装在工厂函数上，SDK 会把工厂注册为 handler，调用时它忽略 kwargs、
# 直接返回内部闭包，工具结果序列化成 "<function ...>"。
@plugin.tool(
    name="task_manage",
    schema={
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': [
                        'get',
                        'continue',
                        'stop',
                        'delete',
                        'change',
                    ],
                    'description': '操作类型：\n- get：查询任务。不传 task_id 返回列表简表，传 task_id 返回详情\n- continue：继续执行（重试/恢复/注入指令，针对非容器任务）\n- stop：停止任务（统一进入 stopped 状态，针对非容器任务）\n- delete：删除任务\n- change：变更容器任务状态（仅L1，仅容器任务）。通过 status 参数指定目标状态，容器只是子任务集合，状态可自由变更（completed/failed/pending/running/stopped/timeout）。status=completed 时会清理子任务 worktree。',
                },
                'task_scope': {
                    'type': 'string',
                    'enum': [
                        'all',
                        'container',
                        'non_container',
                    ],
                    'description': '任务范围过滤（get 列表模式时生效）',
                    'default': 'all',
                },
                'task_id': {
                    'type': 'string',
                    'description': '目标任务 ID',
                },
                'task_ids': {
                    'type': 'array',
                    'items': {
                        'type': 'string',
                    },
                    'description': '批量任务 ID 列表（与 task_id 二选一）。适用于 continue/stop/delete 操作',
                },
                'status': {
                    'type': 'string',
                    'enum': [
                        'pending',
                        'running',
                        'stopped',
                        'completed',
                        'failed',
                        'timeout',
                    ],
                    'description': '双重用途：\n- get 列表模式：按状态筛选\n- change 操作：目标状态（必填），如 completed/failed/pending/running/stopped/timeout',
                },
                'reason': {
                    'type': 'string',
                    'description': '操作原因说明（stop/delete 时推荐填写）',
                },
                'message': {
                    'type': 'string',
                    'description': '注入的指令内容（continue 操作时可选）。\n该消息会以 user 角色注入到子任务的下一轮对话中。\n【内容粒度规则】\n1. 常规检查/提醒：只给方向性提示，不给具体执行步骤\n2. 纠正性注入（下级理解偏了、方向错了）：给出具体的纠正意见\n3. 错误修正（提交参数有误、路径错误）：给出具体修正内容\n4. 用户指令传递（用户有新要求或变更）：给出用户的具体要求\n禁止任何情况下给出工作流程级别的建议，下级 Agent 比你更清楚怎么执行。',
                },
                'container_reason': {
                    'type': 'string',
                    'description': '变更原因（change 操作时填写，记录到任务 metadata）',
                },
                'include_details': {
                    'type': 'boolean',
                    'description': '是否包含详细信息（get 详情模式生效）。设为 true 时返回 recent_activities 和 elapsed_seconds',
                    'default': False,
                },
                'include_agent_calls': {
                    'type': 'boolean',
                    'description': '是否只返回工具调用类型的活动记录（get 详情模式生效，自动启用详细信息）',
                    'default': False,
                },
                'parent_task_id': {
                    'type': 'string',
                    'description': '父任务 ID（get 列表模式时传入可筛选其下子任务）',
                },
                'project_id': {
                    'type': 'string',
                    'description': '项目 ID，用于筛选特定项目的任务',
                },
                'session_id': {
                    'type': 'string',
                    'description': '会话 ID，用于筛选特定会话的任务',
                },
                'limit': {
                    'type': 'integer',
                    'description': '返回数量限制，默认为50，最大100',
                    'default': 50,
                    'maximum': 100,
                },
                'show_all': {
                    'type': 'boolean',
                    'description': '是否显示当前会话的所有任务（含子任务的子任务）。默认 false，L1 只显示自己提交的任务。仅 L1 生效。',
                    'default': False,
                },
            },
            'required': [
                'action',
            ],
        },
    description="任务管理工具",
)
async def task_manage(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """任务管理。"""
    from tool import TaskTool  # noqa: PLC0415

    task_tool = TaskTool()
    result = await task_tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
