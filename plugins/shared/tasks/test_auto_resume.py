# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""排空任务自动恢复契约测试（2026-11-11 用户裁定）。

背景：``drain_and_exit75``（水位自愈 / 手动 G8 / cdylib 集变更）把在途 run
排空为 ``run_status=suspended`` 并落 ``suspended_reason``；重启后 reconcile
把任务投影复位 ``stopped``，此前只能人工 ``task_manage continue`` 续跑。

本文件钉：

- 归因判别纯函数（三个排空触发源命中、非排空停靠不命中）；
- 候选筛选守卫：run 仍 suspended、排空归因、任务管道身份门、真终态排除；
- 恢复逐走 ``_resume_task_pipeline``（run 簿记/续跑轮由内核裁决）+ 单次上限；
- 装载调度只在无在飞快照时建一次。

测试替身仅外部依赖（pipeline-state 能力 + resume 内核通道），筛选与编排
全部走真实实现。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# 与 test_task_terminal_ws 同款隔离：tasks 目录 + 共享根入 sys.path，
# 平铺裸名逐出（防解析到别家同名模块）。
_HERE = Path(__file__).resolve().parent
_SHARED_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(1, str(_SHARED_ROOT))
_EVICT_NAMES = (
    "storage",
    "service",
    "server",
    "http_api",
    "events",
    "state_fields",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    # 重申路径序：收集期其他插件测试模块的 sys.path.insert(0) 会把本目录
    # 顶出首位，裸名逐出后重解析会命中别家同名模块（与 llm 车道同进程
    # 合跑实测 import server 解析到 llm 的 server.py，AttributeError）。
    for p in (str(_HERE), str(_SHARED_ROOT)):
        while p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, str(_HERE))
    sys.path.insert(1, str(_SHARED_ROOT))
    yield
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


import http_api  # noqa: E402 — sys.path 垫根后裸名导入


def _drained_row(**extra: Any) -> dict:
    row = {
        "pipeline_id": "pipe_child",
        "thread_id": "thread-1",
        "task.id": "pipe_child",
        "task.goal": "口径改造",
        "task.status": "stopped",
        "lineage.parent_pipeline_id": "pipe_parent",
        "run_status": "suspended",
        "suspended_reason": "memory watermark self-heal: working set 持续超阈值",
    }
    row.update(extra)
    return row


class _FakeStateCap:
    """pipeline-state 能力替身：list 返固定行，记录 list/update 调用。"""

    def __init__(self, rows: list[Any] | Exception) -> None:
        """rows 刻意可含非 dict 行（覆盖「非行载荷」边界），故不收窄到 dict。"""
        self._rows = rows
        self.list_calls = 0
        self.updates: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
        if method == "update":
            self.updates.append(params)
            return {"ok": True}
        if method != "list":
            raise AssertionError(f"未预期的能力调用: {method}")
        self.list_calls += 1
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


def _patch_state(monkeypatch: pytest.MonkeyPatch, state: _FakeStateCap) -> None:
    monkeypatch.setattr(http_api, "_capability", lambda _name: state)


def _patch_resume(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, tuple[bool, bool, str]] | None = None,
) -> list[str]:
    """替身 resume 内核通道：记录被恢复的 pipeline_id（flags 侧录 allow 参数）。"""
    calls: list[str] = []
    flags: list[bool] = []

    async def _fake_resume(
        task_id: str, user_id: str = "", allow_crash_swept: bool = False
    ) -> tuple[bool, bool, str]:
        calls.append(task_id)
        flags.append(allow_crash_swept)
        if outcomes and task_id in outcomes:
            return outcomes[task_id]
        return True, True, ""

    monkeypatch.setattr(http_api, "_resume_task_pipeline", _fake_resume)
    monkeypatch.setattr(http_api, "_AUTO_RESUME_STAGGER_SECS", 0)
    _patch_resume.flags = flags  # type: ignore[attr-defined]
    return calls


# ════════════════════════════════════════════════════════════
# 归因判别纯函数
# ════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason",
    [
        "memory watermark self-heal: working set 持续超阈值",
        "memory watermark self-heal: private bytes 持续超阈值",
        "POST /api/v1/system/restart",
        "plugin_watcher: InProcess(cdylib) plugin set changed",
    ],
)
def test_drain_markers_match_all_kernel_sources(reason):
    """内核三个排空触发源的归因字串都命中（水位/G8 手动/watcher）。"""
    assert http_api._is_drain_suspend_reason(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "   ",
        "llm_fail_loop",
        "user stopped",
        "approval pending",
        "任务被用户取消",
    ],
)
def test_non_drain_reasons_are_not_auto_resumed(reason):
    """非排空停靠（LLM 失败循环/用户停止/审批挂起）不命中。"""
    assert not http_api._is_drain_suspend_reason(reason)


def test_drain_marker_matching_is_case_insensitive():
    assert http_api._is_drain_suspend_reason("MEMORY WATERMARK SELF-HEAL: x")


# ════════════════════════════════════════════════════════════
# 候选筛选 + 恢复编排
# ════════════════════════════════════════════════════════════


def test_resumes_only_suspended_drained_task_pipelines(monkeypatch):
    """六个诱饵只恢复真候选：非排空归因/已跑/会话行/真终态/空 id 全跳过。"""
    rows = [
        _drained_row(pipeline_id="pipe_ok", **{"task.id": "pipe_ok"}),
        _drained_row(
            pipeline_id="pipe_user_stop",
            **{"task.id": "pipe_user_stop"},
            suspended_reason="llm_fail_loop",
        ),
        _drained_row(
            pipeline_id="pipe_running",
            **{"task.id": "pipe_running"},
            run_status="running",
        ),
        # 会话主管道：无 task.id / lineage 键，即使带排空归因也不是任务
        {
            "pipeline_id": "pipe_session",
            "run_status": "suspended",
            "suspended_reason": "POST /api/v1/system/restart",
        },
        _drained_row(
            pipeline_id="pipe_failed",
            **{"task.id": "pipe_failed"},
            **{"task.status": "failed"},
        ),
        # 任务身份齐备但 pipeline_id 空：无恢复坐标，跳过
        _drained_row(pipeline_id="", **{"task.id": "ghost"}),
        _drained_row(pipeline_id="", **{"task.id": ""}, **{"lineage.parent_pipeline_id": ""}),
        "非行载荷（list 里混入非 dict）",
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert calls == ["pipe_ok"]
    assert resumed == [{"pipeline_id": "pipe_ok", "dispatched": True}]


def test_completed_task_is_skipped_even_if_drained(monkeypatch):
    """真终态 completed 跳过：恢复语义留给上级带指令返工。"""
    rows = [
        _drained_row(pipeline_id="pipe_done", **{"task.id": "pipe_done"}),
    ]
    rows[0]["task.status"] = "completed"
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)

    assert asyncio.run(http_api.auto_resume_drained_tasks()) == []
    assert calls == []


def test_parent_waiting_active_child_is_not_resumed(monkeypatch):
    """父挂起等活跃子任务 → 跳过：子终态通知会唤醒父，重复派发=并行轮。"""
    rows = [
        _drained_row(
            pipeline_id="pipe_parent",
            **{
                "task.id": "pipe_parent",
                "task.subtasks_pending.pipe_child": "2026-11-11T08:30:28+00:00",
            },
        ),
        {
            "pipeline_id": "pipe_child",
            "task.id": "pipe_child",
            "task.status": "running",
            "run_status": "running",
            "lineage.parent_pipeline_id": "pipe_parent",
        },
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)

    assert asyncio.run(http_api.auto_resume_drained_tasks()) == []
    assert calls == []


def test_parent_with_terminal_child_is_resumed(monkeypatch):
    """子 run 已终态（即使 task 投影仍是 running）→ 父可恢复，不被活跃门误拦。"""
    rows = [
        _drained_row(
            pipeline_id="pipe_parent",
            **{
                "task.id": "pipe_parent",
                "task.subtasks_pending.pipe_child": "2026-11-11T08:30:28+00:00",
            },
        ),
        {
            "pipeline_id": "pipe_child",
            "task.id": "pipe_child",
            "task.status": "running",
            "run_status": "completed",
            "lineage.parent_pipeline_id": "pipe_parent",
        },
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert calls == ["pipe_parent"]
    assert [r["pipeline_id"] for r in resumed] == ["pipe_parent"]


def test_auto_resume_limit_bounds_single_boot(monkeypatch):
    """单次启动上限：超出部分不拉起（留 WARNING），不静默丢弃全部。"""
    rows = [
        _drained_row(pipeline_id=f"pipe_{i}", **{"task.id": f"pipe_{i}"})
        for i in range(5)
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)
    monkeypatch.setattr(http_api, "_AUTO_RESUME_LIMIT", 2)

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert calls == ["pipe_0", "pipe_1"]
    assert [r["pipeline_id"] for r in resumed] == ["pipe_0", "pipe_1"]


def test_rejected_resume_does_not_block_remaining(monkeypatch):
    """单个恢复被拒（如 task.status 真终态竞态）只告警，其余继续。"""
    rows = [
        _drained_row(pipeline_id="pipe_reject", **{"task.id": "pipe_reject"}),
        _drained_row(pipeline_id="pipe_next", **{"task.id": "pipe_next"}),
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(
        monkeypatch,
        outcomes={"pipe_reject": (False, False, "任务已失败，不可恢复执行")},
    )

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert calls == ["pipe_reject", "pipe_next"]
    assert [r["pipeline_id"] for r in resumed] == ["pipe_next"]


def test_state_read_failure_degrades_to_no_resume(monkeypatch):
    """读面故障：本轮不恢复（不能凭旧/空数据乱拉起），只告警。"""
    _patch_state(monkeypatch, _FakeStateCap(RuntimeError("state down")))
    calls = _patch_resume(monkeypatch)

    assert asyncio.run(http_api.auto_resume_drained_tasks()) == []
    assert calls == []


def test_non_list_state_payload_degrades(monkeypatch):
    calls = _patch_resume(monkeypatch)

    class _Weird:
        async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
            return {"not": "a list"}

    monkeypatch.setattr(http_api, "_capability", lambda _name: _Weird())
    assert asyncio.run(http_api.auto_resume_drained_tasks()) == []
    assert calls == []


# ════════════════════════════════════════════════════════════
# 崩溃清扫自动拉起（ADR 2026-11-12-crash-swept-running-tasks-auto-relaunch）
# ════════════════════════════════════════════════════════════


def _crash_row(**extra: Any) -> dict:
    """内核重启时在飞、被崩溃清扫署名的任务行（run_status=failed + crash_swept）。"""
    row = {
        "pipeline_id": "pipe_crash",
        "thread_id": "thread-1",
        "task.id": "pipe_crash",
        "task.goal": "在飞被打断的活",
        "task.status": "failed",
        "lineage.parent_pipeline_id": "pipe_parent",
        "run_status": "failed",
        "run_stop_reason": "crash_swept",
    }
    row.update(extra)
    return row


def test_crash_swept_running_tasks_are_relaunched_at_boot(monkeypatch):
    """重启前 running（被清扫署名）→ 自动拉起；真失败/会话行不拉。"""
    rows = [
        # 既有排空类照常恢复（两类共存）
        _drained_row(pipeline_id="pipe_drain", **{"task.id": "pipe_drain"}),
        # 崩溃署名行：本类恢复对象
        _crash_row(pipeline_id="pipe_crash", **{"task.id": "pipe_crash"}),
        # 重启前就真失败（真实归因，非清扫署名）：不拉
        _crash_row(
            pipeline_id="pipe_realfail",
            **{"task.id": "pipe_realfail"},
            run_stop_reason="llm_fail_loop",
        ),
        # 会话主管道即使被署名也不是任务：不拉
        {
            "pipeline_id": "pipe_session",
            "run_status": "failed",
            "run_stop_reason": "crash_swept",
        },
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert calls == ["pipe_drain", "pipe_crash"]
    assert _patch_resume.flags == [False, True]  # type: ignore[attr-defined]
    assert [r["pipeline_id"] for r in resumed] == ["pipe_drain", "pipe_crash"]


def test_crash_swept_success_keeps_running_and_clears_signature(monkeypatch):
    """拉起成功：任务保持 running（不写 failed），只清旧 run 的 crash_swept 署名。"""
    state = _FakeStateCap(
        [
            _crash_row(
                pipeline_id="pipe_crash",
                **{"task.id": "pipe_crash", "task.status": "running"},
            )
        ]
    )
    _patch_state(monkeypatch, state)
    calls = _patch_resume(monkeypatch)

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert calls == ["pipe_crash"]
    assert resumed == [{"pipeline_id": "pipe_crash", "dispatched": True}]
    assert state.updates == [
        {"pipeline_id": "pipe_crash", "fields": {"run_stop_reason": None}}
    ]


def test_crash_swept_relaunch_failure_projects_failed(monkeypatch):
    """拉起失败才投影 failed（failure_class=relaunch_failed）；署名保留供下次重试。"""
    state = _FakeStateCap(
        [
            _crash_row(
                pipeline_id="pipe_crash",
                **{"task.id": "pipe_crash", "task.status": "running"},
            )
        ]
    )
    _patch_state(monkeypatch, state)
    _patch_resume(
        monkeypatch,
        outcomes={"pipe_crash": (False, False, "dispatcher not ready")},
    )

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert resumed == []
    fields = state.updates[0]["fields"]
    assert fields["task.status"] == "failed"
    assert fields["task.failure_class"] == "relaunch_failed"
    assert "dispatcher not ready" in fields["task.failure_reason"]
    assert fields["task.ended_at"]


def test_crash_candidates_respect_guards(monkeypatch):
    """真终态 completed / 活跃子任务门 / 空坐标：崩溃类同受守卫约束。"""
    rows = [
        _crash_row(
            pipeline_id="pipe_done",
            **{"task.id": "pipe_done", "task.status": "completed"},
        ),
        _crash_row(
            pipeline_id="pipe_parent",
            **{
                "task.id": "pipe_parent",
                "task.subtasks_pending.pipe_child": "2026-11-12T04:00:00+00:00",
            },
        ),
        {
            "pipeline_id": "pipe_child",
            "task.id": "pipe_child",
            "task.status": "running",
            "run_status": "running",
            "lineage.parent_pipeline_id": "pipe_parent",
        },
        _crash_row(pipeline_id="", **{"task.id": "ghost"}),
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)

    assert asyncio.run(http_api.auto_resume_drained_tasks()) == []
    assert calls == []


def test_crash_and_drain_share_boot_limit(monkeypatch):
    """排空 + 崩溃两类共用单次上限（防几十任务同时抢配额）。"""
    rows = [
        _drained_row(pipeline_id="pipe_drain", **{"task.id": "pipe_drain"}),
        _crash_row(pipeline_id="pipe_crash", **{"task.id": "pipe_crash"}),
    ]
    _patch_state(monkeypatch, _FakeStateCap(rows))
    calls = _patch_resume(monkeypatch)
    monkeypatch.setattr(http_api, "_AUTO_RESUME_LIMIT", 1)

    asyncio.run(http_api.auto_resume_drained_tasks())

    assert calls == ["pipe_drain"]


class _FakeGuardCap:
    """守卫级替身：list 返固定行；resume_pipeline 记录 overlay 并确认派发。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.resume_calls: list[dict] = []

    async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
        if method == "list":
            return self._rows
        if method == "resume_pipeline":
            self.resume_calls.append(params)
            return {"run_id": "run_rel", "dispatched": True}
        raise AssertionError(f"未预期的能力调用: {method}")


def _patch_guard(monkeypatch: pytest.MonkeyPatch, rows: list[Any]) -> _FakeGuardCap:
    cap = _FakeGuardCap(rows)
    monkeypatch.setattr(http_api, "_capability", lambda _name: cap)
    return cap


def test_resume_guard_admits_crash_swept_failed_with_flag_and_marker(monkeypatch):
    """failed 守卫窄放行：flag + 署名双条件齐备才派发，overlay 清署名与终态残键。"""
    cap = _patch_guard(
        monkeypatch,
        [_crash_row(pipeline_id="pipe_crash", **{"task.id": "pipe_crash"})],
    )

    ok, dispatched, error = asyncio.run(
        http_api._resume_task_pipeline(
            "pipe_crash", user_id="task_service:auto_resume", allow_crash_swept=True
        )
    )

    assert ok
    assert dispatched
    assert not error
    overlay = cap.resume_calls[0]["state_overlay"]
    assert overlay["task.status"] == "running"
    assert overlay["run_stop_reason"] is None, "署名随派发清除（防跨 run 残留误读）"
    assert overlay["task.ended_at"] is None


def test_resume_guard_still_refuses_failed_without_narrow_conditions(monkeypatch):
    """三缺一即拒：无 flag / 有 flag 无署名 / completed（即使署名）。"""
    rows = [
        _crash_row(pipeline_id="pipe_crash", **{"task.id": "pipe_crash"}),
    ]
    # 无 flag：默认路径照旧拒绝（幽灵续跑防线原样）
    _patch_guard(monkeypatch, rows)
    ok, _, error = asyncio.run(http_api._resume_task_pipeline("pipe_crash"))
    assert not ok
    assert "不可恢复" in error
    # 有 flag 但归因非 crash_swept：拒绝
    _patch_guard(
        monkeypatch,
        [_crash_row(pipeline_id="pipe_crash", run_stop_reason="llm_fail_loop")],
    )
    ok, _, error = asyncio.run(
        http_api._resume_task_pipeline("pipe_crash", allow_crash_swept=True)
    )
    assert not ok
    assert "不可恢复" in error
    # completed 即使带署名与 flag：拒绝（返工走上级带指令）
    _patch_guard(
        monkeypatch,
        [_crash_row(pipeline_id="pipe_crash", **{"task.status": "completed"})],
    )
    ok, _, error = asyncio.run(
        http_api._resume_task_pipeline("pipe_crash", allow_crash_swept=True)
    )
    assert not ok
    assert "已完成" in error


# ════════════════════════════════════════════════════════════
# 旧停泊归因清理（2026-11-12 第 4 次实证）
# ════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason",
    [
        "crash_swept",
        "CRASH_SWEPT",
        "memory watermark self-heal: working set 持续超阈值",
        "POST /api/v1/system/restart",
        "plugin_watcher: InProcess(cdylib) plugin set changed",
    ],
)
def test_parking_stop_reason_matches_parking_attributions(reason):
    """停泊词表：崩溃清扫署名 + 排空/水位自愈归因命中。"""
    assert http_api._is_parking_stop_reason(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "   ",
        "llm_fail_loop",
        "duplicate_loop",
        "tool_fail_loop",
        "user_requested",
    ],
)
def test_parking_stop_reason_keeps_real_failures(reason):
    """真实失败/用户停止归因不在停泊词表——恢复链不得覆盖失败史。"""
    assert not http_api._is_parking_stop_reason(reason)


def test_resume_clears_stale_crash_swept_outside_failed_bypass(monkeypatch):
    """task.status=running 的 crash_swept 行不走 failed 窄放行，恢复仍须清署名。

    实证行：``task.status=running`` × ``run_status=suspended`` ×
    ``run_stop_reason=crash_swept``——旧实现只把清键放在 failed 窄放行内，
    该行按普通挂起恢复成功，署名残留被面板误读为当前 run 归因。
    """
    cap = _patch_guard(
        monkeypatch,
        [
            _drained_row(
                pipeline_id="pipe_stale",
                **{"task.id": "pipe_stale", "task.status": "running"},
                run_stop_reason="crash_swept",
            )
        ],
    )

    ok, dispatched, error = asyncio.run(
        http_api._resume_task_pipeline(
            "pipe_stale", user_id="task_service:auto_resume", allow_crash_swept=False
        )
    )

    assert ok
    assert dispatched
    assert not error
    overlay = cap.resume_calls[0]["state_overlay"]
    assert overlay["run_stop_reason"] is None, "旧停泊署名随新 run 清除"


def test_resume_clears_drain_marker_in_stop_reason(monkeypatch):
    """run_stop_reason 本身带排空/水位归因字串时同样按停泊面清除。"""
    cap = _patch_guard(
        monkeypatch,
        [
            _drained_row(
                pipeline_id="pipe_drain_stop",
                **{"task.id": "pipe_drain_stop"},
                run_stop_reason="memory watermark self-heal: private bytes 持续超阈值",
            )
        ],
    )

    ok, _dispatched, error = asyncio.run(
        http_api._resume_task_pipeline(
            "pipe_drain_stop", user_id="task_service:auto_resume"
        )
    )

    assert ok
    assert not error
    assert cap.resume_calls[0]["state_overlay"]["run_stop_reason"] is None


@pytest.mark.parametrize("stop_reason", ["llm_fail_loop", "duplicate_loop", "user_requested"])
def test_resume_keeps_real_failure_attribution(monkeypatch, stop_reason):
    """真实失败/用户停止归因不被恢复链误清（只清停泊面）。"""
    cap = _patch_guard(
        monkeypatch,
        [
            _drained_row(
                pipeline_id="pipe_realfail",
                **{"task.id": "pipe_realfail"},
                run_stop_reason=stop_reason,
            )
        ],
    )

    ok, _dispatched, error = asyncio.run(
        http_api._resume_task_pipeline(
            "pipe_realfail", user_id="task_service:auto_resume"
        )
    )

    assert ok
    assert not error
    overlay = cap.resume_calls[0]["state_overlay"]
    assert "run_stop_reason" not in overlay, "真实归因不得被恢复清键覆盖"


def test_auto_resume_clears_parking_and_keeps_real_attribution(monkeypatch):
    """自动恢复端到端：停泊署名清除、真实失败归因保留（两类同批候选）。"""
    rows = [
        _drained_row(
            pipeline_id="pipe_parking",
            **{"task.id": "pipe_parking", "task.status": "running"},
            run_stop_reason="crash_swept",
        ),
        _drained_row(
            pipeline_id="pipe_realfail",
            **{"task.id": "pipe_realfail"},
            run_stop_reason="llm_fail_loop",
        ),
    ]
    cap = _patch_guard(monkeypatch, rows)
    monkeypatch.setattr(http_api, "_AUTO_RESUME_STAGGER_SECS", 0)

    resumed = asyncio.run(http_api.auto_resume_drained_tasks())

    assert [r["pipeline_id"] for r in resumed] == ["pipe_parking", "pipe_realfail"]
    overlays = {call["pipeline_id"]: call["state_overlay"] for call in cap.resume_calls}
    assert overlays["pipe_parking"]["run_stop_reason"] is None
    assert "run_stop_reason" not in overlays["pipe_realfail"]


# ════════════════════════════════════════════════════════════
# 装载调度（延迟一次，不重复建）
# ════════════════════════════════════════════════════════════


def test_schedule_drain_auto_resume_once_after_delay(monkeypatch):
    """装载调度：延迟后调用一次恢复；二次调度不重复建在飞快照。"""
    import server as srv  # noqa: PLC0415 — 隔离 fixture 逐出后导入
    # 逐出重建后 server 持有的 http_api 是新实例，补丁必须打在它的引用上
    srv_http_api = srv.http_api

    calls: list[int] = []

    async def _fake_auto_resume() -> list[dict[str, Any]]:
        calls.append(1)
        return [{"pipeline_id": "pipe_ok", "dispatched": True}]

    monkeypatch.setattr(srv_http_api, "auto_resume_drained_tasks", _fake_auto_resume)
    monkeypatch.setattr(srv, "_AUTO_RESUME_START_DELAY_SECS", 0)
    monkeypatch.setattr(srv, "_auto_resume_task", None)

    async def _main() -> None:
        srv._schedule_drain_auto_resume()
        srv._schedule_drain_auto_resume()  # 在飞快照在位：不重复建
        handle = srv._auto_resume_task
        assert handle is not None
        await asyncio.wait([handle], timeout=1.0)
        assert handle.done()
        assert not handle.cancelled()
        assert handle.exception() is None

    asyncio.run(_main())
    assert calls == [1]


def test_schedule_swallows_auto_resume_failure(monkeypatch):
    """恢复调度异常不外抛（自愈兜底不阻断任务服务），空结果不打完成日志。"""
    import server as srv  # noqa: PLC0415

    srv_http_api = srv.http_api

    async def _boom() -> list[dict[str, Any]]:
        raise RuntimeError("state down")

    monkeypatch.setattr(srv_http_api, "auto_resume_drained_tasks", _boom)
    monkeypatch.setattr(srv, "_AUTO_RESUME_START_DELAY_SECS", 0)
    monkeypatch.setattr(srv, "_auto_resume_task", None)

    async def _main() -> None:
        srv._schedule_drain_auto_resume()
        handle = srv._auto_resume_task
        assert handle is not None
        await asyncio.wait([handle], timeout=1.0)
        assert handle.done()
        assert handle.exception() is None  # 异常在 _run 内消化

    asyncio.run(_main())


def test_schedule_with_no_candidates_logs_nothing(monkeypatch):
    """无排空任务：正常结束，不产生恢复记录。"""
    import server as srv  # noqa: PLC0415

    srv_http_api = srv.http_api

    async def _empty() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(srv_http_api, "auto_resume_drained_tasks", _empty)
    monkeypatch.setattr(srv, "_AUTO_RESUME_START_DELAY_SECS", 0)
    monkeypatch.setattr(srv, "_auto_resume_task", None)

    async def _main() -> None:
        srv._schedule_drain_auto_resume()
        handle = srv._auto_resume_task
        assert handle is not None
        await asyncio.wait([handle], timeout=1.0)
        assert handle.done()
        assert handle.exception() is None

    asyncio.run(_main())


def test_unload_cancels_pending_auto_resume(monkeypatch):
    """unload 取消未完成的恢复调度（防卸载后残留任务持有能力句柄）。"""
    import server as srv  # noqa: PLC0415

    srv_http_api = srv.http_api

    async def _slow() -> list[dict[str, Any]]:
        await asyncio.sleep(10)
        return []

    monkeypatch.setattr(srv_http_api, "auto_resume_drained_tasks", _slow)
    monkeypatch.setattr(srv, "_AUTO_RESUME_START_DELAY_SECS", 0)

    async def _main() -> None:
        import service_access  # noqa: PLC0415

        srv._schedule_drain_auto_resume()
        handle = srv._auto_resume_task
        assert handle is not None
        await srv._on_unload({})
        assert srv._auto_resume_task is None
        await asyncio.sleep(0)
        assert handle.cancelled() or handle.done()
        # 摘除正门注入的全局 service（避免污染同进程其他用例）
        service_access.set_task_service(None)

    asyncio.run(_main())
