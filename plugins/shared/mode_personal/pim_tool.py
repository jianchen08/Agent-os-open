"""pim 工具 handler——LLM 单工具三 action（capture/query/op），强 schema 面。

I1 聊天捕获走工具强 schema（不经 parse_line）；查询侧读聚合与 GET /today
同一 store 单源；执行侧 op 全部走 op_log（可撤销）。错误是值：业务错误
（缺 id/非法枚举/低置信）以 ``{status: "error", error}`` 返回（形态仍在
output_schema 内，tool_core fail-closed 校验通过），不抛异常出工具面。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pim_parse import parse_line
from pim_store import PimStore

logger = logging.getLogger(__name__)

QUERY_SCOPES = ("today", "todo", "event", "habit", "partner", "journal", "preference")
OP_NAMES = (
    "accept",
    "complete",
    "defer",
    "split",
    "wait",
    "fail",
    "cancel",
    "focus_start",
    "focus_stop",
    "undo",
)
_ENTITIES = ("todo", "event")


def _err(action: str, message: str) -> dict[str, Any]:
    return {"status": "error", "action": action, "error": message}


def _ok(action: str, result: dict[str, Any], op_id: str | None = None) -> dict[str, Any]:
    receipt: dict[str, Any] = {"status": "ok", "action": action, "result": result}
    if op_id is not None:
        receipt["op_id"] = op_id
    return receipt


def handle_pim(action: str, payload: dict[str, Any], store: PimStore) -> dict[str, Any]:
    """pim 工具三 action 分发（capture 建卡 / query 读聚合 / op 状态执行）。

    Args:
        action: ``capture``/``query``/``op`` 三值之一。
        payload: 各 action 的载荷 dict（见 plugin.json input_schema 描述）。
        store: 实体域存储单例。

    Returns:
        符合 output_schema 的回执 dict（status/action 必在，op_id 随记账操作）。
    """
    if not isinstance(payload, dict):
        payload = {}
    if action == "capture":
        return _capture(payload, store)
    if action == "query":
        return _query(payload, store)
    if action == "op":
        return _op(payload, store)
    return _err(str(action), f"action 须为 capture/query/op 之一: {action!r}")


def _capture(payload: dict[str, Any], store: PimStore) -> dict[str, Any]:
    """零确认直建（回执含 op_id → 可撤销）；kind=event 时建日程事件。"""
    if payload.get("text") and not payload.get("title"):
        # 行语法载荷（与页面快速添加行同一 parse_line——两入口零分叉）；
        # 相对词形按 store 时钟展开（确定性，与 REST 同源）
        parsed = parse_line(str(payload["text"]), now=store.now())
        if parsed is None:
            return _err("capture", "低置信：无法从文本提取标题，请改用 title 字段直传")
        fields: dict[str, Any] = {k: v for k, v in parsed.items() if v is not None}
        fields.pop("done", None)  # 行 checkbox 非捕获语义，新建一律未完成
        if payload.get("reminder_mode"):
            # 行语法无此解析字段：显式参数透传落卡（建卡时判定，与结构化路径同轨）
            fields["reminder_mode"] = payload["reminder_mode"]
    else:
        fields = {
            "title": payload.get("title"),
            "domain": payload.get("domain"),
            "due": payload.get("due"),
            "repeat": payload.get("repeat"),
            "priority": payload.get("priority"),
            "direction": payload.get("direction", "none"),
            "reminder_mode": payload.get("reminder_mode"),
        }
        fields = {k: v for k, v in fields.items() if v is not None}
    if payload.get("status"):
        fields["status"] = payload["status"]
    # 来源会话锚（ADR 2026-11-03-pim-trigger-anchor）：capture 时所在会话的
    # 主管道 id——到点触发器挂载/钉住的坐标（不随活跃会话漂移）。
    if payload.get("pipeline_id"):
        fields["source_pipeline_id"] = str(payload["pipeline_id"])
    try:
        if payload.get("kind") == "event":
            at = payload.get("at") or fields.get("due")
            if not isinstance(at, str) or not at.strip():
                return _err("capture", "event 需要 at（时间）字段")
            record, op_id = store.create("event", {**fields, "at": at})
            return _ok("capture", {"event": record}, op_id)
        if not str(fields.get("title", "")).strip():
            return _err("capture", "title 不得为空")
        record, op_id = store.create("todo", fields)
        return _ok("capture", {"todo": record}, op_id)
    except ValueError as exc:
        return _err("capture", str(exc))


def _query(payload: dict[str, Any], store: PimStore) -> dict[str, Any]:
    scope = str(payload.get("scope", "") or "")
    if scope not in QUERY_SCOPES:
        return _err("query", f"scope 须为 {QUERY_SCOPES} 之一: {scope!r}")
    if scope == "today":
        return _ok("query", {"today": store.today()})
    if scope == "partner":
        return _ok("query", {"partner": store.get_partner()})
    if scope == "journal":
        return _ok("query", {"journal": store.list_journal(
            payload.get("month") if isinstance(payload.get("month"), str) else None
        )})
    if scope == "preference":
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            domain = payload.get("domain")
            if isinstance(domain, str) and domain:
                key = f"{domain}:同意"
        if isinstance(key, str) and key:
            record = store.get("preference", key)
            return _ok("query", {"preference": record})
        return _ok("query", {"preference": store.list_records("preference")})
    entity = "todo" if scope == "todo" else "event" if scope == "event" else "habit"
    target_id = payload.get("id")
    if isinstance(target_id, str) and target_id:
        record = store.get(entity, target_id)
        if record is None:
            return _err("query", f"{entity} 不存在: {target_id}")
        return _ok("query", {entity: record})
    items = store.list_records(entity)
    status = payload.get("status")
    if isinstance(status, str) and status:
        items = [t for t in items if t.get("status") == status]
    domain = payload.get("domain")
    if isinstance(domain, str) and domain:
        items = [t for t in items if t.get("domain") == domain]
    if entity == "event":
        start = payload.get("from")
        end = payload.get("to")
        if isinstance(start, str) and start:
            items = [e for e in items if str(e.get("at", ""))[:10] >= start[:10]]
        if isinstance(end, str) and end:
            items = [e for e in items if str(e.get("at", ""))[:10] <= end[:10]]
    return _ok("query", {entity: items})


def _op(payload: dict[str, Any], store: PimStore) -> dict[str, Any]:
    op_name = str(payload.get("op", "") or "")
    if op_name not in OP_NAMES:
        return _err("op", f"op 须为 {OP_NAMES} 之一: {op_name!r}")
    try:
        return _dispatch_op(op_name, payload, store)
    except KeyError as exc:
        return _err("op", f"记录不存在: {exc.args[0]}")
    except ValueError as exc:
        return _err("op", str(exc))


def _dispatch_op(op_name: str, payload: dict[str, Any], store: PimStore) -> dict[str, Any]:
    if op_name == "undo":
        op_id = payload.get("op_id")
        if not isinstance(op_id, str) or not op_id:
            return _err("op", "undo 需要 op_id（建卡/操作回执携带）")
        receipt = store.undo(op_id)
        return _ok("op", {"undone": receipt})
    entity = str(payload.get("entity") or "todo")
    if entity not in _ENTITIES:
        return _err("op", f"entity 须为 {_ENTITIES} 之一: {entity!r}")
    item_id = payload.get("id")
    if not isinstance(item_id, str) or not item_id:
        item_id = payload.get("todo_id") if entity == "todo" else None
    if not isinstance(item_id, str) or not item_id:
        return _err("op", f"op={op_name} 需要 {entity}_id（或 id）")
    if op_name == "focus_start":
        if entity != "todo":
            return _err("op", "focus_start 只支持 todo")
        raw_minutes = payload.get("duration_minutes")
        duration = raw_minutes if isinstance(raw_minutes, int) else None
        started, op_id = store.start_focus(item_id, duration)
        return _ok("op", {"todo": started}, op_id)
    if op_name == "focus_stop":
        if entity != "todo":
            return _err("op", "focus_stop 只支持 todo")
        stopped, op_id = store.stop_focus(item_id)
        return _ok("op", {"todo": stopped}, op_id)
    if op_name == "accept":
        accepted, op_id = store.accept(entity, item_id)
        return _ok("op", {entity: accepted}, op_id)
    if op_name == "complete":
        if entity == "event":
            done, op_id = store.complete_event(item_id)
            return _ok("op", {"event": done}, op_id)
        done, op_id = store.complete_todo(item_id)
        return _ok("op", {"todo": done, "partner": store.get_partner()}, op_id)
    if op_name == "defer":
        new_time = payload.get("new_time")
        if not isinstance(new_time, str) or not new_time:
            new_time = payload.get("new_due")
        new_time = new_time if isinstance(new_time, str) and new_time else None
        if entity == "event":
            deferred, op_id = store.defer_event(item_id, new_time)
            return _ok("op", {"event": deferred}, op_id)
        deferred, op_id = store.defer_todo(item_id, new_time)
        return _ok("op", {"todo": deferred}, op_id)
    if op_name == "split":
        if entity != "todo":
            return _err("op", "split 只支持 todo")
        parts = payload.get("parts")
        created, op_id = store.split_todo(item_id, parts if isinstance(parts, list) else [])
        return _ok("op", {"created": created}, op_id)
    if op_name in ("wait", "fail"):
        status = "wait" if op_name == "wait" else "failed"
        updated, op_id = store.update(entity, item_id, {"status": status})
        return _ok("op", {entity: updated}, op_id)
    if op_name == "cancel":
        if entity == "event":
            cancelled, op_id = store.cancel_event(item_id)
            return _ok("op", {"event": cancelled}, op_id)
        return _err("op", "todo 无 cancel 状态；撤销用 undo，没做成用 fail")
    return _err("op", f"未支持的 op: {op_name!r}")


def receipt_json(receipt: dict[str, Any]) -> str:
    """回执 JSON 串（工具返回统一出口）。"""
    return json.dumps(receipt, ensure_ascii=False)
