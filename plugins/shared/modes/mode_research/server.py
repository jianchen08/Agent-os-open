#!/usr/bin/env python3
"""调研模式插件——模式元数据服务面 + 面板数据面（出厂种子，随用户空间播种分发）。

服务契约（manifest capabilities.services；wire = MCP tools/call）：
- mode.describe：返回 {mode, name, chain, weights, budget, profile_path, panel_page_id}
- mode.get_profile：返回包内 profile.yaml 解析后的完整 dict

profile 与本插件同目录内打包（种子单元自包含，目录级播种/升级/回退的结构前提）；
消费方（eval_harness 等）经内核服务调用获取（tool-executor 显式 plugin_id 通道），
本插件不声明 LLM 工具面。

webview 面板页（工作区 tab 面板，manifest detachable 声明悬浮窗）由 http_endpoints
供页；面板数据面（2026-09-17 成熟化，ADR 2026-09-17-mode-panel-mature-interfaces）：
- 内核读数桥：on_load 注入 pipeline-state / messages / tool-executor providers
  （monitoring kernel_reads 同构；能力未就绪 handler 降级空载荷，前端诚实空态）。
- 数据端点：/ext/mode_research/data/{bootstrap,sessions,messages,report,sources}。
  报告 = 会话内最后一条达长度阈值的 assistant 消息；解析 [n] 引用角标与正文 URL。
- 写动作：/data/actions/{start,followup} 经 tool-executor 调 task_submit 真派发
  （eval_harness 先例），args 携 mode=research + 深度档/原报告上下文；
  session_id（宿主 ctx.sync 下发的对话框线程）非空时透传——followup 即
  「送入对话框追问」，回答流进对话框线程。
- 信源库检索：经 tool-executor 调 hindsight.recall（plugin_id=hindsight_memory_service，
  bank=kb + type:knowledge 标签，同 hindsight KB 域约定）；通道未就绪降级纯 URL 信源。
- 宿主融合：面板页含 theme.sync / ctx.sync 下行桥接收器（主题 token 落 CSS 变量
  --ag-*，会话上下文落 window.__agentosCtx），样式经别名变量跟随宿主主题。

种子单元自包含：HttpHandleResponse 封装与读数桥在此内联——共享根裸模块对
用户空间播种副本不可达（bootstrap 的 shared_root 指纹只认仓内 plugins/shared 布局）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

import yaml
from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller

plugin = AgentOSPlugin("mode_research")

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
    description="返回调研模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
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
    description="返回调研模式包内 profile.yaml 解析后的内容",
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

logger = logging.getLogger("mode_research")


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

def _norm_sessions(rows: Any) -> list[dict[str, Any]]:
    """全量 state 行 → 本模式调研任务行（mode 键过滤 + 面板字段裁剪）。"""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("mode") or "") != "research":
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
    items.sort(key=lambda x: x["pipeline_id"], reverse=True)
    return items


def _norm_messages(rows: Any) -> list[dict[str, Any]]:
    """messages.list 行 → 面板消息行（content_preview = 读时重建全文）。"""
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


# ── 报告解析（正文引用角标 + URL 信源抽取）────────────────────────────────────────

_REPORT_MIN_CHARS = 200

_CITE_RE = re.compile(r"\[(\d{1,3})\]")
# URL 终止符含 CJK 标点：正文里 URL 常直接接中文标点/中文词间无空白
_URL_RE = re.compile(r"https?://[^\s<>\"'，。、；！？：（）【】《》「」『』…—·]+")
_URL_TRAILING_PUNCT = "。，、；！？）》〉】」』\"'.,;:!?"


def _pick_report(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """报告 = 倒序首条达长度阈值的 assistant 消息（调研报告 = 收敛的长答案）。"""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and len(str(msg.get("content") or "")) >= _REPORT_MIN_CHARS:
            return msg
    return None


def _extract_citations(text: str) -> list[int]:
    """正文 [n] 角标 → 有序去重引用号列表。"""
    seen: set[int] = set()
    out: list[int] = []
    for raw in _CITE_RE.findall(text or ""):
        num = int(raw)
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out


def _extract_urls(text: str) -> list[str]:
    """正文 URL 抽取（去尾标点 + 有序去重）。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(_URL_TRAILING_PUNCT)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _report_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """报告消息 → 面板载荷（正文/字数/引用角标/信源 URL）；无报告诚实返回空。"""
    report_msg = _pick_report(messages)
    report = str(report_msg["content"]) if report_msg else ""
    return {
        "report": report,
        "word_count": len(report),
        "citations_found": _extract_citations(report),
        "sources": _extract_urls(report),
    }


# ── 知识库检索（hindsight.recall 服务面代理；未就绪降级 kb_available=False）────────

_KB_RECALL_TOP_K = 8
_KB_BANK_ID = "kb"  # hindsight KB 域约定（bank_id + type:knowledge 标签）


async def _kb_search(query: str) -> list[dict[str, Any]] | None:
    """经 tool-executor 调 hindsight.recall 检索知识库；通道未注入/调用失败/
    后端报错返回 None（kb_available=False，诚实降级不造假数据）。"""
    fn = _PROVIDERS.get("tool-executor")
    if fn is None:
        return None
    payload = {
        "tool_name": "hindsight.recall",
        "plugin_id": "hindsight_memory_service",
        "args": {
            "bank_id": _KB_BANK_ID,
            "query": query,
            "top_k": _KB_RECALL_TOP_K,
            "tags": ["type:knowledge"],
            "tags_match": "all",
        },
    }
    try:
        res = await fn(payload)
    except Exception as exc:  # noqa: BLE001 —— 读面降级：检索失败不崩面板
        logger.warning("hindsight.recall 检索失败（降级不可用）: %s", exc)
        return None
    data = res.get("data") if isinstance(res, dict) else None
    if isinstance(data, dict) and data.get("error"):
        return None
    items: list[Any]
    if isinstance(data, dict):
        items = data.get("results") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or item.get("text") or "")
        if not text.strip():
            continue
        score = item.get("score")
        if score is None:
            nested = item.get("scores")
            final = nested.get("final") if isinstance(nested, dict) else None
            score = final if isinstance(final, (int, float)) else 0.0
        out.append(
            {
                "id": str(item.get("id", "")),
                "content": text,
                "score": float(score or 0.0),
            }
        )
    return out


# ── 写动作（task_submit 真派发；eval_harness 先例：tool-executor 显式 plugin_id）──

_DEPTH_REQUIREMENTS = {
    "quick": "档位要求（quick）：单轮聚焦检索，输出简明调研报告（核心要点+结论）。",
    "deep": (
        "档位要求（deep）：多轮检索交叉核验，输出结构化长报告（分节论述，"
        "信息充分后再收敛）。"
    ),
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


def _start_args(question: str, depth: str, session_id: str = "") -> dict[str, Any]:
    """「发起调研」→ task_submit args（问题 + 深度档要求 + 报告产出口径）。

    session_id = 宿主对话框线程（面板经 ctx.sync 收到 window.__agentosCtx.sessionId），
    非空时透传——任务送入对话框线程；无则省略该键。
    """
    sections = [
        f"针对以下问题开展调研并输出调研报告：{question}",
        _DEPTH_REQUIREMENTS[depth],
        "# 报告口径\n正文以 [1][2] 角标标注引用，末尾附信源 URL 列表；"
        "结尾调用 task_evaluate 结束。",
    ]
    args: dict[str, Any] = {
        "target_type": "agent",
        # 派发目标 = research 链编排 agent：profile.yaml chain.expected_path 首个
        # 非 main 键；config/agents/orchestrator/research_orchestrator_agent.yaml
        # （config_id research_orchestrator_agent，level L2、is_active）经 task_submit
        # 磁盘 rglob 解析可达，实测过目标存在性闸门；"main" 查无此键
        # （TARGET_AGENT_NOT_FOUND，用户旅程模拟 F2 实证）。
        "target_id": "orchestrator/research_orchestrator_agent",
        # 面板经 tool-executor 直调 task_submit 不走管道 param_inject 注入链，
        # 此键须自携：按主 agent（L1）身份代用户派发（tasks/http_api.py 面板
        # 先例同口径）；缺省闸门以 MISSING_INJECTED_PARAM 拒之，L1 身份放行 L2/L3 目标。
        "parent_agent_level": 1,
        "goal_title": f"调研·{question[:40]}",
        "goal_description": "\n\n".join(sections)[:2000],
        "mode": "research",
        "task_kind": f"research_{depth}",
        "metadata": {"depth": depth},
    }
    if session_id:
        args["session_id"] = session_id
    return args


async def _followup_args(pipeline_id: str, question: str, session_id: str = "") -> dict[str, Any]:
    """「送入对话框追问」→ 携原报告节选的追加调研任务 args。

    派发目标与 start 同键（research 链编排 agent，依据见 _start_args 注释）——
    行 agent_id 不作目标：会话出身行的 agent_id 未必是可派发键（如 L1 主上下文）。
    session_id 语义（行级操作锚定来源管道）：锚该行自带 thread_id（GUI 操作
    哪条管道，交互落哪条管道的线程），body 显式 session_id 兜底，皆空省略。
    """
    rows = _norm_sessions(await _call_provider("pipeline-state"))
    session = next((s for s in rows if s["pipeline_id"] == pipeline_id), None)
    if session is None:
        return {"error": f"会话不存在或不在本模式: {pipeline_id}"}
    messages = _norm_messages(await _call_provider("messages", pipeline_id=pipeline_id))
    last_assistant = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
    if last_assistant is None:
        return {"error": "该会话还没有可追问的调研输出"}
    excerpt = last_assistant["content"][:600]
    sections = [
        "基于以下调研报告继续追问，补充调研后作答。",
        f"# 原报告节选\n{excerpt}",
        f"# 追问\n{question}",
        "# 要求\n延续原报告的引用角标与信源列出口径；结尾调用 task_evaluate 结束。",
    ]
    args: dict[str, Any] = {
        "target_type": "agent",
        "target_id": "orchestrator/research_orchestrator_agent",
        "parent_agent_level": 1,
        "goal_title": f"追问·{question[:40]}",
        "goal_description": "\n\n".join(sections)[:2000],
        "mode": "research",
        "task_kind": "research_followup",
        "metadata": {"source_pipeline_id": pipeline_id},
    }
    # 行级动作会话锚点：行自带 thread_id 优先，body 显式 session_id 兜底
    anchor = session["thread_id"] or session_id
    if anchor:
        args["session_id"] = anchor
    return args


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_research/page/research-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "research_panel.html"
)

# GET 数据端点（无副作用）+ POST 写动作端点（会真实派发任务进管道）
_DATA_ROUTES = {
    "/ext/mode_research/data/bootstrap",
    "/ext/mode_research/data/sessions",
    "/ext/mode_research/data/sources",
    "/ext/mode_research/data/report",
}
_ACTION_ROUTES = {
    "/ext/mode_research/data/actions/start",
    "/ext/mode_research/data/actions/followup",
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


async def _handle_messages(path: str, method: str, query: dict[str, str]) -> dict[str, Any] | None:
    """/data/messages 分发；未命中返回 None（交回 404 路径）。"""
    if path != "/ext/mode_research/data/messages":
        return None
    if method != "GET":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    pipeline_id = str(query.get("pipeline_id") or "")
    if not pipeline_id:
        return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
    rows = await _call_provider("messages", pipeline_id=pipeline_id)
    return {"success": True, "data": _json_response({"messages": _norm_messages(rows)})}


async def _handle_sources(query: dict[str, str]) -> dict[str, Any]:
    """/data/sources：pipeline_id → 报告 URL 抽取；q → KB 检索（可用时）。"""
    q = str(query.get("q") or "").strip()
    pipeline_id = str(query.get("pipeline_id") or "")
    if not q and not pipeline_id:
        return {"success": True, "data": _json_response({"error": "缺少 q 或 pipeline_id"}, 400)}
    url_sources: list[str] = []
    if pipeline_id:
        rows = _norm_messages(await _call_provider("messages", pipeline_id=pipeline_id))
        url_sources = _report_payload(rows)["sources"]
    payload: dict[str, Any] = {"sources": url_sources}
    if q:
        kb_results = await _kb_search(q)
        if kb_results is None:
            payload["kb_available"] = False
            payload["kb_results"] = []
            payload["note"] = "知识库检索通道未就绪或调用失败，降级为纯 URL 信源（登记既有债）"
        else:
            payload["kb_available"] = True
            payload["kb_results"] = kb_results
    return {"success": True, "data": _json_response(payload)}


async def _handle_actions(path: str, method: str, raw_body: str) -> dict[str, Any] | None:
    """写动作分发；未命中返回 None（交回 404 路径）。"""
    if path not in _ACTION_ROUTES:
        return None
    if method != "POST":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    body = _parse_body(raw_body)
    question = str(body.get("question") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    if path.endswith("/start"):
        if not question:
            return {"success": True, "data": _json_response({"error": "缺少 question"}, 400)}
        depth = str(body.get("depth") or "")
        if depth not in _DEPTH_REQUIREMENTS:
            return {
                "success": True,
                "data": _json_response({"error": "depth 须为 quick 或 deep"}, 400),
            }
        args = _start_args(question, depth, session_id)
    else:
        pipeline_id = str(body.get("pipeline_id") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
        if not question:
            return {"success": True, "data": _json_response({"error": "缺少 question"}, 400)}
        args = await _followup_args(pipeline_id, question, session_id)
    if "error" in args:
        message = str(args["error"])
        status = 400 if ("不存在" in message or "未知" in message) else 409
        return {"success": True, "data": _json_response(args, status)}
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
    description="HTTP endpoint handler for /ext/mode_research/** (面板页+数据面+写动作)",
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
        if path.endswith("/sources"):
            return await _handle_sources(query)
        # 剩余路由 = /data/report：会话报告载荷（正文/字数/角标/URL 信源）
        pipeline_id = str(query.get("pipeline_id") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
        rows = _norm_messages(await _call_provider("messages", pipeline_id=pipeline_id))
        return {"success": True, "data": _json_response(_report_payload(rows))}
    routed = await _handle_messages(path, method, query)
    if routed is not None:
        return routed
    routed = await _handle_actions(path, method, raw_body)
    if routed is not None:
        return routed
    return {"success": True, "data": _json_response({"error": "not found", "path": path}, 404)}


if __name__ == "__main__":
    plugin.run()
