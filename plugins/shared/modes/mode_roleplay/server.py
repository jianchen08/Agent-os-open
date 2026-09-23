#!/usr/bin/env python3
"""角色扮演模式插件——模式元数据服务面 + 面板数据面（出厂种子，随用户空间播种分发）。

服务契约（manifest capabilities.services；wire = MCP tools/call）：
- mode.describe：返回 {mode, name, chain, weights, budget, profile_path, panel_page_id}
- mode.get_profile：返回包内 profile.yaml 解析后的完整 dict

profile 与本插件同目录内打包（种子单元自包含，目录级播种/升级/回退的结构前提）；
消费方（eval_harness 等）经内核服务调用获取（tool-executor 显式 plugin_id 通道），
本插件不声明 LLM 工具面。

webview 面板页（工作区 tab 面板，manifest detachable 声明悬浮窗）由 http_endpoints
供页；面板数据面（2026-09-17 成熟化，ADR 2026-09-17-mode-panel-mature-interfaces；
2026-09-17 宿主融合：对话回归对话框，面板不再内置对话流）：
- 内核读数桥：on_load 注入 pipeline-state / messages / tool-executor providers
  （monitoring kernel_reads 同构；能力未就绪 handler 降级空载荷，前端诚实空态）。
- 数据端点：/ext/mode_roleplay/data/{bootstrap,sessions,messages,cards,lorebooks}。
  messages 端点保留（零连带），面板 UI 不再消费（对话在宿主聊天区）。
- 写动作：/data/actions/{play,regenerate} 经 tool-executor 调 task_submit 真派发
  （eval_harness 先例），args 携 mode=roleplay + 卡上下文。开演（BUG-73 修复）
  会话锚由服务端按卡派生（thread-rp-<card_id> 卡专属扮演会话，同卡复演复用；
  无 sessions 行的线程内核只落映射不动用户会话）——宿主活跃会话 id 不参与
  归属（此前透传 window.__agentosCtx.sessionId 曾把开演任务落进用户当时活跃
  的无关会话线程）。regenerate 行级锚定来源管道 thread_id，body 显式 session_id
  兜底。卡 = 模式命名空间 agent 键（mode_roleplay/card_<id>，模式体系设计 §8.1）。
- 卡 theme/avatar：/data/cards 透出卡 yaml 的 theme（ThemeConfig 档，面板选卡时
  经 theme.apply 桥注入对话框）与 avatar（画廊头像）。

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

plugin = AgentOSPlugin("mode_roleplay")

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.yaml")
_AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")
_LOREBOOKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lorebooks")

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
    description="返回角色扮演模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
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
    description="返回角色扮演模式包内 profile.yaml 解析后的内容",
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

logger = logging.getLogger("mode_roleplay")


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
    """全量 state 行 → 本模式会话行（mode 键过滤 + 面板字段裁剪）。"""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("mode") or "") != "roleplay":
            continue
        items.append(
            {
                "pipeline_id": str(row.get("pipeline_id") or ""),
                "thread_id": str(row.get("thread_id") or ""),
                "agent_id": str(row.get("agent_id") or ""),
                "run_status": str(row.get("run_status") or ""),
                "goal": str(row.get("task.goal") or row.get("input") or ""),
                "task_status": str(row.get("task.status") or ""),
                # task.created_at：任务域出生键（tasks http_api 同源约定）；缺席如实空
                "created_at": str(row.get("task.created_at") or ""),
                "message_count": row.get("message_count") or 0,
            }
        )
    items.sort(key=lambda x: x["pipeline_id"], reverse=True)
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


# ── 物料读取（包内 cards/lorebooks，出厂种子 + 用户副本同布局）────────────────────

def _load_cards(agents_dir: str = _AGENTS_DIR) -> list[dict[str, Any]]:
    """列包内 agents/card_*.yaml（ST V2 字段子集）；损坏文件跳过不崩面板。"""
    cards: list[dict[str, Any]] = []
    if not os.path.isdir(agents_dir):
        return cards
    for fname in sorted(os.listdir(agents_dir)):
        if not (fname.startswith("card_") and fname.endswith(".yaml")):
            continue
        try:
            with open(os.path.join(agents_dir, fname), encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except OSError:
            continue
        if not isinstance(data, dict):
            continue
        card_id = fname[: -len(".yaml")]
        card = {
            "id": card_id,
            "name": str(data.get("name") or card_id),
            "description": str(data.get("description") or "").strip(),
            "personality": str(data.get("personality") or ""),
            "scenario": str(data.get("scenario") or ""),
            "first_mes": str(data.get("first_mes") or ""),
            "mes_example": str(data.get("mes_example") or ""),
            "alternate_greetings": [str(g) for g in (data.get("alternate_greetings") or [])],
            "tags": [str(t) for t in (data.get("tags") or [])],
            "creator_notes": str(data.get("creator_notes") or ""),
            # 宿主融合字段：theme（ThemeConfig 档，theme.apply 载荷）与
            # avatar（emoji 串或 {fg,bg} 色对）原样透传，合法性由消费端把关
            # （theme.apply 桥 validateThemeConfig fail-closed）。
            "avatar": data.get("avatar"),
            "theme": data.get("theme") if isinstance(data.get("theme"), dict) else None,
        }
        cards.append(card)
    return cards


def _load_lorebooks(lorebooks_dir: str = _LOREBOOKS_DIR) -> list[dict[str, Any]]:
    """列包内 lorebooks/*.yaml（ST World Info 条目结构子集）；损坏文件跳过。"""
    books: list[dict[str, Any]] = []
    if not os.path.isdir(lorebooks_dir):
        return books
    for fname in sorted(os.listdir(lorebooks_dir)):
        if not fname.endswith(".yaml"):
            continue
        try:
            with open(os.path.join(lorebooks_dir, fname), encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except OSError:
            continue
        if not isinstance(data, dict):
            continue
        book_id = fname[: -len(".yaml")]
        entries: list[dict[str, Any]] = []
        for entry in data.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    "keys": [str(k) for k in (entry.get("keys") or [])],
                    "secondary_keys": [str(k) for k in (entry.get("secondary_keys") or [])],
                    "content": str(entry.get("content") or ""),
                    "enabled": bool(entry.get("enabled", True)),
                    "insertion_order": entry.get("insertion_order", 100),
                    "constant": bool(entry.get("constant", False)),
                    "position": str(entry.get("position") or "before_char"),
                    "comment": str(entry.get("comment") or ""),
                }
            )
        books.append(
            {
                "id": book_id,
                "name": str(data.get("name") or book_id),
                "description": str(data.get("description") or ""),
                "scan_depth": data.get("scan_depth", 4),
                "token_budget": data.get("token_budget", 512),
                "entries": entries,
            }
        )
    return books


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


def _roleplay_thread(card_id: str) -> str:
    """卡专属扮演会话锚（BUG-73 断点a 修复）：thread-rp-<card_id>。

    同卡复演=复用同一专属会话（线程维度「创建/复用专属扮演会话」）；内核
    chat.send_message 创建分支对无 sessions 行的 thread 只落 pipeline_sessions
    映射、不动任何用户会话（活跃管道切换对缺席行显式跳过）——开演由此与宿主
    活跃会话彻底解耦（R92「任务落活跃既有 thread」同族根除）。
    """
    return f"thread-rp-{card_id}"


def _selected_greeting(card: dict[str, Any], greeting_index: int) -> tuple[str, int]:
    """所选开场白（原文, 解析后序号）：0=first_mes，≥1=alternate_greetings[i-1]。

    越界/无备选回落 first_mes（序号同步归 0——metadata 记录与实际注入一致）。
    """
    if greeting_index >= 1:
        alternates = card.get("alternate_greetings") or []
        if greeting_index - 1 < len(alternates):
            return alternates[greeting_index - 1], greeting_index
    return card.get("first_mes") or "", 0


def _play_args(card_id: str, user_persona: str, greeting_index: int | None) -> dict[str, Any]:
    """「以此角色开演」→ task_submit args（卡=模式命名空间 agent 键，§8.1）。

    会话锚 = 卡专属扮演会话（_roleplay_thread）；宿主活跃会话 id 不参与
    （BUG-73：面板曾透传 window.__agentosCtx.sessionId，开演任务落用户当时
    活跃的无关会话线程）。所选开场白全文随派发注入（greeting_index 语义），
    开场要求约束产出形态：演出必须作为回复正文输出、task_evaluate 只做收尾。
    """
    card = next((c for c in _load_cards() if c["id"] == card_id), None)
    if card is None:
        return {"error": f"未知角色卡: {card_id}"}
    raw_index = greeting_index if isinstance(greeting_index, int) and greeting_index >= 0 else 0
    greeting, index = _selected_greeting(card, raw_index)
    # 段序 = 重要性序（goal_description 2000 字符硬上限按尾截断）：行为约束与
    # 所选开场白在前，卡数据段（设定/性格/场景）垫底——超长只牺牲卡数据尾部。
    sections = [
        f"以「{card['name']}」的身份进行沉浸式角色扮演开场。",
        "# 开场要求\n先把开场表演作为回复正文完整输出给用户（这是用户能看到的演出，"
        "不得只写进 task_evaluate 的 summary），保持角色身份；正文输出完成后再调用"
        " task_evaluate 结束。",
    ]
    if greeting:
        sections.append(
            f"# 本场开场白（用户已选定）\n以下面这段开场白开场，自然衔接后继续演出：\n{greeting}"
        )
    if user_persona.strip():
        sections.append(f"# 用户设定（对话者扮演）\n{user_persona.strip()}")
    if card["scenario"]:
        sections.append(f"# 场景\n{card['scenario']}")
    if card["personality"]:
        sections.append(f"# 性格\n{card['personality']}")
    sections.append(f"# 角色设定\n{card['description']}")
    return {
        "target_type": "agent",
        "target_id": f"mode_roleplay/{card_id}",
        # 面板经 tool-executor 直调 task_submit 不走管道 param_inject 注入链，
        # 此键须自携：按主 agent（L1）身份代用户派发（tasks/http_api.py 面板
        # 先例同口径）；缺省闸门以 MISSING_INJECTED_PARAM 拒之，L1 身份放行 L2/L3 目标。
        "parent_agent_level": 1,
        "goal_title": f"角色扮演·{card['name']}",
        "goal_description": "\n\n".join(sections)[:2000],
        "mode": "roleplay",
        "task_kind": "roleplay_opening",
        "metadata": {"card_id": card_id, "greeting_index": index},
        # 卡专属扮演会话锚（thread_id 语义；非宿主活跃会话）
        "session_id": _roleplay_thread(card_id),
    }


async def _regenerate_args(pipeline_id: str, session_id: str = "") -> dict[str, Any]:
    """「重新生成」→ 变体任务 args（swipe 一期裁定=重新生成，设计 §7）。

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
        return {"error": "该会话还没有可重新生成的回复"}
    excerpt = last_assistant["content"][:600]
    sections = [
        "对以下角色回复生成一个重新生成的变体（同样的输入，不同但等价的演绎）。",
        f"# 会话（pipeline_id={pipeline_id}）",
        f"# 上一条角色回复\n{excerpt}",
        "# 要求\n保持同一角色身份与场景，输出风格一致但措辞/神态/推进不同的新版本；"
        "结尾调用 task_evaluate 结束。",
    ]
    target = session["agent_id"] or "main"
    args: dict[str, Any] = {
        "target_type": "agent",
        "target_id": target,
        # parent_agent_level=1 依据同 _play_args（面板 = 主 agent 身份代用户派发）
        "parent_agent_level": 1,
        "goal_title": f"重新生成·{session['goal'][:40] or pipeline_id[:12]}",
        "goal_description": "\n\n".join(sections)[:2000],
        "mode": "roleplay",
        "task_kind": "roleplay_regenerate",
        "metadata": {"source_pipeline_id": pipeline_id},
    }
    # 行级动作会话锚点：行自带 thread_id 优先，body 显式 session_id 兜底
    anchor = session["thread_id"] or session_id
    if anchor:
        args["session_id"] = anchor
    return args


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_roleplay/page/roleplay-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "roleplay_panel.html"
)

# GET 数据端点（无副作用）+ POST 写动作端点（会真实派发任务进管道）
_DATA_ROUTES = {
    "/ext/mode_roleplay/data/bootstrap",
    "/ext/mode_roleplay/data/sessions",
    "/ext/mode_roleplay/data/cards",
    "/ext/mode_roleplay/data/lorebooks",
}
_ACTION_ROUTES = {
    "/ext/mode_roleplay/data/actions/play",
    "/ext/mode_roleplay/data/actions/regenerate",
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
    # 重新生成的兜底会话锚（行级动作：行自带 thread_id 优先，body 显式值兜底）。
    # 开演不走此键——会话锚由服务端按卡派生（_roleplay_thread，BUG-73），
    # body 的宿主活跃会话 id 不再参与任何写动作归属。
    session_id = str(body.get("session_id") or "").strip()
    if path.endswith("/play"):
        card_id = str(body.get("card_id") or "")
        if not card_id:
            return {"success": True, "data": _json_response({"error": "缺少 card_id"}, 400)}
        persona = str(body.get("user_persona") or "")
        greeting = body.get("greeting_index")
        args = _play_args(card_id, persona, greeting if isinstance(greeting, int) else None)
    else:
        pipeline_id = str(body.get("pipeline_id") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
        args = await _regenerate_args(pipeline_id, session_id)
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
    description="HTTP endpoint handler for /ext/mode_roleplay/** (面板页+数据面+写动作)",
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
        if path.endswith("/cards"):
            return {"success": True, "data": _json_response({"cards": _load_cards()})}
        return {"success": True, "data": _json_response({"lorebooks": _load_lorebooks()})}
    if path == "/ext/mode_roleplay/data/messages":
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
