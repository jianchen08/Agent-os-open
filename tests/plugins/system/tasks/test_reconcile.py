# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""任务域启动调和器测试（U13/D9 终态两写任务域半边）。

域界定（docs/working/U13终态两写域界定_20260906.md §二）：分歧行仲裁依据 =
pipeline_state.task.status 投影（谁持证据谁裁决）。断行为不断实现：

- classify_orphan_run 纯函数断 输入(runs 侧终态, 投影终态) → 调和裁决三值
  （completed 权威 / failed 对齐 / 不裁），参数化 ≥2 组有区分度输入。
- ADR 2026-09-14 翻案扩规：runs 侧权威终态 × 投影未决 → 裁决收束
  （completed → completed 补落+补通知；cancelled/failed → failed 对齐）；
  真最新 run 在飞（running）/可恢复（suspended）不裁。
- reconcile_startup 断 能力调用 → emit_domain 载荷与投影补落（合成假件只替
  内核能力通道——外部依赖）；healthy 行零调和、多 run 历史取最新不误伤。
- events 派生仲裁：run.failed + 投影 completed/cancelled 终态证据 → 不派生
  task_failed（防对账把 completed 覆写为 failed 的分歧放大）。
- 09-04 实录场景（stop→suspended→收尾）：suspended run + completed 投影 →
  completed 对齐（跨表终态一致）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "tasks"


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出同名裸模块，强制按本插件目录解析（与 test_tasks_http_api.py 同款）。"""
    d = str(_PLUGIN_DIR)
    shared_root = str(_PLUGIN_DIR.parents[1])
    _was_present = d in sys.path
    _added_shared_root = False
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    if shared_root not in sys.path:
        sys.path.insert(1, shared_root)
        _added_shared_root = True
    _evict_names = ("events", "reconcile", "task_types", "state_machine", "storage", "service")
    _evicted: dict[str, object] = {}
    for m in _evict_names:
        if m in sys.modules:
            _evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if _was_present:
        sys.path.insert(0, d)
    if _added_shared_root:
        sys.path.remove(shared_root)
    for m in _evict_names:
        if m in _evicted:
            sys.modules[m] = _evicted[m]
        else:
            sys.modules.pop(m, None)


@pytest.fixture
def mod():
    """插件模块经 fixture 注入 sys.path 后再 import（既有模式：函数内 import）。"""

    import events as task_events  # noqa: PLC0415
    import reconcile  # noqa: PLC0415

    class _Mod:
        pass

    m = _Mod()
    m.events = task_events
    m.reconcile = reconcile
    return m


class _FakeCapability:
    """fake 内核能力句柄：按 method 返回预置响应（未预置 → KeyError 语义）。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        if method not in self._responses:
            raise KeyError(f"unexpected capability method: {method}")
        resp = self._responses[method]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _run(status: str, pid: str, *, run_id: str | None = None, created_at: str = "2026-09-06T08:00:00Z") -> dict[str, Any]:
    """pipeline-runs.list 行形状（PipelineRunInfo serde JSON，capability_router.rs:764-766）。"""
    return {
        "run_id": run_id or f"run-{pid}-{status}",
        "status": status,
        "created_at": created_at,
        "ended_at": None,
        "pipeline_id": pid,
        "thread_id": f"thread-{pid}",
    }


def _task_row(pid: str, status: str) -> dict[str, Any]:
    """pipeline-state.list 行形状（任务管道摘要，events._task_row 同款）。"""
    return {
        "pipeline_id": pid,
        "thread_id": f"thread-{pid}",
        "task.id": pid,
        "task.goal": "写周报",
        "task.status": status,
    }


def _caps(
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    update_error: Exception | None = None,
) -> tuple[_FakeCapability, _FakeCapability, _FakeCapability]:
    """组装 state / runs(service-registry) / bus 三个假件。"""
    state_responses: dict[str, Any] = {"list": rows, "update": {"ok": True}}
    if update_error is not None:
        state_responses["update"] = update_error
    state = _FakeCapability(state_responses)
    runs_cap = _FakeCapability(
        {
            "pipeline-runs.list": runs,
        }
    )
    bus = _FakeCapability({"emit_domain": {"ok": True}})
    return state, runs_cap, bus


# ═══════════════════════════════════════════════════════════
# classify_orphan_run 纯函数（仲裁三值）
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        ("failed", "completed"),
        ("suspended", "completed"),
    ],
)
def test_classify_completed_projection_wins(mod, run_status: str, task_status: str) -> None:
    # 有投影终态 completed → completed 权威（评估闸门证据不可被运行域记账推翻）
    assert mod.reconcile.classify_orphan_run(run_status, task_status) == "completed"


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        ("failed", "running"),
        ("failed", "pending"),
        ("failed", "evaluating"),
        ("failed", "pending_evaluation"),
        ("failed", ""),
    ],
)
def test_classify_no_terminal_evidence_maps_to_failed(mod, run_status: str, task_status: str) -> None:
    # 无终态证据才判 failed（崩溃残留：任务没做完，reap 已判 failed → 对齐）
    assert mod.reconcile.classify_orphan_run(run_status, task_status) == "failed"


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        # 在飞 run 不可判（插件热重载时 on_load 与运行中并存）——一律不裁
        ("running", "completed"),
        ("running", "running"),
        # 一致行不裁
        ("completed", "completed"),
        ("failed", "failed"),
        ("suspended", "running"),  # 正常挂起（可 resume）
        # 用户侧终态证据（stopped/timeout/cancelled）保守不裁
        ("failed", "stopped"),
        ("failed", "timeout"),
        ("suspended", "cancelled"),
    ],
)
def test_classify_healthy_or_ambiguous_rows_never_touched(mod, run_status: str, task_status: str) -> None:
    assert mod.reconcile.classify_orphan_run(run_status, task_status) is None


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        # ADR 2026-09-14 翻案扩规：runs 侧权威终态 completed × 投影未决 →
        # completed 裁决（run 真做完而收尾投影写丢失；评估闸门对死 run 永不再
        # 结论，保守不裁 = 幽灵「执行中」永久悬挂——BUG-2b 面板幽灵残留根因）
        ("completed", "running"),
        ("completed", "pending"),
        ("completed", "evaluating"),
        ("completed", "pending_evaluation"),
        ("completed", ""),
    ],
)
def test_classify_authoritative_run_completed_adjudicates_completed(
    mod, run_status: str, task_status: str
) -> None:
    assert mod.reconcile.classify_orphan_run(run_status, task_status) == "completed"


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        # 用户取消 × 投影未决（取消时投影写丢失）→ failed 对齐（任务域裁决
        # 词汇无 cancelled，终态归 failed；同 runs failed 分支语义）
        ("cancelled", "running"),
        ("cancelled", "pending"),
        ("cancelled", "evaluating"),
        ("cancelled", "pending_evaluation"),
        ("cancelled", ""),
    ],
)
def test_classify_cancelled_run_maps_to_failed(mod, run_status: str, task_status: str) -> None:
    assert mod.reconcile.classify_orphan_run(run_status, task_status) == "failed"


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        # 真最新 run 在飞（running）或可恢复（suspended）× 未决投影 → 不裁
        #（在飞 run 与幽灵无法单次扫描区分；suspended 等待交互可恢复）
        ("running", "running"),
        ("running", "pending"),
        ("running", ""),
        ("suspended", "running"),
        ("suspended", "pending"),
    ],
)
def test_classify_inflight_or_resumable_run_never_adjudicated(
    mod, run_status: str, task_status: str
) -> None:
    assert mod.reconcile.classify_orphan_run(run_status, task_status) is None


def test_classify_output_domain_and_authority_invariant(mod) -> None:
    # 性质断言：裁决值域封闭于 {completed, failed, None}；completed 裁决来源
    # 二值封闭——投影 completed 权威（runs failed/suspended）∨ runs 侧权威
    # completed × 未决投影（ADR 2026-09-14），其余组合造不出 completed
    for run_status in ("running", "suspended", "failed", "completed", "cancelled"):
        for task_status in ("running", "completed", "failed", "stopped", ""):
            verdict = mod.reconcile.classify_orphan_run(run_status, task_status)
            assert verdict in ("completed", "failed", None)
            if verdict == "completed":
                assert (
                    task_status == "completed" and run_status in ("failed", "suspended")
                ) or (
                    run_status == "completed"
                    and task_status in ("", "running", "pending", "evaluating", "pending_evaluation")
                )
    assert mod.reconcile.classify_orphan_run("completed", "completed") != "completed"


# ═══════════════════════════════════════════════════════════
# reconcile_startup 启动调和（行为：能力调用 → 派生/补落）
# ═══════════════════════════════════════════════════════════


async def test_reconcile_derives_task_completed_for_disagreement(mod) -> None:
    # 分歧行：runs 说 failed（reap 误判）、投影说 completed（评估已过）→ 补派完成通知
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-a", "completed")],
        runs=[_run("failed", "pipe-a")],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert [r["pipeline_id"] for r in reconciled] == ["pipe-a"]
    emitted = [c for c in bus.calls if c[0] == "emit_domain"]
    assert len(emitted) == 1
    assert emitted[0][1]["event"] == "task_completed"
    assert emitted[0][1]["tags"]["task_id"] == "pipe-a"
    # completed 权威：不补落投影（投影已是 completed）
    assert all(c[0] != "update" for c in state.calls)


async def test_reconcile_backfills_failed_projection_and_notifies(mod) -> None:
    # 崩溃残留：任务没做完（投影 running）+ reap 已判 failed → 对齐 failed + 补通知
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-b", "running")],
        runs=[_run("failed", "pipe-b")],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert [r["verdict"] for r in reconciled] == ["failed"]
    emitted = [c for c in bus.calls if c[0] == "emit_domain"]
    assert len(emitted) == 1
    assert emitted[0][1]["event"] == "task_failed"
    # 投影补落 failed（对账：读面否则永久看到 running）
    updates = [c for c in state.calls if c[0] == "update"]
    assert any(
        c[1] == {"pipeline_id": "pipe-b", "fields": {"task.status": "failed"}} for c in updates
    )


async def test_reconcile_stop_suspend_scenario_aligns_completed(mod) -> None:
    # 09-04 实录同构：stop→suspended→收尾（persist_run_end 写终态失败）→
    # runs 停留 suspended、投影 completed。跨表一致性 = 任务域以 completed 收束，
    # 不因 runs 记账事故把已完成任务误报为失败/继续挂起。
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-c", "completed")],
        runs=[_run("suspended", "pipe-c")],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert [r["verdict"] for r in reconciled] == ["completed"]
    emitted = [c for c in bus.calls if c[0] == "emit_domain"]
    assert emitted[0][1]["event"] == "task_completed"


async def test_reconcile_authoritative_completed_run_backfills_and_notifies(mod) -> None:
    # BUG-2b 幽灵面（ADR 2026-09-14）：run 真做完（runs 侧权威 completed）而
    # 收尾投影写丢失（投影停 running）→ 补落 completed + 补派完成通知
    #（合成事件携带调和后投影 state 载荷，派生链免回查）
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-k", "running")],
        runs=[_run("completed", "pipe-k")],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert [r["verdict"] for r in reconciled] == ["completed"]
    updates = [c for c in state.calls if c[0] == "update"]
    assert any(
        c[1] == {"pipeline_id": "pipe-k", "fields": {"task.status": "completed"}}
        for c in updates
    )
    emitted = [c for c in bus.calls if c[0] == "emit_domain"]
    assert len(emitted) == 1
    assert emitted[0][1]["event"] == "task_completed"
    assert emitted[0][1]["tags"]["task_id"] == "pipe-k"


async def test_reconcile_cancelled_run_aligns_failed_projection_and_notifies(mod) -> None:
    # 用户取消而取消时投影写丢失（投影停 running）→ failed 对齐 + 补派失败通知
    #（任务域裁决词汇无 cancelled；复用 run.failed 派生+对账+清挂号链）
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-m", "running")],
        runs=[_run("cancelled", "pipe-m")],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert [r["verdict"] for r in reconciled] == ["failed"]
    emitted = [c for c in bus.calls if c[0] == "emit_domain"]
    assert len(emitted) == 1
    assert emitted[0][1]["event"] == "task_failed"
    updates = [c for c in state.calls if c[0] == "update"]
    assert any(
        c[1] == {"pipeline_id": "pipe-m", "fields": {"task.status": "failed"}}
        for c in updates
    )


async def test_reconcile_skips_pipeline_whose_latest_run_is_inflight(mod) -> None:
    # 防误伤（终态候选 × 在飞并存，runs completed/cancelled 入候选集后的新
    # 风险面）：旧 run completed、真最新 run running（续跑中）→ 按真最新 run
    # 判定，不得把在飞任务误裁成 completed
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-n", "running")],
        runs=[
            _run("completed", "pipe-n", run_id="run-old", created_at="2026-09-06T07:00:00Z"),
            _run("running", "pipe-n", run_id="run-new", created_at="2026-09-06T09:00:00Z"),
        ],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert reconciled == []
    assert [c for c in bus.calls if c[0] == "emit_domain"] == []
    assert all(c[0] != "update" for c in state.calls)


async def test_reconcile_skips_healthy_rows_silently(mod) -> None:
    # 一致行（挂起中/都 completed/未判）零调和零事件
    state, runs_cap, bus = _caps(
        rows=[
            _task_row("pipe-ok", "completed"),
            _task_row("pipe-paused", "running"),
        ],
        runs=[
            _run("completed", "pipe-ok"),
            _run("suspended", "pipe-paused"),
        ],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert reconciled == []
    assert [c for c in bus.calls if c[0] == "emit_domain"] == []


async def test_reconcile_uses_latest_run_not_stale_history(mod) -> None:
    # 多 run 历史：旧 run failed、最新 run completed（resume 后重跑成功）→
    # 合法历史，不是分歧行——按最新 run 判定，防误伤
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-d", "completed")],
        runs=[
            _run("failed", "pipe-d", run_id="run-old", created_at="2026-09-06T07:00:00Z"),
            _run("completed", "pipe-d", run_id="run-new", created_at="2026-09-06T09:00:00Z"),
        ],
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert reconciled == []
    assert [c for c in bus.calls if c[0] == "emit_domain"] == []


async def test_reconcile_ignores_non_task_rows(mod) -> None:
    # 非任务管道（无 task.* 键）不在仲裁域
    state, runs_cap, bus = _caps(
        rows=[{"pipeline_id": "pipe-x", "thread_id": "t", "llm_model": "m"}],
        runs=[_run("failed", "pipe-x")],
    )
    assert await mod.reconcile.reconcile_startup(state, runs_cap, bus) == []
    assert [c for c in bus.calls if c[0] == "emit_domain"] == []


async def test_reconcile_degrades_without_capabilities(mod) -> None:
    # 能力读面故障（DB/内核未就绪）→ 降级留痕返回空，不阻断插件装载
    state, runs_cap, bus = _caps(rows=[], runs=[])
    failing = _FakeCapability({})
    assert await mod.reconcile.reconcile_startup(failing, runs_cap, bus) == []
    assert await mod.reconcile.reconcile_startup(state, failing, bus) == []


async def test_reconcile_survives_projection_write_failure(mod) -> None:
    # 注入写失败（DB 属外部依赖可替身）：对账补落失败不阻断失败通知派生
    #（events 既有契约：写失败仅告警不影响事件派生——错误可观测不吞为无声）
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-e", "running")],
        runs=[_run("failed", "pipe-e")],
        update_error=RuntimeError("db write failed"),
    )
    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)
    assert [r["verdict"] for r in reconciled] == ["failed"]
    emitted = [c for c in bus.calls if c[0] == "emit_domain"]
    assert len(emitted) == 1
    assert emitted[0][1]["event"] == "task_failed"


class _SequenceListCapability(_FakeCapability):
    """list 按调用序返回（第 N 次起抛错）——调和中途能力死亡的外部故障替身。"""

    def __init__(self, outcomes: list[Any]) -> None:
        super().__init__({})
        self._outcomes = list(outcomes)

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        outcome = self._outcomes.pop(0) if self._outcomes else RuntimeError("db gone")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_reconcile_skips_pipeline_when_capability_dies_midway(mod) -> None:
    # 分歧行派发途中 state 能力死亡（list 第二次调用抛错）→ 该行留痕跳过、
    # 不中断整体扫描（后续管道仍可达调和）
    ok_state = _SequenceListCapability([[_task_row("pipe-h", "completed")], RuntimeError("db gone")])
    ok_state2 = _SequenceListCapability([[_task_row("pipe-i", "running")], {"ok": True}])
    runs_cap = _FakeCapability(
        {"pipeline-runs.list": [_run("failed", "pipe-h"), _run("failed", "pipe-i")]}
    )
    bus = _FakeCapability({"emit_domain": {"ok": True}})
    reconciled = await mod.reconcile.reconcile_startup(ok_state, runs_cap, bus)
    assert reconciled == []
    reconciled2 = await mod.reconcile.reconcile_startup(ok_state2, runs_cap, bus)
    assert [r["verdict"] for r in reconciled2] == ["failed"]


# ═══════════════════════════════════════════════════════════
# events 派生仲裁（run.failed 不得推翻投影终态证据）
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
def test_derive_run_failed_with_terminal_projection_does_not_report_failure(
    mod, terminal_status: str
) -> None:
    # run.failed 到达而投影已持终态证据 → 投影权威，不派生 task_failed
    #（防对账把 completed 覆写为 failed 的分歧放大器）
    row = _task_row("pipe-f", terminal_status)
    assert mod.events.derive_task_terminal_events("run.failed", row) == []


def test_derive_run_failed_without_terminal_evidence_still_reports(mod) -> None:
    # 未决投影（running）+ run.failed → 照常派生 task_failed（kill 方未随写的对账面）
    row = _task_row("pipe-g", "running")
    derived = mod.events.derive_task_terminal_events("run.failed", row)
    assert [name for name, _ in derived] == ["task_failed"]


async def test_reconcile_completed_backfill_failure_skips_row(mod) -> None:
    """completed 权威裁决但投影补落失败 → 该行留痕跳过，不派发合成事件（155-160）。

    契约：补落失败时投影仍是旧值（未决），若继续派发完成通知会出现
    「通知说完成、投影读不到 completed」的两套账；故失败即 continue，
    留待下一轮扫描重试（启动扫描是兜底面，不阻断装载）。
    """
    state, runs_cap, bus = _caps(
        rows=[_task_row("pipe-fail", "running")],
        runs=[_run("completed", "pipe-fail")],
        update_error=RuntimeError("db write failed"),
    )

    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)

    assert reconciled == [], "补落失败的行不得计入已调和"
    # 补落尝试确实发生过（失败不是「压根没写」）
    assert any(c[0] == "update" for c in state.calls)
    assert [c for c in bus.calls if c[0] == "emit_domain"] == [], (
        "补落失败后不得派发完成通知（否则两套账）"
    )


async def test_reconcile_continues_scan_after_backfill_failure(mod) -> None:
    """对照组：一行补落失败不影响后续行调和（留痕续扫语义）。"""
    class _PartialUpdateCapability(_FakeCapability):
        """update 对指定 pipeline 抛错，其余成功（同一能力面的局部故障）。"""

        def __init__(self, rows: list[dict[str, Any]], bad_pid: str) -> None:
            super().__init__({"list": rows, "update": {"ok": True}})
            self._bad = bad_pid

        async def call(self, method, params, timeout=None):  # type: ignore[override]
            self.calls.append((method, params))
            if method == "update" and params.get("pipeline_id") == self._bad:
                raise RuntimeError("db write failed")
            if method not in self._responses:
                raise KeyError(f"unexpected capability method: {method}")
            return self._responses[method]

    rows = [_task_row("pipe-bad", "running"), _task_row("pipe-good", "running")]
    state = _PartialUpdateCapability(rows, "pipe-bad")
    runs_cap = _FakeCapability(
        {"pipeline-runs.list": [_run("completed", "pipe-bad"), _run("completed", "pipe-good")]}
    )
    bus = _FakeCapability({"emit_domain": {"ok": True}})

    reconciled = await mod.reconcile.reconcile_startup(state, runs_cap, bus)

    assert [r["pipeline_id"] for r in reconciled] == ["pipe-good"]
    assert [r["verdict"] for r in reconciled] == ["completed"]
