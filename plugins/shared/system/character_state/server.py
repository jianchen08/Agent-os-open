#!/usr/bin/env python3
"""角色状态账本插件——状态层唯一真值的读/写/版本/回滚/渲染服务面。

四层物料模型中的状态层（角色扮演成熟化设计 §5.1/§5.2）：结构化动态值
（关系阶段/好感度/位置/时间/持有物/情绪能量等），快变、版本化、可回滚；
表现层（状态栏/形象/桌面 agent）只读本层投影，禁止反向写。schema 由卡声明
（条目形态借鉴表格记忆：key/类型/初始值/展示模板）。

服务契约（manifest capabilities.services；wire = MCP tools/call，每项带
input_schema/output_schema）：
- character_state.init     初始化账本（幂等：已存在不覆盖，返回现状）
- character_state.get      读当前 values + schema
- character_state.update   更新 values（key 须在 schema 且类型匹配）+ 追加 history
- character_state.history  最近 N 条变更记录
- character_state.rollback 恢复到指定 seq 后的 values 快照（history 保留 + 追加回滚记录）
- character_state.render   按 schema.template 渲染状态文本块（未 init 返回空串）
- character_state.delete   删除账本

存储：``<user_config_dir>/character_state/<card_id>.json``（user_space 用户空间，
仓外抗工作区还原）；写经 ``atomic_io.atomic_write_text``（同目录 tmp +
os.replace 原子替换，不截断）。本插件不声明 LLM 工具面、不取内核能力。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

plugin = AgentOSPlugin("character_state")

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from atomic_io import atomic_write_text  # noqa: E402 —— 原子写公共实现
from user_space import user_config_dir  # noqa: E402 —— 用户空间统一落点

# 状态值类型词表（schema 条目 type 域；契约固定三种，未列类型 fail-closed 拒绝）
_STATE_TYPES = ("number", "string", "bool")
_TYPE_DEFAULTS: dict[str, Any] = {"number": 0, "string": "", "bool": False}

# card_id 即存储文件名：只允许安全文件名字符（路径穿越防护，对齐 agent_manager 同族防线）
_CARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_HISTORY_LIMIT_DEFAULT = 20


def _storage_dir(base_dir: str | Path | None = None) -> Path:
    """状态存储根：显式 base_dir 优先（测试注入缝），否则 <user_config_dir>/character_state。

    user_config_dir 不可得（极端环境 user_root 解析失败）时 raise——落盘目标
    不明即拒绝服务，不做静默回退。
    """
    if base_dir is not None:
        return Path(base_dir)
    config_dir = user_config_dir()
    if config_dir is None:
        raise RuntimeError("用户配置目录不可得（user_config_dir()=None），角色状态无处落盘")
    return config_dir / "character_state"


def _card_path(card_id: str, base_dir: str | Path | None = None) -> Path:
    """card_id → 账本文件路径；非法 id（空/越权字符）fail-closed 拒绝。"""
    if not card_id or not _CARD_ID_RE.fullmatch(card_id):
        raise ValueError(f"非法 card_id（须为安全文件名字符）: {card_id!r}")
    return _storage_dir(base_dir) / f"{card_id}.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_state(card_id: str, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    """读账本；未初始化返回 None。损坏文件如实上抛（JSONDecodeError），不吞。"""
    path = _card_path(card_id, base_dir)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save_state(state: dict[str, Any], base_dir: str | Path | None = None) -> None:
    """账本落盘：目录自动创建 + 原子替换（同目录 tmp + os.replace）。"""
    path = _card_path(state["card_id"], base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def _normalize_schema(schema: Any) -> dict[str, dict[str, Any]]:
    """校验并归一 schema：每条目补全 initial（类型缺省值）与 template（缺省不展示）。

    非法形态（非 dict/空表/未知 type）fail-closed 报错——带定位信息上抛。
    """
    if not isinstance(schema, dict) or not schema:
        raise ValueError(f"schema 须为非空 dict（key → 条目声明），收到: {type(schema).__name__}")
    normalized: dict[str, dict[str, Any]] = {}
    for key, decl in schema.items():
        if not isinstance(decl, dict):
            raise ValueError(f"schema 条目 {key!r} 须为 dict，收到: {type(decl).__name__}")
        type_name = decl.get("type")
        if type_name not in _STATE_TYPES:
            raise ValueError(f"schema 条目 {key!r} 的 type 须为 {'/'.join(_STATE_TYPES)} 之一，收到: {type_name!r}")
        normalized[str(key)] = {
            "type": type_name,
            "initial": decl.get("initial", _TYPE_DEFAULTS[str(type_name)]),
            "template": str(decl.get("template", "")),
        }
    return normalized


def _check_value(card_id: str, key: str, decl: dict[str, Any], value: Any) -> None:
    """变更值类型匹配校验（bool 是 int 子类，须先排除再判 number）。"""
    type_name = decl["type"]
    if type_name == "bool":
        ok = isinstance(value, bool)
    elif type_name == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ValueError(
            f"状态键 {key!r} 类型不符（card={card_id}，声明 {type_name}，收到 {type(value).__name__}: {value!r}）"
        )


def _next_seq(state: dict[str, Any]) -> int:
    """下一条历史序号：seq 自 1 起连续递增（rollback 记录同序号轴）。"""
    history = state["history"]
    return history[-1]["seq"] + 1 if history else 1


def _require_state(card_id: str, base_dir: str | Path | None = None) -> dict[str, Any]:
    """取账本，未初始化即带定位报错（除 init/render 外的读写前提）。"""
    state = _load_state(card_id, base_dir)
    if state is None:
        raise ValueError(f"角色状态未初始化（先调 character_state.init）: {card_id}")
    return state


def _snapshot_at(state: dict[str, Any], seq: int) -> dict[str, Any]:
    """重放 history（seq ≤ 目标序号的记录依序套用 to 值）重建该时点快照。

    起点是 schema initial（init 时 values 即由它铺底），故重放与实际写入路径一致。
    """
    values = {key: decl["initial"] for key, decl in state["schema"].items()}
    for entry in state["history"]:
        if entry["seq"] > seq:
            break
        for key, change in entry["changes"].items():
            values[key] = change["to"]
    return values


@plugin.tool(
    name="character_state.init",
    schema={
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "schema": {"type": "object", "additionalProperties": {"type": "object"}},
        },
        "required": ["card_id", "schema"],
    },
    description="初始化角色状态账本（幂等：已存在则不覆盖，返回现状）",
    output_schema={
        "type": "object",
        "required": ["card_id", "created", "schema", "values"],
        "properties": {
            "card_id": {"type": "string"},
            "created": {"type": "boolean"},
            "schema": {"type": "object"},
            "values": {"type": "object"},
        },
    },
)
async def state_init(card_id: str, schema: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_schema(schema)
    existing = _load_state(card_id)
    if existing is not None:
        # 幂等：已有账本原样返回，schema 不覆盖（状态真值不被二次 init 重置）
        return {
            "card_id": card_id,
            "created": False,
            "schema": existing["schema"],
            "values": existing["values"],
        }
    state = {
        "card_id": card_id,
        "schema": normalized,
        "values": {key: decl["initial"] for key, decl in normalized.items()},
        "history": [],
    }
    _save_state(state)
    return {"card_id": card_id, "created": True, "schema": normalized, "values": state["values"]}


@plugin.tool(
    name="character_state.get",
    schema={
        "type": "object",
        "properties": {"card_id": {"type": "string"}},
        "required": ["card_id"],
    },
    description="读取角色当前状态值与 schema",
    output_schema={
        "type": "object",
        "required": ["card_id", "schema", "values"],
        "properties": {
            "card_id": {"type": "string"},
            "schema": {"type": "object"},
            "values": {"type": "object"},
        },
    },
)
async def state_get(card_id: str) -> dict[str, Any]:
    state = _require_state(card_id)
    return {"card_id": card_id, "schema": state["schema"], "values": state["values"]}


@plugin.tool(
    name="character_state.update",
    schema={
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "changes": {"type": "object", "additionalProperties": {}},
        },
        "required": ["card_id", "changes"],
    },
    description="更新状态值（key 须在 schema 且类型匹配），追加 history（含 from/to）",
    output_schema={
        "type": "object",
        "required": ["card_id", "values"],
        "properties": {
            "card_id": {"type": "string"},
            "values": {"type": "object"},
        },
    },
)
async def state_update(card_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    if not changes:
        raise ValueError(f"changes 不能为空（card={card_id}）")
    state = _require_state(card_id)
    schema = state["schema"]
    # 先全量校验后套用：非法变更整体拒绝，values 不落半套（写面原子语义）
    for key, value in changes.items():
        if key not in schema:
            raise ValueError(f"未知状态键 {key!r}（card={card_id}，schema 未声明）")
        _check_value(card_id, key, schema[key], value)
    recorded: dict[str, dict[str, Any]] = {}
    for key, value in changes.items():
        recorded[key] = {"from": state["values"][key], "to": value}
        state["values"][key] = value
    state["history"].append({"seq": _next_seq(state), "ts": _now_iso(), "changes": recorded})
    _save_state(state)
    return {"card_id": card_id, "values": state["values"]}


@plugin.tool(
    name="character_state.history",
    schema={
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "default": _HISTORY_LIMIT_DEFAULT},
        },
        "required": ["card_id"],
    },
    description="查询最近 N 条变更历史（seq 升序尾部）",
    output_schema={"type": "array", "items": {"type": "object"}},
)
async def state_history(card_id: str, limit: int = _HISTORY_LIMIT_DEFAULT) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError(f"limit 须 ≥ 1，收到: {limit}")
    state = _require_state(card_id)
    return state["history"][-limit:]


@plugin.tool(
    name="character_state.rollback",
    schema={
        "type": "object",
        "properties": {
            "card_id": {"type": "string"},
            "seq": {"type": "integer", "minimum": 1},
        },
        "required": ["card_id", "seq"],
    },
    description="回滚到指定 seq 后的 values 快照；history 保留并追加 rollback 记录",
    output_schema={
        "type": "object",
        "required": ["card_id", "values"],
        "properties": {
            "card_id": {"type": "string"},
            "values": {"type": "object"},
        },
    },
)
async def state_rollback(card_id: str, seq: int) -> dict[str, Any]:
    state = _require_state(card_id)
    if not any(entry["seq"] == seq for entry in state["history"]):
        raise ValueError(f"回滚目标 seq 不存在（card={card_id}，seq={seq}）")
    current = state["values"]
    restored = _snapshot_at(state, seq)
    # 回滚也是一条变更：diff 入 history（误刷可审计），序号轴不受影响
    diff = {key: {"from": current[key], "to": restored[key]} for key in restored if current.get(key) != restored[key]}
    state["history"].append(
        {
            "seq": _next_seq(state),
            "ts": _now_iso(),
            "action": "rollback",
            "target_seq": seq,
            "changes": diff,
        }
    )
    state["values"] = restored
    _save_state(state)
    return {"card_id": card_id, "values": restored}


@plugin.tool(
    name="character_state.render",
    schema={
        "type": "object",
        "properties": {"card_id": {"type": "string"}},
        "required": ["card_id"],
    },
    description="按 schema.template 渲染状态文本块（表现层投影）；未初始化返回空串",
    output_schema={"type": "string"},
)
async def state_render(card_id: str) -> str:
    state = _load_state(card_id)
    if state is None:
        return ""
    lines: list[str] = []
    for key, decl in state["schema"].items():
        if not decl.get("template"):
            continue
        try:
            lines.append(decl["template"].format(value=state["values"].get(key)))
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"状态模板渲染失败（card={card_id}，key={key!r}）: {exc}") from exc
    return "\n".join(lines)


@plugin.tool(
    name="character_state.delete",
    schema={
        "type": "object",
        "properties": {"card_id": {"type": "string"}},
        "required": ["card_id"],
    },
    description="删除角色状态账本文件",
    output_schema={
        "type": "object",
        "required": ["card_id", "deleted"],
        "properties": {
            "card_id": {"type": "string"},
            "deleted": {"type": "boolean"},
        },
    },
)
async def state_delete(card_id: str) -> dict[str, Any]:
    path = _card_path(card_id)
    if not path.exists():
        raise ValueError(f"角色状态不存在（无账本可删）: {card_id}")
    path.unlink()
    return {"card_id": card_id, "deleted": True}


if __name__ == "__main__":
    plugin.run()
