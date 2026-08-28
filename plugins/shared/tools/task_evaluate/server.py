#!/usr/bin/env python3
"""Task Evaluate 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 任务领域模块以 plugins/shared/system/tasks/ 为权威（0.2 平铺模块：
# service_access / task_types / agents_types …）。注入 sys.path 供 tool.py 的
# `from task_types import TaskStatus` / `from service_access import …` 直接解析。
# 评估类型面（_eval_core.py）位于本目录，已由上方 sys.path.insert 覆盖。
# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_TASKS_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system', 'tasks')
_SYSTEM_DIR = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'system')
# 跨插件共享契约模块（state_fields.py 等）位于 plugins/shared/ 根。
_SHARED_ROOT = os.path.join(_PROJECT_ROOT, 'plugins', 'shared')
for _d in (_TASKS_DIR, _SYSTEM_DIR, _SHARED_ROOT):
    if os.path.isdir(_d):
        sys.path.insert(0, _d)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("task_evaluate_tool")


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动：注入 state 读写面 + 评估执行器。

    读：评估输入从 state 读（pipeline-state.list）。
    写：评估终态（task.status/task.ended_at）经 pipeline-state.update 落
    state 单一真值——任务状态由任务域插件裁决，内核只管管道运行域
    （run 终态只广播事件，不再回写 task.status）。
    执行器：PipelineEvaluationExecutor（0.2 生产版——tool 型本地跑，agent 型
    派评估子管道继承任务工作区）。能力句柄懒解析（协程内 get_capability），
    on_load 早于 capability 注入完成也能在真正派发时拿到。
    """
    import tool as tool_mod  # noqa: PLC0415
    from _executor import PipelineEvaluationExecutor  # noqa: PLC0415

    async def _read_state_rows() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    async def _write_task_state(pipeline_id: str, fields: dict[str, Any]) -> None:
        handle = plugin.get_capability("pipeline-state")
        await handle.call("update", {"pipeline_id": pipeline_id, "fields": fields})

    async def _chat_send(params: dict[str, Any]) -> dict[str, Any]:
        handle = plugin.get_capability("chat")
        return await handle.call("send_message", params)

    tool_mod.set_state_reader(_read_state_rows)
    tool_mod.set_state_writer(_write_task_state)
    _executor_singleton = PipelineEvaluationExecutor(
        chat_send=_chat_send,
        state_rows=_read_state_rows,
    )
    tool_mod.set_default_executor(_executor_singleton)


@plugin.tool(
    name="task_evaluate",
    schema={
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': [
                        'evaluate_single',
                        'auto_complete',
                    ],
                    'description': '评估模式：evaluate_single-评估单个指标(需提供metric_id)，所有指标逐一通过后任务自动完成；auto_complete-评估所有未通过的指标(已通过的自动跳过)，默认',
                    'default': 'auto_complete',
                },
                'metric_id': {
                    'type': 'string',
                    'description': '评估指标ID，仅在evaluate_single模式时必填',
                },
                'summary': {
                    'type': 'string',
                    'description': "任务完成摘要（推荐填写）。内容应包含：1) 完成了什么工作（简要说明实现思路和做了哪些改动）；2) 产出了什么（文件、配置、数据等产物）；3) 产物的存放路径（相对路径，如 src/auth/login.py、config/rules.yaml）。示例：'实现了用户登录功能，新增 JWT 认证模块。产出：src/auth/login.py、src/auth/jwt_handler.py、tests/test_login.py。'评估器将依据此摘要了解任务成果并验证产物。",
                },
            },
            'required': [],
        },
    description="任务评估工具",
)
async def task_evaluate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """任务评估。"""
    from tool import TaskEvaluateTool  # noqa: PLC0415

    tool = TaskEvaluateTool()
    result = await tool.execute(kwargs)
    # 返回完整 ToolExecutionResult 信封（success/output/metadata）：metadata
    # 携带 result=completed / task_failed 等副作用信号，是内核 tool_core
    # 任务级副作用派生（task_evaluation_completed / has_task_failed）的
    # 唯一载体——剥成裸 output 会静默丢失评估证据（信封由内核归一层消费，
    # LLM 面仍只见 output）。
    if hasattr(result, "to_dict") and callable(result.to_dict):
        return result.to_dict()
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
