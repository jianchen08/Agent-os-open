#!/usr/bin/env python3
"""Godot 游戏开发模式插件——模式元数据服务面 + 面板数据面（出厂种子，随用户空间播种分发）。

服务契约（manifest capabilities.services；wire = MCP tools/call）：
- mode.describe：返回 {mode, name, chain, weights, budget, profile_path, panel_page_id}
- mode.get_profile：返回包内 profile.yaml 解析后的完整 dict

profile 与本插件同目录内打包（种子单元自包含，目录级播种/升级/回退的结构前提）；
消费方（eval_harness 等）经内核服务调用获取（tool-executor 显式 plugin_id 通道），
本插件不声明 LLM 工具面。

webview 面板页（工作区 tab 面板，manifest detachable 声明悬浮/置顶小窗）由
http_endpoints 供页；面板数据面（2026-09-17 成熟化，ADR
2026-09-17-mode-panel-mature-interfaces，对标 godot-mcp/Ziva 类 Godot AI 工具：
场景树感知/运行报错回灌/执行记录）：
- 内核读数桥：on_load 注入 pipeline-state / messages / tool-executor providers
  （monitoring kernel_reads 同构；能力未就绪 handler 降级空载荷，前端诚实空态）。
- 编辑器读数 seam：_addon_get(path) 访问 Godot 宿主桥 addons/agentos 的本地 HTTP
  （127.0.0.1:9600，hosts/godot-addons/agentos/agentos_connector.gd 手写最小服务）。
  addon API 调查结论（2026-09-17，源码为准）：
  - GET /health -> {"status":"ok","version":..}（探活）
  - GET /status -> {"status","version","engine":"godot","engine_version","project"}
  - GET /context -> {"active_scene","selected_object","scene_name","engine_version",
    "selected_objects","selection_detail":[{name,type,path,position,preview_kind}]}
    （选中快照；selection_detail 即面板「选中节点文本树」数据源）
  - GET /selection/preview?index=N -> image/png 字节（贴图缩略或视口截图，
    最长边 ≤512px）；无预览 404 JSON {"error": "no preview for index N"}
- 数据端点：/ext/mode_godot/data/{bootstrap,scene,preview,sessions,messages,records}；
  编辑器离线诚实降级（editor_online:false + 拉起指引 note），不假数据。
- 写动作：/data/actions/dispatch 经 tool-executor 调 task_submit 真派发
  （target=mode_godot/godot_orchestrator_agent，mode=godot，task_kind=godot_scene_edit，
  goal 附「<reference source="godot"> 选中节点为修改目标权威定位」口径；
  session_id 有则透传——宿主 ctx.sync 下发的会话上下文，写动作送入对话框线程）；
  /data/actions/open_editor 经 tool-executor 调 godot_run 探活命令
  （plugin_id=godot_mcp，tool=godot_run，method=engine.commands）——godot_run 调查结论：
  Python 工具代理（原 plugins/shared/tools/external_mcp/godot_mcp，现随用户空间播种），
  必填 method="<group>.<command>"，按任务 workspace 自动路由工程；编辑器未开时按
  GODOT_EDITOR_BIN 自动拉起工程编辑器并重试（code-godot 技能 §0 探活链），即
  「原软件界面」入口（ADR 决策 5：真编辑器窗口 + AI 伴随面板双开）。

godot 链执行记录过滤口径：state 行 mode=="godot" 或 agent_id 含 "godot"
（godot_orchestrator_agent/godot_expert 键在 mode_godot/ 前缀下走 mode 口径，
非模式命名空间键（如裸 godot_expert）走 agent_id 口径，两口径并集防漏）。

种子单元自包含：HttpHandleResponse 封装、读数桥与 addon seam 在此内联——共享根
裸模块对用户空间播种副本不可达（bootstrap 的 shared_root 指纹只认仓内
plugins/shared 布局）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.request
from typing import Any
from urllib.error import HTTPError

import yaml

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller

plugin = AgentOSPlugin("mode_godot")

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
    description="返回 Godot 游戏开发模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
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
    description="返回 Godot 游戏开发模式包内 profile.yaml 解析后的内容",
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

logger = logging.getLogger("mode_godot")


def _set_provider(name: str, fn: Any) -> None:
    """注册内核读 provider（幂等，重复注入以后者为准）。"""
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

    async def _invoke(payload: dict[str, Any]) -> Any:
        te = plugin.get_capability("tool-executor")
        caller = bind_capability_caller(te, "tool-executor")
        return await caller("tool-executor.invoke", payload)

    _set_provider("pipeline-state", _state_rows)
    _set_provider("messages", _messages)
    _set_provider("tool-executor", _invoke)


# ── Godot 宿主桥 seam（addons/agentos 本地 HTTP；测试 monkeypatch 本函数不真连网）──

_ADDON_HOST = "127.0.0.1"
_ADDON_PORT = 9600
_ADDON_TIMEOUT_SEC = 1.5
_PREVIEW_PATH = "/selection/preview?index=0"

_OFFLINE_NOTE = "Godot 编辑器未连接（9600 无响应），点「打开 Godot 编辑器」拉起"


async def _addon_get(path: str) -> tuple[int, bytes]:
    """访问宿主桥 addon HTTP（探活/快照/预览）；返回 (HTTP status, body)。

    连接层失败（拒连/超时）抛 OSError 家族；HTTP 层错误（如预览 404）以 status
    返回不抛——调用方按语义分别诚实降级。
    """

    def _fetch() -> tuple[int, bytes]:
        url = f"http://{_ADDON_HOST}:{_ADDON_PORT}{path}"
        try:
            with urllib.request.urlopen(url, timeout=_ADDON_TIMEOUT_SEC) as resp:
                return int(resp.status), resp.read()
        except HTTPError as exc:
            return int(exc.code), exc.read()

    return await asyncio.to_thread(_fetch)


async def _addon_json(path: str) -> dict[str, Any] | None:
    """addon JSON 端点读取；离线/非 200/坏 JSON → None（调用方诚实降级）。"""
    try:
        status, body = await _addon_get(path)
    except OSError:
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ── 场景/预览载荷（编辑器在线/离线诚实形态）────────────────────────────────────────

def _norm_snapshot(ctx: dict[str, Any]) -> dict[str, Any]:
    """addon /context 响应 → 面板选中快照（字段白名单裁剪，文本树前端直渲染）。"""
    details = ctx.get("selection_detail")
    nodes: list[dict[str, Any]] = []
    for item in details if isinstance(details, list) else []:
        if not isinstance(item, dict):
            continue
        nodes.append(
            {
                "name": str(item.get("name") or ""),
                "type": str(item.get("type") or ""),
                "path": str(item.get("path") or ""),
                "position": str(item.get("position") or ""),
            }
        )
    selected = ctx.get("selected_objects")
    selected_names = [str(s) for s in selected] if isinstance(selected, list) else []
    return {
        "scene_name": str(ctx.get("scene_name") or ""),
        "active_scene": str(ctx.get("active_scene") or ""),
        "selected_object": str(ctx.get("selected_object") or ""),
        "engine_version": str(ctx.get("engine_version") or ""),
        "selected_objects": selected_names,
        "selection_detail": nodes,
        "project": "",
    }


async def _scene_payload() -> dict[str, Any]:
    """场景状态端点载荷：探活 /health + 工程 /status + 选中快照 /context。"""
    health = await _addon_json("/health")
    if health is None:
        return {"editor_online": False, "snapshot": None, "note": _OFFLINE_NOTE}
    status_info = await _addon_json("/status")
    ctx = await _addon_json("/context")
    if ctx is None:
        # 探活通过但快照读取失败：在线事实与快照事实分开报（不混入离线形态）
        return {
            "editor_online": True,
            "snapshot": None,
            "note": "编辑器已连接，但选中快照读取失败（/context 无有效响应）",
        }
    snapshot = _norm_snapshot(ctx)
    if isinstance(status_info, dict):
        snapshot["project"] = str(status_info.get("project") or "")
    return {"editor_online": True, "snapshot": snapshot, "note": ""}


async def _preview_payload() -> dict[str, Any]:
    """预览端点载荷：addon /selection/preview PNG 字节 → data URL。"""
    try:
        status, body = await _addon_get(_PREVIEW_PATH)
    except OSError:
        return {"editor_online": False, "preview_data_url": None, "note": _OFFLINE_NOTE}
    if status == 200 and body:
        data_url = "data:image/png;base64," + base64.b64encode(body).decode("ascii")
        return {"editor_online": True, "preview_data_url": data_url, "note": ""}
    return {
        "editor_online": True,
        "preview_data_url": None,
        "note": "当前选中节点无预览（贴图节点或视口截图不可用）",
    }


# ── 数据归一（pipeline-state 行 = 扁平点号键摘要；godot 记录双口径过滤）────────────

def _is_godot_row(row: dict[str, Any]) -> bool:
    """godot 链记录口径：mode=="godot" 或 agent_id 含 "godot"（并集防漏）。"""
    return str(row.get("mode") or "") == "godot" or "godot" in str(row.get("agent_id") or "")


def _norm_sessions(rows: Any) -> list[dict[str, Any]]:
    """全量 state 行 → godot 链执行记录行（双口径过滤 + 面板字段裁剪）。"""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not _is_godot_row(row):
            continue
        items.append(
            {
                "pipeline_id": str(row.get("pipeline_id") or ""),
                "thread_id": str(row.get("thread_id") or ""),
                "agent_id": str(row.get("agent_id") or ""),
                "run_status": str(row.get("run_status") or ""),
                "goal": str(row.get("task.goal") or row.get("input") or ""),
                "task_status": str(row.get("task.status") or ""),
                "message_count": row.get("message_count") or 0,
            }
        )
    items.sort(key=lambda x: str(x["pipeline_id"]), reverse=True)
    return items


def _norm_messages(rows: Any) -> list[dict[str, Any]]:
    """messages.list 行 → 面板消息流（含 error 字样标 has_error 供前端红显——
    godot_run 运行报错回灌的观测口径）。"""
    items: list[dict[str, Any]] = []
    for msg in rows if isinstance(rows, list) else []:
        if not isinstance(msg, dict):
            continue
        content = str(msg.get("content_preview") or "")
        items.append(
            {
                "role": str(msg.get("role") or ""),
                "content": content,
                "status": str(msg.get("status") or ""),
                "created_at": str(msg.get("created_at") or ""),
                "has_error": "error" in content.lower(),
            }
        )
    return items


# ── 写动作（task_submit / godot_run 真派发；eval_harness 先例：显式 plugin_id）────

_DISPATCH_AGENT = "mode_godot/godot_orchestrator_agent"
_REFERENCE_RULE = '消息中 <reference source="godot"> 选中节点为修改目标权威定位'


def _dispatch_args(instruction: str, session_id: str = "") -> dict[str, Any]:
    """「派发修改」→ task_submit args（编排 agent 键 + godot 场景修改型标注）。

    session_id 有则透传（宿主 ctx.sync 下发的对话框会话，任务入同一线程）；
    缺省不透传空串（task_submit param_inject 注入语义自持）。
    """
    text = instruction.strip()
    args: dict[str, Any] = {
        "target_type": "agent",
        "target_id": _DISPATCH_AGENT,
        # 面板经 tool-executor 直调 task_submit 不走管道 param_inject 注入链，
        # 此键须自携：按主 agent（L1）身份代用户派发（tasks/http_api.py 面板
        # 先例同口径）；缺省闸门以 MISSING_INJECTED_PARAM 拒之，L1 身份放行 L2/L3 目标。
        "parent_agent_level": 1,
        "goal_title": f"Godot 场景修改·{text[:40]}",
        "goal_description": "\n\n".join([text, _REFERENCE_RULE])[:2000],
        "mode": "godot",
        "task_kind": "godot_scene_edit",
    }
    if session_id.strip():
        args["session_id"] = session_id.strip()
    return args


async def _dispatch_task(args: dict[str, Any]) -> dict[str, Any]:
    """经 tool-executor 调 task_submit；通道不可用/调用失败如实返回 error。"""
    fn = _PROVIDERS.get("tool-executor")
    if fn is None:
        return {"error": "任务派发通道不可用（tool-executor 能力未注入）"}
    try:
        res = await fn({"tool_name": "task_submit", "plugin_id": "task_submit_tool", "args": args})
    except Exception as exc:  # noqa: BLE001 —— 写面：失败透传（不静默假成功）
        return {"error": f"task_submit 调用失败: {exc}"}
    data = res.get("data") if isinstance(res, dict) else None
    body = data if isinstance(data, dict) else {}
    task_id = str(body.get("task_id") or body.get("id") or "")
    if not task_id:
        return {"error": f"task_submit 未返回任务 id: {str(res)[:200]}"}
    return {"task_id": task_id}


async def _open_editor() -> dict[str, Any]:
    """「打开 Godot 编辑器」：经 tool-executor 调 godot_run 探活命令。

    探活链（code-godot 技能 §0）：编辑器未开时 godot_run 按 GODOT_EDITOR_BIN 自动
    拉起工程编辑器并重试——原软件拉起入口；通道不可用/返回异常如实报错。
    """
    fn = _PROVIDERS.get("tool-executor")
    if fn is None:
        return {"error": "执行面通道不可用（tool-executor 能力未注入）"}
    try:
        res = await fn(
            {
                "tool_name": "godot_run",
                "plugin_id": "godot_mcp",
                "args": {"method": "engine.commands", "params": {}},
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 写面：失败透传（不静默假成功）
        return {"error": f"godot_run 调用失败: {exc}"}
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, dict):
        return {"error": f"godot_run 未返回有效结果: {str(res)[:200]}"}
    return {"result": data}


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_godot/page/godot-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "godot_panel.html"
)

# GET 数据端点（无副作用）+ POST 写动作端点（会真实派发任务/拉起编辑器）
_DATA_ROUTES = {
    "/ext/mode_godot/data/bootstrap",
    "/ext/mode_godot/data/scene",
    "/ext/mode_godot/data/preview",
    "/ext/mode_godot/data/sessions",
    "/ext/mode_godot/data/records",
}
_MESSAGE_ROUTE = "/ext/mode_godot/data/messages"
_ACTION_ROUTES = {
    "/ext/mode_godot/data/actions/dispatch",
    "/ext/mode_godot/data/actions/open_editor",
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
    if path.endswith("/dispatch"):
        body = _parse_body(raw_body)
        instruction = str(body.get("instruction") or "")
        if not instruction.strip():
            return {"success": True, "data": _json_response({"error": "缺少 instruction"}, 400)}
        args = _dispatch_args(instruction, str(body.get("session_id") or ""))
        return {"success": True, "data": _json_response(await _dispatch_task(args))}
    return {"success": True, "data": _json_response(await _open_editor())}


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
    description="HTTP endpoint handler for /ext/mode_godot/** (面板页+数据面+写动作)",
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
        if path.endswith("/scene"):
            return {"success": True, "data": _json_response(await _scene_payload())}
        if path.endswith("/preview"):
            return {"success": True, "data": _json_response(await _preview_payload())}
        if path.endswith("/records"):
            rows = await _call_provider("pipeline-state")
            return {"success": True, "data": _json_response({"records": _norm_sessions(rows)})}
        rows = await _call_provider("pipeline-state")
        return {"success": True, "data": _json_response({"sessions": _norm_sessions(rows)})}
    if path == _MESSAGE_ROUTE:
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
