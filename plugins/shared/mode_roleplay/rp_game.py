"""游戏运行时（drafts / game 基础与响应 / handlers / operation / tick）（自 server.py 按域拆分）。"""
from __future__ import annotations

import base64
import importlib.util  # noqa: F401
import json
import logging  # noqa: F401
import os
import re  # noqa: F401
import sys  # noqa: F401
import time  # noqa: F401
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard  # noqa: F401

import yaml
from _libs.card_contract import flatten_variables  # noqa: E402
from _libs.mode_server_base import (  # noqa: E402
    html_file_response,
    html_text_response,
    load_mode_profile,  # noqa: F401
    register_profile_services,  # noqa: F401
)
from card_lint import (  # noqa: E402 —— 声明面结构/内容安检单源（本插件内部模块）
    OPERATION_ID_RE,  # noqa: F401
    TICK_WHEN_OPS,  # noqa: F401
    lint_actions,  # noqa: F401
    lint_behaviors,  # noqa: F401
    lint_card_extensions,
    lint_engine,  # noqa: F401
    lint_init_ui,  # noqa: F401
    lint_operations,  # noqa: F401
    lint_tick,  # noqa: F401
    lint_ui,  # noqa: F401
    lint_variables,  # noqa: F401
    scan_html_danger,  # noqa: F401
    scan_html_danger_texts,  # noqa: F401
    scan_suspicious,  # noqa: F401
    walk_str_values,  # noqa: F401
)
from rp_cards import (
    _AI_CREATOR_MARK,
    _DRAFT_REREGISTER_FROM,
    _DRAFT_ROUTE_PREFIX,
    _DRAFT_STATES,
    _DRAFT_TRANSITIONS,
    _DRAFTS_ROUTE,
    _PRECIP_CANDIDATES_ROUTE,
    _PRECIP_PROMOTE_ROUTE,
    _card_from_data,
    _derive_card_id,
    _load_cards,
    _scan_card_danger,
    _scan_card_html_danger,
    _user_config_root,
    _write_user_card,
)
from rp_common import (
    _CARD_ID_RE,
    _GAME_HOST_ID_RE,
    _PANEL_HTML_PATH,
    _call_provider,
    _game_routed,
    _is_game_index,
    _json_response,
    _pack_tick_listing,
    _resolve_workspace,
    plugin,
)
from rp_content import (
    _COMBOS_ROUTE_DELETE,
    _COMBOS_ROUTE_SAVE,
    _SCENE_TURN_ROUTE,
    _WIZARD_AXES_ROUTE,
    _assemble_page_single_source,
    _load_pack_assets,
    _pack_operations_listing,
    _user_packs_dir,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: F401
from agentos_plugin_sdk.bootstrap import bootstrap_plugin  # noqa: F401
from agentos_plugin_sdk.capability import bind_capability_caller


def _user_drafts_dir() -> str | None:
    """用户层创作草稿目录：<user_config_dir>/rp_drafts（与卡库同区双根风格）。"""
    root = _user_config_root()
    return os.path.join(root, "rp_drafts") if root else None

def _draft_read(draft_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """读单草稿 → (meta, card)；id 形态/文件缺失/损坏/形态非法 = None（读面降级）。"""
    if not _CARD_ID_RE.fullmatch(draft_id):
        return None
    directory = _user_drafts_dir()
    path = os.path.join(directory, f"{draft_id}.yaml") if directory else None
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("meta"), dict):
        return None
    raw_card = data.get("card")
    card: dict[str, Any] = raw_card if isinstance(raw_card, dict) else {}
    return data["meta"], card

def _draft_write(draft_id: str, meta: dict[str, Any], card: dict[str, Any]) -> None:
    """草稿原子落盘（meta+card 双键；id 随文件名不入载荷）。调用方保证 id 过闸。"""
    directory = _user_drafts_dir()
    if directory is None:
        raise RuntimeError("用户配置目录不可得（user_config_dir()=None），草稿无处落盘")
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{draft_id}.yaml"),
        yaml.safe_dump({"meta": meta, "card": card}, allow_unicode=True, sort_keys=False),
    )

def _draft_transition_guard(meta: dict[str, Any], action: str) -> str | None:
    """状态机闸：action 在当前态合法来源集内 = None；否则可读错误（409 用）。"""
    state = str(meta.get("state") or "")
    legal: tuple[str, ...]
    label: str
    if action == "register":
        legal, label = _DRAFT_REREGISTER_FROM, "重登记"
    else:
        legal, label = _DRAFT_TRANSITIONS[action], action
    if state not in legal:
        return f"状态机非法迁移：{label} 仅受理 {'/'.join(legal)} 态，草稿当前 {state or '未知'} 态"
    return None

def _draft_card_from_content(content: Any) -> tuple[dict[str, Any] | None, str]:
    """登记内容 → 卡 dict（yaml 文本 / dict / {card} 信封三形态宽容）；
    非法返回 (None, 可读错误)。形态闸只卡「映射 + 非空 name」，深校验归 lint。"""
    if isinstance(content, str):
        try:
            content = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return None, f"草稿 yaml 解析失败: {exc}"
    if not isinstance(content, dict):
        return None, "草稿内容须为卡字段映射（yaml 文本或对象）"
    envelope = content.get("card")
    card = envelope if isinstance(envelope, dict) else content
    if not str(card.get("name") or "").strip():
        return None, "草稿卡缺非空 name（id 由服务端按名派生）"
    return dict(card), ""

async def _draft_register_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/register：{author_session, path|content, draft_id?} → draft 态。

    内容来源二选一：path（相对会话工作空间，经 pipeline-state 行解析 +
    realpath 越界拒绝）或 content（yaml 文本 / 卡对象 / {card} 信封）。形态
    校验过闸后落盘：creator 署名强制、meta 记会话来源/时间；重登记（显式
    draft_id 且已存在）内容替换证据失效重置 draft、审核留痕保留；审核中/
    已入库 409。draft_id 缺省按卡名派生避让（卡库 ∪ 既有草稿占用）。
    """
    session_id = str(body.get("author_session") or "").strip()
    if not session_id:
        return {"error": "缺少 author_session（草稿须记创作会话来源）"}, 400
    if not _GAME_HOST_ID_RE.fullmatch(session_id):
        return {"error": f"非法 author_session（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {session_id}"}, 400
    rel_path = str(body.get("path") or "").strip()
    content = body.get("content")
    if bool(rel_path) == (content is not None):
        return {"error": "path 与 content 二选一（登记内容来源）"}, 400
    if rel_path:
        workspace = await _resolve_workspace(session_id)
        if workspace is None:
            return {"error": f"会话工作空间不可得（无该会话行或缺 workspace）: {session_id}"}, 400
        ws_real = os.path.realpath(workspace)
        target = os.path.realpath(os.path.join(ws_real, rel_path))
        if target != ws_real and not target.startswith(ws_real + os.sep):
            return {"error": f"草稿路径越出会话工作空间: {rel_path}"}, 400
        try:
            with open(target, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return {"error": f"草稿文件不可读: {rel_path}（{exc}）"}, 400
    card, parse_err = _draft_card_from_content(content)
    if card is None:
        return {"error": parse_err}, 400

    explicit_id = str(body.get("draft_id") or "").strip()
    if explicit_id:
        if not _CARD_ID_RE.fullmatch(explicit_id):
            return {"error": f"非法 draft_id（须为 ^[a-z0-9_]{{1,64}}$）: {explicit_id}"}, 400
        draft_id = explicit_id
    else:
        directory = _user_drafts_dir()
        taken = {c["id"] for c in _load_cards()}
        if directory and os.path.isdir(directory):
            taken.update(entry.name[: -len(".yaml")] for entry in Path(directory).glob("*.yaml"))
        draft_id = _derive_card_id(str(card.get("name")), taken)

    now = datetime.now(UTC).isoformat()
    existing = _draft_read(draft_id)
    if existing is not None:
        guard_err = _draft_transition_guard(existing[0], "register")
        if guard_err:
            return {"error": guard_err}, 409
        created_at = str(existing[0].get("created_at") or now)
        trail = existing[0].get("review_trail") if isinstance(existing[0].get("review_trail"), list) else []
    else:
        created_at, trail = now, []
    card["creator"] = _AI_CREATOR_MARK  # 署名强制（客户端值覆盖）
    meta = {
        "draft_id": draft_id,
        "state": "draft",
        "created_at": created_at,
        "updated_at": now,
        "author_session": session_id,
        "creator": _AI_CREATOR_MARK,
        "lint_report": None,   # 内容替换 = 旧证据失效，重登记同步清空
        "smoke_record": None,
        "reject_reason": None,
        "approved_card_id": None,
        "review_trail": trail,
    }
    try:
        _draft_write(draft_id, meta, card)
    except RuntimeError as exc:
        return {"error": str(exc)}, 500
    return {"draft_id": draft_id, "state": "draft", "name": str(card.get("name")), "creator": _AI_CREATOR_MARK}, 200

async def _draft_lint_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/lint：card_lint 单源结构校验 + suspicious 指令扫描 +
    HTML 高危扫描 → lint_pass / rejected（报告落元数据）。

    阻断面 = 结构 warnings（card_lint 三面）+ suspicious 注入形状（AC-6：lint
    拦注入形状）；HTML 高危只标注不阻断（渲染面 rpSanitizeHtml 白名单消毒兜底，
    与 import 面同待遇）。同源契约：结构判定直调本插件单源 lint_card_extensions，
    不复刻第二套。
    """
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "lint")
    if err:
        return {"error": err}, 409

    structure = lint_card_extensions(card.get("extensions"))
    suspicious = _scan_card_danger(card)
    html = _scan_card_html_danger(card)
    lint_pass = not structure and not suspicious
    report = {
        "lint_pass": lint_pass,
        "structure": structure,
        "suspicious": suspicious,
        "html": html,
        "linted_at": datetime.now(UTC).isoformat(),
    }
    meta["lint_report"] = report
    meta["state"] = "lint_pass" if lint_pass else "rejected"
    meta["updated_at"] = report["linted_at"]
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": meta["state"], "report": report}, 200

async def _draft_smoke_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/smoke：一期人工试玩登记（试玩会话 id 引用 + 结论）→
    smoke_pass。自动探针经 B-Ⅱ 考据维持降级（诚实收敛，考据结论见 400 消息与
    模块 docstring）：eval 域 suite.smoke = agent 编排任务链管道 + 自进化账本
    耦合 + agent 键目标，与草稿卡探针三处错配；升级触发 = eval 域提供与进化
    账本解耦的卡级探针库化服务面。probe 参数携带即 400 显式降级说明。"""
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "smoke")
    if err:
        return {"error": err}, 409
    if body.get("probe"):
        return {"error": "probe 自动探针经 B-Ⅱ 考据维持降级（诚实收敛优于假接线）："
                         "eval 域 suite.smoke 是 agent 编排的任务链管道"
                         "（eval_view_suite 展开 → agent task_submit 派发 → "
                         "eval_summarize 落自进化账本），与草稿卡探针三处错配——"
                         "①草稿卡非 agent 键，临时注册须写出厂面（红线）；"
                         "②eval_summarize 落进化账本（rounds/material_ref 重放键），"
                         "卡级探针注入会污染基线链；③本端点为同步 HTTP，探针轮是"
                         "分钟级真 LLM 任务链运行，需异步两段式新编排面。升级触发 = "
                         "eval 域提供模式插件可消费的卡级探针库化服务面（展开/派发/"
                         "聚合与进化账本解耦）。一期请人工开演试玩后登记会话引用"}, 400
    smoke_session_id = str(body.get("smoke_session") or "").strip()
    if not smoke_session_id:
        return {"error": "缺少 smoke_session（试玩会话 id 引用）"}, 400
    record = {
        "smoke_session": smoke_session_id,
        "conclusion": str(body.get("conclusion") or ""),
        "smoked_at": datetime.now(UTC).isoformat(),
    }
    meta["smoke_record"] = record
    meta["state"] = "smoke_pass"
    meta["updated_at"] = record["smoked_at"]
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": meta["state"], "record": record}, 200

async def _draft_submit_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/submit：smoke_pass → awaiting_review（进审核队列）。"""
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "submit")
    if err:
        return {"error": err}, 409
    meta["state"] = "awaiting_review"
    meta["updated_at"] = datetime.now(UTC).isoformat()
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": meta["state"]}, 200

async def _draft_approve_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/approve：三闸强制（安全底线）→ 受控搬移入用户层卡库。

    三闸 = ①状态机来源态 awaiting_review（人审动作本体）②lint 通过证据元数据
    ③smoke 记录元数据——缺一不可 409 可读错误（手工篡改文件态也进不来）；审核人
    = 当前请求用户（token 解析，未认证 401），留痕 who/when/approve 随草稿。
    入库 = save 通道语义（_write_user_card，同 id 接管），creator 署名入库前
    复写断言。草稿文件保留（审核历史随文件）。"""
    user_id = _user_id_from_headers(headers)
    if not user_id:
        return {"error": "未认证：token 缺失或无效"}, 401
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "approve")
    if err:
        return {"error": err}, 409
    report = meta.get("lint_report")
    if not isinstance(report, dict) or report.get("lint_pass") is not True:
        return {"error": "三闸未齐：lint 闸无通过证据（先走 lint 校验到 lint_pass）"}, 409
    if not isinstance(meta.get("smoke_record"), dict):
        return {"error": "三闸未齐：smoke 闸无试玩记录（先登记试玩到 smoke_pass）"}, 409
    card["creator"] = _AI_CREATOR_MARK  # 入库复写断言（草稿文件用户可手改，不信任）
    saved = _write_user_card(_card_from_data(card, draft_id, "user"))
    if "error" in saved:
        return {"error": str(saved["error"])}, 500
    now = datetime.now(UTC).isoformat()
    meta["state"] = "approved"
    meta["approved_card_id"] = draft_id
    meta["updated_at"] = now
    trail = meta["review_trail"] if isinstance(meta.get("review_trail"), list) else []
    trail.append({"who": user_id, "when": now, "action": "approve"})
    meta["review_trail"] = trail
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": "approved", "card_id": draft_id}, 200

async def _draft_reject_request(body: dict[str, Any], headers: dict[str, str] | None) -> tuple[dict[str, Any], int]:
    """POST /wizards/draft/reject：意见回写元数据 + 留痕（who/when/reject/reason）
    → rejected（草稿数据保留，回创作会话迭代后重登记）。"""
    user_id = _user_id_from_headers(headers)
    if not user_id:
        return {"error": "未认证：token 缺失或无效"}, 401
    draft_id = str(body.get("draft_id") or "").strip()
    existing = _draft_read(draft_id)
    if existing is None:
        return {"error": f"草稿不存在: {draft_id}"}, 400
    meta, card = existing
    err = _draft_transition_guard(meta, "reject")
    if err:
        return {"error": err}, 409
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return {"error": "缺少 reason（打回意见回创作会话迭代）"}, 400
    now = datetime.now(UTC).isoformat()
    meta["state"] = "rejected"
    meta["reject_reason"] = reason
    meta["updated_at"] = now
    trail = meta["review_trail"] if isinstance(meta.get("review_trail"), list) else []
    trail.append({"who": user_id, "when": now, "action": "reject", "reason": reason})
    meta["review_trail"] = trail
    _draft_write(draft_id, meta, card)
    return {"draft_id": draft_id, "state": "rejected"}, 200

def _drafts_query(query: dict[str, str]) -> tuple[dict[str, Any], int]:
    """GET /wizards/drafts：列表（state 过滤）/ 单草稿详情（三件套）。

    详情按 draft_id 定位（卡内容 + lint 报告 + smoke 记录 + 审核留痕全随 meta
    透出）；列表项 = 摘要行（updated_at 降序）。读面降级：损坏文件跳过；用户
    空间不可得 = 空表。"""
    draft_id = str(query.get("draft_id") or "").strip()
    if draft_id:
        existing = _draft_read(draft_id)
        if existing is None:
            return {"error": f"草稿不存在或已损坏: {draft_id}"}, 400
        meta, card = existing
        return {"draft_id": draft_id, "meta": meta, "card": card}, 200
    state_filter = str(query.get("state") or "").strip()
    if state_filter and state_filter not in _DRAFT_STATES:
        return {"error": f"非法 state 过滤值（可选 {'/'.join(_DRAFT_STATES)}）: {state_filter}"}, 400
    directory = _user_drafts_dir()
    items: list[dict[str, Any]] = []
    if directory and os.path.isdir(directory):
        for entry in sorted(Path(directory).glob("*.yaml")):
            existing = _draft_read(entry.name[: -len(".yaml")])
            if existing is None:
                continue
            meta, card = existing
            if state_filter and str(meta.get("state") or "") != state_filter:
                continue
            items.append({
                "draft_id": str(meta.get("draft_id") or entry.stem),
                "name": str(card.get("name") or ""),
                "state": str(meta.get("state") or ""),
                "created_at": str(meta.get("created_at") or ""),
                "updated_at": str(meta.get("updated_at") or ""),
                "author_session": str(meta.get("author_session") or ""),
                "creator": str(meta.get("creator") or ""),
            })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"drafts": items}, 200

_DRAFT_HANDLERS = {
    "register": _draft_register_request,
    "lint": _draft_lint_request,
    "smoke": _draft_smoke_request,
    "submit": _draft_submit_request,
    "approve": _draft_approve_request,
    "reject": _draft_reject_request,
}

async def _handle_draft_routes(
    path: str, method: str, raw_body: str, query: dict[str, str], headers: dict[str, str] | None
) -> dict[str, Any] | None:
    """创作草稿路由分发；未命中返回 None（交回 404 路径）。"""
    if path == _DRAFTS_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        payload, status = _drafts_query(query)
        return {"success": True, "data": _json_response(payload, status)}
    action = path[len(_DRAFT_ROUTE_PREFIX):] if path.startswith(_DRAFT_ROUTE_PREFIX) else ""
    handler = _DRAFT_HANDLERS.get(action)
    if handler is None:
        return None
    if method != "POST":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    payload, status = await handler(_parse_body(raw_body), headers)
    return {"success": True, "data": _json_response(payload, status)}

# ── 写动作（task_submit 真派发；eval_harness 先例：tool-executor 显式 plugin_id）──

# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_roleplay/page/roleplay-panel"

_GAME_PAGE_ENDPOINT = "/ext/mode_roleplay/page/game-host"

_GAME_PAGE_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "game_host.html"
)

# message 卡型页退役（2026-10-17 用户裁定：消息流不挂固定 webview 卡——对话流
# 原生渲染，状态/选项呈现归工作区玩法页）：roleplay_bubble_card /
# roleplay_state_card 两 message-style 页、端点与 webview 页件一并移除，
# message_cards marker='state' 条目保留（标记切除的激活门）。

# ── 游戏域会话通道（RP 批 4-Ⅰ）───────────────────────────────────────────────────
#
# 双域分治（设计文档 §二）：游戏域宿主权威，本通道 = 宿主↔后端同步面：
# 单宿主租约（attach/detach，第二宿主 attach=接管+对账）+ 快照 push（薄 sanity
# 后只留最新，不否决宿主权威）+ delta 回推（state_marker_parse 落账步经
# mode.game.push_delta 入队，GET state 取后清）。session_id = RP 扮演会话的
# pipeline_id（游戏页会话绑定指示轮询 data/sessions 跟随）。
#
# 落点契约：租约/快照/待发 delta = 本插件进程内寄存器（索引级只留最新）——
# 持久写面按 manifest state.writes 声明制，本域未声明持久键；进程内寄存器是
# 现状落点（state_marker_parse._latest 同形态先例）；重启即失，宿主重 attach/
# 重推快照自愈（快照权威在宿主，后端只做薄 sanity+审计）。

_GAME_CHANNEL_CAP = 200  # 待发 delta 队列上限（宿主离线保护；超出丢最旧）

_GAME_CHANNELS: dict[str, dict[str, Any]] = {}

_GAME_ATTACH_ROUTE = "/ext/mode_roleplay/game/session/attach"

_GAME_SNAPSHOT_ROUTE = "/ext/mode_roleplay/game/snapshot"

_GAME_STATE_ROUTE = "/ext/mode_roleplay/game/state"

_GAME_DETACH_ROUTE = "/ext/mode_roleplay/game/detach"

_GAME_CONFIG_ROUTE = "/ext/mode_roleplay/game/config"

_GAME_TURN_ROUTE = "/ext/mode_roleplay/game/turn"
# 自由操作→结构化命令端点（世界内嵌谱系右移批次二，调研 §7.6 真缺口①）：
# 玩家 Command 与 NPC Decision 同一队列同一校验（ADR 权威光谱框架支柱⑦）——
# Propose（清单/params fail-closed 400）→ Adjudicate（Validator 同源结算，
# 拒绝必须带原因）。

_GAME_OPERATION_ROUTE = "/ext/mode_roleplay/game/operation"
# 世界演化周期推进端点（世界内嵌谱系右移批次三，调研 §7.6 真缺口②）：宿主页/
# 触发器直达管道接口——不经任务壳（任务状态机/评估闸门与对话轮语义错配，
# ADR 2026-10-17 被否方案④）；确定性引擎结算零 LLM（支柱⑧去 AI 退化判据）。
# 消费登记：定时直达（triggers_ext 定时调用本端点=离台推进）只登记不实现
# ——触发器侧接线属触发器插件域，本端点契约即登记面。

_GAME_TICK_ROUTE = "/ext/mode_roleplay/game/tick"
# 自包含玩法页装配载荷端点（RP 面板 v3.1 刀1）：JSON 载荷 {session_id, page_html}
# 非 HTML 文档——消费方（game_host / 面板）取 page_html 自行注入渲染。

_GAME_PAGE_DATA_ROUTE = "/ext/mode_roleplay/game/page"

# ── turn 服务类型（桥 API 契约，设计文档 §六.1；RP 批 5）──────────────────────────
# turn 请求带 service 字段，契约全集四类型。一期实开 main（玩家主循环输入）；
# npc_chat 一期消费形态 = mode.session 专属会话（游戏页 NPC 深聊入口，会话出生
# 不经 turn 通道）；barks/event 后置占位——批量/事件 LLM 面暂无真实消费方，
# 不实现死代码（触发条件登记 GAME_HOST_SDK.md 服务类型节，消费落地后在此扩分支）。

_TURN_SERVICES = ("main", "npc_chat", "barks", "event")

_TURN_SERVICE_PLACEHOLDERS = {
    "npc_chat": "service=npc_chat 一期走 mode.session 专属会话（NPC 深聊入口），不经 turn 通道",
    "barks": "service=barks 后置占位：批量台词 LLM 面暂无消费方（触发条件见 GAME_HOST_SDK.md 服务类型节）",
    "event": "service=event 后置占位：事件叙事 LLM 面暂无消费方（触发条件见 GAME_HOST_SDK.md 服务类型节）",
}

def _reset_game_channels() -> None:
    """清空通道注册表（单测隔离用；生产随进程生命周期）。"""
    _GAME_CHANNELS.clear()

def _game_channel_id(body: dict[str, Any], key: str) -> tuple[str, str | None]:
    """通道 id 校验（缺参/形态非法 = ("", 可读错误)；fail-closed）。

    失败态返回空串（调用方凭第二元错误短路 400，空串不再下行）。
    """
    raw = str(body.get(key) or "").strip()
    if not raw:
        return "", f"缺少 {key}"
    if not _GAME_HOST_ID_RE.fullmatch(raw):
        return "", f"非法 {key}（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {raw}"
    return raw, None

# GET 数据端点（无副作用）+ POST 写动作端点（regenerate 会真实派发任务进
# 管道；play 为 possess-only 回执面；cards/personas 写面为用户空间文件操作，
# 不派任务）

_DATA_ROUTES = {
    "/ext/mode_roleplay/data/bootstrap",
    "/ext/mode_roleplay/data/sessions",
    "/ext/mode_roleplay/data/cards",
    "/ext/mode_roleplay/data/lorebooks",
    "/ext/mode_roleplay/data/packs",
    _WIZARD_AXES_ROUTE,
    _PRECIP_CANDIDATES_ROUTE,
}

_ACTION_ROUTES = {
    "/ext/mode_roleplay/data/actions/play",
    "/ext/mode_roleplay/data/actions/regenerate",
    "/ext/mode_roleplay/data/cards/save",
    "/ext/mode_roleplay/data/cards/delete",
    "/ext/mode_roleplay/data/cards/import",
    "/ext/mode_roleplay/data/cards/export",
    "/ext/mode_roleplay/data/lorebooks/save",
    "/ext/mode_roleplay/data/lorebooks/delete",
    "/ext/mode_roleplay/data/lorebooks/import",
    "/ext/mode_roleplay/data/personas/save",
    "/ext/mode_roleplay/data/personas/delete",
    _COMBOS_ROUTE_SAVE,
    _COMBOS_ROUTE_DELETE,
    _PRECIP_PROMOTE_ROUTE,
    _SCENE_TURN_ROUTE,
}

def _panel_html_response() -> dict[str, Any]:
    """包内 webview 面板页 → HttpHandleResponse（text/html，body base64）。"""
    return html_file_response(_PANEL_HTML_PATH)

# 游戏页单源装配（RP 批 4-Ⅲ 机制，评审修复刀增桥面）：rpSanitizeHtml 与
# agentosFetch/unwrap 单源均在 roleplay_panel.html（rp-sanitize / rp-bridge
# single-source 块）；游戏页磁盘上不持手抄副本——下发时从面板单源块原位抽取、
# 替换页内装配标记（serve-time include），装配结果与单源逐字节一致由测试执法。

def _game_page_html_response() -> dict[str, Any]:
    """包内 webview 出厂游戏页 → HttpHandleResponse（serve-time 装配面板单源）。

    fail-closed：装配标记/面板单源块缺失 = RuntimeError 上抛（页面缺消毒/
    桥函数属包事实，不静默降级为未装配渲染面）。
    """
    with open(_GAME_PAGE_HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    return html_text_response(_assemble_page_single_source(html, "game page"))

# 请求体解析失败哨兵（_parse_body invalid= 形态返回值，调用方可辨——
# object() 恒不等任何 JSON 解析产物）。

_BODY_INVALID: Any = object()

def _parse_body(raw_body: str, *, invalid: Any | None = None) -> Any:
    """POST 动作载荷解析（内核 base64 契约 + 裸 JSON 双形态，先 base64 后原文）。

    内核 http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入
    （kernel/crates/api/src/http_dispatcher.rs），宿主桥直发 JSON 的 MCP 直调
    形态则是裸 JSON 字符串——两形态都收。解码顺序与语义对齐
    plugins/shared/http_json.py::decode_body（先 base64，解出 {/[ 开头才采用，
    否则回退原文）；两形态都失败或顶层非 dict → {}（交缺参 400 路径）。
    invalid 传值（如 _BODY_INVALID）时解析失败/顶层非 dict 改返该值供调用方
    可辨（缺省 None = 失败回落 {}，既有调用方零变化）。单点复用面：
    _handle_actions / game 域 / draft 域 / pipeline/open。
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
        return invalid if invalid is not None else {}
    if not isinstance(data, dict):
        return invalid if invalid is not None else {}
    return data

def _routed_json(result: dict[str, Any]) -> dict[str, Any]:
    """动作结果 → HttpHandleResponse：error 按类分诊（不存在/未知/非法/缺少/
    无效/不可删除 → 400 客户端可修正；其余 409 冲突/通道类），成功 200。"""
    if "error" not in result:
        return {"success": True, "data": _json_response(result)}
    message = str(result["error"])
    status = (
        400
        if any(k in message for k in ("不存在", "未知", "非法", "缺少", "无效", "不可删除"))
        else 409
    )
    return {"success": True, "data": _json_response(result, status)}

async def _handle_play(body: dict[str, Any]) -> dict[str, Any]:
    """开演端点（possess-only）。

    fresh 开演已会话化（2026-09-25 用户痛点修复：扮演是对话不是任务）——面板
    改走 roleplay.continue 宿主桥建扮演会话（开场白/用户设定经执行选项快照随
    消息透传），本端点不再派发任务；仅保留附身（possess）：校验卡存在后回
    {card_id, play_mode, presenter}，presenter 另附 theme；附身状态由前端持久化
    并在发送时注入 execution_context，服务端无状态。未知/缺省 play_mode 之外
    的取值 400 显式拒绝（缺省按 possess——现存的唯一模式）。
    """
    card_id = str(body.get("card_id") or "")
    if not card_id:
        return {"success": True, "data": _json_response({"error": "缺少 card_id"}, 400)}
    play_mode = str(body.get("play_mode") or "possess")
    if play_mode != "possess":
        return {"success": True, "data": _json_response({"error": f"未知 play_mode: {play_mode}"}, 400)}
    card = next((c for c in _load_cards() if c["id"] == card_id), None)
    if card is None:
        return {"success": True, "data": _json_response({"error": f"未知角色卡: {card_id}"}, 400)}
    return {"success": True, "data": _json_response({
        "card_id": card_id,
        "play_mode": "possess",
        "presenter": {
            "card_id": card_id,
            "name": card["name"],
            "avatar": card.get("avatar"),
            "theme": card.get("theme"),
        },
    })}

# ── 游戏域通道端点（RP 批 4-Ⅰ；四端点 + push_delta 服务，零新增插件）──────────────

async def _handle_game_attach(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/session/attach：单宿主租约登记（设计文档 §二 同步时序）。

    三态：无租约=新租 attached；同 host 重入=幂等续租 renewed（可补带
    game_save_id）；异 host=接管 takeover——返回前宿主信息 + 对账要求
    （reconcile_required），租约换手且清前宿主待发 delta（新宿主从自己的存档
    起步，陈旧 delta 不转移）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    host_id, err = _game_channel_id(body, "host_id")
    if err:
        return _game_routed({"error": err}, 400)
    raw_save = body.get("game_save_id")
    if raw_save is not None and not isinstance(raw_save, str):
        return _game_routed({"error": "game_save_id 须为字符串"}, 400)
    channel = _GAME_CHANNELS.setdefault(
        session_id, {"lease": None, "snapshot": None, "pending_deltas": []}
    )
    lease = channel["lease"]
    now = datetime.now(UTC).isoformat()
    if lease is not None and lease["host_id"] == host_id:
        lease["game_save_id"] = raw_save
        lease["attached_at"] = now
        return _game_routed({"status": "renewed", "lease": dict(lease)})
    if lease is not None:
        previous = dict(lease)
        channel["lease"] = {"host_id": host_id, "game_save_id": raw_save, "attached_at": now}
        channel["pending_deltas"] = []
        return _game_routed({
            "status": "takeover",
            "previous_host": previous,
            "reconcile_required": True,
            "lease": dict(channel["lease"]),
        })
    channel["lease"] = {"host_id": host_id, "game_save_id": raw_save, "attached_at": now}
    return _game_routed({"status": "attached", "lease": dict(channel["lease"])})

async def _handle_game_snapshot(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/snapshot：宿主快照 push——薄 sanity 后只留最新 + 审计追加。

    薄 sanity 仅形态面（state 为 dict/键为字符串/turn_id·seq 非负整数）；内容
    零校验零 clamp（宿主权威，后端不否决，设计文档 §二）。未 attach/非租约
    持有宿主 = 409（单宿主租约执法）。审计复用共享单源 rp_state_audit
    （source=host_snapshot，幂等键含 turn_id+seq；ws 经 pipeline-state 行
    workspace 解析，不可得 = 审计降级跳过不阻断）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    host_id, err = _game_channel_id(body, "host_id")
    if err:
        return _game_routed({"error": err}, 400)
    if not _is_game_index(body.get("turn_id")):
        return _game_routed({"error": "turn_id 须为非负整数"}, 400)
    if not _is_game_index(body.get("seq")):
        return _game_routed({"error": "seq 须为非负整数"}, 400)
    snap_state = body.get("state")
    if not isinstance(snap_state, dict):
        return _game_routed({"error": "state 须为 JSON 对象（薄 sanity）"}, 400)
    if not all(isinstance(key, str) for key in snap_state):
        return _game_routed({"error": "state 键须全为字符串（薄 sanity）"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再推快照"}, 409)
    lease = channel["lease"]
    if lease["host_id"] != host_id:
        return _game_routed(
            {"error": f"非租约持有宿主（当前租约: {lease['host_id']}），快照拒绝"}, 409
        )
    turn_id = body["turn_id"]
    previous_turn = channel["snapshot"]["turn_id"] if channel["snapshot"] else None
    channel["snapshot"] = {
        "turn_id": turn_id,
        "seq": body["seq"],
        "state": snap_state,
        "host_id": host_id,
        "ts": datetime.now(UTC).isoformat(),
    }
    from _root_api import append_audit_rows

    workspace = await _resolve_workspace(session_id)
    append_audit_rows(
        workspace,
        [{
            "key": "game_host_snapshot",
            "op": "=",
            "from": previous_turn,
            "to": turn_id,
            "seq": body["seq"],
            "reason": "host_snapshot",
        }],
        f"{session_id}:host_snapshot:{turn_id}:{body['seq']}",
        "host_snapshot",
    )
    return _game_routed({"status": "accepted", "snapshot": dict(channel["snapshot"])})

async def _handle_game_state(session_id: str) -> dict[str, Any]:
    """GET /game/state：最新快照 + 租约信息 + 待发 deltas（取后清）。

    无快照 = 显式空对象非 404（宿主轮询友好）；未知会话 = 全空形态 200。
    """
    raw = str(session_id or "").strip()
    if not raw:
        return _game_routed({"error": "缺少 session_id"}, 400)
    if not _GAME_HOST_ID_RE.fullmatch(raw):
        return _game_routed(
            {"error": f"非法 session_id（须为 ^[A-Za-z0-9_.:-]{{1,128}}$）: {raw}"}, 400
        )
    channel = _GAME_CHANNELS.get(raw)
    if channel is None:
        return _game_routed(
            {"session_id": raw, "lease": None, "snapshot": {}, "pending_deltas": []}
        )
    pending = channel["pending_deltas"]
    channel["pending_deltas"] = []
    return _game_routed({
        "session_id": raw,
        "lease": dict(channel["lease"]) if channel["lease"] else None,
        "snapshot": dict(channel["snapshot"]) if channel["snapshot"] else {},
        "pending_deltas": pending,
    })

async def _handle_game_detach(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/detach：租约释放（快照保留=宿主离线降级读面；待发 delta 清）。

    无租约 = 幂等成功（released=False）；他宿主 = 409（不可释他宿主租约）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    host_id, err = _game_channel_id(body, "host_id")
    if err:
        return _game_routed({"error": err}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"status": "detached", "released": False})
    lease = channel["lease"]
    if lease["host_id"] != host_id:
        return _game_routed(
            {"error": f"非租约持有宿主，不可释放（当前租约: {lease['host_id']}）"}, 409
        )
    channel["lease"] = None
    channel["pending_deltas"] = []
    return _game_routed({"status": "detached", "released": True})

def _user_id_from_headers(headers: dict[str, str] | None) -> str:
    """请求 token → user_id（内核 access token 载荷 `access:<uid>:<username>:<exp>:
    <jti>:<h>` 自包含 uid；内核转发 /ext 带全量 headers）。解析失败/缺失 = 空串
    （调用方按未认证拒绝——chat.send_message 契约 user_id 非空，不猜默认用户）。"""
    headers_lc = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    auth = headers_lc.get("authorization", "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    try:
        payload_b64 = auth[7:].strip().split(".", 1)[0]
        pad = "=" * (-len(payload_b64) % 4)
        parts = base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8").split(":", 5)
        if len(parts) == 6 and parts[0] == "access":
            return parts[1]
    except (ValueError, IndexError):
        return ""
    return ""

async def _handle_game_turn(body: dict[str, Any], headers: dict[str, str] | None) -> dict[str, Any]:
    """POST /game/turn：玩家输入经服务类型框架派发进绑定 RP 会话（批 5 turn 通道）。

    - service 字段（契约全集见 _TURN_SERVICES）：一期实开 main（缺省）；
      npc_chat/barks/event = 400 可读占位（消费形态/触发条件见 GAME_HOST_SDK.md，
      不实现死代码）；
    - 会话绑定校验：session 须为扮演会话行（pipeline-state mode=roleplay）；
    - 单宿主租约：未 attach = 409（服务点位=同租约内请求类型，设计文档 §六.1）；
    - 派发 = chat.send_message 注入分支薄代理（pipeline_id 定位既有管道 +
      background:true 后台派发即时回执——宿主不等 LLM，叙事经既有 data/messages
      轮询自然回流，零新回推通道）；user_id 从请求 token 解（缺失 401）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    text = str(body.get("text") or "").strip()
    if not text:
        return _game_routed({"error": "缺少 text（玩家输入为空）"}, 400)
    service = str(body.get("service") or "main")
    if service not in _TURN_SERVICES:
        return _game_routed(
            {"error": f"非法 service: {service}（可选 {'/'.join(_TURN_SERVICES)}）"}, 400
        )
    placeholder = _TURN_SERVICE_PLACEHOLDERS.get(service)
    if placeholder:
        return _game_routed({"error": placeholder}, 400)
    user_id = _user_id_from_headers(headers)
    if not user_id:
        return _game_routed({"error": "未认证：token 缺失或无效"}, 401)
    rows = await _call_provider("pipeline-state")
    is_roleplay_session = any(
        isinstance(row, dict)
        and str(row.get("pipeline_id") or "") == session_id
        and str(row.get("mode") or "") == "roleplay"
        for row in rows
    )
    if not is_roleplay_session:
        return _game_routed({"error": f"会话不存在或非扮演会话: {session_id}"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再 turn"}, 409)
    try:
        cap = plugin.get_capability("chat")
        from agentos_plugin_sdk.capability import bind_capability_caller

        res = await bind_capability_caller(cap, "chat")(
            "chat.send_message",
            {"message": text, "user_id": user_id, "pipeline_id": session_id, "background": True},
            timeout=25.0,
        )
    except Exception as exc:  # noqa: BLE001 — 薄代理：capability 失败原样透传
        return {"success": True, "data": _json_response({"error": f"turn 派发失败: {exc}"}, 502)}
    if not isinstance(res, dict) or res.get("status") != "dispatched":
        return {
            "success": True,
            "data": _json_response({"error": f"turn 未走派发分支: {str(res)[:200]}"}, 502),
        }
    return _game_routed({
        "status": "dispatched",
        "session_id": session_id,
        "service": "main",
        "pipeline_id": session_id,
    })

# ── 自由操作→结构化命令（世界内嵌谱系右移批次二，调研 §7.6 真缺口①）────────────
# 玩家在玩法页内对世界直接操作，页经桥发结构化操作命令进与对话同一套校验结算：
# Propose（id 须在绑定包 operations 清单 / params 按 params_schema 校验，均
# fail-closed 400 带原因）→ Adjudicate（effects 走 State Contract Validator
# 同源结算——pipeline_state_marker_parse 的 state.adjudicate 纯委托服务，跨插件
# tool-executor 显式 plugin_id 同通道；玩家也是 Agent，同一校验不做 cheat 旁路；
# 原子结算：任一效果被拒 = 整体 applied:false + 具体原因，审计照记全流水）。

_SOURCE_OPERATION = "operation"

_ADJUDICATE_SERVICE_PLUGIN = "pipeline_state_marker_parse"

async def _invoke_tool(tool_name: str, plugin_id: str, args: dict[str, Any], timeout: float = 10.0) -> Any:
    """tool-executor 显式 plugin_id 跨插件调用（character_state.get 同通道先例）。"""
    te = plugin.get_capability("tool-executor")
    caller = bind_capability_caller(te, "tool-executor")
    return await caller(
        "tool-executor.invoke",
        {"tool_name": tool_name, "plugin_id": plugin_id, "args": args},
        timeout=timeout,
    )

def _validate_operation_params(
    params_schema: dict[str, Any] | None, params: Any
) -> tuple[dict[str, Any], str | None]:
    """params 按 params_schema 校验（Propose 面，fail-closed）。

    返回（校验后参数, 错误原因）：未声明键拒绝（声明制——清单外不可发同闸）、
    类型不符拒绝、min/max 越界拒绝、必填（无 default）缺席拒绝；有 default 的
    字段缺席按缺省补齐。params_schema 缺席 = 零参数契约（携参即拒）。
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {}, f"params 须为映射（字段名 → 值），得 {type(params).__name__}"
    schema = params_schema or {}
    validated: dict[str, Any] = {}
    for key in params:
        if key not in schema:
            return {}, f"参数 {key!r} 未在操作 params_schema 声明（未声明=不可发）"
    for field, decl in schema.items():
        value = params.get(field)
        if value is None:
            if "default" in decl and decl["default"] is not None:
                validated[field] = decl["default"]
                continue
            return {}, f"缺少必填参数 {field!r}（无 default 可补）"
        decl_type = decl.get("type")
        type_ok = (
            isinstance(value, bool) if decl_type == "bool"
            else isinstance(value, str) if decl_type == "string"
            else isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (decl_type == "float" or isinstance(value, int))
        )
        if not type_ok:
            return {}, (
                f"参数 {field!r} 类型不符（声明 {decl_type}，"
                f"得 {type(value).__name__}: {value!r}）"
            )
        minimum, maximum = decl.get("min"), decl.get("max")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if minimum is not None and value < minimum:
                return {}, f"参数 {field!r} 越界：{value} < min {minimum}"
            if maximum is not None and value > maximum:
                return {}, f"参数 {field!r} 越界：{value} > max {maximum}"
        validated[field] = value
    return validated, None

def _operation_reject_reason(rejected: list[dict[str, Any]]) -> str:
    """被拒效果 → 具体原因一行（拒绝必须带原因：键 + reason + layer 可辨）。"""
    parts = [
        f"{row.get('key')} 被拒（{row.get('reason')}/{row.get('layer')}）"
        for row in rejected
        if isinstance(row, dict)
    ]
    return "效果结算未通过：" + "；".join(parts)

async def _handle_game_operation(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/operation：自由操作→结构化命令（Propose→Adjudicate）。

    - 会话校验/单宿主租约与 game/turn 同闸（会话行 mode=roleplay + attach 在场）；
    - Propose：operation.id 须在会话绑定包 operations 清单（fail-closed 400，
      未声明=不可发——枚举声明制）；params 按 params_schema 类型/边界校验
      （fail-closed 400）；
    - Adjudicate：声明 effects（请求不携 effects——玩家不能发明效果，支柱⑦）
      走 state.adjudicate 纯委托服务（Validator 单源）结算：schema/hard_rules
      自会话行 context.rp_state_schema/hard_rules，现值经 character_state.get，
      accepted 经 character_state.update 落账；原子结算（任一被拒 = 整体拒绝，
      回执 ``{applied: false, reason}`` 带具体原因）；审计进 rp_state_audit 流水
      （source=operation，actor=player，summary 带操作 id——沉淀回路输入）。
      传输面失败（服务不可用）= 502；规则面拒绝 = 200 回执（可辨非错误）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    operation = body.get("operation")
    if not isinstance(operation, dict):
        return _game_routed({"error": "operation 须为映射（{id, params}）"}, 400)
    op_id = str(operation.get("id") or "").strip()
    if not op_id:
        return _game_routed({"error": "缺少 operation.id"}, 400)
    rows = await _call_provider("pipeline-state")
    row = next(
        (
            r for r in rows
            if isinstance(r, dict)
            and str(r.get("pipeline_id") or "") == session_id
            and str(r.get("mode") or "") == "roleplay"
        ),
        None,
    )
    if row is None:
        return _game_routed({"error": f"会话不存在或非扮演会话: {session_id}"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再 operation"}, 409)
    # Propose：会话绑定包解析（game/config 同源链）→ 清单全等选取
    agent_id = str(row.get("agent_id") or "")
    prefix = "mode_roleplay/"
    decl: dict[str, Any] | None = None
    if agent_id.startswith(prefix):
        card = next(
            (c for c in _load_cards() if c["id"] == agent_id[len(prefix):]), None
        )
        if card is not None:
            packs = _load_pack_assets(card.get("pack_ids"), _user_packs_dir())
            listing: list[dict[str, Any]] = []
            for index, (_, data, _dir) in enumerate(packs):
                normalized = _pack_operations_listing(
                    data, str(data.get("id") or f"#{index}")
                )
                if normalized:
                    listing = normalized  # 声明序后者非空清单整体覆盖（choices 同构）
            decl = next((e for e in listing if e["id"] == op_id), None)
    if decl is None:
        return _game_routed({
            "error": (
                f"操作 {op_id!r} 未在会话绑定玩法包 operations 清单声明"
                "（未声明=不可发，枚举声明制）"
            )
        }, 400)
    params, param_err = _validate_operation_params(decl.get("params_schema"), operation.get("params"))
    if param_err:
        return _game_routed({"error": param_err}, 400)
    # Adjudicate：schema 缺席 = 无契约可结，fail-closed 回执拒绝（不静默透传）
    schema = row.get("context.rp_state_schema")
    workspace = await _resolve_workspace(session_id)
    from _root_api import append_audit_rows, new_ulid  # noqa: PLC0415 （共享根裸名，惰性同 import 面）

    audit_extra = {"source": _SOURCE_OPERATION, "action": op_id, "params": params}
    idem = f"{session_id}:operation:{op_id}:{new_ulid()}"

    def _audit(rows_: list[dict[str, Any]]) -> None:
        append_audit_rows(
            workspace,
            [{**r, **audit_extra} for r in rows_],
            idem,
            _SOURCE_OPERATION,
        )

    if not isinstance(schema, dict):
        reason = (
            "状态契约未装载（会话行无 context.rp_state_schema），"
            f"操作 {op_id!r} 拒绝结算（fail-closed）"
        )
        _audit([{"key": op_id, "op": None, "from": None, "to": None, "reason": "no_schema"}])
        return _game_routed({"applied": False, "operation": op_id, "reason": reason})
    hard_rules = row.get("context.rp_state_hard_rules")
    card_id = agent_id[len(prefix):] if agent_id.startswith(prefix) else ""
    try:
        current_res = await _invoke_tool(
            "character_state.get", "character_state", {"card_id": card_id}
        )
    except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
        return {"success": True, "data": _json_response(
            {"error": f"操作现值读取失败（character_state 不可用）: {exc}"}, 502)}
    if not (isinstance(current_res, dict) and current_res.get("success") is True):
        return {"success": True, "data": _json_response(
            {"error": "操作现值读取失败（character_state 回执异常）"}, 502)}
    current = (current_res.get("data") or {}).get("values")
    if not isinstance(current, dict):
        current = {}
    try:
        adj_res = await _invoke_tool("state.adjudicate", _ADJUDICATE_SERVICE_PLUGIN, {
            "changes": dict(decl["effects"]),
            "schema": schema,
            "hard_rules": hard_rules if isinstance(hard_rules, dict) else None,
            "current": current,
        })
    except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
        return {"success": True, "data": _json_response(
            {"error": f"操作结算失败（state.adjudicate 不可用）: {exc}"}, 502)}
    if not (isinstance(adj_res, dict) and adj_res.get("success") is True):
        return {"success": True, "data": _json_response(
            {"error": "操作结算失败（state.adjudicate 回执异常）"}, 502)}
    result = adj_res.get("data") or {}
    rejected = result.get("rejected")
    accepted = result.get("accepted")
    audit = result.get("audit")
    if not isinstance(rejected, list) or not isinstance(accepted, dict) or not isinstance(audit, list):
        return {"success": True, "data": _json_response(
            {"error": "操作结算回执形态非法（accepted/rejected/audit 缺失）"}, 502)}
    # 原子结算：任一效果被拒 = 整体拒绝（玩家离散命令无部分应用），审计照记
    if rejected:
        _audit(audit)
        return _game_routed({
            "applied": False,
            "operation": op_id,
            "reason": _operation_reject_reason(rejected),
        })
    if accepted:
        try:
            await _invoke_tool(
                "character_state.update", "character_state",
                {"card_id": card_id, "changes": accepted},
            )
        except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
            return {"success": True, "data": _json_response(
                {"error": f"操作落账失败（character_state 不可用）: {exc}"}, 502)}
    _audit(audit)
    return _game_routed({
        "applied": True,
        "operation": op_id,
        "state_delta": accepted,
    })

# ── tick/长时段演化（世界内嵌谱系右移批次三，调研 §7.6 真缺口②）──────────────
# 玩法包声明世界演化规则（pack yaml 顶层 `tick`），宿主页/触发器经 game 通道
# 直达推进世界状态——不经任务壳；确定性引擎结算零 LLM（支柱⑧：AI 只出现在
# 枚举必败处，确定性段零 LLM）。结算逐 tick × 逐 step 顺序进行，每步一次
# state.adjudicate（Validator 单源）；cycle 算子在编排侧解析为绝对值——
# Validator 契约零改动（禁第二套结算语义）。when 键域执法位 = 消费侧
# _pack_tick_listing（条件求值不经 Validator）；effects 键域照旧归 Validator。

_SOURCE_TICK = "tick"

_TICK_MAX_TICKS = 10  # 单次调用上限钳制（防打爆；定时直达消费侧自限循调用）

def _tick_baseline(running: dict[str, Any], key: str, flat: dict[str, dict[str, Any]]) -> tuple[Any, bool]:
    """when/cycle 基线：running 现值 → schema 声明 default → 双缺 = (None, False)
    （不可判/不可结算，调用方 fail-closed 跳过——不猜）。"""
    if key in running:
        return running[key], True
    decl = flat.get(key)
    if isinstance(decl, dict) and decl.get("default") is not None:
        return decl["default"], True
    return None, False

def _tick_when_met(when: dict[str, Any], running: dict[str, Any], flat: dict[str, dict[str, Any]]) -> bool:
    """结构化简单条件判定（全等函数不抛）：==/!= 全类型；>=/< 仅数值
    （bool 排除）或同型字符串，混型 = 不可判 = 未满足（fail-closed 跳过）。"""
    baseline, ok = _tick_baseline(running, str(when.get("key")), flat)
    if not ok:
        return False
    op = when.get("op")
    value = when.get("value")
    if op == "==":
        return baseline == value
    if op == "!=":
        return baseline != value
    if isinstance(baseline, bool) or isinstance(value, bool):
        return False
    if isinstance(baseline, (int, float)) and isinstance(value, (int, float)):
        return baseline >= value if op == ">=" else baseline < value
    if isinstance(baseline, str) and isinstance(value, str):
        return baseline >= value if op == ">=" else baseline < value
    return False

def _tick_cycle_next(values: list[Any], current: Any) -> Any:
    """enum cycle 推进一位：current 在值域内 = 下一位；不在（含缺席取 default
    后仍不在）= 从头循环 values[0]（确定性总函数——枚举循环是引擎事实）。"""
    try:
        index = values.index(current)
    except ValueError:
        index = -1
    return values[(index + 1) % len(values)]

def _tick_step_changes(
    step: dict[str, Any], running: dict[str, Any], flat: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """声明 effects → 本步结算 changes：op 串原样透传（Validator 同源）；
    cycle 算子按 running 基线解析为绝对值 ``=next``。基线不可得（键缺席且无
    default）= None（步不可结算，调用方跳过——不发明世界没有的状态）。"""
    changes: dict[str, Any] = {}
    for key, spec in step["effects"].items():
        if isinstance(spec, dict):
            baseline, ok = _tick_baseline(running, str(key), flat)
            if not ok:
                return None
            changes[str(key)] = f"={_tick_cycle_next(list(spec['cycle']), baseline)}"
        else:
            changes[str(key)] = spec
    return changes

async def _handle_game_tick(body: dict[str, Any]) -> dict[str, Any]:
    """POST /game/tick：世界演化周期推进（宿主/触发器直达管道接口，不经任务壳）。

    - 会话校验/单宿主租约与 game/operation 同闸；ticks 正整数缺省 1，上限
      ``_TICK_MAX_TICKS`` 钳制（防打爆）；
    - 无声明包 = 空转回执 ``{applied: true, ticks, advanced: false}`` 不 404
      （tick 是通用推进接口）；有声明但会话行无状态契约 = 400 fail-closed
      （无契约可结，兼容态透传会绕开 clamp）；
    - 逐 tick × 逐 step 顺序结算（声明序）：when 判定与 cycle 解析基线 =
      running（随 accepted 更新，后续步见前序结算结果）；每步一次
      state.adjudicate（Validator 单源）；被拒步不入 steps_fired、审计照记
      全流水（漂移可辨不静默，不劫持其余步的世界推进）；全部 tick 终了一次
      character_state.update 合并落账（键后者胜 = 序结算语义）；
    - 审计 default_source=tick、actor=narrator 缺省（世界演化），批标记行
      ``{key: "tick", to: {ticks, steps_fired}}`` summary 载 ticks 数与 fired
      steps——沉淀回路输入；idem 离散事件不幂等去重（operation 同口径）。
    """
    session_id, err = _game_channel_id(body, "session_id")
    if err:
        return _game_routed({"error": err}, 400)
    ticks = body.get("ticks")
    if ticks is None:
        ticks = 1
    if not isinstance(ticks, int) or isinstance(ticks, bool) or ticks < 1:
        return _game_routed({"error": f"ticks 须为正整数（缺省 1，上限 {_TICK_MAX_TICKS} 钳制），得 {ticks!r}"}, 400)
    clamped = min(ticks, _TICK_MAX_TICKS)
    rows = await _call_provider("pipeline-state")
    row = next(
        (
            r for r in rows
            if isinstance(r, dict)
            and str(r.get("pipeline_id") or "") == session_id
            and str(r.get("mode") or "") == "roleplay"
        ),
        None,
    )
    if row is None:
        return _game_routed({"error": f"会话不存在或非扮演会话: {session_id}"}, 400)
    channel = _GAME_CHANNELS.get(session_id)
    if channel is None or channel["lease"] is None:
        return _game_routed({"error": "会话无宿主租约：先 attach 再 tick"}, 409)
    # 会话绑定包解析（game/config 同源链）→ tick 声明归一（when 键域执法位）
    agent_id = str(row.get("agent_id") or "")
    prefix = "mode_roleplay/"
    steps: list[dict[str, Any]] = []
    if agent_id.startswith(prefix):
        card = next(
            (c for c in _load_cards() if c["id"] == agent_id[len(prefix):]), None
        )
        if card is not None:
            packs = _load_pack_assets(card.get("pack_ids"), _user_packs_dir())
            for index, (_, data, _dir) in enumerate(packs):
                normalized = _pack_tick_listing(
                    data, str(data.get("id") or f"#{index}")
                )
                if normalized:
                    steps = normalized  # 声明序后者非空清单整体覆盖（operations 同构）
    if not steps:
        return _game_routed({"applied": True, "ticks": clamped, "advanced": False})
    schema = row.get("context.rp_state_schema")
    if not isinstance(schema, dict):
        return _game_routed({
            "error": (
                "状态契约未装载（会话行无 context.rp_state_schema），"
                "tick 拒绝结算（fail-closed，无契约可结）"
            )
        }, 400)
    flat, _skipped = flatten_variables(schema)
    hard_rules = row.get("context.rp_state_hard_rules")
    card_id = agent_id[len(prefix):] if agent_id.startswith(prefix) else ""
    try:
        current_res = await _invoke_tool(
            "character_state.get", "character_state", {"card_id": card_id}
        )
    except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
        return {"success": True, "data": _json_response(
            {"error": f"tick 现值读取失败（character_state 不可用）: {exc}"}, 502)}
    if not (isinstance(current_res, dict) and current_res.get("success") is True):
        return {"success": True, "data": _json_response(
            {"error": "tick 现值读取失败（character_state 回执异常）"}, 502)}
    running = (current_res.get("data") or {}).get("values")
    if not isinstance(running, dict):
        running = {}
    from _root_api import append_audit_rows, new_ulid  # noqa: PLC0415 （共享根裸名，惰性同 import 面）

    workspace = await _resolve_workspace(session_id)
    audit_rows: list[dict[str, Any]] = []
    delta: dict[str, Any] = {}
    fired: list[int] = []
    settled_any = False
    for _tick_no in range(clamped):
        for step_index, step in enumerate(steps):
            when = step.get("when")
            if when is not None and not _tick_when_met(when, running, flat):
                continue  # 条件不满足跳过（不猜不阻断）
            changes = _tick_step_changes(step, running, flat)
            if changes is None:
                continue  # cycle 基线不可得：步不可结算（不发明状态）
            try:
                adj_res = await _invoke_tool("state.adjudicate", _ADJUDICATE_SERVICE_PLUGIN, {
                    "changes": changes,
                    "schema": schema,
                    "hard_rules": hard_rules if isinstance(hard_rules, dict) else None,
                    "current": running,
                })
            except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
                return {"success": True, "data": _json_response(
                    {"error": f"tick 结算失败（state.adjudicate 不可用）: {exc}"}, 502)}
            if not (isinstance(adj_res, dict) and adj_res.get("success") is True):
                return {"success": True, "data": _json_response(
                    {"error": "tick 结算失败（state.adjudicate 回执异常）"}, 502)}
            result = adj_res.get("data") or {}
            rejected = result.get("rejected")
            accepted = result.get("accepted")
            audit = result.get("audit")
            if not isinstance(rejected, list) or not isinstance(accepted, dict) or not isinstance(audit, list):
                return {"success": True, "data": _json_response(
                    {"error": "tick 结算回执形态非法（accepted/rejected/audit 缺失）"}, 502)}
            audit_rows.extend(audit)
            if rejected:
                continue  # 被拒步不入 fired（审计已照记全流水，漂移可辨）
            running.update(accepted)
            delta.update(accepted)
            fired.append(step_index)
            settled_any = True
    if settled_any:
        try:
            await _invoke_tool(
                "character_state.update", "character_state",
                {"card_id": card_id, "changes": delta},
            )
        except Exception as exc:  # noqa: BLE001 — 传输面失败如实上行（502）
            return {"success": True, "data": _json_response(
                {"error": f"tick 落账失败（character_state 不可用）: {exc}"}, 502)}
    if audit_rows:
        idem = f"{session_id}:tick:{new_ulid()}"
        audit_rows.append({"key": _SOURCE_TICK, "op": None, "from": None,
                           "to": {"ticks": clamped, "steps_fired": fired}})
        append_audit_rows(workspace, audit_rows, idem, _SOURCE_TICK)
    if not settled_any:
        return _game_routed({"applied": True, "ticks": clamped, "advanced": False})
    return _game_routed({
        "applied": True,
        "ticks": clamped,
        "advanced": True,
        "state_delta": delta,
        "steps_fired": fired,
    })

@plugin.tool(
    name="mode.game.push_delta",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "RP 扮演会话 pipeline_id"},
            "turn_seq": {
                "type": ["integer", "null"],
                "description": "产生 delta 的尾条 assistant 消息 seq（对账序）",
            },
            "deltas": {
                "type": "array",
                "items": {"type": "object"},
                "description": "game.* 变更行（raw 值，宿主按自身规则解释）",
            },
        },
        "required": ["session_id", "deltas"],
    },
    description="游戏域 delta 入队（state_marker_parse 推送通道；无租约丢弃=Tier 0，队列上限丢最旧）",
    output_schema={
        "type": "object",
        "required": ["queued", "dropped"],
        "properties": {
            "queued": {"type": "integer"},
            "dropped": {"type": "integer"},
        },
    },
)
async def mode_game_push_delta(
    session_id: str, deltas: list[dict[str, Any]], turn_seq: int | None = None
) -> dict[str, Any]:
    """游戏域 delta 入队（state_marker_parse 落账步经 tool-executor 显式插件调用）。

    仅租约在场的会话入队（无宿主 = Tier 0，delta 无去处，如实 dropped 回执）；
    队列上限 _GAME_CHANNEL_CAP（宿主离线保护，超出丢最旧）。入参 fail-closed
    （ValueError 上行，调用方降级不阻断）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("缺少 session_id")
    if not _GAME_HOST_ID_RE.fullmatch(sid):
        raise ValueError(f"非法 session_id: {sid}")
    if not isinstance(deltas, list):
        raise ValueError("deltas 须为列表")
    if not deltas:
        raise ValueError("deltas 须非空（零 delta 轮不推送）")
    clean: list[dict[str, Any]] = []
    for item in deltas:
        if not isinstance(item, dict) or not str(item.get("key") or "").strip():
            raise ValueError("每条 delta 须为带非空 key 的对象")
        entry = dict(item)
        if turn_seq is not None:
            entry.setdefault("turn_seq", turn_seq)
        clean.append(entry)
    channel = _GAME_CHANNELS.get(sid)
    if channel is None or channel["lease"] is None:
        return {"queued": 0, "dropped": len(clean)}
    queue = channel["pending_deltas"]
    queue.extend(clean)
    dropped = 0
    while len(queue) > _GAME_CHANNEL_CAP:
        queue.pop(0)
        dropped += 1
    return {"queued": len(queue), "dropped": dropped}

# ── 声明聚合通道（RP 批 4-Ⅱ-核；template/ui 透出随批 4-Ⅲ）：GET /game/config ─────
#
# 会话绑定卡+pack 的 Tier 1 声明（rules / narrative_graph / template / ui /
# actions）聚合进向宿主透出的载荷，宿主引擎件（游戏页内 4a/4b/4c/4d/4e）按此
# 装载。游戏域读面自持聚合——装载侧 context.rp_state_* 管线（agent_config_load）
# 只承载 Tier 0 面，不感知本通道；无声明 = 键缺席（宿主引擎件空转 = Tier 0 兼容态）。

# pack 侧 Tier 1 声明键名：graph = 卡侧 narrative_graph 的包内名（rp_packs
# _DEFERRED_PACK_KEYS 契约名，packs/README 同口径）；template/ui 同为
# _DEFERRED_PACK_KEYS 契约名（批 4-Ⅲ 起启用消费，装载侧 warning 忽略随之解除面
# 归宿主）；rules/actions 与卡侧同名。pack 的 variables/behaviors（Tier 0）不进
# 本载荷（归装载侧管线）。
