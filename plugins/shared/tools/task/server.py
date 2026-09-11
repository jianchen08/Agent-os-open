#!/usr/bin/env python3
"""Task Management 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

# extra 显式注入任务域依赖面：system/tasks（平铺模块权威位，tool.py 顶部
# `from service import …` / `from task_types import …` 解析到此）+ system/
# （service_access.get_task_service() 内部 `from tasks.service import …`
# 限定导入要求 tasks 包目录在搜索路径上）。
_paths = bootstrap_plugin(__file__, extra=(os.path.join("system", "tasks"), "system"))

# 任务领域模块以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块：service /
# state_machine / task_types / agents_types / service_access …）。将其注入
# sys.path 以便 tool.py 顶部的 `from service import …` / `from task_types import …`
# 直接解析到该权威位置。另需 system/ 入列——service_access.get_task_service()
# 内部用 `from tasks.service import TaskService` 限定导入（M3 防误解析），
# 要求 `tasks` 包所在目录（system/）也在搜索路径上。跨插件共享类型走 SDK。

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

# 本插件 tool 模块在 exec 期绑定（不得改为 on_load 期/handler 内 `import tool`
# 懒加载）：合宿静息态下裸名 `tool` 槽位是其他成员的同名模块（task_evaluate
# 等成员在 exec 期绑定后占据槽位），运行期 import 会命中异成员模块；exec 期
# 处于宿主 loader 的裸名遮蔽保护窗口（异成员模块已摘除、自身目录在 sys.path
# 首位），解析结果必为本插件 tool.py。
import tool as tool_mod  # noqa: E402

plugin = AgentOSPlugin("task_manage_tool")
@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动：注入 chat / pipeline-state / pipeline-executor 能力（GAP-1 统一）。"""
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
        # service-registry 约定：handle.call("<域>.<op>")。按 pipeline_id 直查
        # 本管道 step 轨迹（traces.list_by_pipeline）——pipeline_id 是执行态
        # 唯一坐标；绑真会话的任务管道在 pipeline_sessions 落 (task→thread-xxx)
        # 映射，按 thread_id=pipeline_id 查恒空（recent_activities 恒 []）。
        handle = plugin.get_capability("service-registry")
        rows = await handle.call("traces.list_by_pipeline", {"pipeline_id": pipeline_id})
        return rows if isinstance(rows, list) else []

    async def _list_runs(pipeline_id: str) -> list[dict[str, Any]]:
        # pipeline-runs.list_by_pipeline：该管道全部 run 记录（含 created_at /
        # ended_at / status）——任务耗时起点终点数据源（elapsed_seconds）。
        handle = plugin.get_capability("service-registry")
        rows = await handle.call(
            "pipeline-runs.list_by_pipeline", {"pipeline_id": pipeline_id}
        )
        return rows if isinstance(rows, list) else []

    tool_mod.set_chat_sender(_chat)
    tool_mod.set_state_reader(_read_state_rows)
    tool_mod.set_pipeline_executor(_exec)
    tool_mod.set_traces_reader(_list_traces)
    tool_mod.set_runs_reader(_list_runs)




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
    task_tool = tool_mod.TaskTool()
    result = await task_tool.execute(kwargs)
    if result.success:
        # output 类型是 T | None：成功但无输出载荷 → 空 dict（契约仍是 object）
        return result.output if isinstance(result.output, dict) else {}
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
