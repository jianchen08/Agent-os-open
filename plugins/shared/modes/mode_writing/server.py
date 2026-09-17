#!/usr/bin/env python3
"""写作模式插件——模式元数据服务面 + 面板数据面（出厂种子，随用户空间播种分发）。

服务契约（manifest capabilities.services；wire = MCP tools/call）：
- mode.describe：返回 {mode, name, chain, weights, budget, profile_path, panel_page_id}
- mode.get_profile：返回包内 profile.yaml 解析后的完整 dict

profile 与本插件同目录内打包（种子单元自包含，目录级播种/升级/回退的结构前提）；
消费方（eval_harness 等）经内核服务调用获取（tool-executor 显式 plugin_id 通道），
本插件不声明 LLM 工具面。

webview 面板页（工作区 tab 面板，manifest detachable 声明悬浮窗）由 http_endpoints
供页；面板数据面（2026-09-17 成熟化，对标 NovelAI/Novelcrafter 作品树+章节+设定集，
ADR 2026-09-17-mode-panel-mature-interfaces）：
- 内核读数桥：on_load 注入 pipeline-state / messages / tool-executor providers
  （monitoring kernel_reads 同构；能力未就绪 handler 降级空载荷，前端诚实空态）。
- 数据端点：/ext/mode_writing/data/{bootstrap,sessions,messages,works,chapter,bible}。
- 写动作：/data/actions/chapter_act 经 tool-executor 调 task_submit 真派发
  （eval_harness 先例），args 携 mode=writing + task_kind=writing_<act>；
  提供 pipeline_id 且行内有 task.id 时带 inherit_mode=workspace 继承作品文件
  （扁平键，task_submit _parse_inherit_modes 口径）；body 透传 session_id
  （宿主 ctx.sync 下发给面板的会话坐标）时任务送入同一对话框线程
  （task_submit inputs.session_id → metadata.session_id 原生支持）。

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

plugin = AgentOSPlugin("mode_writing")

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
    description="返回写作模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
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
    description="返回写作模式包内 profile.yaml 解析后的内容",
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

logger = logging.getLogger("mode_writing")


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


# ── 数据归一（pipeline-state 行 = 扁平点号键摘要，mode/task.* 经出口白名单）────────

def _writing_rows(rows: Any) -> list[dict[str, Any]]:
    """全量 state 行 → 本模式行（mode 键过滤）。"""
    return [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and str(row.get("mode") or "") == "writing"
    ]


def _norm_sessions(rows: Any) -> list[dict[str, Any]]:
    """本模式 state 行 → 会话行（面板字段裁剪）。"""
    items = [
        {
            "pipeline_id": str(row.get("pipeline_id") or ""),
            "thread_id": str(row.get("thread_id") or ""),
            "agent_id": str(row.get("agent_id") or ""),
            "run_status": str(row.get("run_status") or ""),
            "goal": str(row.get("task.goal") or row.get("input") or ""),
            "task_status": str(row.get("task.status") or ""),
            "message_count": row.get("message_count") or 0,
        }
        for row in _writing_rows(rows)
    ]
    items.sort(key=lambda x: x["pipeline_id"], reverse=True)
    return items


def _parse_ws_meta(value: Any) -> dict[str, Any]:
    """task.ws_meta 两形态容忍：dict 直用；JSON 字符串解析；其余视为缺失。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _norm_works(rows: Any) -> list[dict[str, Any]]:
    """本模式 state 行 → 作品树行（目标/任务状态/任务链/工作空间坐标投影）。"""
    items = [
        {
            "pipeline_id": str(row.get("pipeline_id") or ""),
            # thread_id：行级动作（章动作带 pipeline_id）的会话锚点，见 _chapter_act_args
            "thread_id": str(row.get("thread_id") or ""),
            "goal": str(row.get("task.goal") or row.get("input") or ""),
            "task_status": str(row.get("task.status") or ""),
            "task_id": str(row.get("task.id") or ""),
            "parent_project_id": str(row.get("task.parent_project_id") or ""),
            "ws_meta": _parse_ws_meta(row.get("task.ws_meta")),
            "message_count": row.get("message_count") or 0,
        }
        for row in _writing_rows(rows)
    ]
    items.sort(key=lambda x: str(x["pipeline_id"]), reverse=True)
    return items


def _norm_messages(rows: Any) -> list[dict[str, Any]]:
    """messages.list 行 → 面板对话流行（content_preview = 读时重建全文）。"""
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


# ── 章节正文提取（会话消息 → 最后一条较长 assistant 消息）─────────────────────────

_CHAPTER_MIN_CHARS = 200  # 「较长」阈值：章节正文显著长于一般对话回复


def _norm_chapter(pipeline_id: str, messages: Any) -> dict[str, Any]:
    """会话消息 → 章节行：取最后一条达到阈值的 assistant 消息为正文 + 字数。

    无达标消息（含会话无消息）→ 空正文如实返回，不拿短回复凑数。
    """
    body = ""
    for msg in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "assistant":
            continue
        content = str(msg.get("content") or "")
        if len(content) >= _CHAPTER_MIN_CHARS:
            body = content
            break
    return {"pipeline_id": pipeline_id, "content": body, "char_count": len(body)}


# ── 设定集读取（工作空间根白名单三件；文件名硬编码，零外部输入）────────────────────

_BIBLE_FILES = ("BIBLE.md", "OUTLINE.md", "LEDGER.md")


def _read_bible(root_dir: str) -> dict[str, Any]:
    """读工作空间根的设定集三件（白名单文件名，缺失如实进 absent）。

    文件名集合是模块常量、不含任何请求输入——路径穿越无从构造（root 之外的
    文件不可达）；root 先 realpath 归一，再仅拼接白名单名。
    """
    root = os.path.realpath(root_dir)
    sections: list[dict[str, Any]] = []
    absent: list[str] = []
    for name in _BIBLE_FILES:
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                sections.append({"name": name, "content": fh.read()})
        except OSError:
            absent.append(name)
    return {"root": root, "sections": sections, "absent": absent}


def _empty_bible() -> dict[str, Any]:
    """诚实空设定集（无会话/无工作空间坐标时：三件全列 absent，不假内容）。"""
    return {"root": "", "sections": [], "absent": list(_BIBLE_FILES)}


async def _bible_for(pipeline_id: str) -> dict[str, Any]:
    """pipeline_id → 设定集读数：state 行 ws_meta 定根；行不可得降级诚实空。"""
    rows = await _call_provider("pipeline-state")
    row = next(
        (
            r
            for r in _writing_rows(rows)
            if str(r.get("pipeline_id") or "") == pipeline_id
        ),
        None,
    )
    if row is None:
        return _empty_bible()
    root = str(_parse_ws_meta(row.get("task.ws_meta")).get("path") or "")
    if not root:
        return _empty_bible()
    return _read_bible(root)


# ── 写动作（chapter_act → task_submit 真派发；eval_harness 先例：显式 plugin_id）──

_ACT_LABELS = {
    "continue": "续写",
    "expand": "扩写",
    "rewrite": "改写",
    "outline": "生成大纲",
    "brainstorm": "头脑风暴",
}

_ACT_BRIEFS = {
    "continue": "承接现有正文自然向下续写，保持人称、时距与文风一致",
    "expand": "对现有段落扩写：增补感官细节与心理描写，不改变既有情节",
    "rewrite": "重写现有段落：保留情节功能，更换表达与节奏",
    "outline": "基于正文与设定生成后续章节大纲（分章要点与钩子）",
    "brainstorm": "围绕当前作品头脑风暴：给出多个可选走向与冲突点",
}


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


def _chain_target(profile: dict[str, Any]) -> str:
    """profile.chain → 派发目标 agent 键：执行者池首选，链尾次之，main 兜底。"""
    chain = profile.get("chain")
    if isinstance(chain, dict):
        pool = chain.get("executor_pool")
        if isinstance(pool, list):
            for key in pool:
                if isinstance(key, str) and key.strip():
                    return key.strip()
        path = chain.get("expected_path")
        if isinstance(path, list):
            for key in reversed(path):
                if isinstance(key, str) and key.strip():
                    return key.strip()
    return "main"


async def _chapter_act_args(
    act: str,
    instruction: str,
    pipeline_id: str,
    session_id: str = "",
) -> dict[str, Any]:
    """章动作 → task_submit args；带 pipeline_id 时解析工作空间继承键。

    继承 = 扁平键 inherit_mode=workspace + inherit_from=<task.id>
    （task_submit _parse_inherit_modes 口径），让续写任务看到作品文件。
    session_id 语义（行级操作锚定来源管道）：带 pipeline_id 时锚该行自带
    thread_id（GUI 操作哪条管道，交互落哪条管道的线程），body 显式 session_id
    兜底，皆空省略；不带 pipeline_id（新建动作）保持 body 的 session_id
    （面板当前活跃会话），无则省略键。
    """
    label = _ACT_LABELS[act]
    sections = [
        f"{_ACT_BRIEFS[act]}。",
        f"# 动作\n写作模式章节工坊「{label}」（task_kind=writing_{act}）。",
    ]
    if instruction.strip():
        sections.append(f"# 用户指令\n{instruction.strip()}")
    sections.append(
        "# 派发口径\n成稿落盘为工作空间内 markdown 章节文件，goal 写明产物文件名，"
        "声明 file_check 验收（input_params.path=产物相对路径）；结尾调用 task_evaluate 结束。"
    )
    args: dict[str, Any] = {
        "target_type": "agent",
        "target_id": _chain_target(load_profile()),
        # 面板经 tool-executor 直调 task_submit 不走管道 param_inject 注入链，
        # 此键须自携：按主 agent（L1）身份代用户派发（tasks/http_api.py 面板
        # 先例同口径）；缺省闸门以 MISSING_INJECTED_PARAM 拒之，L1 身份放行 L2/L3 目标。
        "parent_agent_level": 1,
        "goal_title": f"写作章节·{label}",
        "goal_description": "\n\n".join(sections)[:2000],
        "mode": "writing",
        "task_kind": f"writing_{act}",
    }
    if pipeline_id:
        row = next(
            (
                r
                for r in _norm_works(await _call_provider("pipeline-state"))
                if r["pipeline_id"] == pipeline_id
            ),
            None,
        )
        if row is None:
            return {"error": f"会话不存在或不在本模式: {pipeline_id}"}
        args["metadata"] = {"source_pipeline_id": pipeline_id}
        if row["task_id"]:
            args["inherit_mode"] = "workspace"
            args["inherit_from"] = row["task_id"]
        # 行级动作会话锚点：行自带 thread_id 优先，body 显式 session_id 兜底
        anchor = row["thread_id"] or session_id
        if anchor:
            args["session_id"] = anchor
    elif session_id:
        # 新建动作（无 pipeline_id）：保持 body 的 session_id（面板当前活跃会话）
        args["session_id"] = session_id
    return args


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_writing/page/writing-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "writing_panel.html"
)

# GET 数据端点（无副作用）+ POST 写动作端点（会真实派发任务进管道）
_DATA_ROUTES = {
    "/ext/mode_writing/data/bootstrap",
    "/ext/mode_writing/data/sessions",
    "/ext/mode_writing/data/messages",
    "/ext/mode_writing/data/works",
    "/ext/mode_writing/data/chapter",
    "/ext/mode_writing/data/bible",
}
_ACTION_ROUTES = {"/ext/mode_writing/data/actions/chapter_act"}


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
    act = str(body.get("act") or "")
    if not act:
        return {"success": True, "data": _json_response({"error": "缺少 act"}, 400)}
    if act not in _ACT_LABELS:
        return {"success": True, "data": _json_response({"error": f"未知 act: {act}"}, 400)}
    args = await _chapter_act_args(
        act,
        str(body.get("instruction") or ""),
        str(body.get("pipeline_id") or ""),
        str(body.get("session_id") or ""),
    )
    if "error" in args:
        return {"success": True, "data": _json_response(args, 400)}
    return {"success": True, "data": _json_response(await _dispatch_task(args))}


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
    description="HTTP endpoint handler for /ext/mode_writing/** (面板页+数据面+写动作)",
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
        if path.endswith("/sessions"):
            rows = await _call_provider("pipeline-state")
            return {"success": True, "data": _json_response({"sessions": _norm_sessions(rows)})}
        if path.endswith("/works"):
            rows = await _call_provider("pipeline-state")
            return {"success": True, "data": _json_response({"works": _norm_works(rows)})}
        if path.endswith("/messages") or path.endswith("/chapter"):
            pipeline_id = str(query.get("pipeline_id") or "")
            if not pipeline_id:
                return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
            rows = await _call_provider("messages", pipeline_id=pipeline_id)
            if path.endswith("/messages"):
                return {"success": True, "data": _json_response({"messages": _norm_messages(rows)})}
            return {
                "success": True,
                "data": _json_response(
                    {"chapter": _norm_chapter(pipeline_id, _norm_messages(rows))}
                ),
            }
        pipeline_id = str(query.get("pipeline_id") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
        return {"success": True, "data": _json_response({"bible": await _bible_for(pipeline_id)})}
    routed = await _handle_actions(path, method, raw_body)
    if routed is not None:
        return routed
    return {"success": True, "data": _json_response({"error": "not found", "path": path}, 404)}


if __name__ == "__main__":
    plugin.run()
