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
import pytest


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
            "task.merge_notes": "docs/working/台账.md→backed_up_to=台账.md.merge-backup-X",
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
    # 合并备注随 tags 带出（上级通知渲染消费；缺键空串兜底）
    assert tags["merge_notes"] == "docs/working/台账.md→backed_up_to=台账.md.merge-backup-X"
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
    # 完成唯一判据 = 评估通过：pending/running/缺失 一律不派生
    for status in ["pending", "running", ""]:
        out = events.derive_task_terminal_events("run.completed", _task_row(status))
        assert out == [], f"task.status={status!r} 不得派生"


def test_failed_run_derives_task_failed_respecting_terminal_evidence():
    # 终态证据闸（U13 域界定 §二；词汇收敛 ADR 2026-10-30 豁免收窄）：未决
    # 投影照常派生 task_failed（运行失败的镜像派生通知面）；completed 是
    # 终态证据——不派生（失败通知不得推翻评估终态）
    for status in ["running", "pending", ""]:
        out = events.derive_task_terminal_events("run.failed", _task_row(status))
        assert _names(out) == ["task_failed"], f"task.status={status!r} 应派生"
    out = events.derive_task_terminal_events("run.failed", _task_row("completed"))
    assert out == [], "task.status=completed 终态证据不得被 failed 推翻"


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


def test_subtasks_pending_registration_is_not_a_task_declaration():
    """提交挂号键不算任务自身声明：提交过任务的聊天主管道（task.subtasks_pending.*
    + task.owned.* 记账键常驻，null 回执也不删键）不是任务管道，终态不派生。"""
    # 仅记账键（提交者主管道的常态：挂号键 + 登记键，回执后值为 null）
    submitter = {
        "pipeline_id": "chat_main",
        "task.subtasks_pending.child1": None,
        "task.owned.child1.title": "子任务",
    }
    assert events.is_task_state(submitter) is False
    assert events.derive_task_terminal_events("run.completed", submitter) == []
    # 反向性质：真任务行（自报了 task.id/goal，同时挂着挂号键——它自己又提交
    # 了子任务）仍是任务管道
    submitter_task = _task_row()
    submitter_task["task.subtasks_pending.grandchild1"] = "2026-10-17T00:00:00+00:00"
    assert events.is_task_state(submitter_task) is True


def test_legacy_trigger_registry_keys_are_not_task_declarations():
    """触发器注册表旧前缀（task.trigger.registry.*）不算任务自身声明。

    装机实证：主对话管道注册定时触发器后，任务管理出现该管道裸 ID 伪任务行
    （task.trigger.* 非空值命中任务判据）。旧前缀迁移期仍可能残留，判据须
    与 owned/挂号同列豁免；真任务行挂触发器键仍是任务管道。
    """
    chat = {
        "pipeline_id": "354a9c97344c",
        "task.trigger.registry.trigger_interval_1": {"trigger_id": "t1"},
        "task.owned.child1.title": "子任务",
        "task.status": None,
    }
    assert events.is_task_state(chat) is False
    assert events.derive_task_terminal_events("run.failed", chat) == []

    task = _task_row()
    task["task.trigger.registry.t1"] = {"trigger_id": "t1"}
    assert events.is_task_state(task) is True


def test_null_tombstone_and_empty_value_keys_are_not_task_declarations():
    """空值键不算任务自身声明（2026-10-27 主管道裸 ID 复发修复）。

    pipeline-state.update 无键删除语义：清扫/回执写 null = 墓碑行保留
    （value_kind='null'，读面键仍在）。判据若只看键存在，被清扫过的会话
    主管道（task.status/task.ended_at = null 墓碑）永久命中任务判据 →
    无 task.goal → 面板伪任务行标题兜底成裸管道 ID。声明 = 非空值，
    墓碑（null）与空串都只是清除署名。"""
    # 装机 2026-10-27 实证形态：被清扫的会话主管道（null 墓碑 + 记账键）
    swept_session = {
        "pipeline_id": "chat_main",
        "task.status": None,
        "task.ended_at": None,
        "task.owned.child1.title": "子任务",
    }
    assert events.is_task_state(swept_session) is False
    assert events.derive_task_terminal_events("run.completed", swept_session) == []
    # 空串残留（历史数据形态）同不算声明
    assert events.is_task_state({"pipeline_id": "p", "task.status": ""}) is False
    # 反向性质：真任务行（task.id/goal 非空）携带 null 墓碑兄弟键（resume
    # overlay 清失败注记的常态）仍是任务管道
    real_task = _task_row()
    real_task["task.ended_at"] = None
    assert events.is_task_state(real_task) is True


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


# ── 终态 state 载荷（ADR 2026-09-11：内核随事件交出 state，订阅方零回查）──


class _NoListStateCap:
    """派生零回查契约的 state 能力 fake。

    载荷在场时**派生**不得回查 state——本 fake 的 list 返回不含子行的行集，
    若派生回退到 list 回查，子行查不到即派生失败（emitted=0 即红）。
    list 允许被**路由面**触达（血缘冒泡需要全行建 by_pid 映射，ADR
    2026-11-06 D3——这与派生零回查是两件事）。
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows if rows is not None else []
        self.list_calls = 0

    async def call(self, method, params):
        assert method == "list"
        self.list_calls += 1
        return self._rows


def test_handle_prefers_event_payload_over_lookup():
    """载荷在场：直接派生（不回查——list 行集不含子行，回查即派生失败）。"""
    payload = _task_row("completed")
    bus = _FakeBusCap()
    cap = _NoListStateCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_t1", "state": payload},
            cap,
            bus,
        )
    )
    assert emitted == 1
    tags = bus.calls[0][1]["tags"]
    assert tags["task_id"] == "pipe_t1"
    # 父锚点从载荷带出——摘要白名单不再参与，这是断链修复的直接断言
    assert tags["parent_pipeline_id"] == "pipe_parent"


def test_handle_payload_carries_parent_anchor_filtered_from_summary():
    """回归防护：载荷里的 lineage 键即使不在摘要出口声明内也必须可达。

    断链实况（2026-09-11）：lineage.parent_pipeline_id 不在任何 manifest
    export_fields 内 → 摘要行恒缺该键 → 父锚点空串 → 通知与挂号清除双双静默
    短路。载荷路径不经白名单，该键必须原样带出。
    """
    payload = {
        "pipeline_id": "pipe_child",
        "task.id": "pipe_child",
        "task.goal": "子任务",
        "task.status": "completed",
        "task.submitted_by": "admin",
        "lineage.parent_pipeline_id": "pipe_parent",
        "lineage.origin_session_id": "thread-1",
        "lineage.parent_ws_meta": {"mode": "plain", "path": "/ws"},
    }
    # 摘要白名单形态的行集（无 lineage 键、无子行）——派生必须来自载荷
    summary_rows = [{"pipeline_id": "pipe_child", "task.status": "completed"}]
    bus = _FakeBusCap()
    cap = _NoListStateCap(summary_rows)
    asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_child", "state": payload},
            cap,
            bus,
        )
    )
    tags = bus.calls[0][1]["tags"]
    assert tags["parent_pipeline_id"] == "pipe_parent"
    assert tags["user_id"] == "admin"


def test_handle_falls_back_to_lookup_when_payload_absent():
    """载荷缺席（旧内核 / 合成事件）：回退 list 回查，兼容不破。"""
    rows = [_task_row("completed")]
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, _FakeStateCap(rows), bus
        )
    )
    assert emitted == 1
    assert bus.calls[0][1]["tags"]["parent_pipeline_id"] == "pipe_parent"


def test_handle_rejects_mismatched_payload_pipeline_id():
    """载荷坐标与事件坐标错配 → 拒绝载荷、退回回查（不按错行派生）。"""
    rows = [_task_row("completed")]
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {
                "pipeline_id": "pipe_t1",
                "state": {"pipeline_id": "other_pipe", "task.status": "failed"},
            },
            _FakeStateCap(rows),
            bus,
        )
    )
    # 回查命中 pipe_t1（completed），不采纳错配载荷的 failed
    assert emitted == 1
    assert bus.calls[0][1]["event"] == "task_completed"


def test_handle_non_dict_payload_falls_back():
    """载荷形状非法（非 dict）→ 回退回查。"""
    rows = [_task_row("completed")]
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_t1", "state": "not-a-dict"},
            _FakeStateCap(rows),
            bus,
        )
    )
    assert emitted == 1


def test_lookup_failure_returns_none_without_raising():
    """回查读面故障：降级为不派生，不裸抛（留痕在 _lookup_state_row）。"""

    class _BoomCap:
        async def call(self, method, params):
            raise RuntimeError("state read down")

    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, _BoomCap(), bus
        )
    )
    assert emitted == 0
    assert bus.calls == []


def test_lookup_non_list_response_derives_nothing():
    """回查返回非 list（形状异常）→ 不派生，不裸抛。"""

    class _BadShapeCap:
        async def call(self, method, params):
            return {"unexpected": "envelope"}

    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, _BadShapeCap(), bus
        )
    )
    assert emitted == 0
    assert bus.calls == []


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
    """update 恒失败（写面故障注入，清挂号故障路径用）。"""

    async def call(self, method, params):
        if method == "update":
            raise RuntimeError("state write down")
        return await super().call(method, params)


# ── 终态随写（谁终态谁随写：任务自身投影随写）──
#
# 事故锚（2026-10-20）：管道 3f968d0d946f 以 run_status=failed 终态化，
# 任务自身 task.status 投影仍是 running——前端任务列表被脏投影骗到「运行中」。
# 内核已有执法信号（谁终态谁随写违纪信号），本组测试即其落地契约。
# owned 镜像已退役（ADR 2026-11-02-state-key-governance）：task.owned.<id>.status
# 是双写漂移源（本次装机排查两条幽灵 running 即它），随写面全部移除——面板
# 改为 join 子管道行真值，单一真源。


class _StatefulStateCap(_RecordingStateCap):
    """写面生效的 state 能力 fake：update 真改行（重复投递幂等可验）。"""

    def __init__(self, rows):
        super().__init__(rows)
        self._by_pid = {str(r.get("pipeline_id") or ""): r for r in rows}

    async def call(self, method, params):
        if method == "update":
            self.updates.append(params)
            row = self._by_pid.get(str(params.get("pipeline_id") or ""))
            if row is not None:
                row.update(params.get("fields") or {})
            return {"status": "updated"}
        return await super().call(method, params)


def _own_status_updates(cap) -> list[dict]:
    """任务自身投影写（task.status 键）。"""
    return [u for u in cap.updates if "task.status" in u["fields"]]


def _owned_mirror_updates(cap) -> list[dict]:
    """父管道 owned 镜像写（task.owned.* 键）——瘦身后应恒为空。"""
    return [u for u in cap.updates if any(k.startswith("task.owned.") for k in u["fields"])]


def test_run_failed_follow_writes_own_projection_without_owned_mirror():
    """核心复现：run.failed 终态化 → task.status 随写 failed；owned 镜像
    已退役，不得再产生任何 task.owned.* 写面（面板 join 子行取真值）。"""
    cap = _RecordingStateCap([_task_row("running")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event("run.failed", {"pipeline_id": "pipe_t1"}, cap, bus)
    )
    assert emitted == 1
    assert {
        "pipeline_id": "pipe_t1",
        "fields": {"task.status": "failed"},
    } in cap.updates, "任务自身投影必须随写终态"
    assert _owned_mirror_updates(cap) == [], "owned 镜像已退役，不得写 task.owned.*"


def test_run_completed_follow_writes_nothing_when_projection_terminal():
    """completed 侧：投影已是 completed（评估闸门落账）→ 零写面（自身不重复
    写；owned 镜像退役无写面）。"""
    cap = _RecordingStateCap([_task_row("completed")])
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap()
        )
    )
    assert emitted == 1
    assert not _own_status_updates(cap), "已达终态的自身投影不得重复写"
    assert _owned_mirror_updates(cap) == [], "owned 镜像已退役"


def test_run_failed_with_failed_projection_writes_nothing():
    """自身投影已 failed（写面纪律随写）→ 零写面（自身不重复写；owned 镜像
    退役，事故的第二个脏面从根上消灭）。"""
    cap = _RecordingStateCap([_task_row("failed")])
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.failed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap()
        )
    )
    assert emitted == 1
    assert not _own_status_updates(cap), "state 已终态不得重复写"
    assert _owned_mirror_updates(cap) == []


def test_root_task_terminal_writes_own_projection_without_parent_mirror():
    """根任务（无父管道锚点）：只写自身投影，不产生 owned 镜像写，不报错。"""
    root = _task_row("running")
    root["lineage.parent_pipeline_id"] = ""
    cap = _RecordingStateCap([root])
    emitted = asyncio.run(
        events.handle_run_terminal_event("run.failed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap())
    )
    assert emitted == 1
    assert {
        "pipeline_id": "pipe_t1",
        "fields": {"task.status": "failed"},
    } in cap.updates
    assert not _owned_mirror_updates(cap), "根任务无父管道镜像可写"


# ── 结算唤醒守卫（P0 止血，ADR 2026-10-30 §4.3）────────────────────────
#
# 实证事故链（装机 2026-10-30）：子任务 B 完成 → 自动唤醒不查任务裁决，
# 已真终态父任务被再拉起跑一轮。守卫：派发结算通知前查父任务投影，
# 真终态（completed/failed）拒绝唤醒并留痕；stopped（暂停放那里）不在
# 拦截列——放那的任务允许被自然唤醒（设计意图）。


def _parent_task_row(status: str = "running", **extra: Any) -> dict:
    """父任务行（聚合形状）：自身任务声明 + 子任务挂号键 + owned 登记键。"""
    row = {
        "pipeline_id": "pipe_parent",
        "thread_id": "thread-parent",
        "task.id": "pipe_parent",
        "task.goal": "父任务",
        "task.status": status,
        "task.subtasks_pending.pipe_t1": "2026-10-30T08:00:00+00:00",
        "task.owned.pipe_t1.title": "子任务",
    }
    row.update(extra)
    return row


def test_settlement_wakeup_refused_when_parent_completed(caplog: pytest.LogCaptureFixture):
    """主形态（事故原样）：父任务 completed → 结算通知不派发（父管道不被
    拉起），告警含父/子坐标；结算簿记照走（回执照 + 父镜像）。"""
    import logging

    cap = _StatefulStateCap([_parent_task_row("completed"), _task_row("completed")])
    bus = _FakeBusCap()
    with caplog.at_level(logging.WARNING):
        emitted = asyncio.run(
            events.handle_run_terminal_event(
                "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
            )
        )
    assert emitted == 0, "父任务已真终态，结算通知不得派发（守卫拒绝唤醒）"
    assert bus.calls == [], "拒绝唤醒 = 不向域总线派发（注入器无从拉起父管道）"
    # 回执照写照清（拒绝是处置不是漏发——不清会让补发面每轮装载重复触发守卫）
    assert {
        "pipeline_id": "pipe_parent",
        "fields": {"task.subtasks_pending.pipe_t1": None},
    } in cap.updates
    # owned 镜像已退役：结算簿记只清挂号，不再有镜像随写
    assert _owned_mirror_updates(cap) == []
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "结算唤醒被拒绝" in m and "pipe_parent" in m and "pipe_t1" in m for m in warned
    ), "守卫拒绝必须留痕（含父/子管道 id）"


def test_settlement_wakeup_refused_when_parent_failed():
    """同构造第二输入：父任务 failed → 同样拒绝（真终态全集两值都拦）。"""
    cap = _StatefulStateCap([_parent_task_row("failed"), _task_row("completed")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert emitted == 0
    assert bus.calls == []


def test_settlement_wakeup_allowed_when_parent_stopped():
    """父任务 stopped（暂停放那里）不在拦截列——结算通知照常派发（可自然
    唤醒是设计意图，ADR 2026-10-30 §4.1 语义取舍）。"""
    cap = _StatefulStateCap([_parent_task_row("stopped"), _task_row("completed")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert emitted == 1
    assert bus.calls[0][1]["event"] == "task_completed"


def test_settlement_wakeup_degrades_to_allow_when_parent_row_missing():
    """父行缺席（聚合无此管道）→ 降级放行：结算通知是主路径，投影读失败
    不得拦死通知链（与恢复守卫降级口径同风格）。"""
    cap = _StatefulStateCap([_task_row("completed")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert emitted == 1
    assert bus.calls, "父行缺席不得拦死结算通知"


def test_settlement_wakeup_holds_on_routing_read_failure(
    caplog: pytest.LogCaptureFixture,
):
    """路由读面暂态故障（list 抛错）→ 扣发 + 保留挂号 + WARNING（ADR
    2026-11-06 D2 读面异常三分）：降级窗直送可能唤醒终态任务父（旧缺陷），
    补发面按恢复后的正确路由重投（重复投递亦无益）。"""

    import logging

    class _ListBoomCap(_RecordingStateCap):
        """list 恒抛（路由读面暂态故障模拟）；update 照常记录。"""

        async def call(self, method, params):
            if method == "list":
                raise RuntimeError("state read down")
            return await super().call(method, params)

    cap = _ListBoomCap([])
    bus = _FakeBusCap()
    with caplog.at_level(logging.WARNING):
        emitted = asyncio.run(
            events.handle_run_terminal_event(
                "run.completed",
                {"pipeline_id": "pipe_t1", "state": _task_row("completed")},
                cap,
                bus,
            )
        )
    assert emitted == 0, "暂态读面故障扣发（直送可能唤醒终态任务父）"
    assert bus.calls == []
    assert cap.updates == [], "暂态故障保留挂号（清了就永久丢失，补发面重投）"
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("pipe_t1" in m for m in warned), "扣发必须留痕（含子任务坐标）"


# ── 任务管道身份判据与血缘冒泡（ADR 2026-11-06
#    task-settlement-notify-guard-identity-and-lineage-bubble）──────────────
#
# 装机事故（主管道 35ee45bf1da2 / 子任务 04c87949c56c）：停止链路把幽灵
# task.status=stopped 写在会话主管道上 → 派生判据 is_task_state 把主管道
# 误判为任务管道 → 终态随写覆写 failed → 子任务完成被守卫拦截且回执被清
# → 通知永久丢失。用户裁定：主管道是会话管道，不该有 task.* 自身字段；
# 主管道失败/停止仍必须被唤醒；仅终态任务管道父才跳过，且跳过必沿血缘
# 冒泡到主管道（跳过 ≠ 丢弃）。


def _session_row(status: str | None = None, **extra: Any) -> dict:
    """会话主管道行（链顶）：无 task.id 无 lineage.*；可带误写残留。"""
    row: dict = {
        "pipeline_id": "pipe_session",
        "thread_id": "thread-session",
    }
    if status is not None:
        row["task.status"] = status
    row.update(extra)
    return row


def _mid_task_row(
    status: str = "completed",
    parent: str = "pipe_session",
    pid: str = "pipe_parent",
    pending_child: str | None = None,
    **extra: Any,
) -> dict:
    """任务管道行（链中节点）：task.id 出生声明 + lineage 指向上级。

    pending_child 非空时挂子任务挂号键（该任务自己提交过子任务的形态）。
    """
    row = {
        "pipeline_id": pid,
        "thread_id": pid,
        "task.id": pid,
        "task.goal": f"任务 {pid}",
        "task.status": status,
        "lineage.parent_pipeline_id": parent,
    }
    if pending_child:
        row[f"task.subtasks_pending.{pending_child}"] = "2026-11-06T08:00:00+00:00"
    row.update(extra)
    return row


class _FakeFrontendEmitter:
    """FrontendEmitter 替身：记录 emit 帧（WS 腿解耦断言用）。"""

    def __init__(self, available: bool = True) -> None:
        self.frames: list[tuple[str, dict]] = []
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    async def emit(self, event: str, payload: dict) -> None:
        self.frames.append((event, payload))


def test_is_task_pipeline_row_identity_criterion():
    """D1 统一判据：``task.id`` 非空 ∨ 任一 ``lineage.*`` 键非空。

    会话主管道带任何 task.* 残留（stopped/failed/completed 幽灵形态）都不
    改变身份；534ef18c1ba8 型（有 task.id 无 lineage.*）真任务命中。"""
    assert events.is_task_pipeline_row(_task_row()) is True
    # 有 task.id 无 lineage.*（装机 534ef18c1ba8 反例形态）= 真任务
    assert (
        events.is_task_pipeline_row(
            {"pipeline_id": "t", "task.id": "t", "task.status": "completed"}
        )
        is True
    )
    # 血缘键非空即任务管道（子任务出生协议）
    assert (
        events.is_task_pipeline_row({"pipeline_id": "p", "lineage.parent_pipeline_id": "x"})
        is True
    )
    # 会话主管道：任何残留都不算身份
    for residue in ("stopped", "failed", "completed", "running"):
        assert events.is_task_pipeline_row(_session_row(residue)) is False, residue
    # 空值键（null 墓碑/空串）不算非空
    assert (
        events.is_task_pipeline_row(
            {"pipeline_id": "p", "task.id": "", "lineage.parent_pipeline_id": None}
        )
        is False
    )
    # 记账键（owned/挂号）不构成身份
    assert (
        events.is_task_pipeline_row(
            {"pipeline_id": "p", "task.owned.c.title": "x", "task.subtasks_pending.c": "ts"}
        )
        is False
    )


def test_resolve_wakeup_target_pure_contract():
    """D3 冒泡解析纯函数：终点/死路/环/缺席四类契约逐条钉死。"""
    # 直接父=会话 → 投递目标=它，停止上行
    target, dead, chain = events.resolve_wakeup_target([_session_row()], "pipe_session")
    assert (target, dead) == ("pipe_session", False)
    assert chain == ["pipe_session"]
    # 终态任务父 → 继续上行到会话祖先
    rows = [
        _mid_task_row("completed", pid="pipe_parent", parent="pipe_session"),
        _session_row(),
    ]
    target, dead, chain = events.resolve_wakeup_target(rows, "pipe_parent")
    assert target == "pipe_session"
    assert dead is False
    assert chain == ["pipe_parent", "pipe_session"]
    # 活任务祖先（status 不在拦截集）即停——不越过它找会话
    rows = [
        _mid_task_row("completed", pid="p1", parent="p2"),
        _mid_task_row("running", pid="p2", parent="pipe_session"),
        _session_row(),
    ]
    target, dead, chain = events.resolve_wakeup_target(rows, "p1")
    assert target == "p2"
    assert chain == ["p1", "p2"]
    # 三级终态链连续上行
    rows = [
        _mid_task_row("completed", pid="p1", parent="p2"),
        _mid_task_row("failed", pid="p2", parent="pipe_session"),
        _session_row(),
    ]
    target, dead, chain = events.resolve_wakeup_target(rows, "p1")
    assert target == "pipe_session"
    assert chain == ["p1", "p2", "pipe_session"]
    # 环（visited 防环）→ 结构性死路
    rows = [
        _mid_task_row("completed", pid="a", parent="b"),
        _mid_task_row("completed", pid="b", parent="a"),
    ]
    target, dead, chain = events.resolve_wakeup_target(rows, "a")
    assert target is None
    assert dead is True
    # 中途祖先行缺失 → 结构性死路（chain 含缺失祖先 id，便于诊断）
    rows = [_mid_task_row("completed", pid="p1", parent="ghost")]
    target, dead, chain = events.resolve_wakeup_target(rows, "p1")
    assert target is None
    assert dead is True
    assert chain == ["p1", "ghost"]
    # 链顶终态任务（无父键）→ 结构性死路
    rows = [_mid_task_row("completed", pid="top", parent="")]
    target, dead, chain = events.resolve_wakeup_target(rows, "top")
    assert target is None
    assert dead is True
    # 直接父行缺席 → fail-open 直送（D2 读面异常三分：行缺席=管道已亡）
    target, dead, chain = events.resolve_wakeup_target([], "ghost_parent")
    assert (target, dead) == ("ghost_parent", False)
    # 空父（根任务）→ 无目标、非死路（事件照常派发，注入侧无处可投即跳过）
    target, dead, chain = events.resolve_wakeup_target([_task_row()], "")
    assert (target, dead, chain) == ("", False, [])


def test_settlement_wakeup_allows_session_parent_with_failed_residue():
    """事故原形（主管道 35ee45bf1da2 幽灵 failed 残留）：会话父放行——守卫
    不再读残留拦截，直送注入照发、回执照清（送达闭环）。"""
    session = _session_row(
        "failed", **{"task.subtasks_pending.pipe_t1": "2026-11-06T08:00:00+00:00"}
    )
    cap = _StatefulStateCap([session, _task_row("completed")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert emitted == 1, "会话父（非任务管道）一律放行——无论 task.status 残留为何值"
    tags = bus.calls[0][1]["tags"]
    assert tags["wakeup_target_pipeline_id"] == "pipe_parent"
    assert tags["parent_pipeline_id"] == "pipe_parent"
    assert {
        "pipeline_id": "pipe_parent",
        "fields": {"task.subtasks_pending.pipe_t1": None},
    } in cap.updates, "送达闭环：直接父挂号照清"


def test_settlement_wakeup_allows_session_parent_with_completed_residue():
    """区分度第二输入：会话父 completed 残留同样放行（拦截只对任务管道父）。"""
    session = _session_row(
        "completed", **{"task.subtasks_pending.pipe_t1": "2026-11-06T08:00:00+00:00"}
    )
    cap = _StatefulStateCap([session, _task_row("completed")])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert emitted == 1
    assert bus.calls[0][1]["tags"]["wakeup_target_pipeline_id"] == "pipe_parent"


def test_terminal_task_parent_bubbles_to_session_ancestor():
    """D3 主形态：直接父是真终态任务 → 跳过唤醒它，冒泡到会话祖先投递；
    ``wakeup_target_pipeline_id`` 新增、``parent_pipeline_id`` 恒=直接父
    （reconcile 回执定位零改动）。"""
    parent = _mid_task_row(
        "completed", pid="pipe_parent", parent="pipe_session", pending_child="pipe_t1"
    )
    session = _session_row(**{"task.owned.pipe_parent.title": "中间任务"})
    cap = _StatefulStateCap([_task_row("completed"), parent, session])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert emitted == 1
    tags = bus.calls[0][1]["tags"]
    assert tags["wakeup_target_pipeline_id"] == "pipe_session", "冒泡目标 = 会话祖先"
    assert tags["parent_pipeline_id"] == "pipe_parent", "直接父锚点语义不变"


def test_bubble_delivery_clears_direct_parent_registration():
    """D4 回执语义：冒泡送达后清**直接父**挂号（回执闭环在直接父，
    注入目标在冒泡终点——两件事分离）。"""
    parent = _mid_task_row(
        "completed", pid="pipe_parent", parent="pipe_session", pending_child="pipe_t1"
    )
    session = _session_row()
    cap = _StatefulStateCap([_task_row("completed"), parent, session])
    bus = _FakeBusCap()
    asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert {
        "pipeline_id": "pipe_parent",
        "fields": {"task.subtasks_pending.pipe_t1": None},
    } in cap.updates, "冒泡送达照清直接父挂号"


def test_three_level_terminal_chain_bubbles_with_chain_log(
    caplog: pytest.LogCaptureFixture,
):
    """三级终态链连续上行到会话祖先；target≠直接父时 INFO 留痕含 chain。"""
    import logging

    child = _task_row("completed", **{"lineage.parent_pipeline_id": "p1"})
    p1 = _mid_task_row("completed", pid="p1", parent="p2", pending_child="pipe_t1")
    p2 = _mid_task_row("failed", pid="p2", parent="pipe_session")
    session = _session_row()
    cap = _StatefulStateCap([child, p1, p2, session])
    bus = _FakeBusCap()
    with caplog.at_level(logging.INFO):
        emitted = asyncio.run(
            events.handle_run_terminal_event(
                "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
            )
        )
    assert emitted == 1
    assert bus.calls[0][1]["tags"]["wakeup_target_pipeline_id"] == "pipe_session"
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "冒泡" in m and "p1" in m and "pipe_session" in m for m in infos
    ), "冒泡上送必须 INFO 留痕（含 chain 坐标）"


def test_live_task_ancestor_stops_bubbling():
    """活任务祖先（status 不在终态拦截集）即停——投递目标 = 它，不越过。"""
    child = _task_row("completed", **{"lineage.parent_pipeline_id": "p1"})
    p1 = _mid_task_row("completed", pid="p1", parent="p2", pending_child="pipe_t1")
    p2 = _mid_task_row("running", pid="p2", parent="pipe_session")
    session = _session_row()
    cap = _StatefulStateCap([child, p1, p2, session])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
        )
    )
    assert emitted == 1
    assert bus.calls[0][1]["tags"]["wakeup_target_pipeline_id"] == "p2"


def test_chain_top_terminal_task_dead_end_clears_registration_with_warning(
    caplog: pytest.LogCaptureFixture,
):
    """链顶终态任务（无父键）= 结构性死路：清挂号 + WARNING（处置非漏发，
    重试同一条死路是噪声）；不得投递给终态任务自身（被否方案 7）。"""
    import logging

    parent = _mid_task_row(
        "completed", pid="pipe_parent", parent="", pending_child="pipe_t1"
    )
    cap = _StatefulStateCap([_task_row("completed"), parent])
    bus = _FakeBusCap()
    with caplog.at_level(logging.WARNING):
        emitted = asyncio.run(
            events.handle_run_terminal_event(
                "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
            )
        )
    assert emitted == 0
    assert bus.calls == [], "结构性死路不派发（终态任务父不得被唤醒）"
    assert {
        "pipeline_id": "pipe_parent",
        "fields": {"task.subtasks_pending.pipe_t1": None},
    } in cap.updates, "死路是处置：清挂号防补发面重复触发"
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "结算唤醒被拒绝" in m and "pipe_parent" in m and "pipe_t1" in m for m in warned
    ), "死路必须 WARNING 留痕（含父/子坐标）"


def test_cycle_dead_end_clears_registration(caplog: pytest.LogCaptureFixture):
    """血缘环（visited 防环）= 结构性死路：清挂号 + WARNING，不派发。"""
    import logging

    child = _task_row("completed", **{"lineage.parent_pipeline_id": "a"})
    a = _mid_task_row("completed", pid="a", parent="b", pending_child="pipe_t1")
    b = _mid_task_row("completed", pid="b", parent="a")
    cap = _StatefulStateCap([child, a, b])
    bus = _FakeBusCap()
    with caplog.at_level(logging.WARNING):
        emitted = asyncio.run(
            events.handle_run_terminal_event(
                "run.completed", {"pipeline_id": "pipe_t1"}, cap, bus
            )
        )
    assert emitted == 0
    assert bus.calls == []
    assert {
        "pipeline_id": "a",
        "fields": {"task.subtasks_pending.pipe_t1": None},
    } in cap.updates


def test_bubble_delivery_still_emits_ws_frame():
    """D6 WS 解耦：冒泡路径（守卫跳过直接父）前端帧照发——终态遥测与
    唤醒路由解耦，守卫不得吞帧（面板终态滞后修复）。"""
    parent = _mid_task_row(
        "completed", pid="pipe_parent", parent="pipe_session", pending_child="pipe_t1"
    )
    session = _session_row()
    cap = _StatefulStateCap([_task_row("completed"), parent, session])
    bus = _FakeBusCap()
    emitter = _FakeFrontendEmitter()
    asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_t1"},
            cap,
            bus,
            frontend_emitter=emitter,
        )
    )
    assert len(emitter.frames) == 1, "冒泡时 WS 帧必发（凡派生必发）"
    assert emitter.frames[0][0] == "task_completed"
    assert emitter.frames[0][1]["task_id"] == "pipe_t1"


def test_session_parent_still_emits_ws_frame():
    """D6 对照：会话父直送路径 WS 帧照发（帧先于路由决策，任何路由结果
    都不吞帧）。"""
    session = _session_row("completed")
    cap = _StatefulStateCap([session, _task_row("completed")])
    bus = _FakeBusCap()
    emitter = _FakeFrontendEmitter()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed",
            {"pipeline_id": "pipe_t1"},
            cap,
            bus,
            frontend_emitter=emitter,
        )
    )
    assert emitted == 1
    assert len(emitter.frames) == 1


def test_run_started_session_residue_never_reset():
    """复位门身份前提（D1）：会话主管道的 task.status 残留（幽灵形态）
    不触发复位写；对照：同残留的真任务行照常复位 running。"""
    session_cap = _RecordingStateCap([_session_row("failed")])
    assert (
        asyncio.run(
            events.handle_run_started_event({"pipeline_id": "pipe_session"}, session_cap)
        )
        is False
    )
    assert session_cap.updates == [], "会话行不复位（无 task.id 无 lineage.*）"
    task_cap = _RecordingStateCap([_task_row("failed")])
    assert (
        asyncio.run(events.handle_run_started_event({"pipeline_id": "pipe_t1"}, task_cap))
        is True
    ), "真任务行（task.id 在场）复位门不受影响"


def test_session_residue_row_never_derives_or_follow_writes():
    """派生门 = is_task_state ∧ is_task_pipeline_row（D1）：会话残留行不派生
    终态事件，随写自然不触发（35ee45bf1da2 被覆写 failed 的根因修复；
    残留清理归 reconcile 清扫刀）。"""
    session = _session_row("stopped", **{"task.ended_at": "2026-11-06T09:02:01+00:00"})
    cap = _RecordingStateCap([session])
    bus = _FakeBusCap()
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.failed", {"pipeline_id": "pipe_session"}, cap, bus
        )
    )
    assert emitted == 0, "会话行（无 task.id 无 lineage.*）不得派生任务终态"
    assert cap.updates == [], "不派生即不随写（会话残留不被终态覆写）"


def test_terminal_follow_write_is_idempotent_on_repeated_delivery():
    """重复投递（终态事件重发）：自身投影第二次不重写，零重复写面。"""
    cap = _StatefulStateCap([_task_row("running")])
    for _ in range(2):
        asyncio.run(
            events.handle_run_terminal_event(
                "run.failed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap()
            )
        )
    own = _own_status_updates(cap)
    assert len(own) == 1, f"自身终态投影只写一次（实际 {len(own)} 次）"
    assert _owned_mirror_updates(cap) == [], "owned 镜像已退役"


def test_non_terminal_event_writes_no_projection():
    """非终态路径不回退：run.completed 但投影未决（评估未过）→ 零写零派生。"""
    cap = _RecordingStateCap([_task_row("running")])
    emitted = asyncio.run(
        events.handle_run_terminal_event(
            "run.completed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap()
        )
    )
    assert emitted == 0
    assert cap.updates == []


def test_follow_write_failure_does_not_break_event_emit(caplog: pytest.LogCaptureFixture):
    """写面故障：随写失败留告警，事件派生与挂号清除不受影响（不裸抛）。"""
    import logging

    class _BoomUpdateCap(_RecordingStateCap):
        async def call(self, method, params):
            if method == "update":
                raise RuntimeError("state write down")
            return await super().call(method, params)

    cap = _BoomUpdateCap([_task_row("running")])
    with caplog.at_level(logging.WARNING):
        emitted = asyncio.run(
            events.handle_run_terminal_event(
                "run.failed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap()
            )
        )
    assert emitted == 1, "写面故障不得吞掉终态事件派生"
    assert any("随写失败" in r.getMessage() for r in caplog.records), "随写失败必须留告警"


def test_follow_write_precedes_event_notification():
    """顺序契约：随写先于通知——父会话被唤醒读投影时投影已是终态。"""
    order: list[str] = []

    class _OrderBus(_FakeBusCap):
        async def call(self, method, params):
            order.append("emit")
            return {"status": "emitted"}

    class _OrderCap(_RecordingStateCap):
        async def call(self, method, params):
            if method == "update":
                order.append("update")
            return await super().call(method, params)

    asyncio.run(
        events.handle_run_terminal_event(
            "run.failed", {"pipeline_id": "pipe_t1"}, _OrderCap([_task_row("running")]), _OrderBus()
        )
    )
    assert order, "必须有写与发动作"
    assert order.index("update") < order.index("emit"), "随写必须先于通知"


def test_terminal_follow_write_plan_is_pure():
    """纯函数形态：随写计划（仅自身投影，owned 镜像退役）可单测，不触达能力面。"""
    tags = {
        "pipeline_id": "pipe_t1",
        "task_id": "pipe_t1",
        "parent_pipeline_id": "pipe_parent",
    }
    row = {"task.status": "running"}
    assert events.terminal_follow_write_plan("task_failed", tags, row) == [
        {"pipeline_id": "pipe_t1", "fields": {"task.status": "failed"}},
    ]
    # 自身投影已是该终态 → 零写面（无镜像可写）
    assert events.terminal_follow_write_plan(
        "task_failed", tags, {"task.status": "failed"}
    ) == []
    # 非终态事件 → 无随写计划
    assert events.terminal_follow_write_plan("task_running", tags, row) == []
    # 无父锚点（根任务）→ 只写自身投影（与有父锚点同形：owned 退役后无差别）
    root_tags = {**tags, "parent_pipeline_id": ""}
    assert events.terminal_follow_write_plan("task_completed", root_tags, row) == [
        {"pipeline_id": "pipe_t1", "fields": {"task.status": "completed"}},
    ]


def test_run_failed_with_undecided_projection_logs_follow_write(
    caplog: pytest.LogCaptureFixture,
):
    """kill 方未随写（投影仍 running）→ 留痕：投影未随写终态 + 任务域已补随写。"""
    import logging

    cap = _RecordingStateCap([_task_row("running")])
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            events.handle_run_terminal_event(
                "run.failed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap()
            )
        )
    warned = [r for r in caplog.records if "未随写终态" in r.getMessage()]
    assert len(warned) == 1, "投影未随写必须留告警"
    assert "pipe_t1" in warned[0].getMessage(), "告警须带管道 id"
    assert "running" in warned[0].getMessage(), "告警须带投影状态"


def test_run_failed_with_failed_projection_no_divergence_warning(
    caplog: pytest.LogCaptureFixture,
):
    """state 已是 failed（写面纪律随写）→ 派生照常、零违纪告警。"""
    import logging

    rows = [_task_row("failed")]
    cap = _RecordingStateCap(rows)
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            events.handle_run_terminal_event(
                "run.failed", {"pipeline_id": "pipe_t1"}, cap, _FakeBusCap()
            )
        )
    assert all("未随写终态" not in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING), (
        "投影已达 failed 不得告警"
    )


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
    assert {
        "pipeline_id": "pipe_parent",
        "fields": {"task.subtasks_pending.pipe_t1": None},
    } in state_cap.updates, "父管道子任务挂号必须清除"
    assert _owned_mirror_updates(state_cap) == [], "owned 镜像已退役，挂号清除不再伴随镜像写"


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


# ── 新 run 启动复位（continue/重新执行复活：历史终态残留复位 running）──


def test_run_started_resets_failed_to_running():
    """核心复现（真机实证 2026-09-30）：failed 任务的新 run 启动 → 复位 running。

    failed 任务被自己的历史终态锁死：重新执行后 stop_check 聚合读面读到
    残留 failed 判死新 run（三次重试三次秒死）。复位必须走 pipeline-state
    .update（DB + registry 写面 = 聚合读面同源），且不得派生终态事件。
    owned 镜像退役（ADR 2026-11-02-state-key-governance）：复活只写自身投影。
    """
    cap = _RecordingStateCap([_task_row("failed")])
    bus = _FakeBusCap()
    reset = asyncio.run(
        events.handle_run_started_event({"pipeline_id": "pipe_t1"}, cap)
    )
    assert reset is True
    assert cap.updates == [
        {"pipeline_id": "pipe_t1", "fields": {"task.status": "running"}},
    ]
    assert bus.calls == [], "run.started 复位不得派生任务域终态事件"


def test_run_started_root_task_no_parent_mirror_write():
    """根任务（无父锚点）只写自身投影，不凭空造 owned 键。"""
    cap = _RecordingStateCap([_task_row("failed", **{"lineage.parent_pipeline_id": ""})])
    reset = asyncio.run(
        events.handle_run_started_event({"pipeline_id": "pipe_t1"}, cap)
    )
    assert reset is True
    assert cap.updates == [
        {"pipeline_id": "pipe_t1", "fields": {"task.status": "running"}}
    ]


@pytest.mark.parametrize(
    "status",
    ["timeout", "stopped", "suspended", "pending", "evaluating", "cancelled", "canceled"],
)
def test_run_started_resets_reactivatable_statuses(status: str):
    """可重入状态（含历史别名/未决态）同样复位——新 run 启动即任务激活。

    词汇收敛（ADR 2026-10-30）：cancelled/canceled 退役并入 stopped，豁免列
    收窄为 {completed, deleted}——旧词残留行不再免疫复位（历史行由 reconcile
    开机迁移 stopped，迁移前的残留也按可重入处理）。"""
    cap = _RecordingStateCap([_task_row(status)])
    reset = asyncio.run(
        events.handle_run_started_event({"pipeline_id": "pipe_t1"}, cap)
    )
    assert reset is True, f"task.status={status!r} 应复位 running"
    assert cap.updates == [
        {"pipeline_id": "pipe_t1", "fields": {"task.status": "running"}},
    ]


@pytest.mark.parametrize("status", ["completed", "deleted"])
def test_run_started_exempts_user_side_terminal(status: str):
    """用户侧终态豁免（词汇收敛后契约，ADR 2026-10-30）：completed = 用户手动
    完成；deleted = 用户侧删除署名——新 run 不得推翻用户显式裁决（与
    _TASK_TRANSITIONS 一致：completed 无 → running 边）。"""
    cap = _RecordingStateCap([_task_row(status)])
    reset = asyncio.run(
        events.handle_run_started_event({"pipeline_id": "pipe_t1"}, cap)
    )
    assert reset is False, f"task.status={status!r} 不得被复位"
    assert cap.updates == []


def test_run_started_noop_when_already_running():
    """running 不重复写（幂等省写：常规续跑轮零写放大）。"""
    cap = _RecordingStateCap([_task_row("running")])
    reset = asyncio.run(
        events.handle_run_started_event({"pipeline_id": "pipe_t1"}, cap)
    )
    assert reset is False
    assert cap.updates == []


def test_run_started_non_task_pipeline_noop():
    """chat 会话管道（无 task.* 自身键）不复位——复位只作用于任务管道。"""
    cap = _RecordingStateCap([{"pipeline_id": "chat_main", "messages": []}])
    reset = asyncio.run(
        events.handle_run_started_event({"pipeline_id": "chat_main"}, cap)
    )
    assert reset is False
    assert cap.updates == []


def test_run_started_missing_pipeline_id_never_touches_capability():
    """缺 pipeline_id（事件标签异常）：不动能力面（读也不该有）。"""

    class _AnyCallFails:
        async def call(self, method: str, params: dict) -> None:
            raise AssertionError(f"缺 pipeline_id 不得触达能力面（收到 {method}）")

    assert (
        asyncio.run(events.handle_run_started_event({"event": "run.started"}, _AnyCallFails()))
        is False
    )
    assert asyncio.run(events.handle_run_started_event({}, _AnyCallFails())) is False


def test_run_started_row_not_found_noop():
    """聚合行不存在（冷管道/坐标错）：不写。"""
    cap = _RecordingStateCap([])
    assert (
        asyncio.run(
            events.handle_run_started_event({"pipeline_id": "ghost"}, cap)
        )
        is False
    )
    assert cap.updates == []


def test_run_started_state_read_failure_does_not_raise():
    """读面故障：降级为不复位，不裸抛（复位失败不阻断 run）。"""

    class _BoomCap:
        async def call(self, method: str, params: dict) -> None:
            raise RuntimeError("state read down")

    assert (
        asyncio.run(
            events.handle_run_started_event({"pipeline_id": "pipe_t1"}, _BoomCap())
        )
        is False
    )


def test_run_started_write_failure_reports_false_without_raising():
    """写面故障：返回 False（未复位），异常留日志不外抛。"""
    cap = _BrokenUpdateStateCap([_task_row("failed")])
    reset = asyncio.run(
        events.handle_run_started_event({"pipeline_id": "pipe_t1"}, cap)
    )
    assert reset is False
