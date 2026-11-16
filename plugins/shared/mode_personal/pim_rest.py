"""pim REST 路由——/ext/mode_personal/** 的 http.handle 分发（纯函数路由核）。

路由与 op 枚举完全同轨（页面动作不设第二套行为语义）；focus/start|stop 为
番茄钟启停（计时到由 PimFocusTimer 定向任务自动落账）。status/code 语义：
200 读成功、201 建成功、400 载荷非法、404 记录不存在、409 幂等冲突、
422 低置信（parse_line 返回 None → 升级 LLM 链属调用方）。
"""

from __future__ import annotations

import re
from typing import Any

from pim_parse import parse_line
from pim_store import PimStore

_PREFIX = "/ext/mode_personal"

_TODO_OP_PATH_RE = re.compile(rf"^{_PREFIX}/todos/(?P<id>[^/]+)/(?P<op>complete|defer|split)$")
_FOCUS_PATH_RE = re.compile(rf"^{_PREFIX}/focus/(?P<id>[^/]+)/(?P<action>start|stop)$")
_HABIT_CHECKIN_PATH_RE = re.compile(rf"^{_PREFIX}/habits/(?P<id>[^/]+)/checkin$")
_COLLECTIONS = ("todos", "events", "habits")
_ENTITY_BY_COLLECTION = {"todos": "todo", "events": "event", "habits": "habit"}


def route(
    store: PimStore,
    path: str,
    method: str,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """按 path/method 分发；返回 (HTTP status, payload)（server 层包 base64 信封）。"""
    q = query or {}
    b = body if isinstance(body, dict) else {}
    try:
        return _route_inner(store, path, method, q, b)
    except KeyError as exc:
        return 404, {"error": f"记录不存在: {exc.args[0]}"}
    except ValueError as exc:
        return 400, {"error": str(exc)}


def _route_inner(
    store: PimStore,
    path: str,
    method: str,
    q: dict[str, str],
    b: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if path == f"{_PREFIX}/today" and method == "GET":
        return 200, store.today()

    if path == f"{_PREFIX}/capture" and method == "POST":
        text = b.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"error": "text 不得为空"}
        parsed = parse_line(text, now=store.now())  # store 时钟注入（确定性）
        if parsed is None:
            return 422, {"error": "低置信：无法从文本提取标题（M2 升级 LLM capture 链）"}
        fields = {k: v for k, v in parsed.items() if v is not None}
        fields.pop("done", None)
        record, op_id = store.create("todo", fields)
        return 201, {"todo": record, "op_id": op_id}
    if path == f"{_PREFIX}/partner" and method == "GET":
        return 200, {"partner": store.get_partner()}

    m = re.match(rf"^{_PREFIX}/undo/(?P<op_id>[^/]+)$", path)
    if m and method == "POST":
        receipt = store.undo(m.group("op_id"))
        return 200, receipt

    if path in (f"{_PREFIX}/{c}" for c in _COLLECTIONS):
        entity = _ENTITY_BY_COLLECTION[path.rsplit("/", 1)[-1]]
        if method == "GET":
            items = store.list_records(entity)
            status = q.get("status")
            if status:
                items = [r for r in items if r.get("status") == status]
            domain = q.get("domain")
            if domain:
                items = [r for r in items if r.get("domain") == domain]
            return 200, {"items": items, "total": len(items)}
        if method == "POST":
            record, op_id = store.create(entity, b)
            return 201, {"record": record, "op_id": op_id}

    m = re.match(rf"^{_PREFIX}/(?P<coll>todos|events|habits)/(?P<id>[^/]+)$", path)
    if m and method == "PATCH":
        entity = _ENTITY_BY_COLLECTION[m.group("coll")]
        patch = {k: v for k, v in b.items() if k not in ("id", "created", "key")}
        if not patch:
            return 400, {"error": "PATCH 需要至少一个可更新字段"}
        record, op_id = store.update(entity, m.group("id"), patch)
        return 200, {"record": record, "op_id": op_id}

    m = _HABIT_CHECKIN_PATH_RE.match(path)
    if m and method == "POST":
        # 幂等：同日重复打卡 op_id 为 None（零变化不记账，回执照常 200）
        habit, checkin_op = store.checkin_habit(m.group("id"))
        return 200, {"habit": habit, "op_id": checkin_op}

    m = _TODO_OP_PATH_RE.match(path)
    if m and method == "POST":
        return _todo_op(store, m.group("id"), m.group("op"), b)

    m = _FOCUS_PATH_RE.match(path)
    if m and method == "POST":
        todo_id = m.group("id")
        if m.group("action") == "start":
            raw_minutes = b.get("duration_minutes")
            duration = raw_minutes if isinstance(raw_minutes, int) else None
            started, op_id = store.start_focus(todo_id, duration)
            return 200, {"todo": started, "op_id": op_id}
        stopped, op_id = store.stop_focus(todo_id)
        return 200, {"todo": stopped, "op_id": op_id}

    return 404, {"error": "not found", "path": path}


def _todo_op(store: PimStore, todo_id: str, op: str, b: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """todo 状态机 op（页面行内轻菜单与工具 op 同一枚举同轨）。"""
    if op == "complete":
        done, op_id = store.complete_todo(todo_id)
        return 200, {"todo": done, "partner": store.get_partner(), "op_id": op_id}
    if op == "defer":
        new_due = b.get("new_due")
        deferred, op_id = store.defer_todo(
            todo_id, new_due if isinstance(new_due, str) and new_due else None
        )
        return 200, {"todo": deferred, "op_id": op_id}
    # split
    parts = b.get("parts")
    created, op_id = store.split_todo(todo_id, parts if isinstance(parts, list) else [])
    return 200, {"created": created, "op_id": op_id}
