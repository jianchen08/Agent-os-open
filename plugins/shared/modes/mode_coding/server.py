#!/usr/bin/env python3
"""编码模式插件——模式元数据服务面 + 面板数据面（出厂种子，随用户空间播种分发）。

服务契约（manifest capabilities.services；wire = MCP tools/call）：
- mode.describe：返回 {mode, name, chain, weights, budget, profile_path, panel_page_id}
- mode.get_profile：返回包内 profile.yaml 解析后的完整 dict

profile 与本插件同目录内打包（种子单元自包含，目录级播种/升级/回退的结构前提）；
消费方（eval_harness 等）经内核服务调用获取（tool-executor 显式 plugin_id 通道），
本插件不声明 LLM 工具面。

webview 面板页（工作区 tab 面板，manifest detachable 声明悬浮窗）由 http_endpoints
供页；面板数据面（2026-09-17 成熟化，对标 Devin/Cursor/Cline 的编码面板，ADR
2026-09-17-mode-panel-mature-interfaces）：
- 内核读数桥：on_load 注入 pipeline-state / messages / tool-executor providers
  （monitoring kernel_reads 同构；能力未就绪 handler 降级空载荷，前端诚实空态）。
- 数据端点：/ext/mode_coding/data/{bootstrap,sessions,messages,board,reviews}。
  board 按「修复流水线」六列状态机落列（issue→分诊→worktree→测试→审批→合并，
  落列是诚实启发式，口径见 _board_column 注释）；reviews 为服务端代理的待审批
  列表（interaction.get_pending，审批数据内存态、重启即失——降级口径见
  _reviews_payload 注释）。
- 写动作：/data/actions/dispatch_issue 经 tool-executor 调 task_submit 真派发
  （eval_harness 先例），args 携 mode=coding；session_id（宿主 ctx.sync 下发的
  对话框线程）非空时透传——任务送入对话框线程；模式规则经 context_build 自动注入，
  面板不拼规则文本。
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
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller

plugin = AgentOSPlugin("mode_coding")

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
    description="返回编码模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
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
    description="返回编码模式包内 profile.yaml 解析后的内容",
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

logger = logging.getLogger("mode_coding")


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

# 「修复流水线」六列状态机（列键, 列头）：issue → 分诊 → worktree → 测试 → 审批 → 合并
_BOARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("issue", "issue"),
    ("triage", "分诊"),
    ("worktree", "worktree"),
    ("testing", "测试"),
    ("review", "审批"),
    ("merged", "合并"),
)


def _board_column(row: dict[str, Any]) -> str:
    """六列落列口径（诚实启发式，按信息最充分的信号优先，只为面板呈现不改管道语义）：
    task.status=pending（未开跑）→ issue；current_phase 含 worktree → worktree 列；
    current_phase 含 test → 测试；task.status=pending_evaluation（等评估证据/审批）
    → 审批；task.status=completed → 合并；run_status=failed → issue（重开）；
    其余在途态（run_status=running 等）→ 分诊。
    """
    task_status = str(row.get("task.status") or "")
    phase = str(row.get("current_phase") or "")
    run_status = str(row.get("run_status") or "")
    if task_status == "pending":
        return "issue"
    if "worktree" in phase:
        return "worktree"
    if "test" in phase:
        return "testing"
    if task_status == "pending_evaluation":
        return "review"
    if task_status == "completed":
        return "merged"
    if run_status == "failed":
        return "issue"  # 失败重开：回 issue 列等重新分诊
    return "triage"


def _norm_sessions(rows: Any) -> list[dict[str, Any]]:
    """全量 state 行 → 本模式会话行（mode 键过滤 + 面板字段裁剪 + 落列/checkpoint）。"""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("mode") or "") != "coding":
            continue
        try:
            ckpt = int(row.get("ckpt_max_seq") or 0)
        except (TypeError, ValueError):
            ckpt = 0
        items.append(
            {
                "pipeline_id": str(row.get("pipeline_id") or ""),
                "thread_id": str(row.get("thread_id") or ""),
                "agent_id": str(row.get("agent_id") or ""),
                "task_id": str(row.get("task.id") or ""),
                "run_status": str(row.get("run_status") or ""),
                "goal": str(row.get("task.goal") or row.get("input") or ""),
                "task_status": str(row.get("task.status") or ""),
                "current_phase": str(row.get("current_phase") or ""),
                "message_count": row.get("message_count") or 0,
                # checkpoint 数 = ckpt_max_seq 水位；无消息时内核写 -1，计数口径钳到 0
                "checkpoints": max(ckpt, 0),
                "column": _board_column(row),
            }
        )
    items.sort(key=lambda x: x["pipeline_id"], reverse=True)
    return items


def _norm_messages(rows: Any) -> list[dict[str, Any]]:
    """messages.list 行 → 面板对话行（content_preview = 读时重建全文）。"""
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


async def _board_payload() -> dict[str, Any]:
    """会话行按六列状态机落列（列序固定，卡片 = 会话任务）。"""
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in _BOARD_COLUMNS}
    for session in _norm_sessions(await _call_provider("pipeline-state")):
        buckets[session["column"]].append(session)
    return {
        "columns": [
            {"key": key, "label": label, "cards": buckets[key]}
            for key, label in _BOARD_COLUMNS
        ]
    }


# ── reviews（待审批列表，服务端代理 human_interaction_tool）─────────────────────────

# 审批数据在 human sidecar 为内存态（重启即失），且批准/拒绝必须走宿主审批卡——
# 面板只读汇总、不旁路审批安全面。通道不可用时如实降级为空列表 + 口径说明。
_REVIEWS_DEGRADE_NOTE = "审批数据为内存态，重启即失；批准/拒绝走宿主审批卡，面板不旁路审批安全面"


def _norm_reviews(res: Any) -> list[dict[str, Any]]:
    """interaction.get_pending 结果 → [{request_id,title,status,created_at}]。"""
    inner: Any = res
    if isinstance(res, dict) and isinstance(res.get("data"), (dict, list)):
        inner = res["data"]
    if isinstance(inner, dict):
        rows: Any = inner.get("requests") if isinstance(inner.get("requests"), list) else []
    elif isinstance(inner, list):
        rows = inner
    else:
        rows = []
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        message_data = row.get("message_data")
        title = message_data.get("title") if isinstance(message_data, dict) else None
        items.append(
            {
                "request_id": str(row.get("request_id") or row.get("id") or ""),
                "title": str(title or row.get("title") or ""),
                "status": str(row.get("status") or ""),
                "created_at": str(row.get("created_at") or ""),
            }
        )
    return items


async def _reviews_payload() -> dict[str, Any]:
    """待审批列表；通道未注入/调用失败诚实降级 200 空列表 + note（不假数据）。"""
    fn = _PROVIDERS.get("tool-executor")
    if fn is None:
        return {"items": [], "note": _REVIEWS_DEGRADE_NOTE}
    try:
        res = await fn(
            {"tool_name": "interaction.get_pending", "plugin_id": "human_interaction_tool", "args": {}}
        )
    except Exception as exc:  # noqa: BLE001 —— 读面降级：审批通道失败不崩面板
        logger.warning("reviews 通道调用失败（降级空列表）: %s", exc)
        return {"items": [], "note": _REVIEWS_DEGRADE_NOTE}
    return {"items": _norm_reviews(res)}


# ── 写动作（task_submit 真派发；eval_harness 先例：tool-executor 显式 plugin_id）──

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


def _issue_args(issue_text: str, title: str, session_id: str = "") -> dict[str, Any]:
    """派 issue → task_submit args（target=本包编排 agent，模式键二级解析）。

    goal_description = issue 文本 + 修复要求；模式规则/工作区纪律由 context_build
    按 mode=coding 自动注入，面板不拼规则文本。session_id = 宿主对话框线程
    （面板经 ctx.sync 收到 window.__agentosCtx.sessionId），非空时透传——
    任务与回答流进对话框线程；无则省略该键。
    """
    sections = [
        issue_text.strip(),
        "# 修复要求\n定位并修复精确根因（止血不截肢，禁止绕过或回退旧方案），"
        "补或改测试锁住行为后收尾。",
    ]
    args: dict[str, Any] = {
        "target_type": "agent",
        # 派发目标 = 本包编排 agent：模式键二级解析可达（task_submit 磁盘回退
        # find_mode_agent_yaml → 包内 agents/programming_orchestrator_agent_v2.yaml，
        # level L2、is_active，实测过目标存在性闸门）；"main" 是 L1 主执行上下文键，
        # 闸门查无此键（TARGET_AGENT_NOT_FOUND，用户旅程模拟 F2 实证）。
        "target_id": "mode_coding/programming_orchestrator_agent_v2",
        # 面板经 tool-executor 直调 task_submit 不走管道 param_inject 注入链，
        # 此键须自携：按主 agent（L1）身份代用户派发（tasks/http_api.py 面板
        # 先例同口径）；缺省闸门以 MISSING_INJECTED_PARAM 拒之，L1 身份放行 L2/L3 目标。
        "parent_agent_level": 1,
        "goal_title": title or f"编码修复·{issue_text.strip()[:40]}",
        "goal_description": "\n\n".join(sections)[:2000],
        "mode": "coding",
        "task_kind": "coding_issue",
    }
    if session_id:
        args["session_id"] = session_id
    return args


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_coding/page/coding-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "coding_panel.html"
)

# GET 数据端点（无副作用）+ POST 写动作端点（会真实派发任务进管道）
_DATA_ROUTES = {
    "/ext/mode_coding/data/bootstrap",
    "/ext/mode_coding/data/sessions",
    "/ext/mode_coding/data/board",
    "/ext/mode_coding/data/reviews",
}
_ACTION_ROUTES = {"/ext/mode_coding/data/actions/dispatch_issue"}


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
    issue_text = str(body.get("issue_text") or "").strip()
    if not issue_text:
        return {"success": True, "data": _json_response({"error": "缺少 issue_text"}, 400)}
    title = str(body.get("title") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    return {
        "success": True,
        "data": _json_response(await _dispatch_task(_issue_args(issue_text, title, session_id))),
    }


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
    description="HTTP endpoint handler for /ext/mode_coding/** (面板页+数据面+写动作)",
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
        if path.endswith("/board"):
            return {"success": True, "data": _json_response(await _board_payload())}
        return {"success": True, "data": _json_response(await _reviews_payload())}
    if path == "/ext/mode_coding/data/messages":
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
