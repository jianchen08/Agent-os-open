# @feature: FP-0.2.二 mode_personal 个人助理模式包 M0 | @ci: python-coverage
"""pim 工具三 action 单测（capture/query/op）：错误是值、回执形态契约、可撤销。

I1 聊天捕获走强 schema 结构化字段；行语法 text 载荷与页面快速添加同一
parse_line（parity）。断言回执可观察行为（status/op_id/result），不断言内部。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parents[1]


def _load(name: str, mod_name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pim_store = _load("pim_store", "mode_personal_test_tool_pim_store")
pim_tool = _load("pim_tool", "mode_personal_test_pim_tool")


@pytest.fixture
def store(tmp_path: Path) -> Any:
    return pim_store.PimStore(tmp_path / "data", clock=lambda: datetime(2026, 10, 12, 9, 0, 0))


class TestReceiptShape:
    def test_capture_receipt_carries_op_id(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "A", "domain": "工作"}, store)
        assert rec["status"] == "ok"
        assert rec["action"] == "capture"
        assert isinstance(rec["op_id"], str) and rec["op_id"]
        assert rec["result"]["todo"]["title"] == "A"
        assert rec["result"]["todo"]["status"] == "backlog"

    def test_error_receipts_stay_in_schema_shape(self, store: Any) -> None:
        """错误是值：status=error + error 原因，不抛异常出工具面。"""
        for action, payload in [
            ("bogus", {}),
            ("capture", {"title": "  "}),
            ("query", {"scope": "nope"}),
            ("op", {"op": "nope"}),
            ("op", {"op": "complete", "todo_id": "missing"}),
            ("op", {"op": "undo"}),
            ("op", {"op": "undo", "op_id": "missing"}),
        ]:
            rec = pim_tool.handle_pim(action, payload, store)
            assert rec["status"] == "error", (action, payload)
            assert rec["error"]
            assert rec["action"] == action


class TestCapture:
    def test_capture_zero_confirm_direct_create(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "买菜", "domain": "生活"}, store)
        assert store.get("todo", rec["result"]["todo"]["id"]) is not None

    def test_capture_text_line_syntax(self, store: Any) -> None:
        """text 载荷走同一 parse_line（与页面快速添加行 parity）。"""
        rec = pim_tool.handle_pim(
            "capture", {"text": "- [ ] @明天 #工作 交日报"}, store
        )
        todo = rec["result"]["todo"]
        assert todo["title"] == "交日报"
        assert todo["due"] == "2026-10-13"
        assert todo["domain"] == "工作"

    def test_capture_text_low_confidence_is_error(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"text": "#工作 @每天"}, store)
        assert rec["status"] == "error"
        assert "低置信" in rec["error"]

    def test_capture_event_kind(self, store: Any) -> None:
        rec = pim_tool.handle_pim(
            "capture", {"title": "例会", "kind": "event", "at": "2026-10-13 10:00"}, store
        )
        assert rec["result"]["event"]["at"] == "2026-10-13 10:00"

    def test_capture_event_requires_at(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "例会", "kind": "event"}, store)
        assert rec["status"] == "error"
        assert "at" in rec["error"]


class TestQuery:
    def test_query_today_aggregation(self, store: Any) -> None:
        pim_tool.handle_pim("capture", {"title": "今日事", "due": "2026-10-12"}, store)
        rec = pim_tool.handle_pim("query", {"scope": "today"}, store)
        assert rec["result"]["today"]["date"] == "2026-10-12"
        assert rec["result"]["today"]["todos"]["today"]

    @pytest.mark.parametrize(("scope", "key"), [
        ("todo", "todo"), ("habit", "habit"), ("partner", "partner"), ("journal", "journal"),
    ])
    def test_query_scopes(self, store: Any, scope: str, key: str) -> None:
        rec = pim_tool.handle_pim("query", {"scope": scope}, store)
        assert rec["status"] == "ok"
        assert key in rec["result"]

    def test_query_filters(self, store: Any) -> None:
        pim_tool.handle_pim("capture", {"title": "工作事", "domain": "工作"}, store)
        pim_tool.handle_pim("capture", {"title": "生活事", "domain": "生活"}, store)
        rec = pim_tool.handle_pim("query", {"scope": "todo", "domain": "工作"}, store)
        assert [t["title"] for t in rec["result"]["todo"]] == ["工作事"]

    def test_query_single_by_id_and_missing(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "A"}, store)
        todo_id = rec["result"]["todo"]["id"]
        single = pim_tool.handle_pim("query", {"scope": "todo", "id": todo_id}, store)
        assert single["result"]["todo"]["title"] == "A"
        missing = pim_tool.handle_pim("query", {"scope": "todo", "id": "zzz"}, store)
        assert missing["status"] == "error"


class TestOp:
    def test_complete_defer_split_wait_fail(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "A"}, store)
        todo_id = rec["result"]["todo"]["id"]
        done = pim_tool.handle_pim("op", {"op": "complete", "todo_id": todo_id}, store)
        assert done["result"]["todo"]["status"] == "done"
        assert done["result"]["partner"]["xp"] == 10
        split = pim_tool.handle_pim(
            "op", {"op": "split", "todo_id": todo_id, "parts": ["a", "b"]}, store
        )
        assert len(split["result"]["created"]) == 2
        wait = pim_tool.handle_pim("op", {"op": "wait", "todo_id": todo_id}, store)
        assert wait["result"]["todo"]["status"] == "wait"
        fail = pim_tool.handle_pim("op", {"op": "fail", "todo_id": todo_id}, store)
        assert fail["result"]["todo"]["status"] == "failed"
        defer = pim_tool.handle_pim("op", {"op": "defer", "todo_id": todo_id}, store)
        assert defer["result"]["todo"]["status"] == "backlog"
        assert defer["result"]["todo"]["due"] == "2026-10-13"  # clock 明天

    def test_defer_explicit_new_due(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "A"}, store)
        todo_id = rec["result"]["todo"]["id"]
        defer = pim_tool.handle_pim(
            "op", {"op": "defer", "todo_id": todo_id, "new_due": "2026-11-01"}, store
        )
        assert defer["result"]["todo"]["due"] == "2026-11-01"

    def test_split_requires_parts(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "A"}, store)
        todo_id = rec["result"]["todo"]["id"]
        bad = pim_tool.handle_pim("op", {"op": "split", "todo_id": todo_id, "parts": []}, store)
        assert bad["status"] == "error"

    def test_undo_via_tool(self, store: Any) -> None:
        rec = pim_tool.handle_pim("capture", {"title": "A"}, store)
        todo_id = rec["result"]["todo"]["id"]
        undone = pim_tool.handle_pim("op", {"op": "undo", "op_id": rec["op_id"]}, store)
        assert undone["status"] == "ok"
        assert store.get("todo", todo_id) is None

    def test_op_requires_todo_id(self, store: Any) -> None:
        rec = pim_tool.handle_pim("op", {"op": "complete"}, store)
        assert rec["status"] == "error"
        assert "todo_id" in rec["error"]


class TestEventAndPreferenceFaces:
    """event 读写面 + preference 查询（2026-10-20 改造）。"""

    def test_query_event_by_id_and_date_range(self, store: Any) -> None:
        pim_tool.handle_pim(
            "capture", {"kind": "event", "title": "例会", "at": "2026-10-13 10:00"}, store
        )
        pim_tool.handle_pim(
            "capture", {"kind": "event", "title": "远会", "at": "2026-11-01 10:00"}, store
        )
        listed = pim_tool.handle_pim("query", {"scope": "event"}, store)
        assert [e["title"] for e in listed["result"]["event"]] == ["例会", "远会"]
        ranged = pim_tool.handle_pim(
            "query", {"scope": "event", "from": "2026-10-01", "to": "2026-10-31"}, store
        )
        assert [e["title"] for e in ranged["result"]["event"]] == ["例会"]
        one = pim_tool.handle_pim(
            "query", {"scope": "event", "id": listed["result"]["event"][0]["id"]}, store
        )
        assert one["result"]["event"]["title"] == "例会"

    def test_query_preference_by_key_and_domain(self, store: Any) -> None:
        store.set_preference("工作:同意", "auto_approve")
        by_key = pim_tool.handle_pim(
            "query", {"scope": "preference", "key": "工作:同意"}, store
        )
        assert by_key["result"]["preference"]["value"] == "auto_approve"
        by_domain = pim_tool.handle_pim(
            "query", {"scope": "preference", "domain": "工作"}, store
        )
        assert by_domain["result"]["preference"]["key"] == "工作:同意"

    def test_accept_event_and_todo(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        rec = pim_tool.handle_pim(
            "op", {"op": "accept", "entity": "event", "id": event["id"]}, store
        )
        assert rec["result"]["event"]["status"] == "doing"
        todo, _ = store.create("todo", {"title": "交周报"})
        rec2 = pim_tool.handle_pim("op", {"op": "accept", "id": todo["id"]}, store)
        assert rec2["result"]["todo"]["status"] == "doing"
        assert rec2["result"]["todo"]["preauthorized"] is True

    def test_event_complete_defer_cancel(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        deferred = pim_tool.handle_pim(
            "op",
            {"op": "defer", "entity": "event", "id": event["id"], "new_time": "2026-10-14 10:00"},
            store,
        )
        assert deferred["result"]["event"]["at"] == "2026-10-14 10:00"
        done = pim_tool.handle_pim(
            "op", {"op": "complete", "entity": "event", "id": event["id"]}, store
        )
        assert done["result"]["event"]["status"] == "done"
        event2, _ = store.create("event", {"title": "取消会", "at": "2026-10-13 11:00"})
        cancelled = pim_tool.handle_pim(
            "op", {"op": "cancel", "entity": "event", "id": event2["id"]}, store
        )
        assert cancelled["result"]["event"]["status"] == "cancelled"

    def test_todo_cancel_rejected(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "A"})
        rec = pim_tool.handle_pim("op", {"op": "cancel", "id": todo["id"]}, store)
        assert rec["status"] == "error"
        assert "undo" in rec["error"]

    def test_split_event_rejected(self, store: Any) -> None:
        event, _ = store.create("event", {"title": "例会", "at": "2026-10-13 10:00"})
        rec = pim_tool.handle_pim(
            "op", {"op": "split", "entity": "event", "id": event["id"], "parts": ["a"]}, store
        )
        assert rec["status"] == "error"

    def test_focus_start_duration_and_stop(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        started = pim_tool.handle_pim(
            "op", {"op": "focus_start", "id": todo["id"], "duration_minutes": 5}, store
        )
        assert started["result"]["todo"]["focus_duration_minutes"] == 5
        assert started["result"]["todo"]["status"] == "doing"
        stopped = pim_tool.handle_pim(
            "op", {"op": "focus_stop", "id": todo["id"]}, store
        )
        assert stopped["result"]["todo"]["focus_started_at"] is None
