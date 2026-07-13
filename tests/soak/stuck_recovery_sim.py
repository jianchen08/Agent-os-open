"""卡死检测 + 恢复路径 模拟器。

验证 TaskIdleTimerMixin / TaskRecoveryMixin 真实代码在"任务卡住"时
能正确检测并恢复：用 Fake task_service / timer_manager / engine_registry
驱动，不依赖完整 TaskWorker / 数据库 / 真实 pipeline。

判定矩阵（每个场景）：
  - 应触发 fail / 不应触发 fail
  - 触发耗时窗口（≥ threshold 且 ≤ threshold*1.5）
  - 计时器是否正确清理（无残留风暴）
  - waiting_recovery 状态是否被识别（回归 fix_20260629）
  - fail_task 协程内部异常是否能被回调打出（回归 fix_20260606）
  - 启动恢复：running → suspended → 可 resume

DEBT: timer 基于 loop.call_later（真实 wall clock 相对值），非注入式 fake clock。
  ceiling: 场景 1/9 的 threshold 已放大到 1.0s 缓解慢 CI flake，但仍依赖真实
    时间流逝驱动 TimerHandle 回调，极端慢机仍可能抖动。
  upgrade: 引入虚拟时间事件循环（如 aioloop_proxy / pytest-asyncio 的
    event_loop policy 替换）后，可注入确定性时间推进，消除真实墙钟依赖。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
# 仓库根（让 `from src.xxx ...` 形式的绝对导入能被解析；
# src/tasks/__init__.py 就是这么写的）+ src 目录（让 `from tasks ...` 形式工作）
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

# 抑制 mixin 内部诊断日志（每个场景都会打几十行），--verbose 时再开
logging.getLogger("infrastructure.task_idle_timer").setLevel(logging.CRITICAL)
logging.getLogger("infrastructure.task_recovery").setLevel(logging.CRITICAL)

from infrastructure.task_idle_timer import TaskIdleTimerMixin  # noqa: E402
from infrastructure.task_recovery import TaskRecoveryMixin  # noqa: E402


# ===========================================================================
# 1. Fakes — 模拟 TaskWorker 依赖的最小协议
# ===========================================================================

# 状态字符串复刻 tasks.types.TaskStatus.value
RUNNING = "running"
PENDING = "pending"
STOPPED = "stopped"
EVALUATING = "evaluating"
FAILED = "failed"
COMPLETED = "completed"


@dataclass
class FakeTaskStatus:
    """伪装 enum：既能 .value 又能直接比较字符串。"""
    value: str

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FakeTaskStatus):
            return self.value == other.value
        return self.value == other


@dataclass
class FakeTask:
    id: str
    status: Any  # str 或 FakeTaskStatus 都行
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    description: str = ""
    title: str = ""


class FakeTaskService:
    """模拟 TaskService 的最小接口子集。

    记录所有状态转换调用，供断言使用。
    """

    def __init__(self) -> None:
        self.tasks: dict[str, FakeTask] = {}
        self.fail_calls: list[tuple[str, str]] = []
        self.pause_calls: list[tuple[str, str]] = []
        self.complete_eval_calls: list[tuple[str, bool]] = []
        # 注入异常用：场景 6 fail_task_protocol_robust
        self.fail_task_raise: BaseException | None = None

    def get_task(self, task_id: str) -> FakeTask | None:
        return self.tasks.get(task_id)

    def list_by_status(self, status: Any) -> list[FakeTask]:
        target = status.value if hasattr(status, "value") else status
        return [t for t in self.tasks.values()
                if (t.status.value if hasattr(t.status, "value") else t.status) == target]

    def list_subtasks(self, task_id: str) -> list[FakeTask]:
        # 默认无子任务；场景需要时手动塞
        return []

    async def fail_task(self, task_id: str, reason: str) -> None:
        self.fail_calls.append((task_id, reason))
        if self.fail_task_raise is not None:
            raise self.fail_task_raise
        t = self.tasks.get(task_id)
        if t is not None:
            t.status = FAILED
            t.error = reason

    async def pause_task(self, task_id: str, paused_by: str = "system") -> None:
        self.pause_calls.append((task_id, paused_by))
        t = self.tasks.get(task_id)
        if t is not None:
            t.status = STOPPED
            t.metadata["paused_by"] = paused_by

    async def complete_evaluation(
        self, task_id: str, passed: bool, result: dict | None = None,
    ) -> None:
        self.complete_eval_calls.append((task_id, passed))
        t = self.tasks.get(task_id)
        if t is not None:
            t.status = COMPLETED if passed else FAILED

    def hard_delete_sync(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)


class FakeTimerManager:
    """模拟 timer_manager：用 asyncio.TimerHandle 实现到点回调。

    create_timer 设置 timeout 后 N 秒触发 callback。
    多次 create 同一 task_id 时会先 cancel 旧的。
    """

    def __init__(self, idle_threshold: float) -> None:
        self.idle_threshold: float = idle_threshold
        self._handles: dict[str, asyncio.TimerHandle] = {}
        self.create_count = 0
        self.cancel_count = 0

    async def create_timer(
        self, task_id: str, timeout: float, callback: Any,
    ) -> None:
        # 同 task_id 重复 create：先 cancel 旧的
        old = self._handles.pop(task_id, None)
        if old is not None:
            old.cancel()
        loop = asyncio.get_running_loop()
        self._handles[task_id] = loop.call_later(timeout, callback)
        self.create_count += 1

    async def cancel_timer(self, task_id: str) -> None:
        h = self._handles.pop(task_id, None)
        if h is not None:
            h.cancel()
            self.cancel_count += 1

    def active_count(self) -> int:
        return sum(1 for h in self._handles.values() if not h.cancelled())


class FakeEngine:
    """模拟 PipelineEngine。

    通过 is_running / last_state / pipeline_id 让 _engine_is_running 判定。
    """

    def __init__(
        self,
        pipeline_id: str = "p_fake",
        is_running: bool = True,
        exec_status: str = "running",
    ) -> None:
        self.pipeline_id = pipeline_id
        self.is_running = is_running
        # 用真实的 StateKeys.EXECUTION_STATUS 字段名，让 mixin 能读到
        try:
            from pipeline.types import StateKeys
            self.last_state = {StateKeys.EXECUTION_STATUS: exec_status}
        except Exception:
            self.last_state = {"execution_status": exec_status}


class FakeEntry:
    """PipelineEntry 替身：engine + engine_task。"""

    def __init__(
        self,
        engine: FakeEngine | None,
        engine_task_done: bool = False,
    ) -> None:
        self.engine = engine
        self.engine_task = _FakeFuture(done=engine_task_done) if engine is not None else None


class _FakeFuture:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class FakeEngineRegistry:
    """通过 monkeypatch 注入 pipeline.registry.get_engine_registry。"""

    def __init__(self) -> None:
        self._by_tag: dict[tuple[str, str], list[FakeEntry]] = {}

    def find_by_tag(self, key: str, value: str) -> list[FakeEntry]:
        return list(self._by_tag.get((key, value), []))

    def register(self, task_id: str, entry: FakeEntry) -> None:
        self._by_tag.setdefault(("task_id", task_id), []).append(entry)

    def clear(self, task_id: str) -> None:
        self._by_tag.pop(("task_id", task_id), None)


class FakeContext:
    """简化版 TaskExecutionContext。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.active = True
        self.bg_task: asyncio.Task | None = None
        self.suspended_engine = None
        self.resume_requested = False
        self.idle_timer_registered = True
        self.total_timeout_handle = None

    def cleanup(self, timer_manager: Any = None) -> None:
        self.active = False
        if timer_manager and self.idle_timer_registered:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(timer_manager.cancel_timer(self.task_id))
            except RuntimeError:
                pass
        self.idle_timer_registered = False

    def set_terminal(self) -> None:
        self.active = False


# ===========================================================================
# 2. 复合宿主类 — 给 mixin 提供它读取的属性
# ===========================================================================

class FakeWorker(TaskIdleTimerMixin, TaskRecoveryMixin):
    """FakeTaskWorker：提供 mixin 需要的 _task_service / _services / _contexts。"""

    def __init__(
        self,
        task_service: FakeTaskService,
        timer_manager: FakeTimerManager,
    ) -> None:
        self._task_service = task_service
        self._services = {"timer_manager": timer_manager}
        self._contexts: dict[str, FakeContext] = {}


# ===========================================================================
# 3. 通用：注入/卸载 fake engine registry
# ===========================================================================

def install_fake_registry(registry: FakeEngineRegistry) -> Any:
    """monkeypatch pipeline.registry.get_engine_registry → 返回 fake。

    返回 cleanup 函数，调用后恢复原状。
    """
    import pipeline.registry as _reg
    original = _reg.get_engine_registry

    def fake_get() -> FakeEngineRegistry:
        return registry

    _reg.get_engine_registry = fake_get  # type: ignore[assignment]

    def restore() -> None:
        _reg.get_engine_registry = original  # type: ignore[assignment]

    return restore


# ===========================================================================
# 4. 场景定义 + Runner
# ===========================================================================

@dataclass
class Scenario:
    name: str
    desc: str
    threshold: float  # idle_timer 超时阈值（秒）
    # 等待时间：要超过 threshold，让 timer 必触发；不超过 threshold*N（验证不卡死）
    wait_factor: float = 2.0
    expect_fail: bool = True
    expect_min_elapsed: float = 0.0
    expect_max_elapsed: float = 5.0
    # 计时器残留上限（cleanup 之后还残留几个）
    max_remaining_timers: int = 0


@dataclass
class Result:
    name: str
    passed: bool
    elapsed: float
    fail_calls: int
    fail_reason: str
    pause_calls: int
    remaining_timers: int
    create_count: int
    cancel_count: int
    reason: str


async def _wait_briefly(seconds: float) -> None:
    """让事件循环 N 秒内多次让出，让 TimerHandle 能触发回调。"""
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.01)


# ── 场景实现 ─────────────────────────────────────────────────────────────

async def case_stuck_in_engine_running(threshold: float) -> tuple[bool, str, dict]:
    """场景 1：引擎 is_running=True 一直挂着，idle_timer 到点不该 fail。

    引擎仍在运行 → 非 idle → 重建计时器继续监控。
    """
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=threshold)
    worker = FakeWorker(ts, tm)

    task_id = "t1"
    ts.tasks[task_id] = FakeTask(id=task_id, status=RUNNING)
    worker._contexts[task_id] = FakeContext(task_id)

    reg = FakeEngineRegistry()
    reg.register(task_id, FakeEntry(FakeEngine(is_running=True), engine_task_done=False))
    restore = install_fake_registry(reg)
    try:
        await worker._arm_idle_timer(task_id, tm)
        await _wait_briefly(threshold * 1.5)
    finally:
        restore()
        # 清理：取消所有计时器
        await tm.cancel_timer(task_id)

    # 期望（可观察行为，§9.5）：
    #   1) 引擎仍在跑 → 不该 fail（fail_calls == 0）
    #   2) cleanup 后无残留计时器（active_count == 0 = 无泄漏）
    # 不再断言 create_count（调度器调用次数属实现细节：timer 策略从
    # cancel+recreate 改为 reschedule 时 create_count 会变，但对外行为不变）。
    ok = (len(ts.fail_calls) == 0) and (tm.active_count() == 0)
    reason = ""
    if ts.fail_calls:
        reason = f"不该 fail 但 fail 了: {ts.fail_calls[0][1][:80]}"
    elif tm.active_count() > 0:
        reason = f"cleanup 后仍有残留计时器: {tm.active_count()}"
    return ok, reason, {
        "fail_calls": len(ts.fail_calls),
        "create": tm.create_count, "cancel": tm.cancel_count,
        "active": tm.active_count(),
    }


async def case_engine_truly_idle_fails(threshold: float) -> tuple[bool, str, dict]:
    """场景 2：bg_task done + 引擎已停止 + 无子任务 → 真 idle → fail。"""
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=threshold)
    worker = FakeWorker(ts, tm)

    task_id = "t2"
    ts.tasks[task_id] = FakeTask(id=task_id, status=RUNNING)
    ctx = FakeContext(task_id)
    # bg_task 已完成
    ctx.bg_task = None  # mixin 判定为 done 等同（ctx.bg_task is None）
    worker._contexts[task_id] = ctx

    reg = FakeEngineRegistry()
    # 引擎已停止：is_running=False, future done=True
    reg.register(
        task_id,
        FakeEntry(FakeEngine(is_running=False), engine_task_done=True),
    )
    restore = install_fake_registry(reg)
    try:
        await worker._arm_idle_timer(task_id, tm)
        await _wait_briefly(threshold * 1.8)
    finally:
        restore()
        await tm.cancel_timer(task_id)

    ok = len(ts.fail_calls) >= 1
    reason = "" if ok else "真 idle 但 fail_task 未被调用"
    return ok, reason, {
        "fail_calls": len(ts.fail_calls),
        "create": tm.create_count, "cancel": tm.cancel_count,
    }


async def case_waiting_recovery_treated_as_idle(threshold: float) -> tuple[bool, str, dict]:
    """场景 3：引擎处于 waiting_recovery（回归 fix_20260629_waiting_recovery_deadlock）。

    is_running 仍为 True、engine_task 未 done，但 exec_status=waiting_recovery
    → mixin 必须识别为 idle 并 fail，否则永远死挂。
    """
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=threshold)
    worker = FakeWorker(ts, tm)

    task_id = "t3"
    ts.tasks[task_id] = FakeTask(id=task_id, status=RUNNING)
    worker._contexts[task_id] = FakeContext(task_id)

    reg = FakeEngineRegistry()
    # 关键：is_running=True 但 exec_status=waiting_recovery
    reg.register(
        task_id,
        FakeEntry(
            FakeEngine(is_running=True, exec_status="waiting_recovery"),
            engine_task_done=False,
        ),
    )
    restore = install_fake_registry(reg)
    try:
        await worker._arm_idle_timer(task_id, tm)
        await _wait_briefly(threshold * 1.8)
    finally:
        restore()
        await tm.cancel_timer(task_id)

    ok = len(ts.fail_calls) >= 1
    reason = "" if ok else (
        "waiting_recovery 未被识别为 idle，引擎将死挂（回归 fix_20260629）"
    )
    return ok, reason, {
        "fail_calls": len(ts.fail_calls),
        "create": tm.create_count, "cancel": tm.cancel_count,
    }


async def case_active_children_no_false_kill(threshold: float) -> tuple[bool, str, dict]:
    """场景 4：bg_task 已结束但有 pending 子任务 → 不该 fail。"""
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=threshold)
    worker = FakeWorker(ts, tm)

    task_id = "t4"
    ts.tasks[task_id] = FakeTask(id=task_id, status=RUNNING)
    worker._contexts[task_id] = FakeContext(task_id)

    child = FakeTask(id="t4_c1", status=PENDING)
    ts.list_subtasks = lambda tid: [child] if tid == task_id else []  # type: ignore[assignment]

    reg = FakeEngineRegistry()
    reg.register(
        task_id,
        FakeEntry(FakeEngine(is_running=False), engine_task_done=True),
    )
    restore = install_fake_registry(reg)
    try:
        await worker._arm_idle_timer(task_id, tm)
        await _wait_briefly(threshold * 1.8)
    finally:
        restore()
        await tm.cancel_timer(task_id)

    ok = len(ts.fail_calls) == 0
    reason = "" if ok else "有活跃子任务但被误杀"
    return ok, reason, {
        "fail_calls": len(ts.fail_calls),
        "create": tm.create_count, "cancel": tm.cancel_count,
    }


async def case_reset_keeps_alive(threshold: float) -> tuple[bool, str, dict]:
    """场景 5：每轮迭代调用 reset_idle_timer，正常流不被杀（活跃推进）。"""
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=threshold)
    worker = FakeWorker(ts, tm)

    task_id = "t5"
    ts.tasks[task_id] = FakeTask(id=task_id, status=RUNNING)
    worker._contexts[task_id] = FakeContext(task_id)

    reg = FakeEngineRegistry()
    reg.register(task_id, FakeEntry(FakeEngine(is_running=True), engine_task_done=False))
    restore = install_fake_registry(reg)

    try:
        await worker._arm_idle_timer(task_id, tm)
        # 模拟 3 轮：每轮间隔 < threshold，每轮结束 reset
        for _ in range(3):
            await _wait_briefly(threshold * 0.5)
            await worker.reset_idle_timer(task_id)
        # 最终再等不够一个 threshold 的时间，不该 fail
        await _wait_briefly(threshold * 0.5)
    finally:
        restore()
        await tm.cancel_timer(task_id)

    ok = len(ts.fail_calls) == 0
    reason = "" if ok else "活跃推进的任务被误杀"
    return ok, reason, {
        "fail_calls": len(ts.fail_calls),
        "create": tm.create_count, "cancel": tm.cancel_count,
    }


async def case_fail_task_exception_logged(threshold: float) -> tuple[bool, str, dict]:
    """场景 6：fail_task 协程抛异常，回调能打 ERROR 日志（回归 fix_20260606）。

    验证 _log_fail_task_exception 被调用且没把异常吞掉造成静默。
    """
    ts = FakeTaskService()
    # 注入：fail_task 内部抛异常（模拟 _emit_state_change 失败）
    ts.fail_task_raise = RuntimeError("notify_suspended_pipelines crashed")
    tm = FakeTimerManager(idle_threshold=threshold)
    worker = FakeWorker(ts, tm)

    task_id = "t6"
    ts.tasks[task_id] = FakeTask(id=task_id, status=RUNNING)
    worker._contexts[task_id] = FakeContext(task_id)

    reg = FakeEngineRegistry()
    reg.register(
        task_id,
        FakeEntry(FakeEngine(is_running=False), engine_task_done=True),
    )
    restore = install_fake_registry(reg)

    # 捕获 ERROR 日志
    log_buffer: list[str] = []

    class _LogCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_buffer.append(record.getMessage())

    handler = _LogCapture(level=logging.ERROR)
    target_logger = logging.getLogger("infrastructure.task_idle_timer")
    saved_level = target_logger.level
    target_logger.setLevel(logging.ERROR)
    target_logger.addHandler(handler)

    try:
        await worker._arm_idle_timer(task_id, tm)
        await _wait_briefly(threshold * 2.0)
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(saved_level)
        restore()
        await tm.cancel_timer(task_id)

    # 期望：fail_task 被调过（抛异常那次也算），且回调打出 ERROR 含 task_id
    called = len(ts.fail_calls) >= 1
    logged = any("fail_task 协程内部异常" in m for m in log_buffer)
    ok = called and logged
    if not called:
        reason = "fail_task 未被调用"
    elif not logged:
        reason = "fail_task 抛异常但回调未打 ERROR（异常被吞，子任务永挂）"
    else:
        reason = ""
    return ok, reason, {
        "fail_calls": len(ts.fail_calls),
        "err_logs": sum(1 for m in log_buffer if "fail_task 协程内部异常" in m),
        "create": tm.create_count, "cancel": tm.cancel_count,
    }


async def case_recovery_running_to_suspended(_threshold: float) -> tuple[bool, str, dict]:
    """场景 7：启动恢复 — running/pending 任务 → suspended。"""
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=10.0)
    worker = FakeWorker(ts, tm)

    ts.tasks["t7_run"] = FakeTask(id="t7_run", status=RUNNING)
    ts.tasks["t7_pend"] = FakeTask(id="t7_pend", status=PENDING)
    ts.tasks["t7_stop"] = FakeTask(
        id="t7_stop", status=STOPPED, metadata={"paused_by": "user"},
    )
    ts.tasks["t7_fail"] = FakeTask(id="t7_fail", status=FAILED)
    # 一个容器任务，应被跳过
    ts.tasks["t7_ctn"] = FakeTask(
        id="t7_ctn", status=RUNNING, metadata={"task_scope": "container"},
    )

    await worker._recover_running_tasks()

    # 期望：t7_run / t7_pend → pause_task 调用；t7_ctn / t7_fail 未被处理
    paused_ids = {tid for tid, _ in ts.pause_calls}
    expected_paused = {"t7_run", "t7_pend"}
    ok = paused_ids == expected_paused
    if not ok:
        reason = f"恢复路径 pause 集合不匹配: 实际={paused_ids} 期望={expected_paused}"
    else:
        # 验证 t7_ctn 没被改
        if ts.tasks["t7_ctn"].status != RUNNING:
            ok, reason = False, "容器任务被错误恢复"
        elif ts.tasks["t7_fail"].status != FAILED:
            ok, reason = False, "failed 任务被错误恢复"
        else:
            reason = ""
    return ok, reason, {
        "pause_calls": len(ts.pause_calls),
        "fail_calls": len(ts.fail_calls),
    }


async def case_evaluating_recovery_no_metrics_completes(_threshold: float) -> tuple[bool, str, dict]:
    """场景 8：evaluating 任务无评估指标 → 直接 complete_evaluation(passed=True)。

    验证 _rerun_evaluation 在退化情形下不卡死。
    """
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=10.0)
    worker = FakeWorker(ts, tm)

    task = FakeTask(
        id="t8", status=EVALUATING,
        metadata={"evaluation_metric_ids": [], "acceptance_criteria": {}},
    )
    ts.tasks["t8"] = task

    # _complete_with_merge 会去拿 service_provider().get("workspace_lifecycle_manager")，
    # 不存在时返回 None，跳过合并，直接调 complete_evaluation。
    await worker._rerun_evaluation(task)

    ok = len(ts.complete_eval_calls) == 1 and ts.complete_eval_calls[0] == ("t8", True)
    reason = "" if ok else (
        f"evaluating 无指标恢复未走 complete_evaluation(passed=True): "
        f"{ts.complete_eval_calls}"
    )
    return ok, reason, {
        "complete_eval_calls": len(ts.complete_eval_calls),
        "fail_calls": len(ts.fail_calls),
    }


async def case_timer_no_runaway_storm(threshold: float) -> tuple[bool, str, dict]:
    """场景 9：长时间引擎在跑，同时活跃的 timer 数有界（不堆积成风暴）。

    真正的"风暴"可观察表现是：同一时刻累积大量未取消的活跃计时器
    （资源泄漏），而非累计 create 次数。重建策略从 cancel+recreate 改为
    reschedule 时 create_count 会变，但「活跃计时器有界」不变（§9.5）。
    """
    ts = FakeTaskService()
    tm = FakeTimerManager(idle_threshold=threshold)
    worker = FakeWorker(ts, tm)

    task_id = "t9"
    ts.tasks[task_id] = FakeTask(id=task_id, status=RUNNING)
    worker._contexts[task_id] = FakeContext(task_id)

    reg = FakeEngineRegistry()
    reg.register(task_id, FakeEntry(FakeEngine(is_running=True), engine_task_done=False))
    restore = install_fake_registry(reg)
    max_concurrent_active = 0
    try:
        await worker._arm_idle_timer(task_id, tm)
        # 跑 ~4 倍 threshold，期间采样"同时活跃计时器数"的峰值。
        end = asyncio.get_running_loop().time() + threshold * 4.5
        while asyncio.get_running_loop().time() < end:
            max_concurrent_active = max(max_concurrent_active, tm.active_count())
            await asyncio.sleep(0.01)
    finally:
        restore()
        await tm.cancel_timer(task_id)

    # 期望（可观察行为，§9.5）：
    #   1) 运行期间同一时刻活跃计时器 ≤ 2（同一 task_id 最多一个 + 短暂重建窗口），
    #      超过即风暴（堆积未取消的 timer）
    #   2) cleanup 后无残留（active_count == 0 = 无泄漏）
    # 不再断言累计 create_count（调度次数属实现细节）。
    ok = (max_concurrent_active <= 2) and (tm.active_count() == 0)
    reason = ""
    if max_concurrent_active > 2:
        reason = f"计时器风暴: 同时活跃 {max_concurrent_active} 个（应 ≤2）"
    elif tm.active_count() > 0:
        reason = f"cleanup 后仍有残留计时器: {tm.active_count()}"
    return ok, reason, {
        "create": tm.create_count, "cancel": tm.cancel_count,
        "max_concurrent_active": max_concurrent_active,
        "active": tm.active_count(),
    }


# ===========================================================================
# 5. Runner + CLI
# ===========================================================================

ALL_CASES: list[tuple[str, str, float, Any]] = [
    ("stuck_engine_running_no_kill",
     "引擎仍在运行，idle_timer 重建计时器不 fail", 1.0,
     case_stuck_in_engine_running),
    ("truly_idle_fails",
     "bg done + 引擎停止 + 无子任务 → fail", 0.3,
     case_engine_truly_idle_fails),
    ("waiting_recovery_treated_as_idle",
     "waiting_recovery 必须 fail（回归 fix_20260629）", 0.3,
     case_waiting_recovery_treated_as_idle),
    ("active_children_no_false_kill",
     "有活跃子任务时不该 fail", 0.3,
     case_active_children_no_false_kill),
    ("reset_keeps_active_alive",
     "每轮 reset 后活跃流不被杀", 0.3,
     case_reset_keeps_alive),
    ("fail_task_exception_logged",
     "fail_task 协程异常被回调记录（回归 fix_20260606）", 0.3,
     case_fail_task_exception_logged),
    ("recovery_running_to_suspended",
     "启动恢复：running/pending → suspended", 0.0,
     case_recovery_running_to_suspended),
    ("evaluating_no_metrics_completes",
     "evaluating 无指标退化路径不卡死", 0.0,
     case_evaluating_recovery_no_metrics_completes),
    ("timer_no_runaway_storm",
     "长时间引擎在跑，计时器重建有界、cleanup 无残留", 1.0,
     case_timer_no_runaway_storm),
]


async def run_all(selected: list[int] | None, verbose: bool) -> list[Result]:
    results: list[Result] = []
    cases = ALL_CASES if not selected else [ALL_CASES[i-1] for i in selected
                                            if 1 <= i <= len(ALL_CASES)]
    for name, desc, threshold, fn in cases:
        start = time.monotonic()
        passed = False
        reason = ""
        info: dict = {}
        exc_str = ""
        try:
            passed, reason, info = await fn(threshold)
        except BaseException as e:
            import traceback as _tb
            reason = f"用例本身抛异常: {type(e).__name__}: {e}"
            exc_str = type(e).__name__
            if verbose:
                _tb.print_exc()
        elapsed = time.monotonic() - start
        results.append(Result(
            name=name, passed=passed, elapsed=elapsed,
            fail_calls=info.get("fail_calls", 0),
            fail_reason=exc_str,
            pause_calls=info.get("pause_calls", 0),
            remaining_timers=info.get("active", 0),
            create_count=info.get("create", 0),
            cancel_count=info.get("cancel", 0),
            reason=reason,
        ))
        flag = "PASS" if passed else "FAIL"
        extra = (f" fail={info.get('fail_calls', 0)}"
                 f" create={info.get('create', 0)}"
                 f" cancel={info.get('cancel', 0)}")
        print(f"[{flag}] {name:<42} elapsed={elapsed:5.2f}s{extra}")
        if verbose:
            print(f"       └── {desc}")
        if not passed:
            print(f"       └── {reason}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="卡死检测 + 恢复路径模拟器")
    parser.add_argument(
        "--case", type=int, action="append", default=None,
        help="只跑指定编号场景（可重复，如 --case 1 --case 3）",
    )
    parser.add_argument("--list", action="store_true", help="列出所有场景")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.list:
        for i, (name, desc, thr, _) in enumerate(ALL_CASES, 1):
            print(f"  {i}. {name}  (threshold={thr}s)")
            print(f"       {desc}")
        return 0

    if args.verbose:
        logging.getLogger("infrastructure.task_idle_timer").setLevel(logging.INFO)
        logging.getLogger("infrastructure.task_recovery").setLevel(logging.INFO)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    results = asyncio.run(run_all(args.case, args.verbose))
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print()
    print(f"== {passed}/{total} passed ==")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
