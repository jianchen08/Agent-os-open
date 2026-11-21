"""卡片内容生命周期（card lint/scan / library 加载 / combos / llm 配置 / styles / crud / personas）（自 server.py 按域拆分）。"""
from __future__ import annotations

import base64
import importlib.util  # noqa: F401
import json
import logging  # noqa: F401
import os
import re
import sys  # noqa: F401
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard  # noqa: F401

import yaml

# 段内自由名依赖的原 import（自动复制）
# 段内自由名依赖的原 import（自动复制）
from card_lint import (  # noqa: E402 —— 声明面结构/内容安检单源（本插件内部模块）
    OPERATION_ID_RE,  # noqa: F401
    TICK_WHEN_OPS,  # noqa: F401
    lint_actions,
    lint_behaviors,
    lint_card_extensions,
    lint_engine,
    lint_init_ui,
    lint_operations,  # noqa: F401
    lint_tick,  # noqa: F401
    lint_ui,
    lint_variables,
    scan_html_danger,
    scan_html_danger_texts,
    scan_suspicious,
    walk_str_values,
)
from rp_common import (
    _AGENCY_AXIS_OPTIONS,
    _AGENTS_DIR,
    _CARD_ID_RE,
    _LOREBOOKS_DIR,
    _PACE_AXIS_OPTIONS,
    _PROFILE_PATH,
    _SCALE_AXIS_OPTIONS,
    _STYLES_DIR,
    logger,
    plugin,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: F401
from agentos_plugin_sdk.bootstrap import bootstrap_plugin  # noqa: F401
from agentos_plugin_sdk.capability import bind_capability_caller  # noqa: F401


def _card_vocab() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """card_io 属主词汇 → 模式卡三表（字符串归一表/列表表/透传表）。

    惰性导入 card_io：其顶层拉起 Pillow，而 Pillow 非本插件运行时依赖——
    插件装载不得被 PNG 能力绑架（与 _write_user_card 的 atomic_io 惰性导入同款）。
    """
    from card_io import DICT_FIELDS, LIST_FIELDS, STR_FIELDS

    str_fields = tuple(f for f in STR_FIELDS if f not in ("creator", "character_version"))
    list_fields = (*LIST_FIELDS, "lorebook_ids", "pack_ids")
    passthrough_fields = ("creator", "avatar", "voice_ref", "state_schema", "cover", "portrait", *DICT_FIELDS)
    return str_fields, list_fields, passthrough_fields

# 创作卡类别（扩展波 B-Ⅰ）：category=creation 的 agent 卡是创作工作台的执行者
# （card_author），不是扮演卡——卡库读面过滤，不用工作台 = 卡库现状零变化。

from _libs.mode_server_base import (  # noqa: E402 — 模式 profile 单源（_libs 基面）
    load_mode_profile,
    register_profile_services,
)

_CREATION_CATEGORY = "creation"

# 内容安全扫描单源在本插件 card_lint（扩展波 B-Ⅱ 收编：suspicious 注入形状 +
# 高危 HTML 形态两模式表迁址，卡导入/世界书导入/创作 lint/出厂内容提案四消费
# 面同源禁第四套）；本文件 _scan_card_danger/_scan_card_html_danger 为薄委托。


def load_profile(path: str = _PROFILE_PATH) -> dict[str, Any]:
    """读取包内 mode.yaml（缺省本包种子；fail-closed 实现在 _libs 基面）。"""
    return load_mode_profile(path)


# 模式元数据服务（manifest capabilities.services；handler 别名供契约测试直调）

mode_describe, mode_get_profile = register_profile_services(
    plugin,
    _PROFILE_PATH,
    "返回角色扮演模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
    "返回角色扮演模式包内 mode.yaml 解析后的内容",
)


# ── 卡面质检服务（provides namespace ``card``，T6c；消费方 agent_config_load /
#    mode_evolution 经 requires_services + capability handle 直连，禁模块级 import）──
#
# 提供方形态跟 skill_manager 先例（服务方法 = sidecar 同名工具，内核 McpBridge
# 经 tool_prefix 转发 card.<method>）；wire 契约 = 工具函数裸 dict 返回（无信封）。
# 逐方法为 card_lint 纯函数的 1:1 薄透出（零策略加工，消费面语义与本插件直调
# 完全同源），入参出参全 JSON 可序列化。





@plugin.tool(
    name="card.lint_decl",
    schema={
        "type": "object",
        "properties": {
            "variables": {"type": ["object", "null"]},
            "behaviors": {"type": ["object", "null"]},
            "engine": {"type": ["object", "null"]},
        },
    },
    description="卡声明结构 lint（variables/behaviors/engine；单源 card_lint）",
)
async def card_lint_decl(
    variables: dict[str, Any] | None = None,
    behaviors: dict[str, Any] | None = None,
    engine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """合并产物声明面 warnings（lint_variables + lint_behaviors + lint_engine）。"""
    warnings: list[str] = []
    if variables is not None:
        warnings.extend(lint_variables(variables))
    if behaviors is not None:
        warnings.extend(lint_behaviors(behaviors))
    if engine is not None:
        warnings.extend(lint_engine(engine))
    return {"warnings": warnings}



@plugin.tool(
    name="card.lint_actions",
    schema={
        "type": "object",
        "properties": {"actions": {"type": ["array", "null"]}},
    },
    description="actions target 面校验+归一（缺省补 engine、非法剔除）",
)
async def card_lint_actions(actions: list[Any] | None = None) -> dict[str, Any]:
    """actions 规范化列表 + warnings（装载侧消费，归一化产物写 context.rp_actions）。"""
    valid, warnings = lint_actions(actions)
    return {"actions": valid, "warnings": warnings}



@plugin.tool(
    name="card.lint_pack_ui",
    schema={
        "type": "object",
        "properties": {
            "ui": {"type": ["object", "null"]},
            "init_ui": {"type": ["object", "null"]},
        },
    },
    description="pack 呈现面 ui/init_ui 结构 lint",
)
async def card_lint_pack_ui(
    ui: dict[str, Any] | None = None,
    init_ui: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """pack 呈现声明 warnings（lint_ui + lint_init_ui）。"""
    return {"warnings": [*lint_ui(ui), *lint_init_ui(init_ui)]}



@plugin.tool(
    name="card.lint_extensions",
    schema={
        "type": "object",
        "properties": {"extensions": {"type": ["object", "null"]}},
    },
    description="卡 extensions 四面结构 lint 汇总",
)
async def card_lint_extensions_service(
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """卡/包声明结构 warnings 汇总（content_audit 消费）。"""
    return {"warnings": lint_card_extensions(extensions)}



@plugin.tool(
    name="card.scan_suspicious",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string"}, "subject": {"type": "string"}},
        "required": ["text"],
    },
    description="可疑指令模式扫描（大小写不敏感子串）",
)
async def card_scan_suspicious(text: str, subject: str = "角色设定") -> dict[str, Any]:
    """可疑注入形状单条可读标注（命中才非空）。"""
    return {"warnings": scan_suspicious(text, subject)}



@plugin.tool(
    name="card.scan_html_card",
    schema={
        "type": "object",
        "properties": {"card": {"type": "object"}},
        "required": ["card"],
    },
    description="卡形态高危 HTML 扫描（prompt 载体全字段）",
)
async def card_scan_html_card(card: dict[str, Any]) -> dict[str, Any]:
    """卡各文本值逐形态独立标注。"""
    return {"warnings": scan_html_danger(card)}



@plugin.tool(
    name="card.scan_html_texts",
    schema={
        "type": "object",
        "properties": {"texts": {"type": "array", "items": {"type": "string"}}, "subject": {"type": "string"}},
        "required": ["texts"],
    },
    description="高危 HTML 形态扫描（文本集入参）",
)
async def card_scan_html_texts(texts: list[str], subject: str = "角色卡内容") -> dict[str, Any]:
    """文本集逐形态独立标注（同形态多文本命中合并）。"""
    return {"warnings": scan_html_danger_texts(texts, subject)}



@plugin.tool(
    name="card.texts_of_tree",
    schema={
        "type": "object",
        "properties": {"data": {"type": "object"}},
        "required": ["data"],
    },
    description="任意嵌套结构收集全部字符串叶子",
)
async def card_texts_of_tree(data: dict[str, Any]) -> dict[str, Any]:
    """嵌套声明数据全部字符串叶子（玩法包内容扫描取料）。"""
    return {"texts": walk_str_values(data)}


# ── 内核读数桥（monitoring kernel_reads 同构：provider 注册 + 未就绪降级空）────────


def _session_pack_ids(row: dict[str, Any], cards: list[dict[str, Any]]) -> list[str]:
    """会话行 → 玩法归属 id 列表（行内可得什么透什么，都不可得 = 空数组如实）。

    两源：行 agent_id（mode_roleplay/<card_id> 卡键）→ 卡 pack_ids 归一；行
    wizard_pack_ids 持久键（mode.session 透传键若随行出口——非出口保证，键在
    才并入）兼容 list 与逗号串两形态。卡在前、向导增量在后，去重保序。
    """
    ids: list[str] = []
    agent_id = str(row.get("agent_id") or "")
    prefix = "mode_roleplay/"
    if agent_id.startswith(prefix):
        card = next((c for c in cards if c["id"] == agent_id[len(prefix):]), None)
        if card:
            ids.extend(str(p) for p in card.get("pack_ids") or [])
    wizard = row.get("wizard_pack_ids")
    wizard_ids = (
        [str(p) for p in wizard] if isinstance(wizard, list) else (
            [seg.strip() for seg in str(wizard).split(",")] if isinstance(wizard, str) else []
        )
    )
    for pack_id in wizard_ids:
        if pack_id and pack_id not in ids:
            ids.append(pack_id)
    return ids



def _session_state_summary(row: dict[str, Any]) -> dict[str, Any] | None:
    """会话行持久 state 键 → 摘要 dict（键不在 = None，调用方键缺席——禁止发明）。

    行内出口键候选：context.character_state_text（state_marker_parse 每轮回写的
    状态渲染文本，context.* 前缀随插件出口声明随行）。game_host_delta 等其余
    持久键不进摘要（delta 是转发面不是状态面）。
    """
    state_text = row.get("context.character_state_text")
    if isinstance(state_text, str) and state_text.strip():
        return {"character_state_text": state_text}
    return None



def _norm_sessions(rows: Any, cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """全量 state 行 → 本模式会话行（mode 键过滤 + 面板字段裁剪）。

    RP 面板 v3.1 刀1 行增：pack_ids（玩法归属，?pack= 过滤与「继续上局」按包
    过滤依据）+ state_summary（持久 state 摘要 dict；行内键不在 = 键缺席）。
    """
    resolved_cards = _load_cards() if cards is None else cards
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("mode") or "") != "roleplay":
            continue
        item = {
            "pipeline_id": str(row.get("pipeline_id") or ""),
            "thread_id": str(row.get("thread_id") or ""),
            "agent_id": str(row.get("agent_id") or ""),
            "run_status": str(row.get("run_status") or ""),
            "goal": str(row.get("task.goal") or row.get("input") or ""),
            "task_status": str(row.get("task.status") or ""),
            # task.created_at：任务域出生键（tasks http_api 同源约定）；缺席如实空
            "created_at": str(row.get("task.created_at") or ""),
            "message_count": row.get("message_count") or 0,
            "pack_ids": _session_pack_ids(row, resolved_cards),
        }
        summary = _session_state_summary(row)
        if summary is not None:
            item["state_summary"] = summary
        items.append(item)
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


# ── 物料读取（卡/世界书 = 出厂根 ∪ 用户层双根合并）───────────────────────────────


def _user_config_root() -> str | None:
    """用户配置层根（user_space 共享裸模块；裸名导入放函数内维持种子自包含）。

    bootstrap_plugin 已把 plugins/shared 根推上 sys.path（用户空间播种副本由
    SDK 位置锚回退兜底，见模块头注），函数内裸名导入即达；模块顶层不新增
    共享根导入前提。极端环境解析失败 → None，调用方按「无用户空间」定语义
    （读面降级空，写面如实报错不静默假成功）。
    """
    from _root_api import user_config_dir

    resolved = user_config_dir()
    return str(resolved) if resolved is not None else None



def _user_cards_dir() -> str | None:
    """用户层角色卡目录：<user_config_dir>/agents/mode_roleplay（镜像包内布局）。"""
    root = _user_config_root()
    return os.path.join(root, "agents", "mode_roleplay") if root else None



def _user_lorebooks_dir() -> str | None:
    """用户层世界书目录：<user_config_dir>/lorebooks（镜像包内布局）。"""
    root = _user_config_root()
    return os.path.join(root, "lorebooks") if root else None



def _personas_path() -> str | None:
    """persona 档案文件：<user_config_dir>/persona/roleplay_personas.json。"""
    root = _user_config_root()
    return os.path.join(root, "persona", "roleplay_personas.json") if root else None



def _card_from_data(data: dict[str, Any], card_id: str, origin: str) -> dict[str, Any]:
    """卡源数据 → 模式卡扁平 dict（词表归一 + 新字段宽容透传，词表见模块头常量）。

    name 缺失回落卡 id（画廊不可无名）；lorebook_ids/voice_ref/state_schema/
    cover/portrait/extensions 原样透传（.get 宽容，合法性由消费端把关）。
    """
    str_fields, list_fields, passthrough_fields = _card_vocab()
    card: dict[str, Any] = {"id": card_id, "origin": origin}
    for field in str_fields:
        card[field] = str(data.get(field) or "")
    for field in list_fields:
        value = data.get(field)
        card[field] = [str(item) for item in value] if isinstance(value, list) else []
    theme = data.get("theme")
    card["theme"] = theme if isinstance(theme, dict) else None
    for field in passthrough_fields:
        card[field] = data.get(field)
    if not card["name"]:
        card["name"] = card_id
    return card



def _scan_cards_dir(directory: str | None, origin: str) -> list[tuple[str, dict[str, Any]]]:
    """单根扫描 → (card_id, 源数据) 有序列表；损坏/非 dict 文件跳过不崩面板。

    出厂根按包内约定只认 card_*.yaml；用户层受理面 = save/import 写入的 id
    命名空间（stem 须过 _CARD_ID_RE）——import 派生 id（如 seraphina）不带
    card_ 前缀，按前缀过滤会静默隐身，故用户层按 stem 校验全量扫描。
    """
    found: list[tuple[str, dict[str, Any]]] = []
    if not directory or not os.path.isdir(directory):
        return found
    for entry in sorted(Path(directory).iterdir()):
        fname = entry.name
        if not fname.endswith(".yaml"):
            continue
        card_id = fname[: -len(".yaml")]
        if origin == "builtin":
            if not fname.startswith("card_"):
                continue
        elif not _CARD_ID_RE.fullmatch(card_id):
            continue
        try:
            with open(os.path.join(directory, fname), encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            found.append((card_id, data))
    return found



def _load_cards(agents_dir: str = _AGENTS_DIR, user_dir: str | None = None) -> list[dict[str, Any]]:
    """双根合并列卡：出厂（包内 agents/card_*.yaml，origin="builtin"）∪ 用户层
    （<user_config_dir>/agents/mode_roleplay，origin="user"）。

    用户层同 id 接管出厂卡（文件级所有权转移，与 user_space 覆盖语义同构；
    出厂件永不被写）。user_dir=None（缺省）经 user_space 现场解析，用户空间
    不可得 → 只出厂根（读面降级）；显式传路径供测试注入隔离。损坏文件跳过。
    """
    resolved_user_dir = _user_cards_dir() if user_dir is None else user_dir
    merged: dict[str, dict[str, Any]] = {}
    for origin, directory in (("builtin", agents_dir), ("user", resolved_user_dir)):
        for card_id, data in _scan_cards_dir(directory, origin):
            if str(data.get("category") or "") == _CREATION_CATEGORY:
                continue  # 创作 agent 卡不进扮演卡库（工作台 mode.session 直附身）
            merged[card_id] = _card_from_data(data, card_id, origin)  # 同 id 后根接管
    return list(merged.values())



def _entry_from_st(entry: dict[str, Any]) -> dict[str, Any]:
    """ST World Info 条目 → 模式书条目（字段映射单一真值，缺省补默认）。

    keys / secondary_keys / content / enable→enabled / insertion_order /
    constant / position / case_sensitive / comment。enabled 存储键优先、
    ST 导出键 enable 次之（自有 save/import 落盘形态与 ST 导出双兼容）。

    批 1c 字面扩展（匹配语义见 lorebook.py）：源键回落归一 key→keys /
    keysecondary→secondary_keys / disable→enabled 取反 / order→insertion_order
    / caseSensitive→case_sensitive；批 1c 新增字面字段（keysecondary/
    selectiveLogic/disable/order/probability/useProbability/caseSensitive/
    matchWholeWords）随条目透传保留——存在才写，旧形态条目存储形状零增。
    """
    if "enabled" in entry:
        enabled = bool(entry["enabled"])
    elif "enable" in entry:
        enabled = bool(entry["enable"])
    elif "disable" in entry:
        enabled = not bool(entry["disable"])
    else:
        enabled = True
    normalized: dict[str, Any] = {
        "keys": [str(k) for k in (entry.get("keys") or entry.get("key") or [])],
        "secondary_keys": [
            str(k) for k in (entry.get("secondary_keys") or entry.get("keysecondary") or [])
        ],
        "content": str(entry.get("content") or ""),
        "enabled": enabled,
        "insertion_order": entry.get("insertion_order", entry.get("order", 100)),
        "constant": bool(entry.get("constant", False)),
        "position": str(entry.get("position") or "before_char"),
        "case_sensitive": bool(entry.get("case_sensitive", entry.get("caseSensitive", False))),
        "comment": str(entry.get("comment") or ""),
    }
    for field in (
        "keysecondary",
        "selectiveLogic",
        "disable",
        "order",
        "probability",
        "useProbability",
        "caseSensitive",
        "matchWholeWords",
    ):
        if field in entry:
            normalized[field] = entry[field]
    return normalized



def _book_from_data(data: dict[str, Any], book_id: str, origin: str) -> dict[str, Any]:
    """书源数据 → 面板书 dict（条目经 _entry_from_st 归一 + origin 标来源）。

    name 缺失回落书 id；entries 缺席 = 空表（空书合法，可在面板编辑后使用）。
    """
    entries = [
        _entry_from_st(entry)
        for entry in (data.get("entries") or [])
        if isinstance(entry, dict)
    ]
    return {
        "id": book_id,
        "origin": origin,
        "name": str(data.get("name") or book_id),
        "description": str(data.get("description") or ""),
        "scan_depth": data.get("scan_depth", 4),
        "token_budget": data.get("token_budget", 512),
        "entries": entries,
    }



def _scan_books_dir(directory: str | None, origin: str) -> list[tuple[str, dict[str, Any]]]:
    """单根扫描 → (book_id, 源数据) 有序列表；损坏/非 dict 文件跳过不崩面板。

    用户层受理面 = save/import 写入的 id 命名空间（stem 须过 _CARD_ID_RE，
    与卡同款路径安全闸）；出厂根全量 .yaml（包内物料随包命名）。
    """
    found: list[tuple[str, dict[str, Any]]] = []
    if not directory or not os.path.isdir(directory):
        return found
    for path in sorted(Path(directory).glob("*.yaml")):
        book_id = path.stem
        if origin != "builtin" and not _CARD_ID_RE.fullmatch(book_id):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            found.append((book_id, data))
    return found



def _load_lorebooks(lorebooks_dir: str = _LOREBOOKS_DIR, user_dir: str | None = None) -> list[dict[str, Any]]:
    """双根合并列书：出厂（包内 lorebooks/*.yaml，origin="builtin"）∪ 用户层
    <user_config_dir>/lorebooks（origin="user"）。

    用户层同 id 接管出厂书（与卡库双根/文件级所有权转移同构，出厂件永不被
    写）。user_dir=None（缺省）经 user_space 现场解析，用户空间不可得 → 只
    出厂根（读面降级）；显式传路径供测试注入隔离。损坏文件跳过。
    """
    resolved_user_dir = _user_lorebooks_dir() if user_dir is None else user_dir
    merged: dict[str, dict[str, Any]] = {}
    for origin, directory in (("builtin", lorebooks_dir), ("user", resolved_user_dir)):
        for book_id, data in _scan_books_dir(directory, origin):
            merged[book_id] = _book_from_data(data, book_id, origin)  # 同 id 后根接管
    return list(merged.values())



def _user_combos_dir() -> str | None:
    """用户层 combos 目录：<user_config_dir>/rp_combos（rp_drafts 同区双根风格）。"""
    root = _user_config_root()
    return os.path.join(root, "rp_combos") if root else None



def _combos_raw() -> list[tuple[str, dict[str, Any]]]:
    """单根扫描 → (combo_id, 源数据) 有序列表；损坏/非 dict 文件跳过 + warning。"""
    directory = _user_combos_dir()
    if not directory or not os.path.isdir(directory):
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("combo 文件损坏，跳过: %s (%s)", path.name, exc)
            continue
        if isinstance(data, dict):
            found.append((path.stem, data))
    return found



def _combo_from_data(data: dict[str, Any], combo_id: str) -> dict[str, Any]:
    """combo 源数据 → 面板载荷（词表归一：列表字段非 list 一律空表，id/origin
    服务端语义键不信任存储值）。"""
    combo: dict[str, Any] = {
        "id": combo_id,
        "origin": "user",
        "name": str(data.get("name") or combo_id),
        "card_id": str(data.get("card_id") or ""),
        "route_id": str(data.get("route_id") or ""),
        "created_at": str(data.get("created_at") or ""),
    }
    for field in ("lorebook_ids", "trait_ids", "pack_ids"):
        value = data.get(field)
        combo[field] = [str(item) for item in value] if isinstance(value, list) else []
    extra = data.get("profile_extra")
    if isinstance(extra, str) and extra.strip():
        combo["profile_extra"] = extra  # 可选承载：键缺席式（旧件/未填零新增键）
    return combo



def _load_combos() -> list[dict[str, Any]]:
    """combos 读面（GET data/packs 一并返回，面板少一枪）；id 字典序稳定。"""
    return [_combo_from_data(data, combo_id) for combo_id, data in _combos_raw()]



def _combo_save_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/combos/save：{combo} 嵌套与平铺双形态宽容（卡 save 同款）。

    name/card_id 必填，card_id 须在卡库（fail-closed——组合挂在未知卡上不可
    开演）；id 缺省按 name 派生避让（卡 id 派生器复用），显式 id 过白名单闸；
    同 id 覆盖（用户件），created_at 首存定格、覆盖保留；profile_extra 可选
    透传（自由文本初始状态段，空值键缺席只增不破）。
    """
    combo = body["combo"] if isinstance(body.get("combo"), dict) else body
    name = str(combo.get("name") or "").strip()
    card_id = str(combo.get("card_id") or "").strip()
    if not name:
        return {"error": "缺少 name"}
    if not card_id:
        return {"error": "缺少 card_id"}
    if not any(c["id"] == card_id for c in _load_cards()):
        return {"error": f"未知角色卡: {card_id}"}
    explicit_id = str(combo.get("id") or body.get("combo_id") or "").strip()
    if explicit_id:
        if not _CARD_ID_RE.fullmatch(explicit_id):
            return {"error": f"非法 id（须为 ^[a-z0-9_]{{1,64}}$）: {explicit_id}"}
        combo_id = explicit_id
    else:
        combo_id = _derive_card_id(name, {cid for cid, _ in _combos_raw()})
    directory = _user_combos_dir()
    if directory is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），combo 无处落盘"}
    created_at = datetime.now(UTC).isoformat()
    for existing_id, data in _combos_raw():
        if existing_id == combo_id:
            created_at = str(data.get("created_at") or created_at)
            break
    lorebook_ids = combo.get("lorebook_ids")
    trait_ids = combo.get("trait_ids")
    pack_ids = combo.get("pack_ids")
    # profile_extra（可选透传，刀2：面板组合③自由文本初始状态段的承载字段）——
    # 只增不破：空值键缺席，旧读面/旧件零感知。
    profile_extra = str(combo.get("profile_extra") or "").strip()
    payload: dict[str, Any] = {
        "id": combo_id,
        "name": name,
        "card_id": card_id,
        "lorebook_ids": [str(i) for i in lorebook_ids] if isinstance(lorebook_ids, list) else [],
        "route_id": str(combo.get("route_id") or ""),
        "trait_ids": [str(i) for i in trait_ids] if isinstance(trait_ids, list) else [],
        "pack_ids": [str(i) for i in pack_ids] if isinstance(pack_ids, list) else [],
        "created_at": created_at,
        "origin": "user",
    }
    if profile_extra:
        payload["profile_extra"] = profile_extra
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{combo_id}.json"),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return {"combo_id": combo_id, "created_at": created_at}



def _combo_delete_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/combos/delete {id}：仅用户件（combos 本就全用户层），不存在如实报错。"""
    combo_id = str(body.get("id") or body.get("combo_id") or "").strip()
    if not combo_id:
        return {"error": "缺少 id"}
    if not _CARD_ID_RE.fullmatch(combo_id):
        return {"error": f"非法 id: {combo_id}"}
    directory = _user_combos_dir()
    path = os.path.join(directory, f"{combo_id}.json") if directory else None
    if path is not None and os.path.isfile(path):
        os.remove(path)
        return {"combo_id": combo_id, "deleted": True}
    return {"error": f"combo 不存在: {combo_id}"}


# ── 沉淀回路（缺口批次三：§7.4 游玩流水→统计→候选→审核闸→固化）───────────────────
#
# 输入 = 审计事件流水（rp_state_audit 单源写面产物，事件四件套 actor/action/
# target/summary 批次二已落）；统计判据 = 可枚举性（§7.4）：命名动作（行来源
# source=action）对同状态键同向同幅的转移重复出现才聚为候选规则（标准
# behaviors 形态 trigger/effect）；幅度漂移/被拒行/作弊旁路/既有 behavior 行/
# op 动词簿记行不成例——非枚举文本永久留 LLM。固化 = 人工审核闸（POST promote
# 显式调用才写，本批不做自动沉淀）：候选规则体写入用户层玩法包 behaviors（双根
# 装载同 id 接管即生效，消费走既有求值器 state_marker_parse 4a 通道零 LLM），
# 条目带来源标记（source=precipitated + precipitated_at + precipitated_from）
# 可单独识别回退；作者声明 behavior 同 id = 409 不覆写作者面，沉淀条目自身
# 幂等更新。


_PRECIP_CANDIDATES_ROUTE = "/ext/mode_roleplay/data/precipitation/candidates"

_PRECIP_PROMOTE_ROUTE = "/ext/mode_roleplay/data/precipitation/promote"


_PRECIP_SOURCE = "precipitated"
# 复盘统计读面窗口上限（recent_events 复用单源读面；会话游玩流水远小于此，
# 上限仅内存护栏——语义即全量史）。

_PRECIP_STAT_WINDOW = 10000
# op 动词映射动作名（事件 action 面的簿记动词）：这些是 op 派生词不是声明动作
# id，无「动作→状态」映射可言，不成例。

_PRECIP_BOOKKEEPING_ACTIONS = {"increase", "decrease", "set", "observe"}



def _llm_yaml_candidates() -> list[str]:
    """llm.yaml 候选路径（用户空间优先 → 项目根回落）；测试注入单点。"""
    candidates = []
    config_root = _user_config_root()
    if config_root:
        candidates.append(os.path.join(config_root, "plugins", "llm", "llm.yaml"))
    candidates.append(
        os.path.abspath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "..",
                "..",
                "config",
                "plugins",
                "llm",
                "llm.yaml",
            )
        )
    )
    return candidates



def _llm_default_chat_model() -> str:
    """defaults.chat 读面（系统模型注册真值 llm.yaml）：用户空间优先 → 项目根回落
    （hindsight _load_llm_yaml 同款双根）。缺失/损坏 = 空串（调用方 502 显式，
    不猜默认模型）。"""
    for path in _llm_yaml_candidates():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("[mode_roleplay] llm.yaml 读取失败 | path=%s | err=%s", path, exc)
            return ""
        if not isinstance(data, dict):
            continue
        defaults = data.get("defaults")
        model = defaults.get("chat") if isinstance(defaults, dict) else None
        if isinstance(model, str) and model.strip():
            return model.strip()
    return ""



def _scan_styles_dir(directory: str | None) -> list[dict[str, Any]]:
    """文风档目录扫描 → 展示条目列表（按文件名有序；损坏/缺字段文件跳过）。

    条目 = {id, name, description}，description 取特征清单首行（档位一眼可辨，
    全清单属注入面=后续刀）。fail-soft：目录缺失 = 空列表（文风轴空选项，
    向导照常开演=无文风注入常态），不炸候选集端点。
    """
    found: list[dict[str, Any]] = []
    if not directory or not os.path.isdir(directory):
        return found
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        style_id = str(data.get("id") or "")
        name = str(data.get("name") or "")
        traits = data.get("traits")
        if not style_id or not name or not isinstance(traits, list):
            continue
        first_trait = traits[0] if traits and isinstance(traits[0], str) else ""
        found.append({"id": style_id, "name": name, "description": str(first_trait)})
    return found



def _wizard_axes_listing() -> list[dict[str, Any]]:
    """GET /data/wizard/axes 载荷：四轴候选集（文风轴动态读 styles/，其余三轴
    静态清单）。轴 id = rp_axes 快照键词表，轴序即面板渲染序。"""
    return [
        {"id": "style", "label": "文风", "options": _scan_styles_dir(_STYLES_DIR)},
        {"id": "scale", "label": "内容尺度", "options": list(_SCALE_AXIS_OPTIONS)},
        {"id": "pace", "label": "剧情节奏", "options": list(_PACE_AXIS_OPTIONS)},
        {"id": "agency", "label": "你的代理权", "options": list(_AGENCY_AXIS_OPTIONS)},
    ]


# ── 卡/世界书/persona 写面（用户空间落盘；出厂件只读，接管=用户层同 id 覆盖）────────


def _write_user_card(card: dict[str, Any]) -> dict[str, Any]:
    """模式卡 dict → 用户层 yaml（<user_cards_dir>/<id>.yaml）。

    载荷 = 已知字段并集宽容写入（None 不落）；id 随文件名不入载荷（读面以
    stem 为 id，两处写必然漂移）。写经 atomic_io（同目录 tmp + os.replace）
    防半写。出厂同 id 覆盖即接管语义（读面用户赢），不产出错误。
    返回 {card_id}；用户空间不可得 → {error}（写面不静默降级）。
    """
    directory = _user_cards_dir()
    if directory is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），用户卡无处落盘"}
    str_fields, list_fields, passthrough_fields = _card_vocab()
    payload = {
        field: card[field]
        for field in (*str_fields, *list_fields, "theme", *passthrough_fields)
        if card.get(field) is not None
    }
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{card['id']}.yaml"),
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )
    return {"card_id": card["id"]}



def _card_id_from_filename(filename: str) -> str:
    """导入文件名 → 卡 id：去扩展名、小写、非法字符转 _、截 64；派生为空返空。"""
    stem = Path(filename).stem
    return re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")[:64].strip("_")



def _derive_card_id(name: str, taken: set[str]) -> str:
    """name → 合法卡 id（面板新建/出厂复制流不持 id，id 由服务端派生——面板契约）。

    非法字符转 _ 截 64；纯 CJK 等不可派生名以秒级时间戳兜底；与既有 id 冲突
    递增后缀避让（不静默覆盖他卡）。
    """
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:64].strip("_")
    if not slug:
        slug = "card_" + time.strftime("%Y%m%d_%H%M%S")
    candidate = slug
    suffix = 2
    while candidate in taken:
        candidate = f"{slug}_{suffix}"
        suffix += 1
    return candidate



def _scan_card_danger(card: dict[str, Any]) -> list[str]:
    """内容安全简化闸：description+system_prompt 拼文扫描可疑指令模式。

    只标注不阻断（人工检查指引随 warnings 上行）；大小写不敏感子串匹配。
    模式表单源在本插件 card_lint（B-Ⅱ 收编），本函数为消费面薄委托。
    """
    from card_lint import scan_suspicious  # noqa: PLC0415 （本插件单源，惰性同 import 面）

    haystack = str(card.get("description") or "") + str(card.get("system_prompt") or "")
    return scan_suspicious(haystack, "角色设定")



def _scan_card_html_danger(card: dict[str, Any]) -> list[str]:
    """高危 HTML 形态扫描（面板刀-Ⅱ）：description/personality/scenario/
    first_mes/alternate_greetings 及 extensions 各文本值，逐形态独立 warnings。

    只标注不清洗（卡资产原样落盘——与 suspicious 指令同待遇，作者知情）；
    模式表单源在本插件 card_lint（B-Ⅱ 收编），本函数为消费面薄委托。
    """
    from card_lint import scan_html_danger  # noqa: PLC0415 （本插件单源，惰性同 import 面）

    return scan_html_danger(card)



def _save_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/save → 用户层落卡（{card} 嵌套与面板平铺双形态宽容）。

    id 来源优先级：card.id > body.card_id（面板键）> name 派生（新建流，面板
    契约「出厂卡副本 id 由服务端派生」；name 缺席如实拒绝——无名卡不入库）；
    显式 id 须过 _CARD_ID_RE（路径安全闸）。已存在出厂同 id → 覆盖写用户层
    =接管语义，不产出错误。
    """
    card = body["card"] if isinstance(body.get("card"), dict) else body
    card_id = str(card.get("id") or body.get("card_id") or "").strip()
    if not card_id:
        name = str(card.get("name") or "").strip()
        if not name:
            return {"error": "缺少 id/name（新建卡须带 name 以派生合法 id）"}
        card_id = _derive_card_id(name, {c["id"] for c in _load_cards()})
    if not _CARD_ID_RE.fullmatch(card_id):
        return {"error": f"非法 id（须为 ^[a-z0-9_]{{1,64}}$）: {card_id}"}
    saved = _write_user_card(_card_from_data(card, card_id, "user"))
    return saved



def _delete_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/delete：仅用户层可删。

    用户文件先行（接管副本删除即解除接管，出厂房仍在）；出厂卡拒绝（面板
    指引复制后修改）；都不存在如实报错。card_id 先过正则再拼路径（穿越防线）。
    """
    card_id = str(body.get("card_id") or "").strip()
    if not card_id:
        return {"error": "缺少 card_id"}
    if not _CARD_ID_RE.fullmatch(card_id):
        return {"error": f"非法 card_id: {card_id}"}
    directory = _user_cards_dir()
    user_path = os.path.join(directory, f"{card_id}.yaml") if directory else None
    if user_path is not None and os.path.isfile(user_path):
        os.remove(user_path)
        return {"card_id": card_id, "deleted": True}
    if any(c["id"] == card_id for c in _load_cards()):
        return {"error": "出厂卡不可删除，可复制后修改"}
    return {"error": f"卡不存在: {card_id}"}



def _import_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/import：ST V2/V3 卡文件 → 用户层落盘。

    字节经 base64 解码后交 card_io.load_card（PNG tEXt/JSON 双形态自动识别，
    平铺共享模块函数内裸名导入），data 字段平铺为模式卡（extensions 原样入
    extensions 键）。file_b64（规格键）/data_base64（面板键）双形态宽容。
    响应 {card_id, issues}（issues = card_io.validate_card 结构问题清单），
    命中可疑指令模式加 warnings（只标注不阻断）。解码/解析失败如实 error。
    """
    raw_b64 = str(body.get("file_b64") or body.get("data_base64") or "")
    filename = str(body.get("filename") or "")
    if not raw_b64 or not filename:
        return {"error": "缺少 file_b64/filename"}
    try:
        raw = base64.b64decode(raw_b64)
    except ValueError as exc:  # binascii.Error 是 ValueError 子类
        return {"error": f"file_b64 base64 解码失败: {exc}"}
    from card_io import load_card, validate_card
    from card_lint import lint_card_extensions

    try:
        envelope = load_card(raw)
    except ValueError as exc:
        return {"error": f"角色卡解析失败: {exc}"}
    issues = validate_card(envelope)
    card_id = _card_id_from_filename(filename)
    if not card_id:
        return {"error": f"无法从 filename 派生合法卡 id: {filename}"}
    raw_data = envelope.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    stored = _card_from_data(data, card_id, "user")
    saved = _write_user_card(stored)
    if "error" in saved:
        return saved
    response: dict[str, Any] = {"card_id": card_id, "issues": issues}
    # 声明面结构 lint（card_lint 单源，RP 批 2-Ⅱ 2d——与装载侧 agent_config_load
    # 同一套判定）：variables 类型面/enum 面 + behaviors trigger/effect 面 +
    # actions target 面的问题清单随 warnings 上行（只标注不阻断）；高危 HTML
    # 形态扫描（面板刀-Ⅱ）同通道合流（只标注不清洗）。
    warnings = (
        _scan_card_danger(stored)
        + _scan_card_html_danger(stored)
        + lint_card_extensions(stored.get("extensions"))
    )
    if warnings:
        response["warnings"] = warnings
    return response



def _export_envelope(card: dict[str, Any]) -> dict[str, Any]:
    """合并卡 → ST V2 信封（export 唯一构造点，JSON/PNG 两形态共用）。

    data = 卡扁平字段去 id/origin（信封层语义键不落 data），None 字段不落。
    """
    data = {k: v for k, v in card.items() if k not in ("id", "origin") and v is not None}
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}



def _export_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/export：合并后卡 → ST V2 信封 → base64 载荷下载。

    format 缺省 "json"（既有契约零变化）；"png" 走 card_io.write_png_card
    （tEXt 手写 PNG，与 import 的 base64 载荷对称，content_base64 为面板
    data URI 下载别名）。其余取值参数错误（fail-closed，不静默回落 json）。
    """
    card_id = str(body.get("card_id") or "").strip()
    if not card_id:
        return {"error": "缺少 card_id"}
    fmt = str(body.get("format") or "json")
    if fmt not in ("json", "png"):
        return {"error": f"非法 format: {fmt}（可选 json/png）"}
    card = next((c for c in _load_cards() if c["id"] == card_id), None)
    if card is None:
        return {"error": f"未知角色卡: {card_id}"}
    envelope = _export_envelope(card)
    if fmt == "png":
        from card_io import write_png_card

        payload = base64.b64encode(write_png_card(envelope)).decode("ascii")
        return {"filename": f"{card_id}.png", "file_b64": payload, "content_base64": payload}
    payload = base64.b64encode(json.dumps(envelope, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return {"filename": f"{card_id}.json", "file_b64": payload, "content_base64": payload}



def _write_user_lorebook(book: dict[str, Any]) -> dict[str, Any]:
    """世界书 dict → 用户层 yaml（<user_lorebooks_dir>/<id>.yaml）。

    载荷 = 归一书字段去 id/origin（id 随文件名不入载荷，读面以 stem 为 id，
    两处写必然漂移）。写经 atomic_io（同目录 tmp + os.replace）防半写。出厂
    同 id 覆盖即接管语义（读面用户赢），不产出错误。返回 {book_id}；用户空间
    不可得 → {error}（写面不静默降级）。
    """
    directory = _user_lorebooks_dir()
    if directory is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），世界书无处落盘"}
    payload = {k: v for k, v in book.items() if k not in ("id", "origin")}
    os.makedirs(directory, exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{book['id']}.yaml"),
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )
    return {"book_id": book["id"]}



def _save_lorebook_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/lorebooks/save → 用户层落书（{book} 嵌套与面板平铺双形态宽容）。

    id 来源优先级：book.id > body.book_id（面板键）> name 派生（新建流，复用
    卡 id 派生器——面板新建书不持 id 的契约同卡）；显式 id 须过 _CARD_ID_RE
    （路径安全闸）。出厂同 id 覆盖即接管语义（读面用户赢），不产出错误。
    """
    book = body["book"] if isinstance(body.get("book"), dict) else body
    book_id = str(book.get("id") or body.get("book_id") or "").strip()
    if not book_id:
        name = str(book.get("name") or "").strip()
        if not name:
            return {"error": "缺少 id/name（新建世界书须带 name 以派生合法 id）"}
        book_id = _derive_card_id(name, {b["id"] for b in _load_lorebooks()})
    if not _CARD_ID_RE.fullmatch(book_id):
        return {"error": f"非法 id（须为 ^[a-z0-9_]{{1,64}}$）: {book_id}"}
    return _write_user_lorebook(_book_from_data(book, book_id, "user"))



def _delete_lorebook_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/lorebooks/delete：仅用户层可删。

    用户文件先行（接管副本删除即解除接管，出厂书仍在）；出厂书拒绝（面板
    指引复制后修改）；都不存在如实报错。book_id 先过正则再拼路径（穿越防线）。
    """
    book_id = str(body.get("book_id") or "").strip()
    if not book_id:
        return {"error": "缺少 book_id"}
    if not _CARD_ID_RE.fullmatch(book_id):
        return {"error": f"非法 book_id: {book_id}"}
    directory = _user_lorebooks_dir()
    user_path = os.path.join(directory, f"{book_id}.yaml") if directory else None
    if user_path is not None and os.path.isfile(user_path):
        os.remove(user_path)
        return {"book_id": book_id, "deleted": True}
    if any(b["id"] == book_id for b in _load_lorebooks()):
        return {"error": "出厂世界书不可删除，可复制后修改"}
    return {"error": f"世界书不存在: {book_id}"}



def _st_world_info_entries(data: Any) -> list[dict[str, Any]]:
    """ST World Info 导出 → 条目 list（数字键对象/数组双形态兼容，同源序保持）。

    ST 导出为数字键对象（{"entries": {"0": {...}}}），按数字键序转 list；
    数组形态原样；顶层裸数组 = 无信封直接是条目集；其余形态 = 空表（空书合法）。
    """
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    if isinstance(entries, dict):
        items = [(key, value) for key, value in entries.items() if isinstance(value, dict)]
        try:
            items.sort(key=lambda pair: int(pair[0]))
        except ValueError:
            pass  # 非数字键形态：保持 JSON 原序
        return [value for _, value in items]
    return []



def _import_lorebook_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/lorebooks/import：ST World Info JSON → 用户层落书。

    file_b64（规格键）/data_base64（面板键）双形态宽容；条目形态兼容见
    _st_world_info_entries，字段映射见 _entry_from_st；id 由 filename 派生
    （卡导入同款规范化），name/description 取导出顶层（缺省回落 id/空串）。
    响应 {book_id, warnings}（warnings = 条目拼文可疑指令模式清单，只标注不
    阻断）；解码/解析失败如实 error 不落盘。
    """
    raw_b64 = str(body.get("file_b64") or body.get("data_base64") or "")
    filename = str(body.get("filename") or "")
    if not raw_b64 or not filename:
        return {"error": "缺少 file_b64/filename"}
    try:
        raw = base64.b64decode(raw_b64)
    except ValueError as exc:  # binascii.Error 是 ValueError 子类
        return {"error": f"file_b64 base64 解码失败: {exc}"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"error": f"世界书 JSON 解析失败: {exc}"}
    book_id = _card_id_from_filename(filename)
    if not book_id:
        return {"error": f"无法从 filename 派生合法书 id: {filename}"}
    source = data if isinstance(data, dict) else {}
    entries = _st_world_info_entries(data)
    stored = _book_from_data(
        {"name": source.get("name"), "description": source.get("description"), "entries": entries},
        book_id,
        "user",
    )
    saved = _write_user_lorebook(stored)
    if "error" in saved:
        return saved
    from card_lint import scan_suspicious  # noqa: PLC0415 （模式表单源，B-Ⅱ 收编）

    haystack = "\n".join(str(entry.get("content") or "") for entry in entries)
    return {"book_id": book_id, "warnings": scan_suspicious(haystack, "世界书内容")}



def _personas_raw() -> list[dict[str, Any]]:
    """persona 档案原样读（存储形态 {personas: [{name, description, avatar}]}）。

    缺文件 → 空（首次使用）；损坏 → 空并 warn（卡读取同款降级：不崩面板，
    下一次 save 以重写自愈）。
    """
    path = _personas_path()
    if path is None or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("persona 档案损坏，降级空列表（下次保存自愈）: %s (%s)", path, exc)
        return []
    items = data.get("personas") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]



def _personas_load() -> list[dict[str, Any]]:
    """persona 档案读面：面板以 id 选择/删除，name 即唯一键 → id 与 name 同值。"""
    return [
        {
            "id": str(item.get("name") or ""),
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "avatar": item.get("avatar"),
        }
        for item in _personas_raw()
        if str(item.get("name") or "").strip()
    ]



def _write_personas(path: str, personas: list[dict[str, Any]]) -> None:
    """persona 档案原子写（atomic_io：同目录 tmp + os.replace，防半写/截断）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from _root_api import atomic_write_text

    atomic_write_text(path, json.dumps({"personas": personas}, ensure_ascii=False, indent=2))



def _persona_save_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/personas/save：name 唯一键 upsert（已存在即更新，保持原位）。"""
    name = str(body.get("name") or "").strip()
    if not name:
        return {"error": "缺少 name"}
    path = _personas_path()
    if path is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），persona 无处落盘"}
    entry: dict[str, Any] = {"name": name, "description": str(body.get("description") or "")}
    if body.get("avatar") is not None:
        entry["avatar"] = body.get("avatar")
    personas = _personas_raw()
    for index, item in enumerate(personas):
        if str(item.get("name") or "") == name:
            personas[index] = entry
            break
    else:
        personas.append(entry)
    _write_personas(path, personas)
    return {"name": name, "id": name}



def _persona_delete_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/personas/delete：persona_id（面板键）/name（规格键）同值二选一。"""
    name = str(body.get("persona_id") or body.get("name") or "").strip()
    if not name:
        return {"error": "缺少 name"}
    path = _personas_path()
    if path is None:
        return {"error": "用户配置目录不可得（user_config_dir()=None），persona 无法删除"}
    personas = _personas_raw()
    remaining = [item for item in personas if str(item.get("name") or "") != name]
    if len(remaining) == len(personas):
        return {"error": f"persona 不存在: {name}"}
    _write_personas(path, remaining)
    return {"deleted": name}


# ── 创作草稿五态状态机（扩展波 B-Ⅰ，设计文档 §9.2；零新增插件进本端点组）─────────
#
# 个人内容级 AI 创作流：创作 agent（agents/card_author.yaml，工作台「AI 帮我
# 生成」经 mode.session 附身）在会话工作空间 drafts/ 起草完整卡 yaml → 登记
# （本面校验形态落 <user_config>/rp_drafts/，与卡库同区双根风格）→ lint（card_lint
# 单源 + suspicious 指令 + HTML 高危三面）→ smoke（一期人工试玩登记；自动探针
# 接 eval 域 = B-Ⅱ，probe 参数契约预留）→ 提交人审 → approve（受控搬移入用户层
# 卡库，save 通道语义）/ reject（意见回写元数据回创作会话迭代）。
#
# 安全线（制度级）：AI 生成内容一律不可直通用户层——三闸（lint/smoke/人审）全部
# 必经，approve 端点强制执法（状态机来源态 + 证据元数据缺一不可 = 409）；AI 署名
# 强制（卡 creator 字段服务端写定 _AI_CREATOR_MARK，客户端值一律覆盖）。
#
# 落点契约：草稿 = <user_config>/rp_drafts/<draft_id>.yaml（meta+card 双键映射，
# 原子写），审核留痕/报告全随草稿文件（rp_state_audit 不适用此域）；reject/批准
# 均不删文件（草稿数据保留）。draft_id 即入库卡 id（登记时按卡名派生避让，approve
# 以同 id 写用户层——与卡库同 id 接管语义一致）。


_DRAFTS_ROUTE = "/ext/mode_roleplay/data/wizards/drafts"

_DRAFT_ROUTE_PREFIX = "/ext/mode_roleplay/data/wizards/draft/"

# AI 署名强制标记（登记写入 + 入库复写双保险；草稿文件用户可手改，入库端点
# 不信任文件内容再断言一次）。

_AI_CREATOR_MARK = "AI"

# 状态机：register（重登记）合法来源态（内容替换=证据失效重置 draft；审核中/
# 已入库拒绝登记）+ 各动作来源态表。非法迁移 fail-closed 409。

_DRAFT_REREGISTER_FROM = ("draft", "lint_pass", "smoke_pass", "rejected")

_DRAFT_TRANSITIONS = {
    "lint": ("draft", "lint_pass"),
    "smoke": ("lint_pass",),
    "submit": ("smoke_pass",),
    "approve": ("awaiting_review",),
    "reject": ("awaiting_review",),
}

_DRAFT_STATES = ("draft", "lint_pass", "rejected", "smoke_pass", "awaiting_review", "approved")
