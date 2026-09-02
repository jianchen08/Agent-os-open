# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""task_service 任务域事件派生测试（ADR 2026-08-28 事件下沉）。

断行为不断实现：derive 纯函数断 输入 state 行 → 派生事件与标签；
handle 断 能力调用 → emit_domain 载荷。覆盖评估未通过不派生、
task.owned 登记键不误判、bus 失败不中断等契约。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import events


def _task_row(status: str = "completed", **extra: Any) -> dict:
    row = {
        "pipeline_id": "pipe_t1",
        "thread_id": "thread-1",
        "task.id": "pipe_t1",
        "task.goal": "写周报",
        "task.status": status,
        "lineage.parent_pipeline_id": "pipe_parent",
        "task.submitted_by": "admin",
    }
    row.update(extra)
    return row


def _names(result):
    return [name for name, _ in result]


def test_completed_status_derives_task_completed_with_tags():
    (name, tags) = events.derive_task_terminal_events("run.completed", _task_row())[0]
    assert name == "task_completed"
    assert tags["pipeline_id"] == "pipe_t1"
    assert tags["task_id"] == "pipe_t1"
    # 子任务通知锚点：parent/user_id 从血缘/提交人扁平键带出
    assert tags["parent_pipeline_id"] == "pipe_parent"
    assert tags["user_id"] == "admin"


def test_terminal_tags_carry_rich_notification_fields():
    """富通知字段（0.1 task_notifier 同款）：标题/失败原因/重试/评估结论/上下文。"""
    row = _task_row(
        "failed",
        **{
            "task.error": "评估未通过: 1/2 项指标通过",
            "task.eval_total_calls": 3,
            "task.eval_summary": "1/2 项指标通过",
            # 上下文占用 = 最近 LLM 轮输入（last_*），非跨轮累计（total_*）
            "track.llm_usage": {"last_input_tokens": 50000, "total_input_tokens": 200000},
            "context_window": 128000,
        },
    )
    (name, tags) = events.derive_task_terminal_events("run.completed", row)[0]
    assert name == "task_failed"
    assert tags["title"] == "写周报"
    assert tags["error"] == "评估未通过: 1/2 项指标通过"
    assert tags["retry_count"] == 3
    assert tags["eval_summary"] == "1/2 项指标通过"
    assert tags["context_usage"] == {
        "pct": 39.1,
        "input_tokens": 50000,
        "context_window": 128000,
    }


def test_context_usage_missing_keys_returns_empty():
    """缺 track.llm_usage / context_window 任一 → 空 dict（通知侧按无遥测处理）。"""
    assert events._context_usage(_task_row()) == {}
    assert events._context_usage(_task_row(**cast("dict[str, Any]", {"track.llm_usage": {"last_input_tokens": 1}}))) == {}
    assert events._context_usage(_task_row(**cast("dict[str, Any]", {"context_window": 128000}))) == {}
    assert events._context_usage(_task_row(**cast("dict[str, Any]", {"track.llm_usage": {}, "context_window": 0}))) == {}


def test_unevaluated_statuses_derive_nothing():
    # 完成唯一判据 = 评估通过：pending/pending_evaluation/running/缺失 一律不派生
    for status in ["pending", "running", "pending_evaluation", ""]:
        out = events.derive_task_terminal_events("run.completed", _task_row(status))
        assert out == [], f"task.status={status!r} 不得派生"


def test_failed_run_derives_task_failed_regardless_of_status():
    for status in ["running", "completed", "pending"]:
        out = events.derive_task_terminal_events("run.failed", _task_row(status))
        assert _names(out) == ["task_failed"]


def test_run_suspended_and_other_events_never_derive():
    # 挂起不是任务终态；非 run.* 终态事件一律不派生
    for ev in ["run.suspended", "run.cancelled", "run.started", "session.created"]:
        assert events.derive_task_terminal_events(ev, _task_row()) == []


def test_owned_only_state_is_not_a_task_pipeline():
    # 幽灵任务防护：仅登记过子任务的聊天主管道（只有 task.owned.*）不得派生
    row = {"pipeline_id": "chat_main", "task.owned.child1.title": "子任务"}
    assert events.derive_task_terminal_events("run.completed", row) == []
    # 反向性质：真任务行（混有 owned 登记键）仍派生
    mixed = _task_row()
    mixed["task.owned.child1.title"] = "子任务"
    assert _names(events.derive_task_terminal_events("run.completed", mixed)) == [
        "task_completed"
    ]


def test_non_dict_row_and_non_task_row_derive_nothing():
    assert events.derive_task_terminal_events("run.completed", None) == []
    assert events.derive_task_terminal_events("run.completed", {"pipeline_id": "p"}) == []


class _FakeStateCap:
    def __init__(self, rows):
        self._rows = rows

    async def call(self, method, params):
        assert method == "list"
        return self._rows


class _FakeBusCap:
    def __init__(self, fail_on=None):
        self.calls = []
        self._fail_on = fail_on or set()

    async def call(self, method, params):
        self.calls.append((method, params))
        if params.get("event") in self._fail_on:
            raise RuntimeError("bus down")
        return {"status": "emitted"}


def test_handle_emits_derived_events_via_bus():
    rows = [_task_row("completed"), {"pipeline_id": "other"}]
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, _FakeStateCap(rows), bus
        )
    )
    assert emitted == 1
    assert bus.calls[0][0] == "emit_domain"
    assert bus.calls[0][1]["event"] == "task_completed"
    assert bus.calls[0][1]["tags"]["task_id"] == "pipe_t1"


def test_handle_row_not_found_or_missing_pipeline_emits_nothing():
    bus = _FakeBusCap()
    assert (
        asyncio.run(
            events.handle_run_terminal_event(
                "run.completed", {"pipeline_id": "ghost"}, _FakeStateCap([_task_row()]), bus
            )
        )
        == 0
    )
    assert (
        asyncio.run(
            events.handle_run_terminal_event("run.completed", {}, _FakeStateCap([]), bus)
        )
        == 0
    )
    assert bus.calls == []


def test_handle_bus_failure_does_not_raise():
    rows = [_task_row("failed")]
    bus = _FakeBusCap(fail_on={"task_failed"})
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.failed", {"pipeline_id": "pipe_t1"}, _FakeStateCap(rows), bus
        )
    )
    assert emitted == 0, "发射失败计 0（异常已留日志）"


# ── 子任务挂号键清除（ADR 2026-08-28-task-closure-three-signal-gate 信号③）──


class _RecordingStateCap(_FakeStateCap):
    """记录 update 调用的 state 能力 fake（list 沿用基类）。"""

    def __init__(self, rows):
        super().__init__(rows)
        self.updates: list[dict] = []

    async def call(self, method, params):
        if method == "update":
            self.updates.append(params)
            return {"status": "updated"}
        return await super().call(method, params)


class _BrokenUpdateStateCap(_RecordingStateCap):
    """update 恒失败（写面故障注入）。"""

    async def call(self, method, params):
        if method == "update":
            raise RuntimeError("state write down")
        return await super().call(method, params)


# ── 终态对账（两态模型绑定不变量：派生 task_failed 须补落 state task.status）──


def test_run_failed_reconciles_state_task_status_to_failed():
    """kill 方未随写 task.status（如 stalled 署名终止）→ 派生时单点补落。"""
    rows = [_task_row("running")]
    bus = _FakeBusCap()
    cap = _RecordingStateCap(rows)
    emitted = asyncio.run(
        events.handle_run_terminal_event("run.failed", {"pipeline_id": "pipe_t1"}, cap, bus)
    )
    assert emitted == 1
    reconcile = [u for u in cap.updates if u["fields"] == {"task.status": "failed"}]
    assert reconcile, "run.failed 派生 task_failed 必须对账写回 state task.status"
    assert reconcile[0]["pipeline_id"] == "pipe_t1"
    # 挂号清除照旧发生（父管道锚点在 tags 中）
    clears = [u for u in cap.updates if "task.subtasks_pending.pipe_t1" in u["fields"]]
    assert clears, "挂号清除不受对账影响"


def test_run_failed_skips_reconcile_when_state_already_failed():
    """state 已是 failed（评估闸门自落）→ 不重复写，仅挂号清除。"""
    rows = [_task_row("failed")]
    cap = _RecordingStateCap(rows)
    asyncio.run(
        events.handle_run_terminal_event("run.failed", {"pipeline_id": "pipe_t1"}, cap, bus := _FakeBusCap())
    )
    assert all(u["fields"] != {"task.status": "failed"} for u in cap.updates), (
        "state 已终态不得重复写"
    )


def test_reconcile_write_failure_does_not_break_derivation():
    """对账写失败仅告警，事件派生与挂号清除照常。"""
    rows = [_task_row("running")]
    bus = _FakeBusCap()
    cap = _BrokenUpdateStateCap(rows)
    emitted = asyncio.run(
        events.handle_run_terminal_event("run.failed", {"pipeline_id": "pipe_t1"}, cap, bus)
    )
    assert emitted == 1, "对账写失败不得吞掉事件派生"


def test_pending_clear_fields_from_terminal_tags():
    tags = {"task_id": "pipe_t1", "parent_pipeline_id": "pipe_parent"}
    assert events.pending_registration_clear_fields(tags) == {
        "task.subtasks_pending.pipe_t1": None
    }


def test_pending_clear_fields_requires_parent_and_task():
    # 无父锚点（根任务）/无任务 id → 不产生清除写
    for tags in [
        {"task_id": "t1", "parent_pipeline_id": ""},
        {"task_id": "", "parent_pipeline_id": "p"},
        {},
    ]:
        assert events.pending_registration_clear_fields(tags) is None


def test_handle_terminal_event_clears_parent_registration():
    rows = [_task_row("completed")]
    state_cap = _RecordingStateCap(rows)
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, state_cap, bus
        )
    )
    assert emitted == 1
    assert state_cap.updates == [
        {
            "pipeline_id": "pipe_parent",
            "fields": {"task.subtasks_pending.pipe_t1": None},
        }
    ]


def test_handle_no_derivation_no_registration_write():
    # 评估未通过不派生（无终态事件）→ 挂号键不动（父继续等待）
    state_cap = _RecordingStateCap([_task_row("running")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, state_cap, bus
        )
    )
    assert emitted == 0
    assert state_cap.updates == []


def test_handle_registration_clear_failure_does_not_break_emit():
    # 清除写失败只留告警，不破坏事件派生主流程
    state_cap = _BrokenUpdateStateCap([_task_row("completed")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, state_cap, bus
        )
    )
    assert emitted == 1
    assert len(state_cap.updates) == 0


def test_handle_root_task_terminal_writes_no_registration():
    # 根任务（无 lineage.parent_pipeline_id）终态：派生事件但无挂号可清
    root_row = _task_row("completed")
    root_row["lineage.parent_pipeline_id"] = ""
    state_cap = _RecordingStateCap([root_row])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, state_cap, bus
        )
    )
    assert emitted == 1
    assert state_cap.updates == []
