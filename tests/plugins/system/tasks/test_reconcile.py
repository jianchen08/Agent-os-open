# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""任务域启动对账器测试（runs 退役收尾：纯告警对账）。

域界定（ADR 2026-10-08-runs-retire-completion，翻案 ADR 2026-09-14 读面裁决权）：
分歧只告警不裁决——不写终态、不派发域事件，「谁终态谁随写」。断行为不断实现：

- classify_divergence 纯函数断 输入(簿记状态, 投影终态) → 告警类别四值
  （projection_completed_run_diverged / undecided_run_terminal /
  run_completed_evaluation_missing / 不告警），参数化 ≥2 组有区分度输入。
- 事故 2026-09-28 形态（簿记 completed × 投影未决）→ 只告警零写零派发；
  废弃值 pending_evaluation 残行（B 拍板 2026-10-16）不构成分歧——零告警零写。
- reconcile_startup 断 能力调用 → WARNING 留痕（带双方状态与 task id）+
  分歧清单；healthy 行零告警、多 run 历史取最新不误报、能力读面故障降级空。
- events 派生闸：run.failed + 投影 completed/cancelled 终态证据 → 不派生
  task_failed（终态证据不被运行域失败事件推翻）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tasks"


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """逐出同名裸模块，强制按本插件目录解析（与 test_tasks_http_api.py 同款）。"""
    d = str(_PLUGIN_DIR)
    shared_root = str(_PLUGIN_DIR.parents[0])  # plugins/shared（_root_api 解析前提）
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
    """pipeline-state.list 行形状（任务管道摘要，events._task_row 同款）。

    必带 lineage 键（根任务形态）：补写面以血缘键自证任务管道身份
    （reconcile._is_task_pipeline_row），缺血缘键的分歧行降级纯告警不落写。
    """
    return {
        "pipeline_id": pid,
        "thread_id": f"thread-{pid}",
        "task.id": pid,
        "task.goal": "写周报",
        "task.status": status,
        "lineage.origin.kind": "root",
    }


def _caps(
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> tuple[_FakeCapability, _FakeCapability]:
    """组装 state / runs(service-registry) 两个假件（纯告警对账无 bus 依赖）。"""
    state = _FakeCapability({"list": rows})
    runs_cap = _FakeCapability(
        {
            "pipeline-runs.list": runs,
        }
    )
    return state, runs_cap


# ═══════════════════════════════════════════════════════════
# classify_divergence 纯函数（告警类别）
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        ("failed", "completed"),
        ("suspended", "completed"),
    ],
)
def test_classify_completed_projection_vs_diverged_bookkeeping(
    mod, run_status: str, task_status: str
) -> None:
    # 投影持完成证据（评估已过）而簿记分歧 → 告警（不裁决：投影不被簿记改写）
    assert mod.reconcile.classify_divergence(run_status, task_status) == (
        "projection_completed_run_diverged"
    )


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        ("failed", "running"),
        ("failed", "pending"),
        ("failed", "evaluating"),
        ("failed", ""),
        ("cancelled", "running"),
    ],
)
def test_classify_undecided_projection_vs_terminal_run(
    mod, run_status: str, task_status: str
) -> None:
    # 运行已终而投影未决（kill 方未随写形态）→ 告警
    assert mod.reconcile.classify_divergence(run_status, task_status) == "undecided_run_terminal"


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        # 事故 2026-09-28 形态：run 正常收尾 × 投影未决 → 告警不托底（完成唯一
        # 判据 = 评估通过；熔断收尾曾被托底裁 completed 假通知「已完成 ✅」）
        ("completed", "running"),
        ("completed", "pending"),
        ("completed", "evaluating"),
        ("completed", ""),
    ],
)
def test_classify_run_completed_without_evaluation_evidence(
    mod, run_status: str, task_status: str
) -> None:
    assert mod.reconcile.classify_divergence(run_status, task_status) == (
        "run_completed_evaluation_missing"
    )


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        # 在飞 run（投影未决侧）不可判——一律不告警
        ("running", "running"),
        # 一致行不告警
        ("completed", "completed"),
        ("failed", "failed"),
        # 投影 running × 簿记 suspended = 排空对账分歧（suspended_run_drain，
        # 补写 stopped）；投影真终态 × 簿记在飞 = 僵尸告警
        # （zombie_terminal_projection）——均不再属「健康行」，见下方值域与
        # 排空/僵尸专项用例（ADR 2026-10-30 reconcile 三刀）
        # 用户侧终态证据（stopped/timeout/cancelled）保守不告警
        ("failed", "stopped"),
        ("failed", "timeout"),
        ("suspended", "cancelled"),
        # 真最新 run 在飞/可恢复 × 未决投影 → 不告警
        ("running", "pending"),
        ("running", ""),
        ("suspended", "pending"),
        # 废弃值 pending_evaluation 残行（B 拍板 2026-10-16）：不入未决集，
        # 簿记任一侧都不构成分歧——零告警零写
        ("completed", "pending_evaluation"),
        ("failed", "pending_evaluation"),
        ("cancelled", "pending_evaluation"),
    ],
)
def test_classify_healthy_or_ambiguous_rows_never_warned(
    mod, run_status: str, task_status: str
) -> None:
    assert mod.reconcile.classify_divergence(run_status, task_status) is None


def test_classify_output_domain_and_alert_invariant(mod) -> None:
    # 性质断言：告警类别值域封闭；completed 投影分歧唯一来源 = 投影 completed ×
    # 簿记 failed/suspended（簿记 completed 永远造不出该类别——run 正常收尾不是
    # 完成证据）；run_completed 类别唯一来源 = 簿记 completed × 未决投影；
    # 纯告警词汇表无「裁决值」
    for run_status in ("running", "suspended", "failed", "completed", "cancelled"):
        for task_status in (
            "running", "completed", "failed", "stopped", "",
            "pending", "evaluating",
        ):
            kind = mod.reconcile.classify_divergence(run_status, task_status)
            assert kind in (
                "projection_completed_run_diverged",
                "zombie_terminal_projection",
                "suspended_run_drain",
                "undecided_run_terminal",
                "run_completed_evaluation_missing",
                None,
            )
            if kind == "projection_completed_run_diverged":
                assert task_status == "completed"
                assert run_status in ("failed", "suspended")
            if kind == "run_completed_evaluation_missing":
                assert run_status == "completed"
                assert task_status in ("", "running", "pending", "evaluating")
            if kind == "zombie_terminal_projection":
                # 僵尸告警（纯告警不裁决）：投影真终态 × 簿记在飞/挂起
                assert task_status in ("completed", "failed")
                assert run_status in ("running", "suspended")
            if kind == "suspended_run_drain":
                # 排空对账（补写 stopped）：投影 running × 簿记 suspended
                assert task_status == "running"
                assert run_status == "suspended"
    assert mod.reconcile.classify_divergence("completed", "completed") is None
    # 排空对账 / 僵尸投影两类新域（原「健康行」参数化两行的正向断言）
    assert mod.reconcile.classify_divergence("suspended", "running") == "suspended_run_drain"
    assert mod.reconcile.classify_divergence("running", "completed") == "zombie_terminal_projection"
    assert mod.reconcile.classify_divergence("running", "failed") == "zombie_terminal_projection"


# ═══════════════════════════════════════════════════════════
# reconcile_startup 启动对账（行为：能力调用 → WARNING 留痕 + 分歧清单）
# ═══════════════════════════════════════════════════════════


async def test_reconcile_warns_on_completed_projection_divergence(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    # 分歧行：簿记说 failed、投影说 completed（评估已过）→ 告警，不写不派发
    state, runs_cap = _caps(
        rows=[_task_row("pipe-a", "completed")],
        runs=[_run("failed", "pipe-a")],
    )
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["pipeline_id"] for d in divergences] == ["pipe-a"]
    assert [d["kind"] for d in divergences] == ["projection_completed_run_diverged"]
    warned = [r for r in caplog.records if "分歧" in r.getMessage()]
    assert len(warned) == 1
    message = warned[0].getMessage()
    assert "pipe-a" in message, "告警必须带 task/管道 id"
    assert "completed" in message, "告警必须带投影侧状态"
    assert "failed" in message, "告警必须带簿记侧状态"
    # 纯告警：零写面（无 update 调用——state fake 未预置 update，写了即 KeyError）
    assert all(c[0] != "update" for c in state.calls)


async def test_reconcile_warns_on_undecided_projection_vs_failed_run(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    # 崩溃残留：任务没做完（投影 running）+ 簿记 failed → 告警 + failed 侧补随写
    # （补写面之一，模块 docstring 契约：failed 无假成功风险，投影随写 failed；
    # completed 侧仍零写零派发）
    # fake 预置 update 响应（未预置=KeyError 语义，会走写失败降级分支）
    state, runs_cap = _caps(
        rows=[_task_row("pipe-b", "running")],
        runs=[_run("failed", "pipe-b")],
    )
    state._responses["update"] = {"ok": True}
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
    assert any("分歧" in r.getMessage() for r in caplog.records)
    assert divergences[0]["followed_write"] is True, "failed 侧补随写已落"
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 1, "只补写该分歧行（completed 侧零写）"
    assert updates[0][1]["pipeline_id"] == "pipe-b"
    assert updates[0][1]["fields"]["task.status"] == "failed"


async def test_reconcile_follow_writes_cancelled_side(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    """缺陷 I（R2 移动端 2026-10-15）：cancel 随写被垂死 run 的步边界 stale
    投影覆写回 running（内核 per-step 投影逐键 upsert run 本地 state 旧值）——
    投影恒卡 running，reconcile 重复告警无人执笔（与 failed 侧 B74/B75/B76
    同构）。cancelled 侧从纯告警升补随写：写任务域停止终态 stopped
    （ADR 2026-10-30 词汇收敛：cancelled 退役，用户停止 = stopped；
    簿记侧 run.cancelled 为内核 run 域词汇保留），不写 failure_class
    （取消是用户裁决不是失败，与停止写面同形状）。"""
    state, runs_cap = _caps(
        rows=[_task_row("pipe-c", "running")],
        runs=[_run("cancelled", "pipe-c")],
    )
    state._responses["update"] = {"ok": True}
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
    assert divergences[0]["followed_write"] is True, "cancelled 侧补随写已落"
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 1
    fields = updates[0][1]["fields"]
    assert fields["task.status"] == "stopped"
    assert "failure_class" not in fields, "用户裁决不写失败归因"


async def test_reconcile_cancelled_write_carries_run_ended_at(mod) -> None:
    """ended_at 取 run 簿记值（实际取消时刻），与 failed 侧补随写同源。"""
    run = _run("cancelled", "pipe-c2")
    run["ended_at"] = "2026-10-15T12:00:00+00:00"
    state, runs_cap = _caps(rows=[_task_row("pipe-c2", "running")], runs=[run])
    state._responses["update"] = {"ok": True}
    await mod.reconcile.reconcile_startup(state, runs_cap)
    updates = [c for c in state.calls if c[0] == "update"]
    assert updates[0][1]["fields"]["task.ended_at"] == "2026-10-15T12:00:00+00:00"


async def test_reconcile_cancelled_follow_write_failure_degrades(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    """写面故障降级留痕（观测面不阻断装载，下轮装载重试，failed 侧同款）。"""
    state, runs_cap = _caps(
        rows=[_task_row("pipe-c3", "running")],
        runs=[_run("cancelled", "pipe-c3")],
    )
    # update 未预置 → KeyError 语义 → 降级分支
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert divergences[0]["followed_write"] is False
    assert any("补随写失败" in r.getMessage() for r in caplog.records)


async def test_reconcile_session_bookkeeping_keys_alone_are_not_task_row(mod) -> None:
    """装机 2026-10-21 死锁事故回归锁（毒前形态）：仅携带提交者侧记账键
    （task.owned.* / task.subtasks_pending.*）的会话行不构成任务行——其自身
    run 的取消簿记不进分歧扫描（零分歧零投影写）。幽灵记账键由第六刀回收
    （ADR 2026-11-02-state-key-governance：子行不存在 → 登记键+挂号键清
    null；owned.status 已退役，存量键一并回收）。"""
    session_row = {
        "pipeline_id": "session-pipeline",
        "thread_id": "thread-1",
        "task.owned.child1.status": "running",
        "task.subtasks_pending.child1": "2026-10-21T00:00:00+00:00",
    }
    state, runs_cap = _caps(
        rows=[session_row],
        runs=[_run("cancelled", "session-pipeline")],
    )
    state._responses["update"] = {"ok": True}
    divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    # 会话行自身不进簿记分歧扫描（cancelled 簿记零分歧零任务投影写）；
    # 唯一分歧 = 第六刀幽灵登记清理
    assert [d["kind"] for d in divergences] == ["ghost_owned_swept"]
    assert divergences[0]["owned_id"] == "child1"
    assert divergences[0]["swept"] is True
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 1, "唯一写面 = 幽灵记账键回收"
    assert updates[0][1]["pipeline_id"] == "session-pipeline"
    assert updates[0][1]["fields"] == {
        "task.owned.child1.status": None,
        "task.subtasks_pending.child1": None,
    }


async def test_reconcile_task_like_row_without_lineage_is_never_written(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    """装机 2026-10-27 主管道裸 ID 复发回归锁（清扫面全值覆盖）：带 task.status
    投影但缺血缘键的行（会话）不进分歧补写面（身份守卫不变），但**任意非空
    残留都清**——未决值 'running' 不再豁免：run-started 复活复位把 'running'
    误落在会话上后永不复位，面板伪任务行标题兜底成裸管道 ID（2026-10-27
    用户报告装机实证：8bde3343a8bb 残留 running + 3 主管道 null 墓碑）。"""
    poisoned_session_row = {
        "pipeline_id": "session-pipeline",
        "thread_id": "thread-1",
        "task.status": "running",  # 未决残留：判据命中但绝不能当作真任务
        "task.owned.child1.status": "running",
    }
    state, runs_cap = _caps(rows=[poisoned_session_row], runs=[_run("cancelled", "session-pipeline")])
    state._responses["update"] = {"ok": True}
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    # 缺血缘键 → 不进分歧补写域；清扫面按身份独立触发（running 也清）。
    # 幽灵 owned 记账键由第六刀回收（ADR 2026-11-02-state-key-governance），
    # 与残留清扫并列且各写各键
    assert [d["kind"] for d in divergences] == [
        "ghost_owned_swept",
        "non_task_residue_cleared",
    ]
    assert divergences[1]["residue"] == "running"
    assert divergences[1]["residue_cleared"] is True
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 2
    assert updates[0][1]["fields"] == {"task.owned.child1.status": None}
    # 清扫键集含失败注记（ADR 2026-11-06 D5 N9：残留自键不清会让会话永久
    # 命中任务判据）
    assert updates[1][1]["fields"] == {
        "task.status": None,
        "task.ended_at": None,
        "task.error": None,
        "task.failure_class": None,
        "task.failure_reason": None,
    }


async def test_reconcile_null_tombstone_row_needs_no_resweep(mod) -> None:
    """清扫过的会话（task.status/ended_at 已是 null 墓碑）不重复进清扫域：
    空值键不再命中任务判据（判据=非空自身声明），墓碑键零重写——重复
    null 写纯负载。幽灵记账键（子行不存在）仍由第六刀回收：墓碑免的是
    投影键重扫，不是活登记的回收（ADR 2026-11-02-state-key-governance）。"""
    swept = {
        "pipeline_id": "session-pipeline",
        "thread_id": "thread-1",
        "task.status": None,
        "task.ended_at": None,
        "task.owned.child1.status": "running",
    }
    state, runs_cap = _caps(rows=[swept], runs=[_run("cancelled", "session-pipeline")])
    state._responses["update"] = {"ok": True}
    divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["ghost_owned_swept"]
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 1
    # null 墓碑（task.status/task.ended_at）不在写面——重扫免除契约不变
    assert updates[0][1]["fields"] == {"task.owned.child1.status": None}


async def test_reconcile_clears_terminal_residue_on_non_task_row(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    """装机 2026-10-21 死锁事故清扫面：被旧版误写 task.status=cancelled 的
    会话行（缺血缘键）在后续扫描中被清键复位（null = state 清键，与 resume
    overlay 同写法；ended_at 同批清）——这是豁免署名（新 run 不复位）之外
    唯一的合法清除面。"""
    poisoned = {
        "pipeline_id": "session-pipeline",
        "thread_id": "thread-1",
        "task.status": "cancelled",
        "task.ended_at": "2026-10-20T16:30:38+00:00",
        "task.owned.child1.status": "running",
    }
    state, runs_cap = _caps(rows=[poisoned], runs=[_run("cancelled", "session-pipeline")])
    state._responses["update"] = {"ok": True}
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    # 终态残留被 classify 视为用户终态（不构成分歧）——清扫面按身份独立触发；
    # 幽灵 owned 记账键同批由第六刀回收（ADR 2026-11-02-state-key-governance）
    assert [d["kind"] for d in divergences] == [
        "ghost_owned_swept",
        "non_task_residue_cleared",
    ]
    assert divergences[1]["residue"] == "cancelled"
    assert divergences[1]["residue_cleared"] is True, "终态残留已被清扫"
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 2
    assert updates[0][1]["pipeline_id"] == "session-pipeline"
    assert updates[0][1]["fields"] == {"task.owned.child1.status": None}
    assert updates[1][1]["pipeline_id"] == "session-pipeline"
    # 清扫键集含失败注记（ADR 2026-11-06 D5 N9）
    assert updates[1][1]["fields"] == {
        "task.status": None,
        "task.ended_at": None,
        "task.error": None,
        "task.failure_class": None,
        "task.failure_reason": None,
    }
    assert any("清扫" in r.getMessage() for r in caplog.records)


async def test_reconcile_residue_clear_failure_degrades(mod, caplog: pytest.LogCaptureFixture) -> None:
    """清扫写面故障 → 降级留痕（followed 写面同款），不阻断装载。"""
    poisoned = {
        "pipeline_id": "session-pipeline",
        "thread_id": "thread-1",
        "task.status": "cancelled",
    }
    state, runs_cap = _caps(rows=[poisoned], runs=[_run("cancelled", "session-pipeline")])
    # update 未预置 → KeyError 语义 → 降级分支
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert divergences[0]["residue_cleared"] is False
    assert any("清扫失败" in r.getMessage() for r in caplog.records)


async def test_reconcile_writes_task_row_with_lineage_but_not_session_row(mod) -> None:
    """身份判别对照：同形态分歧下，带血缘键的任务行照常补写（守卫只拦会话）。"""
    task_row = {
        "pipeline_id": "task-pipe",
        "thread_id": "thread-task",
        "task.id": "task-pipe",
        "task.status": "running",
        "lineage.parent_pipeline_id": "parent-1",
    }
    state, runs_cap = _caps(rows=[task_row], runs=[_run("cancelled", "task-pipe")])
    state._responses["update"] = {"ok": True}
    divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert divergences[0]["followed_write"] is True, "带血缘键的任务行补写面不受守卫影响"
    updates = [c for c in state.calls if c[0] == "update"]
    assert [u[1]["pipeline_id"] for u in updates] == ["task-pipe"], "自身投影照常补写"
    # owned 镜像已退役（ADR 2026-11-02-state-key-governance）：父会话面板
    # join 子行真值，无父侧镜像写面
    assert all(u[1]["pipeline_id"] != "parent-1" for u in updates), "父镜像写面已退役"


async def test_reconcile_cancelled_side_keeps_user_terminal_undisturbed(mod) -> None:
    """用户侧终态投影（stopped）× 簿记 cancelled → 非分歧，不补写
    （处置权归用户侧；仅未决投影被 heal）。旧词 cancelled 投影行不再免检：
    走历史迁移刀（legacy_cancelled_migrated → 写 stopped，ADR 2026-10-30
    三分刀之三），簿记分歧面不重复告警。timeout 存量行由第七刀迁移 failed
    （ADR 2026-11-02-state-key-governance：timeout 死枚举值退役）。"""
    state, runs_cap = _caps(
        rows=[_task_row("pipe-keep", "stopped")],
        runs=[_run("cancelled", "pipe-keep")],
    )
    assert await mod.reconcile.reconcile_startup(state, runs_cap) == [], (
        "投影 stopped 不构成分歧行"
    )
    assert all(c[0] != "update" for c in state.calls)

    # timeout 存量行：第七刀迁移 failed（一次性），无簿记分歧条目
    state, runs_cap = _caps(
        rows=[_task_row("pipe-timeout", "timeout")],
        runs=[_run("cancelled", "pipe-timeout")],
    )
    state._responses["update"] = {"ok": True}
    divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["legacy_timeout_migrated"]
    assert divergences[0]["migrated"] is True
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][1]["fields"]["task.status"] == "failed"

    # 旧词存量行：迁移刀改写 stopped（一次性），无簿记分歧条目
    state, runs_cap = _caps(
        rows=[_task_row("pipe-legacy", "cancelled")],
        runs=[_run("cancelled", "pipe-legacy")],
    )
    state._responses["update"] = {"ok": True}
    divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["legacy_cancelled_migrated"]
    assert divergences[0]["migrated"] is True
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][1]["fields"]["task.status"] == "stopped"


async def test_reconcile_run_completed_undecided_warns_without_backfill(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    # 事故 2026-09-28 回归：run 正常收尾而投影未决 → 零写零派发（旧托底在此
    # 托底裁 completed 并曾假通知「已完成 ✅」），只发告警
    state, runs_cap = _caps(
        rows=[_task_row("pipe-k", "running")],
        runs=[_run("completed", "pipe-k")],
    )
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["run_completed_evaluation_missing"]
    assert all(c[0] != "update" for c in state.calls), "不托底裁 completed"


async def test_reconcile_skips_pipeline_whose_latest_run_is_inflight(mod) -> None:
    # 防误报（终态候选 × 在飞并存）：旧 run completed、真最新 run running（续跑
    # 中）→ 按真最新 run 判定，不得把在飞任务误报为分歧
    state, runs_cap = _caps(
        rows=[_task_row("pipe-n", "running")],
        runs=[
            _run("completed", "pipe-n", run_id="run-old", created_at="2026-09-06T07:00:00Z"),
            _run("running", "pipe-n", run_id="run-new", created_at="2026-09-06T09:00:00Z"),
        ],
    )
    assert await mod.reconcile.reconcile_startup(state, runs_cap) == []


async def test_reconcile_skips_healthy_rows_silently(mod) -> None:
    # 一致行（都 completed）零告警零分歧
    state, runs_cap = _caps(
        rows=[_task_row("pipe-ok", "completed")],
        runs=[_run("completed", "pipe-ok")],
    )
    assert await mod.reconcile.reconcile_startup(state, runs_cap) == []


async def test_reconcile_drains_suspended_run_with_running_projection(mod) -> None:
    """排空对账刀（ADR 2026-10-30 三分刀之一）：投影 running × 簿记 suspended
    → 补写 stopped（重启排空遗留的脱钩行复位；挂起 run 可经 run-started
    复位回 running，不写 ended_at——排空不是终局）。"""
    state, runs_cap = _caps(
        rows=[_task_row("pipe-paused", "running")],
        runs=[_run("suspended", "pipe-paused")],
    )
    state._responses["update"] = {"ok": True}
    divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["suspended_run_drain"]
    assert divergences[0]["followed_write"] is True, "排空对账写 stopped 已落"
    updates = [c for c in state.calls if c[0] == "update"]
    assert len(updates) == 1
    fields = updates[0][1]["fields"]
    assert fields["task.status"] == "stopped"
    assert "task.ended_at" not in fields, "排空不是终局，不写 ended_at"


async def test_reconcile_uses_latest_run_not_stale_history(mod) -> None:
    # 多 run 历史：旧 run failed、最新 run completed（resume 后重跑成功）→
    # 合法历史，不是分歧行——按最新 run 判定，防误报
    state, runs_cap = _caps(
        rows=[_task_row("pipe-d", "completed")],
        runs=[
            _run("failed", "pipe-d", run_id="run-old", created_at="2026-09-06T07:00:00Z"),
            _run("completed", "pipe-d", run_id="run-new", created_at="2026-09-06T09:00:00Z"),
        ],
    )
    assert await mod.reconcile.reconcile_startup(state, runs_cap) == []


async def test_reconcile_ignores_non_task_rows(mod) -> None:
    # 非任务管道（无 task.* 键）不在对账域
    state, runs_cap = _caps(
        rows=[{"pipeline_id": "pipe-x", "thread_id": "t", "llm_model": "m"}],
        runs=[_run("failed", "pipe-x")],
    )
    assert await mod.reconcile.reconcile_startup(state, runs_cap) == []


async def test_reconcile_degrades_without_capabilities(mod) -> None:
    # 能力读面故障（DB/内核未就绪）→ 降级留痕返回空，不阻断插件装载
    state, runs_cap = _caps(rows=[], runs=[])
    failing = _FakeCapability({})
    assert await mod.reconcile.reconcile_startup(failing, runs_cap) == []
    assert await mod.reconcile.reconcile_startup(state, failing) == []


# ═══════════════════════════════════════════════════════════
# events 派生闸（run.failed 不得推翻投影终态证据）
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("terminal_status", ["completed"])
def test_derive_run_failed_with_terminal_projection_does_not_report_failure(
    mod, terminal_status: str
) -> None:
    # run.failed 到达而投影已持 completed 终态证据 → 终态证据权威，不派生
    # task_failed（失败通知不得推翻评估终态）。词汇收敛后豁免收窄至
    # completed（ADR 2026-10-30）：stopped 是可恢复态非真终态证据，其失败
    # 镜像照常派生（见下组）。
    row = _task_row("pipe-f", terminal_status)
    assert mod.events.derive_task_terminal_events("run.failed", row) == []


@pytest.mark.parametrize("undecided_status", ["running", "stopped"])
def test_derive_run_failed_without_terminal_evidence_still_reports(
    mod, undecided_status: str
) -> None:
    # 未决/可恢复投影（running、stopped）+ run.failed → 照常派生 task_failed
    #（镜像派生通知面；stopped 豁免已收窄，可恢复态的失败事实照常可见）
    row = _task_row("pipe-g", undecided_status)
    derived = mod.events.derive_task_terminal_events("run.failed", row)
    assert [name for name, _ in derived] == ["task_failed"]


# ═══════════════════════════════════════════════════════════
# 插件级失败纳入视野（ADR 2026-10-17-task-birth-workspace-inclusion）
# ═══════════════════════════════════════════════════════════


def _task_row_with_plugin_errors(pid: str, status: str, errors: list[dict] | None) -> dict:
    """带 run.plugin_errors 常驻镜像的任务行（内核收尾镜像键）。"""
    row = _task_row(pid, status)
    if errors is not None:
        row["run.plugin_errors"] = errors
    return row


@pytest.mark.parametrize(
    ("run_status", "task_status", "count", "expected"),
    [
        # 残余事故形态：run 正常收尾、有插件级失败、投影未决 → 告警类别升级为
        # 「插件级失败未随写」（旧判据会误报成"评估证据未落"）
        ("completed", "running", 2, "plugin_errors_unrecorded"),
        ("completed", "pending", 1, "plugin_errors_unrecorded"),
        ("completed", "", 1, "plugin_errors_unrecorded"),
        # 对照组 A：无插件级失败 → 维持旧类别（无回归）
        ("completed", "running", 0, "run_completed_evaluation_missing"),
        # 对照组 B：簿记终态的补写面优先（healing 比告警更值钱，不被抢占）
        ("failed", "running", 3, "undecided_run_terminal"),
        ("cancelled", "pending", 3, "undecided_run_terminal"),
        # 在飞 run 不可判（插件热重载 / 续跑中）→ 零告警
        ("running", "running", 3, None),
        # 投影已持终态 → 失败事实已在投影里，无需另告警
        ("completed", "completed", 3, None),
        ("failed", "failed", 3, None),
        # 用户侧终态投影处置权归用户侧
        ("completed", "cancelled", 3, None),
    ],
)
def test_classify_plugin_errors_visible_after_restart(
    mod, run_status: str, task_status: str, count: int, expected: str | None
) -> None:
    assert (
        mod.reconcile.classify_divergence(run_status, task_status, count) == expected
    )


async def test_reconcile_reports_plugin_errors_after_restart(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    """重启后插件级失败仍可对账：run.plugin_errors 常驻镜像在 state 行里，
    未决投影 × run 已终 → 告警带失败条数（纯告警，不改投影——ADR 2026-10-15
    已定「有产出 + 步失败 = degraded」为合法完成态，本模块不改其语义）。"""
    state, runs_cap = _caps(
        rows=[
            _task_row_with_plugin_errors(
                "pipe-pe",
                "running",
                [
                    {"plugin_id": "workspace_lifecycle", "message": "worktree 未落地"},
                    {"plugin_id": "llm_core", "message": "boom"},
                ],
            )
        ],
        runs=[_run("completed", "pipe-pe")],
    )
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["plugin_errors_unrecorded"]
    assert divergences[0]["plugin_error_count"] == 2
    assert "followed_write" not in divergences[0], "插件级失败面纯告警不裁决"
    assert all(c[0] != "update" for c in state.calls)
    assert any("插件级失败" in r.getMessage() for r in caplog.records)


async def test_reconcile_plugin_errors_json_string_form_tolerated(
    mod, caplog: pytest.LogCaptureFixture
) -> None:
    """跨边界 JSON 字符串形态（state_fields.as_list 契约）同口径还原。"""
    state, runs_cap = _caps(
        rows=[
            _task_row_with_plugin_errors(
                "pipe-pe2", "pending", '[{"plugin_id": "x", "message": "boom"}]'
            )
        ],
        runs=[_run("completed", "pipe-pe2")],
    )
    with caplog.at_level("WARNING"):
        divergences = await mod.reconcile.reconcile_startup(state, runs_cap)
    assert [d["kind"] for d in divergences] == ["plugin_errors_unrecorded"]
    assert divergences[0]["plugin_error_count"] == 1


async def test_reconcile_plugin_error_alert_kind_in_value_domain(mod) -> None:
    """性质：告警类别值域封闭（ADR 2026-10-30 三刀扩容后仍无裁决值）。"""
    for run_status in ("running", "suspended", "failed", "completed", "cancelled"):
        for task_status in (
            "running", "completed", "failed", "stopped", "", "pending", "evaluating",
        ):
            for count in (0, 1):
                kind = mod.reconcile.classify_divergence(run_status, task_status, count)
                assert kind in (
                    "projection_completed_run_diverged",
                    "zombie_terminal_projection",
                    "suspended_run_drain",
                    "undecided_run_terminal",
                    "run_completed_evaluation_missing",
                    "plugin_errors_unrecorded",
                    None,
                )
                if kind == "plugin_errors_unrecorded":
                    assert count > 0
                    assert task_status in ("", "running", "pending", "evaluating")
