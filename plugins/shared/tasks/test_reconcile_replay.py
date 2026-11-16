# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""未回执终态补发面测试（ADR 2026-10-28-task-terminal-notify-loss-and-reopen-evidence D2）。

事故（装机 2026-10-28）：子任务终态域事件 run.completed 广播时任务域订阅者
task_service 被空闲卸载（未声明常驻）→ 事件无人接收（广播即发、无重放无死信）
→ 上级没收到「子任务已完成」通知、父管道 task.subtasks_pending.<id> 回执从未
写过、父侧 task.owned.<id>.status 停在 running。

本组用例断补发面的行为契约（判据 → 副作用）：

- 判据：子任务投影已是任务域终态（completed/failed）× 父管道仍持非空挂号键
  → 补发；已回执（null）/未决投影/根任务（无父锚点）/父行缺席 → 不补发；
- 动作：重发终态事件（标签组 = events.derive_task_terminal_events 同一构造）
  + 父挂号写 null（回执）+ 父 owned 镜像随写终态；
- 只补发不改终态：绝不写子任务的 task.status / task.ended_at；
- 幂等：挂号清空后同一 (父,子) 不再命中（连续两轮只补发一次）；
- 降级：event-bus 能力缺席 → 只回执 + 父镜像并留告警；重发抛错 → 记异常且
  保留挂号（下轮重试）；写失败 → 只告警留痕不抛。

外部依赖替身：event-bus / pipeline-state / service-registry 三个内核能力句柄
（按外部依赖替身）；关键路径真调 events.derive_task_terminal_events 与
events.terminal_follow_write_plan（不 mock 成永远返回空）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import events
import pytest
import reconcile

pytestmark = pytest.mark.unit

# 固定钟点（fake clock 锚）：时序打桩经 setattr(reconcile, "_time", fake) 生效
# （模块命名空间打桩，不改全局 time）。所有「距今多久」的断言都相对此钟点。
_NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
_NOW_EPOCH = _NOW.timestamp()
_NOW_ISO = _NOW.isoformat()


class _FakeClock:
    """reconcile._time 的替身：固定「现在」。"""

    def time(self) -> float:
        return _NOW_EPOCH


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconcile, "_time", _FakeClock())
    # 时效窗配置隔离：默认走内置默认窗（24h），不受宿主环境影响
    monkeypatch.delenv(reconcile._REPLAY_WINDOW_ENV, raising=False)


def _iso_ago(hours: float) -> str:
    """相对固定钟点过去 hours 小时的 ISO 时间戳。"""
    return datetime.fromtimestamp(
        _NOW_EPOCH - hours * 3600.0, tz=UTC
    ).isoformat()


class _StateCap:
    """pipeline-state 能力替身：list 返回行快照，update 真改行（幂等可验）。"""

    def __init__(self, rows: list[Any], *, fail_update: bool = False) -> None:
        self.rows = rows
        self.fail_update = fail_update
        self.updates: list[dict[str, Any]] = []
        self._by_pid = {
            str(r.get("pipeline_id") or ""): r for r in rows if isinstance(r, dict)
        }

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "list":
            return self.rows
        if method != "update":
            raise KeyError(f"unexpected capability method: {method}")
        self.updates.append(params)
        if self.fail_update:
            raise RuntimeError("state write down")
        row = self._by_pid.get(str(params.get("pipeline_id") or ""))
        if row is not None:
            row.update(params.get("fields") or {})
        return {"status": "updated"}


class _RunsCap:
    """service-registry（运行簿记读面）替身：本组用例不需要簿记。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if self.fail:
            raise RuntimeError("runs read down")
        return []


class _RunsCapWithRows:
    """service-registry 替身：按 status 返回预置 run 行（三分刀用例）。"""

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self._runs = runs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method != "pipeline-runs.list":
            raise KeyError(f"unexpected method: {method}")
        want = str(params.get("status") or "")
        return [r for r in self._runs if str(r.get("status") or "") == want]


def _run_row(status: str, pipeline_id: str = "child-1", **extra: Any) -> dict[str, Any]:
    """run 簿记行（内核 pipeline-runs.list 形状：status/created_at 收敛键）。"""
    row = {
        "run_id": f"run-{status}-1",
        "pipeline_id": pipeline_id,
        "status": status,
        "created_at": _NOW_ISO,
    }
    row.update(extra)
    return row


class _BusCap:
    """event-bus 能力替身：记录 emit_domain 调用；可注入发射失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("bus down")
        return {"status": "emitted"}


def _child_row(status: str = "completed", **extra: Any) -> dict[str, Any]:
    """子任务行（任务管道摘要形状）：血缘键 + 任务域自身声明。

    默认带 ``task.ended_at`` = 固定钟点（窗内）——时效窗判据（ADR 2026-10-28
    D2-1）以终态时刻为准，不带该键的候选会回退父登记时间判定。
    """
    row = {
        "pipeline_id": "child-1",
        "thread_id": "thread-1",
        "task.id": "child-1",
        "task.goal": "按方案步骤 5 落地四件",
        "task.status": status,
        "task.ended_at": _NOW_ISO,
        "task.submitted_by": "admin",
        "lineage.parent_pipeline_id": "parent-1",
        "lineage.origin.kind": "subtask",
    }
    row.update(extra)
    return row


def _parent_row(pending: Any = _NOW_ISO, **extra: Any) -> dict[str, Any]:
    """父管道行（聊天主管道）：挂号键 + owned 登记键。

    挂号值 = 提交时间戳（默认固定钟点 = 窗内）；父登记时间只在子任务缺
    ``task.ended_at`` 时作为终态时刻的回退来源。
    """
    row = {
        "pipeline_id": "parent-1",
        "thread_id": "thread-parent",
        "task.subtasks_pending.child-1": pending,
        "task.owned.child-1.title": "子任务",
    }
    row.update(extra)
    return row


def _updates_for(state: _StateCap, pipeline_id: str) -> list[dict[str, Any]]:
    return [u for u in state.updates if str(u.get("pipeline_id") or "") == pipeline_id]


# ═══════════════════════════════════════════════════════════
# 判据：命中才补发
# ═══════════════════════════════════════════════════════════


async def test_completed_child_replays_terminal_event_and_receipts_parent() -> None:
    """主形态（事故原样）：子任务投影 completed × 父仍持非空挂号 → 补发
    task_completed，父挂号写 null（回执）。owned 镜像退役（ADR 2026-11-02
    -state-key-governance）：无镜像写面。"""
    child = _child_row("completed")
    parent = _parent_row()
    state = _StateCap([parent, child])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    # ① 重发终态事件：事件名 + 标签组与在线派生同一构造（真调 events 纯函数）
    assert len(bus.calls) == 1
    assert bus.calls[0]["event"] == "task_completed"
    expected_name, expected_tags = events.derive_task_terminal_events("run.completed", child)[0]
    assert expected_name == "task_completed"
    sent_tags = bus.calls[0]["tags"]
    # tag 契约（ADR 2026-11-06）：只增不删——wakeup_target_pipeline_id 恒写，
    # 其余标签组与在线派生构造逐键一致
    assert sent_tags.pop("wakeup_target_pipeline_id") == "parent-1", (
        "补发也恒写投递目标（直送时=直接父）"
    )
    assert sent_tags == expected_tags, "标签组必须复用在线派生构造"
    assert bus.calls[0]["tags"]["pipeline_id"] == "child-1"
    assert bus.calls[0]["tags"]["task_id"] == "child-1"
    assert bus.calls[0]["tags"]["parent_pipeline_id"] == "parent-1"
    assert bus.calls[0]["tags"]["title"] == "按方案步骤 5 落地四件"
    assert bus.calls[0]["tags"]["user_id"] == "admin"
    # ② 父管道挂号回执（null = 已回执）
    assert {
        "pipeline_id": "parent-1",
        "fields": {"task.subtasks_pending.child-1": None},
    } in state.updates
    # owned 镜像退役：补发面无 ③ 镜像纠正面，不得写 task.owned.*
    assert not [
        u for u in state.updates if any(k.startswith("task.owned.") for k in u["fields"])
    ]
    # 补发清单留痕
    assert [r["kind"] for r in report] == ["unreceipted_terminal_replay"]
    assert report[0]["replayed"] is True
    assert report[0]["receipt_written"] is True


async def test_failed_child_replays_task_failed() -> None:
    """同构造第二输入：子任务投影 failed → 补发 task_failed（词汇随之切换）。"""
    child = _child_row("failed", **{"task.error": "评估未通过: 1/2 项指标通过"})
    state = _StateCap([_parent_row(), child])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert [c["event"] for c in bus.calls] == ["task_failed"]
    assert bus.calls[0]["tags"]["error"] == "评估未通过: 1/2 项指标通过"
    assert report[0]["event"] == "task_failed"


async def test_receipted_parent_is_not_replayed_again() -> None:
    """已回执（挂号键 null）→ 不补发（防重复通知：正常送达过的终态事件
    挂号早已清空）。"""
    state = _StateCap([_parent_row(pending=None), _child_row("completed")])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert bus.calls == []
    assert state.updates == []
    assert report == []


@pytest.mark.parametrize("status", ["running", "pending", "evaluating", ""])
async def test_undecided_child_is_not_replayed(status: str) -> None:
    """子任务投影未决 → 不补发（终态未落定，通知不得凭空发出）。

    注：running/pending × 无 run_id 键会被第五刀收束自身投影（独立合法
    行为，写 child 不写 parent），本测试断补发域隔离（零域事件、父挂号
    不清）。"""
    state = _StateCap([_parent_row(), _child_row(status)])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert bus.calls == []
    assert [r for r in report if r["kind"] == "unreceipted_terminal_replay"] == []
    pending_clears = [
        u for u in state.updates
        if "task.subtasks_pending.child-1" in u.get("fields", {})
    ]
    assert pending_clears == [], "未决投影不得清父挂号（无终态可回执）"


async def test_root_task_without_parent_anchor_is_not_replayed() -> None:
    """根任务（无父锚点）→ 不补发（没有「上级等回执」这回事）。"""
    root = _child_row("completed")
    root.pop("lineage.parent_pipeline_id")
    root["lineage.origin.kind"] = "root"
    state = _StateCap([root])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert bus.calls == []
    assert state.updates == []
    assert report == []


async def test_parent_row_absent_is_not_replayed() -> None:
    """父管道行不在读面（父行缺席）→ 不补发（挂号面不可读 = 无判据，
    不猜：宁可漏补也不重复通知）。"""
    state = _StateCap([_child_row("completed")])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert bus.calls == []
    assert state.updates == []
    assert report == []


async def test_session_bookkeeping_row_is_not_a_task_row() -> None:
    """提交者侧记账键行（会话主管道）不构成任务行 → 不进补发域。

    注：行上挂的 owned/挂号键若子管道行不存在，会被第六刀清 null（独立合法
    行为）——本测试断补发域隔离（零域事件、零补发清单）。"""
    session = {
        "pipeline_id": "session-1",
        "thread_id": "thread-session",
        "task.subtasks_pending.child-1": "2026-10-28T07:00:00Z",
        "task.owned.child-1.status": "running",
    }
    state = _StateCap([session])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert bus.calls == []
    assert [r for r in report if r["kind"] == "unreceipted_terminal_replay"] == []
    assert not [
        u for u in state.updates
        if any("task.status" in u.get("fields", {}) for u in [u])
    ], "会话行不写任务状态（第六刀只清幽灵登记键，不涉 task.status）"


async def test_non_dict_rows_are_skipped() -> None:
    """读面含非 dict 行 → 跳过（形状违约即放弃该行），不炸不补发。"""
    state = _StateCap(["junk-row", _parent_row(), _child_row("completed")])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert len(bus.calls) == 1, "合法行照常补发"
    assert report[0]["kind"] == "unreceipted_terminal_replay"


# ═══════════════════════════════════════════════════════════
# 只补发，不改终态
# ═══════════════════════════════════════════════════════════


async def test_replay_never_rewrites_child_terminal_projection() -> None:
    """补发面不改写任何终态词汇：子任务的 task.status / task.ended_at 绝不
    被写（那是评估闸门 task_evaluate 的写面，ADR 2026-10-20 红线）。"""
    state = _StateCap([_parent_row(), _child_row("completed")])

    await reconcile.reconcile_startup(state, _RunsCap(), _BusCap())

    child_updates = _updates_for(state, "child-1")
    assert child_updates == [], f"子任务自身管道不得有任何写面：{child_updates}"
    written_fields = {k for u in state.updates for k in u["fields"]}
    assert "task.status" not in written_fields
    assert "task.ended_at" not in written_fields


# ═══════════════════════════════════════════════════════════
# 幂等
# ═══════════════════════════════════════════════════════════


async def test_replay_is_idempotent_across_two_reconcile_runs() -> None:
    """连续两轮对账只补发一次：挂号清空即幂等锚（同一 (父,子) 只可能补发一次）。"""
    state = _StateCap([_parent_row(), _child_row("completed")])
    bus = _BusCap()

    first = await reconcile.reconcile_startup(state, _RunsCap(), bus)
    second = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert len(bus.calls) == 1, "第二轮不得重复补发"
    assert [r["kind"] for r in first] == ["unreceipted_terminal_replay"]
    assert second == [], "第二轮无补发清单"


# ═══════════════════════════════════════════════════════════
# 降级与留痕
# ═══════════════════════════════════════════════════════════


async def test_missing_event_bus_capability_degrades_to_receipt_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """event-bus 能力缺席 → 降级：只做回执并留告警，不抛、不牵连既有
    failed/用户停止侧补写面。owned 镜像退役，无镜像降级面。"""
    state = _StateCap([_parent_row(), _child_row("completed")])

    with caplog.at_level(logging.WARNING):
        report = await reconcile.reconcile_startup(state, _RunsCap(), None)

    assert {
        "pipeline_id": "parent-1",
        "fields": {"task.subtasks_pending.child-1": None},
    } in state.updates
    assert report[0]["replayed"] is False
    assert report[0]["receipt_written"] is True
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("event-bus" in m for m in warned), "能力缺席必须留告警"
    assert any("parent-1" in m and "child-1" in m for m in warned), "告警须带父子坐标"


async def test_emit_failure_keeps_registration_for_next_load(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重发抛错 → 记异常（不静默吞）且保留挂号（不写回执）：下轮装载重试，
    不把「未送达」变成「永久丢失」。"""
    state = _StateCap([_parent_row(), _child_row("completed")])
    bus = _BusCap(fail=True)

    with caplog.at_level(logging.WARNING):
        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert report[0]["replayed"] is False
    assert report[0]["receipt_written"] is False, "未送达不得写回执"
    assert {
        "pipeline_id": "parent-1",
        "fields": {"task.subtasks_pending.child-1": None},
    } not in state.updates
    assert any("补发失败" in r.getMessage() for r in caplog.records), "补发失败必须留痕"


async def test_write_failures_degrade_to_warnings_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """写面故障 → 只告警留痕（下轮装载重试），不抛、不中断对账。"""
    state = _StateCap([_parent_row(), _child_row("completed")], fail_update=True)
    bus = _BusCap()

    with caplog.at_level(logging.WARNING):
        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert report[0]["replayed"] is True, "事件已送达（写失败不影响重发）"
    assert report[0]["receipt_written"] is False
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("回执写失败" in m for m in messages)


async def test_runs_bookkeeping_read_failure_does_not_block_replay() -> None:
    """运行簿记读面故障 → 簿记分歧面降级，补发面不受牵连（两面并列互不牵连）。"""
    state = _StateCap([_parent_row(), _child_row("completed")])
    bus = _BusCap()

    report = await reconcile.reconcile_startup(state, _RunsCap(fail=True), bus)

    assert len(bus.calls) == 1, "补发面不依赖簿记读面"
    assert [r["kind"] for r in report] == ["unreceipted_terminal_replay"]


async def test_reconcile_startup_signature_keeps_legacy_call_shape() -> None:
    """向后兼容：bus_cap 是可选参数，既有调用方（两参）不破——按能力缺席
    降级口径只回执 + 父镜像，不抛。"""
    state = _StateCap([_parent_row(), _child_row("completed")])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    assert [r["kind"] for r in report] == ["unreceipted_terminal_replay"]
    assert report[0]["replayed"] is False, "无 bus 能力不得凭空重发"
    assert report[0]["receipt_written"] is True
    assert {
        "pipeline_id": "parent-1",
        "fields": {"task.subtasks_pending.child-1": None},
    } in state.updates


# ═══════════════════════════════════════════════════════════
# 三分刀（ADR 2026-10-30-task-status-authority-vocabulary-convergence §4.4）
# ═══════════════════════════════════════════════════════════


class TestReconcileThreeKnives:
    """词汇收敛三分刀：排空对账 / 僵尸告警 / 历史迁移（纯插件面，内核零改动）。"""

    async def test_drain_reconciliation_marks_running_task_stopped(self) -> None:
        """排空对账（刀一）：run 簿记 suspended × 投影 running → 写 stopped
        + 父镜像同批随写（重启排空遗留：G8 只翻 run 不写任务键）。幂等：
        第二轮投影已 stopped，零写。"""
        child = _child_row("running")
        state = _StateCap([_parent_row(pending=None), child])
        runs = _RunsCapWithRows([_run_row("suspended")])
        bus = _BusCap()

        report = await reconcile.reconcile_startup(state, runs, bus)

        entry = next(r for r in report if r["kind"] == "suspended_run_drain")
        assert entry["pipeline_id"] == "child-1"
        assert entry["followed_write"] is True
        assert {
            "pipeline_id": "child-1",
            "fields": {"task.status": "stopped"},
        } in state.updates, "排空对账写 stopped（暂停放那里，可恢复）"
        assert not [
            u for u in state.updates
            if u.get("pipeline_id") == "parent-1"
            and any(k.startswith("task.owned.") for k in u["fields"])
        ], "owned 镜像退役：排空对账无父侧写面"
        # 排空不是终局：不写 task.ended_at（挂起 run 的续跑轮自然复位 running）
        assert not any(
            "task.ended_at" in u["fields"] and u["pipeline_id"] == "child-1"
            for u in state.updates
        )
        # 幂等：第二轮（投影已 stopped）不再进分歧集
        second = await reconcile.reconcile_startup(state, runs, bus)
        assert [r for r in second if r.get("kind") == "suspended_run_drain"] == []

    async def test_zombie_terminal_projection_warns_without_writing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """僵尸告警（刀二）：投影真终态 × 簿记非终态 → 纯 logger.warning，
        不写任何键（run 簿记属内核域，机制性禁写）。"""
        import logging

        for task_status, run_status in [("completed", "running"), ("failed", "suspended")]:
            state = _StateCap([_parent_row(pending=None), _child_row(task_status)])
            runs = _RunsCapWithRows([_run_row(run_status)])
            bus = _BusCap()

            with caplog.at_level(logging.WARNING):
                report = await reconcile.reconcile_startup(state, runs, bus)

            entry = next(r for r in report if r["kind"] == "zombie_terminal_projection")
            assert entry["task_status"] == task_status
            assert entry["run_status"] == run_status
            assert state.updates == [], "僵尸告警纯告警：不得写任何键（不裁决）"
            assert bus.calls == [], "僵尸告警零派发"
            warned = [
                r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
            ]
            assert any(
                "zombie_terminal_projection" in m and "child-1" in m for m in warned
            ), f"僵尸形态必须留告警（{task_status} × {run_status}）"

    async def test_legacy_cancelled_rows_migrate_to_stopped(self) -> None:
        """历史迁移（刀三）：投影 cancelled（含 canceled 双拼写）存量任务行
        → stopped，一次性通过；迁移后第二轮零写。"""
        for legacy in ("cancelled", "canceled"):
            child = _child_row(legacy)
            state = _StateCap([_parent_row(pending=None), child])
            runs = _RunsCapWithRows([])

            report = await reconcile.reconcile_startup(state, runs, _BusCap())

            entry = next(r for r in report if r["kind"] == "legacy_cancelled_migrated")
            assert entry["legacy"] == legacy
            assert entry["migrated"] is True
            assert {
                "pipeline_id": "child-1",
                "fields": {"task.status": "stopped"},
            } in state.updates
            # 一次性：迁移后（_StateCap 真改行）第二轮不再命中
            second = await reconcile.reconcile_startup(state, runs, _BusCap())
            assert [
                r for r in second if r.get("kind") == "legacy_cancelled_migrated"
            ] == []

    async def test_migration_survives_runs_read_failure(self) -> None:
        """迁移刀独立于簿记读面：runs 读面故障不牵连迁移（补发面同款并列）。"""
        state = _StateCap([_parent_row(pending=None), _child_row("cancelled")])

        report = await reconcile.reconcile_startup(state, _RunsCap(fail=True), _BusCap())

        assert [
            r for r in report if r.get("kind") == "legacy_cancelled_migrated"
        ], "簿记读面故障不得牵连历史迁移"
        assert {
            "pipeline_id": "child-1",
            "fields": {"task.status": "stopped"},
        } in state.updates

    async def test_migration_write_failure_degrades_to_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """迁移写失败 → 降级留痕（migrated=False），下轮装载重试，不抛。"""
        import logging

        state = _StateCap(
            [_parent_row(pending=None), _child_row("cancelled")], fail_update=True
        )

        with caplog.at_level(logging.WARNING):
            report = await reconcile.reconcile_startup(state, _RunsCap(), _BusCap())

        entry = next(r for r in report if r["kind"] == "legacy_cancelled_migrated")
        assert entry["migrated"] is False
        assert any("历史迁移写失败" in r.getMessage() for r in caplog.records)

    async def test_drain_write_failure_degrades_to_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """排空对账写失败 → 降级留痕（followed_write=False），不抛不阻断装载。"""
        import logging

        state = _StateCap(
            [_parent_row(pending=None), _child_row("running")], fail_update=True
        )
        runs = _RunsCapWithRows([_run_row("suspended")])

        with caplog.at_level(logging.WARNING):
            report = await reconcile.reconcile_startup(state, runs, _BusCap())

        entry = next(r for r in report if r["kind"] == "suspended_run_drain")
        assert entry["followed_write"] is False
        assert any("排空对账写失败" in r.getMessage() for r in caplog.records)

    async def test_migration_skips_non_task_rows(self) -> None:
        """迁移刀只作用于任务管道行：会话行（缺血缘键）携带旧词残留不迁移——
        其清扫走主循环残留面（non_task_residue_cleared），两刀互不越界。"""
        session = {
            "pipeline_id": "session-9",
            "thread_id": "thread-9",
            # 误写残留形态（2026-10-21 装机实锤）：会话行带旧停止词
            "task.status": "cancelled",
        }
        state = _StateCap([session, _parent_row(pending=None), _child_row("cancelled")])

        report = await reconcile.reconcile_startup(state, _RunsCapWithRows([]), _BusCap())

        migrated = [r for r in report if r.get("kind") == "legacy_cancelled_migrated"]
        assert [r["pipeline_id"] for r in migrated] == ["child-1"], (
            "会话残留行不得进迁移刀"
        )
        assert not any(
            u["pipeline_id"] == "session-9"
            and u["fields"] == {"task.status": "stopped"}
            for u in state.updates
        ), "会话行不得被迁移写 stopped（清扫归清扫刀）"


# ═══════════════════════════════════════════════════════════
# 时效窗判据（ADR 2026-10-28 D2-1）：只补上一轮工作窗口内漏发的候选
# ═══════════════════════════════════════════════════════════
#
# 装机实测：父管道挂着 9 条跨 10-19~10-28 的未回执挂号 → 无窗补发 = 一次性
# 把 9 天前的旧事倒给上级（被当新消息处理、冲掉当前会话上下文）。故补发只
# 覆盖「上一轮工作窗口内漏发」，更早的漏发 = 历史积压 → 人工治理。


class TestReplayTimeWindow:
    async def test_fresh_terminal_moment_is_replayed(self) -> None:
        """窗内（终态时刻 = 现在）→ 补发：事件重发 + 挂号回执 + 父镜像随写。"""
        state = _StateCap([_parent_row(), _child_row("completed", **{"task.ended_at": _NOW_ISO})])
        bus = _BusCap()

        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert [c["event"] for c in bus.calls] == ["task_completed"]
        assert bus.calls[0]["tags"]["task_id"] == "child-1"
        assert {
            "pipeline_id": "parent-1",
            "fields": {"task.subtasks_pending.child-1": None},
        } in state.updates
        assert report[0]["terminal_at"] == _NOW_ISO
        assert report[0]["window_hours"] == 24.0

    async def test_just_inside_window_is_replayed(self) -> None:
        """窗内边界（终态时刻 = 24h 前 1 分钟）→ 补发。"""
        state = _StateCap(
            [_parent_row(), _child_row("completed", **{"task.ended_at": _iso_ago(24 - 1 / 60)})]
        )
        bus = _BusCap()

        await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert len(bus.calls) == 1
        assert {
            "pipeline_id": "parent-1",
            "fields": {"task.subtasks_pending.child-1": None},
        } in state.updates

    async def test_outside_window_is_not_replayed_and_is_traceable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """窗外（终态时刻 = 25h 前）→ 不补发：不发事件、**不清回执**（清了
        就永远没人知道它漏过），但必须留 WARNING 痕（parent/child/终态时刻/
        超窗天数齐全）——历史积压可观测。"""
        state = _StateCap(
            [_parent_row(), _child_row("completed", **{"task.ended_at": _iso_ago(25)})]
        )
        bus = _BusCap()

        with caplog.at_level(logging.WARNING):
            report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert bus.calls == [], "窗外候选不得重发事件"
        assert state.updates == [], "窗外候选不得清回执、不得改父镜像"
        assert report == []
        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "超时效窗" in m and "parent-1" in m and "child-1" in m for m in warned
        ), "超窗候选必须留告警（含父子坐标）"
        message = next(m for m in warned if "超时效窗" in m)
        assert _iso_ago(25) in message, "告警须带终态时刻"
        assert "age_days=" in message, "告警须带超窗天数"

    @pytest.mark.parametrize(
        ("ended_hours", "expected_replay"),
        [(24.0, True), (24.0 + 1 / 3600, False)],
        ids=["exactly_24h_in", "over_24h_out"],
    )
    async def test_window_boundary_is_exact(
        self, ended_hours: float, expected_replay: bool
    ) -> None:
        """边界口径：距今恰好 24h = 窗内（超窗判定为严格大于）；24h+1s = 窗外。"""
        state = _StateCap(
            [_parent_row(), _child_row("completed", **{"task.ended_at": _iso_ago(ended_hours)})]
        )
        bus = _BusCap()

        await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert bool(bus.calls) is expected_replay

    async def test_missing_ended_at_falls_back_to_parent_registration_in_window(
        self,
    ) -> None:
        """task.ended_at 缺失 → 回退父登记时间判定；登记时间在窗内 → 补发。"""
        child = _child_row("completed")
        child.pop("task.ended_at")
        state = _StateCap([_parent_row(pending=_iso_ago(2)), child])
        bus = _BusCap()

        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert len(bus.calls) == 1
        assert report[0]["replayed"] is True
        assert report[0]["terminal_at"] == _iso_ago(2)

    async def test_missing_ended_at_falls_back_to_parent_registration_outside_window(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """task.ended_at 缺失且父登记时间已超窗 → 不补发（回退取旧 → 更保守），
        且留告警。"""
        child = _child_row("completed")
        child.pop("task.ended_at")
        state = _StateCap([_parent_row(pending=_iso_ago(30)), child])
        bus = _BusCap()

        with caplog.at_level(logging.WARNING):
            report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert bus.calls == []
        assert state.updates == []
        assert report == []
        assert any("超时效窗" in r.getMessage() for r in caplog.records)

    async def test_ended_at_takes_priority_over_parent_registration(self) -> None:
        """终态时刻优先取 task.ended_at（父登记时间是回退，不是并列判据）：
        登记时间很旧但终态时刻在窗内 → 补发（那一轮的通知才是刚丢的）。"""
        state = _StateCap(
            [
                _parent_row(pending=_iso_ago(24 * 9)),
                _child_row("completed", **{"task.ended_at": _NOW_ISO}),
            ]
        )
        bus = _BusCap()

        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert len(bus.calls) == 1
        assert report[0]["terminal_at"] == _NOW_ISO

    async def test_undecidable_moment_is_not_replayed_and_is_traceable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """终态时刻两处都不可判定（无 ended_at + 登记值非时间戳）→ 不补发
        （不制造噪声），但留痕。"""
        child = _child_row("completed")
        child.pop("task.ended_at")
        state = _StateCap([_parent_row(pending="not-a-timestamp"), child])
        bus = _BusCap()

        with caplog.at_level(logging.WARNING):
            report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert bus.calls == []
        assert state.updates == []
        assert report == []
        assert any("终态时刻不可判定" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("value", ["0", "-1", "0.0"], ids=["zero", "negative", "zero_float"])
    async def test_window_disabled_means_no_replay_at_all(
        self, monkeypatch: pytest.MonkeyPatch, value: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """窗口 ≤ 0 = **显式关闭补发面**（一个都不补；候选仍留痕）——「关掉就
        一个都不补」比「回落到默认窗」更确定（宁可不补也不制造噪声）。"""
        monkeypatch.setenv(reconcile._REPLAY_WINDOW_ENV, value)
        state = _StateCap([_parent_row(), _child_row("completed")])
        bus = _BusCap()

        with caplog.at_level(logging.WARNING):
            report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert bus.calls == []
        assert state.updates == []
        assert report == []
        assert any("补发面关闭" in r.getMessage() for r in caplog.records)

    async def test_window_config_can_widen_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """窗口可配（调大）：默认窗外的候选在调大后照常补发。"""
        monkeypatch.setenv(reconcile._REPLAY_WINDOW_ENV, "72")
        state = _StateCap(
            [_parent_row(), _child_row("completed", **{"task.ended_at": _iso_ago(48)})]
        )
        bus = _BusCap()

        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert len(bus.calls) == 1
        assert report[0]["window_hours"] == 72.0

    async def test_unparsable_window_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """窗口配置不可解析 → 回默认 24h（配置坏掉不改变默认行为）并留痕。"""
        monkeypatch.setenv(reconcile._REPLAY_WINDOW_ENV, "not-a-number")
        state = _StateCap(
            [_parent_row(), _child_row("completed", **{"task.ended_at": _iso_ago(25)})]
        )
        bus = _BusCap()

        with caplog.at_level(logging.WARNING):
            report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert bus.calls == [], "回默认 24h → 25h 前的候选仍超窗"
        assert report == []
        assert any("时效窗配置不可解析" in r.getMessage() for r in caplog.records)

    async def test_zulu_suffix_timestamp_is_parsed(self) -> None:
        """``Z`` 结尾时间戳（装机实证形态：task.ended_at=2026-10-28T07:36:19Z）
        必须被解析——不解析 = 时刻不可判定 = 候选永不被补发。"""
        state = _StateCap(
            [
                _parent_row(),
                _child_row("completed", **{"task.ended_at": "2026-10-28T12:00:00Z"}),
            ]
        )
        bus = _BusCap()

        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert len(bus.calls) == 1
        assert report[0]["terminal_at"].startswith("2026-10-28T12:00:00")

    async def test_naive_timestamp_is_read_as_utc(self) -> None:
        """无时区时间戳按 UTC 解读（仓内口径带时区；无时区形态只出现在外部
        写入的历史数据——按 UTC 读不丢候选）。"""
        state = _StateCap(
            [
                _parent_row(),
                _child_row("completed", **{"task.ended_at": "2026-10-28T12:00:00"}),
            ]
        )
        bus = _BusCap()

        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

        assert len(bus.calls) == 1
        assert report[0]["replayed"] is True



# ═══════════════════════════════════════════════════════════
# 状态键治理三刀（ADR 2026-11-02-state-key-governance）
# ═══════════════════════════════════════════════════════════


async def test_no_run_evidence_running_task_is_stopped() -> None:
    """第五刀：无 run 证据的未决残留收束。任务提交后、执行起手前崩溃（或 run
    簿记从未落库）→ 投影 running × 行内无 run_id 键 × 血缘锚在 → 裁 stopped +
    failure_class=run_aborted（不触 09-28 红线：不托底 completed）。"""
    child = _child_row("running")
    child.pop("task.ended_at", None)  # 从未真正起跑，无终局时刻
    state = _StateCap([child])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    assert {
        "pipeline_id": "child-1",
        "fields": {
            "task.status": "stopped",
            "task.failure_class": "run_aborted",
        },
    } in state.updates
    assert [r["kind"] for r in report] == ["no_run_evidence_stopped"]


async def test_no_run_evidence_pending_task_is_stopped() -> None:
    """第五刀 pending 形态：提交挂号后立即崩溃，投影停在 pending → 同裁 stopped。"""
    child = _child_row("pending")
    child.pop("task.ended_at", None)
    state = _StateCap([child])

    await reconcile.reconcile_startup(state, _RunsCap())

    assert {
        "pipeline_id": "child-1",
        "fields": {
            "task.status": "stopped",
            "task.failure_class": "run_aborted",
        },
    } in state.updates


async def test_no_run_evidence_with_run_id_key_is_not_adjudicated() -> None:
    """防护形态：行内 run_id 键在场（至少跑过一轮）× 簿记扫描未命中 → 可能是
    五态分页截断漏扫，宁可漏裁不误裁——不裁决（现状跳过）。"""
    child = _child_row("running", **{"run_id": "run_seen_before"})
    child.pop("task.ended_at", None)
    state = _StateCap([child])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    assert [r for r in report if r.get("kind") == "no_run_evidence_stopped"] == []
    assert state.updates == [], "有 run 证据的行不得被第五刀触碰"


async def test_no_run_evidence_terminal_task_is_not_adjudicated() -> None:
    """终态投影（completed/failed）不在第五刀域——终态是评估闸门落的事实。"""
    child = _child_row("completed")
    state = _StateCap([child])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    assert [r for r in report if r.get("kind") == "no_run_evidence_stopped"] == []


async def test_ghost_owned_registration_is_swept() -> None:
    """第六刀：子管道行已不存在（已删/从未出生）→ 幽灵 owned 登记键与挂号键
    一并清 null（含 24h 超窗积压挂号——补发无对象，回收）。"""
    ghost_parent = {
        "pipeline_id": "parent-1",
        "thread_id": "thread-parent",
        "task.owned.ghost-1.title": "已删任务",
        "task.owned.ghost-1.created_at": _NOW_ISO,
        "task.subtasks_pending.ghost-1": _NOW_ISO,
    }
    state = _StateCap([ghost_parent])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    swept = [r for r in report if r["kind"] == "ghost_owned_swept"]
    assert len(swept) == 1
    assert swept[0]["parent_pipeline_id"] == "parent-1"
    updates = [u for u in state.updates if u.get("pipeline_id") == "parent-1"]
    assert updates, "幽灵登记必须有清键写面"
    fields = updates[-1]["fields"]
    assert fields["task.owned.ghost-1.title"] is None
    assert fields["task.owned.ghost-1.created_at"] is None
    assert fields["task.subtasks_pending.ghost-1"] is None


async def test_live_owned_registration_is_not_swept() -> None:
    """子管道行在场 → 登记声明合法存活，不清理（面板第二趟出口数据源）。

    注：child-1 running 无 run_id 键会被第五刀收束（独立合法行为），本测试
    只断 owned 清理面。"""
    parent = _parent_row()
    child = _child_row("running")
    state = _StateCap([parent, child])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    assert [r for r in report if r["kind"] == "ghost_owned_swept"] == []
    owned_updates = [
        u for u in state.updates
        if u.get("pipeline_id") == "parent-1"
        and any(k.startswith("task.owned.") for k in u["fields"])
    ]
    assert owned_updates == [], "活登记不得被清"


async def test_tombstoned_owned_registration_is_not_rewritten() -> None:
    """全 null 墓碑（历史清理后残留键名）→ 不重复写（幂等省写）。"""
    tomb = {
        "pipeline_id": "parent-2",
        "thread_id": "thread-2",
        "task.owned.gone-1.title": None,
        "task.subtasks_pending.gone-1": None,
    }
    state = _StateCap([tomb])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    assert [r for r in report if r["kind"] == "ghost_owned_swept"] == []
    assert state.updates == []


async def test_legacy_timeout_status_migrates_to_failed() -> None:
    """第七刀：timeout 是无写点死枚举值（超时实际经 run 署名 → failed），
    历史残留行迁移 failed 干净参与后续判定。"""
    child = _child_row("timeout")
    state = _StateCap([child])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    migrated = [r for r in report if r["kind"] == "legacy_timeout_migrated"]
    assert len(migrated) == 1
    assert migrated[0]["migrated"] is True
    assert {
        "pipeline_id": "child-1",
        "fields": {"task.status": "failed"},
    } in state.updates


# ═══════════════════════════════════════════════════════════
# 判据统一与补发接冒泡（ADR 2026-11-06
# task-settlement-notify-guard-identity-and-lineage-bubble D1/D3/D4/D5）
# ═══════════════════════════════════════════════════════════


async def test_replay_bubbles_to_session_ancestor_when_direct_parent_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⑭ 补发接冒泡（D3/D4）：直接父是真终态任务 → 补发目标冒泡到会话祖先；
    ``wakeup_target_pipeline_id`` 写入 tags（``parent_pipeline_id`` 恒=直接
    父不变）；送达才清直接父挂号——不接冒泡会把终态父任务重新唤醒（回归
    在线守卫要防的原缺陷）。"""
    import logging

    session = {
        "pipeline_id": "session-1",
        "thread_id": "thread-session",
    }
    mid = {
        "pipeline_id": "parent-1",
        "thread_id": "parent-1",
        "task.id": "parent-1",
        "task.goal": "中间任务",
        "task.status": "completed",
        "task.subtasks_pending.child-1": _NOW_ISO,
        "lineage.parent_pipeline_id": "session-1",
    }
    child = _child_row("completed")
    state = _StateCap([session, mid, child])
    bus = _BusCap()

    with caplog.at_level(logging.WARNING):
        report = await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert len(bus.calls) == 1
    assert bus.calls[0]["tags"]["wakeup_target_pipeline_id"] == "session-1", (
        "补发目标 = 冒泡终点的会话祖先（终态中间任务不得被重新唤醒）"
    )
    assert bus.calls[0]["tags"]["parent_pipeline_id"] == "parent-1"
    assert {
        "pipeline_id": "parent-1",
        "fields": {"task.subtasks_pending.child-1": None},
    } in state.updates, "送达才清直接父挂号"
    entry = next(r for r in report if r["kind"] == "unreceipted_terminal_replay")
    assert entry["replayed"] is True
    assert entry["receipt_written"] is True


async def test_replay_structural_dead_end_receipts_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⑭ 对照（D4）：补发冒泡遇结构性死路（链顶终态任务）→ 不发事件、
    清直接父挂号 + WARNING（处置非漏发，重试同一条死路是噪声）。"""
    import logging

    mid = {
        "pipeline_id": "parent-1",
        "thread_id": "parent-1",
        "task.id": "parent-1",
        "task.goal": "中间任务",
        "task.status": "completed",
        "task.subtasks_pending.child-1": _NOW_ISO,
        # 链顶终态任务：无父键
    }
    child = _child_row("completed")
    state = _StateCap([mid, child])
    bus = _BusCap()

    with caplog.at_level(logging.WARNING):
        await reconcile.reconcile_startup(state, _RunsCap(), bus)

    assert bus.calls == []
    assert {
        "pipeline_id": "parent-1",
        "fields": {"task.subtasks_pending.child-1": None},
    } in state.updates, "死路是处置：清挂号"
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("死路" in m or "冒泡" in m for m in warned), "死路必须留痕"


async def test_task_id_only_row_is_not_swept_as_residue() -> None:
    """⑮ 534ef18c1ba8 型（有 task.id 无 lineage.*）真任务：不进清扫域
    （清扫对象 = 非任务管道行）；终态投影 × 无簿记也不构成分歧——零写零清。"""
    orphan = {
        "pipeline_id": "t-orphan",
        "task.id": "t-orphan",
        "task.goal": "真任务",
        "task.status": "completed",
    }
    state = _StateCap([orphan])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    assert [r for r in report if r["kind"] == "non_task_residue_cleared"] == [], (
        "task.id 非空的行是真任务管道，不得被残留清扫刀触碰"
    )
    assert state.updates == []


async def test_residue_sweep_clears_failure_annotation_keys() -> None:
    """⑯ 清扫键集扩展（D5 N9 审计）：task.error/failure_class/failure_reason
    与 status/ended_at 一并置 null——残留自键不清会让会话永久命中任务判据
    （is_task_state），伪任务行永不退场。"""
    poisoned = {
        "pipeline_id": "session-1",
        "thread_id": "thread-1",
        "task.status": "failed",
        "task.ended_at": _NOW_ISO,
        "task.error": "评估未通过",
        "task.failure_class": "run_aborted",
        "task.failure_reason": "运行终止",
    }
    state = _StateCap([poisoned])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    swept = [r for r in report if r["kind"] == "non_task_residue_cleared"]
    assert len(swept) == 1
    update = next(u for u in state.updates if u["pipeline_id"] == "session-1")
    for key in (
        "task.status",
        "task.ended_at",
        "task.error",
        "task.failure_class",
        "task.failure_reason",
    ):
        assert update["fields"][key] is None, f"{key} 必须一并清 null"


async def test_error_only_residue_on_session_is_swept() -> None:
    """⑯ 触发面扩展：仅失败注记残留（无 task.status）的会话行同样进清扫域
    （任意残留自键非空即触发——is_task_state 命中即伪任务行）。"""
    poisoned = {
        "pipeline_id": "session-2",
        "thread_id": "thread-2",
        "task.error": "残留失败注记",
    }
    state = _StateCap([poisoned])

    report = await reconcile.reconcile_startup(state, _RunsCap())

    swept = [r for r in report if r["kind"] == "non_task_residue_cleared"]
    assert len(swept) == 1
    update = next(u for u in state.updates if u["pipeline_id"] == "session-2")
    for key in (
        "task.status",
        "task.ended_at",
        "task.error",
        "task.failure_class",
        "task.failure_reason",
    ):
        assert update["fields"][key] is None
