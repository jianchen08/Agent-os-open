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
- 写动作：/data/actions/play（possess-only；开演已会话化，变体生成收编进
  扮演会话的宿主消息操作——重新生成/‹i·n› 多代切换，本插件零任务派发面）
  （eval_harness 先例），args 携 mode=roleplay；行级锚定来源管道 thread_id，
  body 显式 session_id 兜底。/data/actions/play 为 possess-only（校验卡 + 回执
  presenter；fresh 开演已会话化——面板走 roleplay.continue 宿主桥建扮演会话，
  扮演是对话不是任务，无任务派发无评估语义，2026-09-25 用户痛点修复）。
  卡 = 模式命名空间 agent 键（mode_roleplay/card_<id>，模式体系设计 §8.1）。
- 卡 theme/avatar：/data/cards 透出卡 yaml 的 theme（ThemeConfig 档，面板选卡时
  经 theme.apply 桥注入对话框）与 avatar（画廊头像）。
- 卡库双根（成熟化 Wave B）：/data/cards = 包内出厂卡（origin="builtin"）∪ 用户层
  <user_config_dir>/agents/mode_roleplay（origin="user"；同 id 接管出厂，出厂件
  永不被写）。写面 /data/cards/{save,delete,import,export}：save/import 落用户层
  （出厂同 id 覆盖即接管语义，不产出错误）；delete 仅用户卡（出厂卡拒绝并指引
  复制修改）；import 收 ST V2/V3 PNG/JSON（card_io，data 字段平铺）并对
  description+system_prompt 拼文做可疑指令模式扫描（只标注 warnings 不阻断）；
  export 出 ST V2 JSON 信封（base64，PNG 导出后置不做）。
- 世界书双根（用户层物料双根化）：/data/lorebooks = 包内出厂书（origin=
  "builtin"）∪ 用户层 <user_config_dir>/lorebooks（origin="user"；同 id 接管，
  出厂件永不被写）。写面 /data/lorebooks/{save,delete,import}：save/import 落
  用户层（同 id 覆盖即接管语义）；delete 仅用户书（出厂书拒绝并指引复制修改）；
  import 收 SillyTavern World Info JSON（entries 数字键对象/数组双形态，字段
  映射见 _entry_from_st），命中可疑指令模式只标注 warnings 不阻断。世界书经卡
  lorebook_ids 绑定生效：扮演组装器（material.py）按同款双根查找注入，用户
  新建/导入书入卡绑定即进扮演上下文。
- persona 档案：<user_config_dir>/persona/roleplay_personas.json（name 唯一键，
  原子写）；GET /data/personas + POST /data/personas/{save,delete}（开演档用户
  设定的档案面，面板选中档经桥上行 description 文本）。
- 开演端点 possess-only（2026-09-25 会话化，扮演是对话不是任务）：校验卡存在
  后回卡档案（presenter 含 theme），附身状态由前端持久化并在发送时注入
  execution_context，服务端无状态；fresh 开演已移除（面板走 roleplay.continue
  宿主桥建扮演会话，开场白/用户设定经会话执行选项快照随消息透传），未知
  play_mode 400 显式拒绝。

种子单元自包含：HttpHandleResponse 封装与读数桥在此内联——共享根裸模块对
用户空间播种副本不可达（bootstrap 的 shared_root 指纹只认仓内 plugins/shared 布局）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
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

# 卡 id 即用户层文件名（<id>.yaml）：正则白名单同时是路径穿越防线（越权字符
# 进不了文件名）。出厂根保留 card_* 前缀命名约定，id 空间两侧同一正则。
_CARD_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# 模式卡扁平字段词表（读/写/导出共用的单一真值）：
# - 字符串字段 = ST V2 字段子集（str 归一，缺失补空串）
# - 列表字段 = 字符串列表（非 list 一律 []）
# - 透传字段 = 原样 .get 宽容（avatar/lorebook_ids/voice_ref/state_schema/cover/
#   portrait/extensions；theme 例外见 _card_from_data——非 dict 归 None）
_CARD_STR_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
    "creator_notes",
)
_CARD_LIST_FIELDS = ("alternate_greetings", "tags", "lorebook_ids")
_CARD_PASSTHROUGH_FIELDS = ("avatar", "voice_ref", "state_schema", "cover", "portrait", "extensions")

# 内容安全简化闸（import 只标注不阻断）：description+system_prompt 拼文的
# 可疑指令模式（大小写不敏感子串；命中加 warnings 建议人工检查）
_SUSPICIOUS_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "jailbreak",
    "developer mode",
    "dan mode",
    "绕过",
    "忽略之前",
)

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


# ── 物料读取（卡/世界书 = 出厂根 ∪ 用户层双根合并）───────────────────────────────

def _user_config_root() -> str | None:
    """用户配置层根（user_space 共享裸模块；裸名导入放函数内维持种子自包含）。

    bootstrap_plugin 已把 plugins/shared 根推上 sys.path（用户空间播种副本由
    SDK 位置锚回退兜底，见模块头注），函数内裸名导入即达；模块顶层不新增
    共享根导入前提。极端环境解析失败 → None，调用方按「无用户空间」定语义
    （读面降级空，写面如实报错不静默假成功）。
    """
    from user_space import user_config_dir

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
    card: dict[str, Any] = {"id": card_id, "origin": origin}
    for field in _CARD_STR_FIELDS:
        card[field] = str(data.get(field) or "")
    for field in _CARD_LIST_FIELDS:
        value = data.get(field)
        card[field] = [str(item) for item in value] if isinstance(value, list) else []
    theme = data.get("theme")
    card["theme"] = theme if isinstance(theme, dict) else None
    for field in _CARD_PASSTHROUGH_FIELDS:
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
    for fname in sorted(os.listdir(directory)):
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
        except OSError:
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
            merged[card_id] = _card_from_data(data, card_id, origin)  # 同 id 后根接管
    return list(merged.values())


def _entry_from_st(entry: dict[str, Any]) -> dict[str, Any]:
    """ST World Info 条目 → 模式书条目（字段映射单一真值，缺省补默认）。

    keys / secondary_keys / content / enable→enabled / insertion_order /
    constant / position / case_sensitive / comment。enabled 存储键优先、
    ST 导出键 enable 次之（自有 save/import 落盘形态与 ST 导出双兼容）。
    """
    return {
        "keys": [str(k) for k in (entry.get("keys") or [])],
        "secondary_keys": [str(k) for k in (entry.get("secondary_keys") or [])],
        "content": str(entry.get("content") or ""),
        "enabled": bool(entry.get("enabled", entry.get("enable", True))),
        "insertion_order": entry.get("insertion_order", 100),
        "constant": bool(entry.get("constant", False)),
        "position": str(entry.get("position") or "before_char"),
        "case_sensitive": bool(entry.get("case_sensitive", False)),
        "comment": str(entry.get("comment") or ""),
    }


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
        except OSError:
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
    payload = {
        field: card[field]
        for field in (*_CARD_STR_FIELDS, *_CARD_LIST_FIELDS, "theme", *_CARD_PASSTHROUGH_FIELDS)
        if card.get(field) is not None
    }
    os.makedirs(directory, exist_ok=True)
    from atomic_io import atomic_write_text

    atomic_write_text(
        os.path.join(directory, f"{card['id']}.yaml"),
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
    )
    return {"card_id": card["id"]}


def _card_id_from_filename(filename: str) -> str:
    """导入文件名 → 卡 id：去扩展名、小写、非法字符转 _、截 64；派生为空返空。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
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
    """
    haystack = (str(card.get("description") or "") + str(card.get("system_prompt") or "")).lower()
    if any(pattern.lower() in haystack for pattern in _SUSPICIOUS_PATTERNS):
        return ["检测到可疑指令模式，建议人工检查角色设定"]
    return []


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

    try:
        envelope = load_card(raw)
    except ValueError as exc:
        return {"error": f"角色卡解析失败: {exc}"}
    issues = validate_card(envelope)
    card_id = _card_id_from_filename(filename)
    if not card_id:
        return {"error": f"无法从 filename 派生合法卡 id: {filename}"}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    stored = _card_from_data(data, card_id, "user")
    saved = _write_user_card(stored)
    if "error" in saved:
        return saved
    response: dict[str, Any] = {"card_id": card_id, "issues": issues}
    warnings = _scan_card_danger(stored)
    if warnings:
        response["warnings"] = warnings
    return response


def _export_card_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /data/cards/export：合并后卡 → ST V2 信封 JSON → base64（PNG 后置不做）。

    data = 卡扁平字段去 id/origin（信封层语义键不落 data），None 字段不落。
    content_base64 为 file_b64 的面板消费别名（同值，webview 走 data URI 下载）。
    """
    card_id = str(body.get("card_id") or "").strip()
    if not card_id:
        return {"error": "缺少 card_id"}
    card = next((c for c in _load_cards() if c["id"] == card_id), None)
    if card is None:
        return {"error": f"未知角色卡: {card_id}"}
    data = {k: v for k, v in card.items() if k not in ("id", "origin") and v is not None}
    envelope = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
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
    from atomic_io import atomic_write_text

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
    haystack = "\n".join(str(entry.get("content") or "") for entry in entries).lower()
    warnings = (
        ["检测到可疑指令模式，建议人工检查世界书内容"]
        if any(pattern.lower() in haystack for pattern in _SUSPICIOUS_PATTERNS)
        else []
    )
    return {"book_id": book_id, "warnings": warnings}


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
    from atomic_io import atomic_write_text

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


# ── 写动作（task_submit 真派发；eval_harness 先例：tool-executor 显式 plugin_id）──


# ── HTTP 端点（http.handle）——面板页 + 数据面 + 写动作 ──────────────────────────────

_PANEL_ENDPOINT = "/ext/mode_roleplay/page/roleplay-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "roleplay_panel.html"
)

# GET 数据端点（无副作用）+ POST 写动作端点（regenerate 会真实派发任务进
# 管道；play 为 possess-only 回执面；cards/personas 写面为用户空间文件操作，
# 不派任务）
_DATA_ROUTES = {
    "/ext/mode_roleplay/data/bootstrap",
    "/ext/mode_roleplay/data/sessions",
    "/ext/mode_roleplay/data/cards",
    "/ext/mode_roleplay/data/lorebooks",
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


async def _handle_actions(path: str, method: str, raw_body: str) -> dict[str, Any] | None:
    """写动作分发；未命中返回 None（交回 404 路径）。"""
    if path not in _ACTION_ROUTES:
        return None
    if method != "POST":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    body = _parse_body(raw_body)
    if path.endswith("/play"):
        return await _handle_play(body)
    if path.endswith("/regenerate"):
        # 已收编：变体生成在扮演会话内走宿主消息操作（重新生成/‹i·n› 多代
        # 切换），本动作面退役——保留 410 语义化响应而非 404，旧面板缓存可辨。
        return {"success": True,
                "data": _json_response({"error": "regenerate 已收编进扮演会话的宿主消息操作"}, 410)}
    if path.endswith("/cards/save"):
        return _routed_json(_save_card_request(body))
    if path.endswith("/cards/delete"):
        return _routed_json(_delete_card_request(body))
    if path.endswith("/cards/import"):
        return _routed_json(_import_card_request(body))
    if path.endswith("/cards/export"):
        return _routed_json(_export_card_request(body))
    if path.endswith("/lorebooks/save"):
        return _routed_json(_save_lorebook_request(body))
    if path.endswith("/lorebooks/delete"):
        return _routed_json(_delete_lorebook_request(body))
    if path.endswith("/lorebooks/import"):
        return _routed_json(_import_lorebook_request(body))
    if path.endswith("/personas/save"):
        return _routed_json(_persona_save_request(body))
    return _routed_json(_persona_delete_request(body))


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
    if path == "/ext/mode_roleplay/data/personas":
        # GET 端点但在 _DATA_ROUTES 集合外——messages 端点同款独立分发形态
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return {"success": True, "data": _json_response({"personas": _personas_load()})}
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
