#!/usr/bin/env python3
"""计划模式插件——模式元数据服务面 + 面板数据面（出厂种子，随用户空间播种分发）。

服务契约（manifest capabilities.services；wire = MCP tools/call）：
- mode.describe：返回 {mode, name, chain, weights, budget, profile_path, panel_page_id}
- mode.get_profile：返回包内 profile.yaml 解析后的完整 dict

profile 与本插件同目录内打包（种子单元自包含，目录级播种/升级/回退的结构前提）；
消费方（eval_harness 等）经内核服务调用获取（tool-executor 显式 plugin_id 通道），
本插件不声明 LLM 工具面。

webview 面板页（工作区 tab 面板，manifest detachable 声明悬浮窗）由 http_endpoints
供页；面板数据面（2026-09-17 成熟化，ADR 2026-09-17-mode-panel-mature-interfaces，
对标 Linear/Notion Projects 规划台：项目列表 + 任务链树 + 状态徽标 + 会话消息流）：
- 内核读数桥：on_load 注入 pipeline-state / messages / tool-executor providers
  （monitoring kernel_reads 同构；能力未就绪 handler 降级空载荷，前端诚实空态）。
- 数据端点：/ext/mode_planning/data/{bootstrap,projects,sessions,tasks,discussions}。
- 写动作：/data/actions/{create_project,plan} 经 tool-executor 真派发
  （eval_harness 先例）：创建项目调 project_create 工具；发起规划调 task_submit
  （target=profile.chain 执行者池首选，mode=planning，task_kind=planning_plan）
  进真实管道，session_id 有则透传（宿主 ctx.sync 下发的对话框会话，写动作
  送入对话框线程）。

调查结论（/data 端点口径依据，2026-09-17 只读核查）：
- tasks 插件 id=task_service，capabilities.services = task.create / task.get /
  task.transition / task.list / task.cancel / task.delete / task.get_transitions /
  http.handle——**无 projects 读服务**：projects = 真实文件夹 + 登记行
  （plugins/shared/project_registry.py，ProjectModel 含 title / status /
  workflow_state[plan|running|done] / auto_execute），只暴露 7 个 HTTP 端点
  /ext/task_service/projects*。故 /data/projects 从 pipeline-state 行按
  task.parent_project_id 分组诚实派生（登记簿字段派生口径不可得，留空不造假）；
  登记簿读服务出现后经 "projects" provider 注入即切登记态（行为测试两态覆盖）。
- task.list 服务单层返回 {"tasks":[{id,title,status,priority}],"total"}，
  parent_task_id 过滤子任务；项目→任务锚 = state 行 task.parent_project_id
  （task_submit 双写镜像 metadata.project_id）。任务树 = 项目行作根 + 后端单层
  查询（/data/tasks?parent_task_id=），前端按 parent 递归拉取组树。
- project_create 工具（id=project_create_tool）必填仅 goal；path 可选（缺省
  {工作空间根}/projects/<标题>）；project_type 枚举 auto|godot（默认 auto）。
- 跨插件通道：tool-executor.invoke 显式 plugin_id（eval_harness 先例）——
  service-registry 能力只路由内核域（messages.list 等），task_service 的
  task.list 服务必须经 tool-executor 调用。

种子单元自包含：HttpHandleResponse 封装与读数桥在此内联——共享根裸模块对
用户空间播种副本不可达（bootstrap 的 shared_root 指纹只认仓内 plugins/shared 布局）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller

plugin = AgentOSPlugin("mode_planning")

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.yaml")

_DESCRIBE_FIELDS = ("mode", "name", "chain", "weights", "budget", "panel_page_id")


def load_profile(path: str = _PROFILE_PATH) -> dict[str, Any]:
    """读取包内 profile.yaml；空文件或缺 mode 键视为种子损坏（fail-closed）。"""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or not data.get("mode"):
        raise ValueError(f"profile 种子损坏（缺 mode 键）: {path}")
    return data


@plugin.tool(
    name="mode.describe",
    schema={"type": "object", "properties": {}},
    description="返回计划模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
    output_schema={
        "type": "object",
        "required": ["mode", "name", "chain", "weights", "budget", "profile_path", "panel_page_id"],
        "properties": {
            "mode": {"type": "string"},
            "name": {"type": "string"},
            "chain": {"type": "object"},
            "weights": {"type": "object"},
            "budget": {"type": "object"},
            "profile_path": {"type": "string"},
            "panel_page_id": {"type": "string"},
        },
    },
)
async def mode_describe() -> dict[str, Any]:
    profile = load_profile()
    describe = {field: profile[field] for field in _DESCRIBE_FIELDS}
    describe["profile_path"] = _PROFILE_PATH
    return describe


@plugin.tool(
    name="mode.get_profile",
    schema={"type": "object", "properties": {}},
    description="返回计划模式包内 profile.yaml 解析后的内容",
    output_schema={
        "type": "object",
        "required": ["mode"],
        "properties": {"mode": {"type": "string"}},
    },
)
async def mode_get_profile() -> dict[str, Any]:
    return load_profile()


# ── 内核读数桥（monitoring kernel_reads 同构：provider 注册 + 未就绪降级空）────────

_PROVIDERS: dict[str, Any] = {}
_WARNED: set[str] = set()

logger = logging.getLogger("mode_planning")


def _set_provider(name: str, fn: Any) -> None:
    """注册读/派发 provider（幂等，重复注入以后者为准）。"""
    _PROVIDERS[name] = fn
    _WARNED.discard(name)


def reset_providers() -> None:
    """清空全部 provider（单测隔离用）。"""
    _PROVIDERS.clear()
    _WARNED.clear()


async def _call_provider(name: str, **kwargs: Any) -> Any:
    """调用已注入 provider；未注入/调用失败时 warn 一次并返回空（读面降级）。"""
    fn = _PROVIDERS.get(name)
    if fn is None:
        if name not in _WARNED:
            _WARNED.add(name)
            logger.warning("provider %s 未注入（内核能力不可用），降级空数据", name)
        return []
    try:
        return await fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 —— 读面降级：能力失败不崩 handler
        if name not in _WARNED:
            _WARNED.add(name)
            logger.warning("provider %s 调用失败（降级空数据）: %s", name, exc)
        return []


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """注入内核读 providers（调用时惰性取能力——on_load 时机能力握手未必完成）。"""

    async def _state_rows() -> Any:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    async def _messages(pipeline_id: str, limit: int | None = None) -> Any:
        params: dict[str, Any] = {"pipeline_id": pipeline_id}
        if limit is not None:
            params["limit"] = int(limit)
        handle = plugin.get_capability("service-registry")
        return await handle.call("messages.list", params)

    async def _task_list(parent_task_id: str) -> Any:
        te = plugin.get_capability("tool-executor")
        caller = bind_capability_caller(te, "tool-executor")
        return await caller(
            "tool-executor.invoke",
            {"tool_name": "task.list", "plugin_id": "task_service",
             "args": {"parent_task_id": parent_task_id}},
        )

    async def _invoke(payload: dict[str, Any]) -> Any:
        te = plugin.get_capability("tool-executor")
        caller = bind_capability_caller(te, "tool-executor")
        return await caller("tool-executor.invoke", payload)

    _set_provider("pipeline-state", _state_rows)
    _set_provider("messages", _messages)
    _set_provider("task-list", _task_list)
    _set_provider("tool-executor", _invoke)
    # "projects" 不注入：task_service 无 projects 读服务（登记簿仅 HTTP 端点），
    # /data/projects 走派生口径；登记簿读服务出现后在此 _set_provider("projects", …)
    # 即切登记态（归一形状见 _norm_registry_projects）。


# ── 数据归一（pipeline-state 行 = 扁平点号键摘要；mode/task.* 出口白名单）──────────

def _norm_sessions(rows: Any) -> list[dict[str, Any]]:
    """全量 state 行 → 本模式会话行（mode 键过滤 + 面板字段裁剪）。"""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("mode") or "") != "planning":
            continue
        items.append(
            {
                "pipeline_id": str(row.get("pipeline_id") or ""),
                "thread_id": str(row.get("thread_id") or ""),
                "agent_id": str(row.get("agent_id") or ""),
                "run_status": str(row.get("run_status") or ""),
                "goal": str(row.get("task.goal") or row.get("input") or ""),
                "task_status": str(row.get("task.status") or ""),
                "task_id": str(row.get("task.id") or ""),
                "parent_project_id": str(row.get("task.parent_project_id") or ""),
                "message_count": row.get("message_count") or 0,
            }
        )
    items.sort(key=lambda x: x["pipeline_id"], reverse=True)
    return items


def _norm_messages(rows: Any) -> list[dict[str, Any]]:
    """messages.list 行 → 面板讨论消息行（content_preview = 读时重建全文）。"""
    items: list[dict[str, Any]] = []
    for msg in rows if isinstance(rows, list) else []:
        if not isinstance(msg, dict):
            continue
        items.append(
            {
                "role": str(msg.get("role") or ""),
                "content": str(msg.get("content_preview") or ""),
                "status": str(msg.get("status") or ""),
                "created_at": str(msg.get("created_at") or ""),
            }
        )
    return items


def _project_task_rows(rows: Any) -> list[dict[str, Any]]:
    """全量 state 行 → 挂靠项目的任务行（锚 = task.parent_project_id，不限 mode）。"""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        project_id = str(row.get("task.parent_project_id") or "")
        if not project_id:
            continue
        items.append(
            {
                "project_id": project_id,
                "task_id": str(row.get("task.id") or ""),
                "pipeline_id": str(row.get("pipeline_id") or ""),
                "title": str(row.get("task.goal") or row.get("input") or ""),
                "status": str(row.get("task.status") or ""),
            }
        )
    return items


_TASK_ROW_FIELDS = ("task_id", "pipeline_id", "title", "status")


def _derive_projects(task_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 task.parent_project_id 分组派生项目（诚实口径：登记簿字段不可得留空）。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in task_rows:
        groups.setdefault(row["project_id"], []).append(
            {field: row[field] for field in _TASK_ROW_FIELDS}
        )
    return [
        {
            "project_id": project_id,
            "title": "",
            "workflow_state": "",
            "auto_execute": None,
            "source": "derived",
            "tasks": tasks,
        }
        for project_id, tasks in sorted(groups.items())
    ]


def _norm_registry_projects(
    rows: Any, task_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """登记簿行 → 项目行（登记态：workflow_state/auto_execute 为登记真值）。"""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        project_id = str(row.get("id") or "")
        if not project_id:
            continue
        items.append(
            {
                "project_id": project_id,
                "title": str(row.get("title") or ""),
                "workflow_state": str(row.get("workflow_state") or ""),
                "auto_execute": bool(row.get("auto_execute", False)),
                "source": "registry",
                "tasks": [t for t in task_rows if t["project_id"] == project_id],
            }
        )
    return items


_DERIVED_NOTE = (
    "派生口径：task_service 无 projects 读服务（登记簿仅 HTTP 端点），项目由 "
    "pipeline-state 行按 task.parent_project_id 分组派生；workflow_state/auto_execute "
    "为登记簿字段不可得，登记后尚无子任务行的项目不可见。"
)


async def _load_projects() -> dict[str, Any]:
    """项目列表：登记 provider 在 → 登记态归一；否则 state 行派生（带 note）。"""
    rows = await _call_provider("pipeline-state")
    task_rows = _project_task_rows(rows)
    if "projects" in _PROVIDERS:
        registry_rows = await _call_provider("projects")
        return {
            "projects": _norm_registry_projects(registry_rows, task_rows),
            "source": "registry",
        }
    return {"projects": _derive_projects(task_rows), "source": "derived", "note": _DERIVED_NOTE}


def _norm_task_list(res: Any) -> list[dict[str, Any]]:
    """task.list 结果 → 树行（单层；children 恒空，前端按 parent 递归拉取）。"""
    data = res.get("tasks") if isinstance(res, dict) else res
    items: list[dict[str, Any]] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "task_id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""),
                "status": str(row.get("status") or ""),
                "children": [],
            }
        )
    return items


# ── 写动作（project_create / task_submit 真派发；tool-executor 显式 plugin_id）────

async def _invoke_tool(tool_name: str, plugin_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """经 tool-executor 调目标插件工具/服务；通道不可用/失败/空结果如实报 error。"""
    fn = _PROVIDERS.get("tool-executor")
    if fn is None:
        return {"error": "派发通道不可用（tool-executor 能力未注入）"}
    try:
        res = await fn({"tool_name": tool_name, "plugin_id": plugin_id, "args": args})
    except Exception as exc:  # noqa: BLE001 —— 写面：失败透传（不静默假成功）
        return {"error": f"{tool_name} 调用失败: {exc}"}
    data = res.get("data") if isinstance(res, dict) else None
    if isinstance(data, dict) and data:
        return data
    return {"error": f"{tool_name} 未返回结果: {str(res)[:200]}"}


def _extract_id(body: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if body.get(key):
            return str(body[key])
    return ""


def _create_project_args(goal: str, path: str, project_type: str) -> dict[str, Any]:
    """「创建项目」→ project_create args（必填 goal；path/project_type 可选透传）。"""
    args: dict[str, Any] = {"goal": goal.strip()}
    if path.strip():
        args["path"] = path.strip()
    if project_type.strip():
        args["project_type"] = project_type.strip()
    return args


async def _create_project(goal: str, path: str, project_type: str) -> dict[str, Any]:
    body = await _invoke_tool(
        "project_create", "project_create_tool", _create_project_args(goal, path, project_type)
    )
    if "error" in body:
        return body
    project_id = _extract_id(body, "project_id", "id")
    if not project_id:
        return {"error": f"project_create 未返回 project_id: {str(body)[:200]}"}
    return {
        "project_id": project_id,
        "title": str(body.get("title") or ""),
        "path": str(body.get("path") or ""),
        "workflow_state": str(body.get("workflow_state") or ""),
        "created": bool(body.get("created")),
    }


def _plan_args(goal: str, session_id: str = "") -> dict[str, Any]:
    """「发起规划」→ task_submit args（派发目标=profile.chain 执行者池首选）。

    session_id 有则透传（宿主 ctx.sync 下发的对话框会话，任务入同一线程）；
    缺省不透传空串（create_project 无 session 语义，不走此参）。
    """
    text = goal.strip()
    args: dict[str, Any] = {
        "target_type": "agent",
        # 派发目标 = mode_planning/profile.yaml chain.executor_pool 首个合法键
        # （chain.entry=main 是 L1 主执行上下文键，闸门查无此键必拒——用户旅程
        # 模拟 F2 实证；executor/generation/research_agent.yaml level L3、
        # is_active，经 task_submit 磁盘 rglob 解析可达，实测过目标存在性闸门；
        # 模式材料按 mode=planning 经 context_build 注入，不随目标键走）。
        "target_id": "executor/generation/research_agent",
        # 面板经 tool-executor 直调 task_submit 不走管道 param_inject 注入链，
        # 此键须自携：按主 agent（L1）身份代用户派发（tasks/http_api.py 面板
        # 先例同口径）；缺省闸门以 MISSING_INJECTED_PARAM 拒之，L1 身份放行 L2/L3 目标。
        "parent_agent_level": 1,
        "goal_title": f"方案规划·{text[:40]}",
        "goal_description": text[:2000],
        "mode": "planning",
        "task_kind": "planning_plan",
    }
    if session_id.strip():
        args["session_id"] = session_id.strip()
    return args


async def _plan(goal: str, session_id: str = "") -> dict[str, Any]:
    body = await _invoke_tool("task_submit", "task_submit_tool", _plan_args(goal, session_id))
    if "error" in body:
        return body
    task_id = _extract_id(body, "task_id", "id")
    if not task_id:
        return {"error": f"task_submit 未返回任务 id: {str(body)[:200]}"}
    return {"task_id": task_id}


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_planning/page/planning-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "planning_panel.html"
)
_EP = "/ext/mode_planning/data"

# GET 数据端点（无副作用）+ POST 写动作端点（会真实创建项目/派发任务进管道）
_DATA_ROUTES = {
    f"{_EP}/bootstrap",
    f"{_EP}/projects",
    f"{_EP}/sessions",
}
_ACTION_ROUTES = {
    f"{_EP}/actions/create_project",
    f"{_EP}/actions/plan",
}


def _json_response(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """任意 JSON 对象 → HttpHandleResponse（body base64，内核约定）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


def _panel_html_response() -> dict[str, Any]:
    """包内 webview 面板页 → HttpHandleResponse（text/html，body base64）。"""
    with open(_PANEL_HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


def _parse_body(raw_body: str) -> dict[str, Any]:
    """POST 动作载荷解析（内核 base64 契约 + 裸 JSON 双形态，先 base64 后原文）。

    内核 http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入
    （kernel/crates/api/src/http_dispatcher.rs），宿主桥直发 JSON 的 MCP 直调
    形态则是裸 JSON 字符串——两形态都收。解码顺序与语义对齐
    plugins/shared/http_json.py::decode_body（先 base64，解出 {/[ 开头才采用，
    否则回退原文）；两形态都失败或顶层非 dict → {}（交缺参 400 路径）。
    内联而非 import 共享根模块：种子单元自包含（用户空间播种副本不可达共享根）。
    """
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        data = json.loads(decoded)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _handle_actions(path: str, method: str, raw_body: str) -> dict[str, Any] | None:
    """写动作分发；未命中返回 None（交回 404 路径）。"""
    if path not in _ACTION_ROUTES:
        return None
    if method != "POST":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    body = _parse_body(raw_body)
    goal = str(body.get("goal") or "")
    if not goal.strip():
        return {"success": True, "data": _json_response({"error": "缺少 goal"}, 400)}
    if path.endswith("/create_project"):
        result = await _create_project(
            goal, str(body.get("path") or ""), str(body.get("project_type") or "")
        )
    else:
        result = await _plan(goal, str(body.get("session_id") or ""))
    if "error" in result:
        return {"success": True, "data": _json_response(result, 409)}
    return {"success": True, "data": _json_response(result)}


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/mode_planning/** (面板页+数据面+写动作)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：面板页 HTML / 数据端点 / 写动作；未路由返回 404 JSON。"""
    query = query or {}
    if path == _PANEL_ENDPOINT and method == "GET":
        try:
            return {"success": True, "data": _panel_html_response()}
        except OSError as exc:
            return {
                "success": False,
                "error": f"panel html read failed: {exc}",
                "data": _json_response({"error": "panel html unavailable"}, 500),
            }
    if path in _DATA_ROUTES and method == "GET":
        if path.endswith("/bootstrap"):
            profile = load_profile()
            payload = {
                "mode": profile["mode"],
                "name": profile["name"],
                "chain": profile.get("chain"),
                "panel_page_id": profile.get("panel_page_id"),
            }
            return {"success": True, "data": _json_response(payload)}
        if path.endswith("/projects"):
            return {"success": True, "data": _json_response(await _load_projects())}
        rows = await _call_provider("pipeline-state")
        return {"success": True, "data": _json_response({"sessions": _norm_sessions(rows)})}
    if path == f"{_EP}/tasks":
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        parent_task_id = str(query.get("parent_task_id") or "")
        if not parent_task_id:
            return {"success": True, "data": _json_response({"error": "缺少 parent_task_id"}, 400)}
        res = await _call_provider("task-list", parent_task_id=parent_task_id)
        return {"success": True, "data": _json_response({"tasks": _norm_task_list(res)})}
    if path == f"{_EP}/discussions":
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        pipeline_id = str(query.get("pipeline_id") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
        rows = await _call_provider("messages", pipeline_id=pipeline_id)
        return {"success": True, "data": _json_response({"messages": _norm_messages(rows)})}
    routed = await _handle_actions(path, method, raw_body)
    if routed is not None:
        return routed
    return {"success": True, "data": _json_response({"error": "not found", "path": path}, 404)}


if __name__ == "__main__":
    plugin.run()
