#!/usr/bin/env python3
"""Task Submit 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

# extra 显式注入任务域依赖面：tasks/（平铺模块权威位）。
_paths = bootstrap_plugin(__file__, extra=("tasks",))

# 任务领域模块以 plugins/shared/tasks/ 为权威（0.2 平铺模块：
# service_access / task_types / agents_types …）。将其注入 sys.path，任务域
# 懒加载解析到该权威位置；跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。

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
    async def _send(params: dict[str, Any]) -> dict[str, Any]:
        handle = plugin.get_capability("chat")
        return await handle.call("send_message", params)

    _tool_mod.set_chat_sender(_send)

    async def _read_state_rows() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    _tool_mod.set_state_reader(_read_state_rows)

    # agent_registry 查询接 agent_manager 插件的 agent.get 服务
    # （tool-executor 显式 plugin_id 跨插件通道）。
    # 结果信封：{"success": bool, "data": {"found": bool, "config": dict}}。
    # 未命中（found=false）→ None；服务不可用/信封违约 → 抛异常——调用方
    # 把异常翻译成"配置服务不可用"，不与"Agent 不存在"（None）混淆。
    async def _agent_lookup(agent_id: str) -> dict[str, Any] | None:
        handle = plugin.get_capability("tool-executor")
        if handle is None:
            raise RuntimeError("tool-executor capability 未注入，agent 配置查询不可用")
        result = await handle.call(
            "invoke",
            {
                "plugin_id": "agent_manager",
                "tool_name": "agent.get",
                "args": {"agent_id": agent_id},
            },
        )
        if not isinstance(result, dict) or not result.get("success"):
            raise RuntimeError(f"agent_manager agent.get 服务调用失败: {result!r}")
        data = result.get("data")
        if not isinstance(data, dict) or not data.get("found"):
            return None
        config = data.get("config")
        if not isinstance(config, dict):
            raise RuntimeError(
                f"agent_manager agent.get 信封违约（found=true 但 config 非 dict）: {data!r}"
            )
        return config

    _tool_mod.set_agent_registry_lookup(_agent_lookup)

    # project.* 服务调用方（T6a：登记簿读面收口项目域主 project_manager，
    # capability handle 直连 provides namespace=project，search→skill 同款）。
    async def _project_service(method: str, args: dict[str, Any]) -> dict[str, Any]:
        handle = plugin.get_capability("project")
        return await handle.call(method, args)

    _tool_mod.set_project_service(_project_service)

    # workspace.prepare_task 服务（workspace_lifecycle 插件 provides namespace
    # ``workspace``）：出生协议第三段的工作空间同步创建。本插件只做信封收口
    # （ok=false / 非法信封 → 抛异常），坐标四形态决策归 workspace_lifecycle。
    async def _prepare_workspace(
        *, pipeline_id: str, state: dict[str, Any], execution_context: dict[str, Any]
    ) -> dict[str, Any]:
        handle = plugin.get_capability("workspace")
        resp = await handle.call(
            "prepare_task",
            {
                "pipeline_id": pipeline_id,
                "state": state,
                "execution_context": execution_context,
            },
        )
        if not isinstance(resp, dict):
            raise RuntimeError(f"workspace.prepare_task 返回非法信封: {resp!r}")
        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error") or "工作空间创建失败（原因未随行）"))
        updates = resp.get("state_updates")
        if not isinstance(updates, dict):
            raise RuntimeError(f"workspace.prepare_task 未返回工作空间坐标: {resp!r}")
        return updates

    _tool_mod.set_workspace_preparer(_prepare_workspace)


# schema/description 单一事实源：tool.TaskSubmitTool.get_tool_definition()——
# 与 manifest 声明同源同形（平铺 goal_title + allOf 容器约束）。tool.py 模块级
# 仅依赖 SDK（领域模块均函数内懒加载），模块级 import 安全。
# 合宿契约（light 组）：tool 裸名被多个成员持有，静息态 sys.modules 槽位常驻
# 他人模块——模块引用必须在装载期捕获（此刻本成员目录置顶 sys.path，裸名解析
# 到自身），on_load 经捕获引用接线，运行期不解析裸名。
from tool import TaskSubmitTool  # noqa: E402
import tool as _tool_mod  # noqa: E402

_TASK_SUBMIT_DEF = TaskSubmitTool.get_tool_definition()


@plugin.tool(
    name="task_submit",
    schema=_TASK_SUBMIT_DEF.input_schema,
    description=_TASK_SUBMIT_DEF.description,
)
async def task_submit(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """任务提交。"""
    tool = TaskSubmitTool()
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    # 失败载荷必须显式 success=False：内核 invoker 归一化只认显式 success 键
    # （task_manage 同构），裸 {"error": …} 会被包成 success=true 信封——
    # tasks/http_api 等按 success 判定的消费方会把拒绝载荷当成功 data 返回。
    # 同时透传 error_code 与结构化 metadata（H3/M2：编排键不存在/完备性失败
    # 等错误携 {missing_fields, orchestration_key, suggestion,
    # available_orchestrations} 等可编程字段——错误是值，不只给人读文本；
    # error 键语义不变，additive）。
    payload: dict[str, Any] = {"success": False, "error": result.error}
    if result.error_code:
        payload["error_code"] = result.error_code
    payload.update(
        {k: v for k, v in (result.metadata or {}).items() if v is not None}
    )
    return payload


if __name__ == "__main__":
    plugin.run()
