# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""_TaskStateMixin / _TaskCleanupMixin 行为测试。

走真实 TaskService + TaskStorage（tmp data_dir）：
1. 状态查询：can_transition / get_valid_transitions（含 storage 未初始化降级）；
2. force_transition：合法/非法/容器任务跳过状态机/任务不存在；
3. pause/resume/start/move_to_evaluating：合法链、非法转换、元数据副作用
   （paused_by 记录、started_at 幂等、paused_by 清除）；
4. fail/cancel/complete：错误链追加、extra_meta 合并、级联（终态跳过）、
   complete_evaluation 通过/失败（summary/metrics/缺省 reason）；
5. recover_to_completed / reset_to_pending；
6. 清理：_cancel_pipeline（provider 缺 task_worker）、_is_child_of_container、
   _cleanup_pipeline_file（空/无存储/删除成功/异常）、_cascade_cleanup_subtasks
   （无后代/管道文件/工作空间/记录删除）、soft_delete_container 任务不存在、
   hard_delete_task 任务不存在、_cleanup_task_resources 隔离管理器路径、
   _remove_worktree（gitdir 文件 + 分支删除 + 失败留痕）。

外部依赖 mock 边界：isolation.manager / infrastructure.service_provider /
pipeline.registry 为跨进程/第三方句柄，按外部依赖 mock；TaskService 内部
方法用真实实现。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_EVICT_NAMES = (
    "task_types",
    "state_machine",
    "storage",
    "service",
    "timer_manager",
    "agents_types",
    "enum_utils",
    "workspace",
    "service_access",
    "_task_cleanup",
    "_task_crud",
    "_task_state",
    "server",
    "http_api",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """裸名逐出 + 代际还原（同 test_tasks_plugin.py，串扰防线）。"""
    d = str(_PLUGIN_DIR)
    was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if was_present:
        sys.path.insert(0, d)
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


@pytest.fixture()
def svc(tmp_path: Path) -> Any:
    from service import TaskService

    return TaskService(data_dir=str(tmp_path / "tasks"))


def _install_fake_package(monkeypatch: pytest.MonkeyPatch, dotted: str, module: ModuleType) -> None:
    """注册假包层级（from a.b import x 需要 a 与 a.b 都在 sys.modules）。"""
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, ModuleType(parent))
    monkeypatch.setitem(sys.modules, dotted, module)


class TestStateQueries:
    def test_can_transition(self, svc: Any) -> None:
        import asyncio

        task = asyncio.run(svc.create_task(title="q"))
        assert svc.can_transition(task.id, "running") is True
        assert svc.can_transition(task.id, "evaluating") is False  # pending 不允许
        assert svc.can_transition("missing", "running") is False

    def test_can_transition_storage_none(self) -> None:
        from service import TaskService

        s = TaskService(task_id="t")
        assert s.can_transition("x", "running") is False
        assert s.get_valid_transitions("x") == []

    def test_get_valid_transitions(self, svc: Any) -> None:
        import asyncio

        task = asyncio.run(svc.create_task(title="g"))
        transitions = svc.get_valid_transitions(task.id)
        assert set(transitions) == {"running", "stopped", "completed", "failed"}
        assert svc.get_valid_transitions("missing") == []


class TestStorageNoneErrorBranches:
    """非门面模式（storage=None）下各状态方法按契约抛 KeyError。"""

    @pytest.mark.asyncio
    async def test_pause_resume_start_move_raise_keyerror(self) -> None:
        from service import TaskService

        s = TaskService(task_id="t")
        with pytest.raises(KeyError, match="任务不存在"):
            await s.pause_task("x")
        with pytest.raises(KeyError, match="任务不存在"):
            await s.resume_task("x")
        with pytest.raises(KeyError, match="任务不存在"):
            await s.start_task("x")
        with pytest.raises(KeyError, match="任务不存在"):
            await s.move_to_evaluating("x")

    @pytest.mark.asyncio
    async def test_fail_cancel_complete_silent(self) -> None:
        from service import TaskService

        s = TaskService(task_id="t")
        assert await s.fail_task("x") is None
        assert await s.cancel_task("x") is None
        assert await s.complete_task("x") is None
        assert await s.complete_evaluation("x", passed=True) is None
        assert await s.recover_to_completed("x") is None
        assert await s.reset_to_pending("x") is None


class TestForceTransition:
    @pytest.mark.asyncio
    async def test_legal_transition(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="f")
        await svc.force_transition(task.id, TaskStatus.RUNNING)
        assert svc.get_task(task.id).status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_illegal_transition_raises(self, svc: Any) -> None:
        from state_machine import InvalidTransitionError
        from task_types import TaskStatus

        task = await svc.create_task(title="i")
        with pytest.raises(InvalidTransitionError):
            await svc.force_transition(task.id, TaskStatus.EVALUATING)

    @pytest.mark.asyncio
    async def test_container_skips_state_machine(self, svc: Any) -> None:
        from task_types import TaskStatus

        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        # 容器任务允许任意互转（含 pending → completed 这种状态机外路径）
        await svc.force_transition(container.id, TaskStatus.COMPLETED)
        assert svc.get_task(container.id).status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_missing_task_raises(self, svc: Any) -> None:
        from task_types import TaskStatus

        with pytest.raises(KeyError, match="任务不存在"):
            await svc.force_transition("missing", TaskStatus.RUNNING)

    @pytest.mark.asyncio
    async def test_storage_none_raises(self) -> None:
        from service import TaskService
        from task_types import TaskStatus

        s = TaskService(task_id="t")
        with pytest.raises(KeyError, match="任务不存在"):
            await s.force_transition("x", TaskStatus.RUNNING)


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_records_paused_by(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="p")
        await svc.start_task(task.id)
        await svc.pause_task(task.id, paused_by="system")
        fetched = svc.get_task(task.id)
        assert fetched.status == TaskStatus.STOPPED
        assert fetched.metadata["paused_by"] == "system"

    @pytest.mark.asyncio
    async def test_pause_creates_metadata_when_none(self, svc: Any) -> None:
        """metadata 为 None 时 pause 新建 dict 记录 paused_by。"""
        task = await svc.create_task(title="p-meta")
        task.metadata = None
        await svc.save_task(task)
        await svc.start_task(task.id)
        await svc.pause_task(task.id, paused_by="user")
        fetched = svc.get_task(task.id)
        assert fetched.metadata["paused_by"] == "user"

    @pytest.mark.asyncio
    async def test_pause_invalid_transition(self, svc: Any) -> None:
        from state_machine import InvalidTransitionError
        from task_types import TaskStatus

        task = await svc.create_task(title="p2")
        await svc.start_task(task.id)
        await svc.force_transition(task.id, TaskStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            await svc.pause_task(task.id)  # completed 不允许暂停

    @pytest.mark.asyncio
    async def test_pause_missing_task(self, svc: Any) -> None:
        with pytest.raises(KeyError, match="任务不存在"):
            await svc.pause_task("missing")

    @pytest.mark.asyncio
    async def test_resume_sets_started_at_and_clears_paused_by(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="r")
        await svc.start_task(task.id)
        await svc.pause_task(task.id)
        resumed = await svc.resume_task(task.id)
        assert resumed.status == TaskStatus.RUNNING
        assert resumed.started_at is not None
        assert "paused_by" not in resumed.metadata

    @pytest.mark.asyncio
    async def test_resume_keeps_existing_started_at(self, svc: Any) -> None:
        task = await svc.create_task(title="r2")
        await svc.start_task(task.id)
        started = task.started_at
        await svc.pause_task(task.id)
        resumed = await svc.resume_task(task.id)
        assert resumed.started_at == started  # 幂等：不覆盖已有起点

    @pytest.mark.asyncio
    async def test_resume_invalid_transition(self, svc: Any) -> None:
        from state_machine import InvalidTransitionError

        task = await svc.create_task(title="r3")
        with pytest.raises(InvalidTransitionError):
            await svc.resume_task(task.id)  # pending 不允许恢复

    @pytest.mark.asyncio
    async def test_resume_missing_task(self, svc: Any) -> None:
        with pytest.raises(KeyError, match="任务不存在"):
            await svc.resume_task("missing")

    @pytest.mark.asyncio
    async def test_resume_wakes_suspended_engine(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resume 唤醒挂起引擎（pipeline.registry 为外部句柄，mock）。"""
        from task_types import TaskStatus

        task = await svc.create_task(title="w")
        await svc.start_task(task.id)
        await svc.pause_task(task.id)

        woke: list[str] = []

        class FakeEngine:
            is_suspended = True

            def wake(self) -> None:
                woke.append("woke")

        class FakeEntry:
            pipeline_id = "pipe-1234567890"
            engine = FakeEngine()

        class FakeRegistry:
            def find_by_tag(self, tag: str, value: str) -> list:
                assert tag == "task_id"
                return [FakeEntry()]

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": lambda self: FakeRegistry()})()
        )
        await svc.resume_task(task.id)
        assert woke == ["woke"]

    @pytest.mark.asyncio
    async def test_resume_engine_wake_failure_non_fatal(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="w2")
        await svc.start_task(task.id)
        await svc.pause_task(task.id)

        def boom() -> Any:
            raise RuntimeError("registry down")

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": boom})()
        )
        resumed = await svc.resume_task(task.id)
        assert resumed.status.value == "running"  # 唤醒失败不阻断恢复


class TestStartMoveEvaluating:
    @pytest.mark.asyncio
    async def test_start_idempotent_started_at(self, svc: Any) -> None:
        task = await svc.create_task(title="s")
        await svc.start_task(task.id)
        first_started = svc.get_task(task.id).started_at
        await svc.start_task(task.id)  # running → running 幂等
        assert svc.get_task(task.id).started_at == first_started

    @pytest.mark.asyncio
    async def test_start_invalid_transition(self, svc: Any) -> None:
        from state_machine import InvalidTransitionError
        from task_types import TaskStatus

        task = await svc.create_task(title="s2")
        await svc.start_task(task.id)
        await svc.force_transition(task.id, TaskStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            await svc.start_task(task.id)

    @pytest.mark.asyncio
    async def test_start_missing_task(self, svc: Any) -> None:
        with pytest.raises(KeyError, match="任务不存在"):
            await svc.start_task("missing")

    @pytest.mark.asyncio
    async def test_move_to_evaluating(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="m")
        await svc.start_task(task.id)
        await svc.move_to_evaluating(task.id)
        assert svc.get_task(task.id).status == TaskStatus.EVALUATING

    @pytest.mark.asyncio
    async def test_move_to_evaluating_invalid(self, svc: Any) -> None:
        from state_machine import InvalidTransitionError

        task = await svc.create_task(title="m2")
        with pytest.raises(InvalidTransitionError):
            await svc.move_to_evaluating(task.id)  # pending 不允许

    @pytest.mark.asyncio
    async def test_move_to_evaluating_missing(self, svc: Any) -> None:
        with pytest.raises(KeyError, match="任务不存在"):
            await svc.move_to_evaluating("missing")


class TestFailCancelComplete:
    @pytest.mark.asyncio
    async def test_fail_appends_error_chain(self, svc: Any) -> None:
        task = await svc.create_task(title="f")
        await svc.start_task(task.id)
        await svc.fail_task(task.id, reason="第一错")
        await svc.fail_task(task.id, reason="第二错")
        fetched = svc.get_task(task.id)
        assert fetched.error == "第一错 → 第二错"
        assert fetched.metadata["fail_reason"] == "第二错"

    @pytest.mark.asyncio
    async def test_fail_merges_extra_meta(self, svc: Any) -> None:
        task = await svc.create_task(title="f2")
        await svc.start_task(task.id)
        await svc.fail_task(task.id, extra_meta={"error_type": "timeout"})
        assert svc.get_task(task.id).metadata["error_type"] == "timeout"

    @pytest.mark.asyncio
    async def test_fail_missing_task_silent(self, svc: Any) -> None:
        assert await svc.fail_task("missing") is None

    @pytest.mark.asyncio
    async def test_fail_cascade_cancels_subtasks(self, svc: Any) -> None:
        from task_types import TaskStatus

        parent = await svc.create_task(title="父")
        c1 = await svc.create_task(title="子1", parent_task_id=parent.id)
        c2 = await svc.create_task(title="子2", parent_task_id=parent.id)
        await svc.start_task(c1.id)
        await svc.start_task(c2.id)
        await svc.fail_task(parent.id, reason="父失败")
        assert svc.get_task(c1.id).status == TaskStatus.STOPPED
        assert svc.get_task(c2.id).status == TaskStatus.STOPPED

    @pytest.mark.asyncio
    async def test_fail_cascade_skips_terminal_subtasks(self, svc: Any) -> None:
        from task_types import TaskStatus

        parent = await svc.create_task(title="父2")
        done = await svc.create_task(title="已完成", parent_task_id=parent.id)
        await svc.start_task(done.id)
        await svc.complete_task(done.id)
        running = await svc.create_task(title="运行中", parent_task_id=parent.id)
        await svc.start_task(running.id)
        await svc.fail_task(parent.id, reason="父失败")
        assert svc.get_task(done.id).status == TaskStatus.COMPLETED  # 终态跳过
        assert svc.get_task(running.id).status == TaskStatus.STOPPED

    @pytest.mark.asyncio
    async def test_cancel_records_reason_and_chain(self, svc: Any) -> None:
        task = await svc.create_task(title="c")
        await svc.start_task(task.id)
        await svc.cancel_task(task.id, reason="不需要")
        fetched = svc.get_task(task.id)
        assert fetched.status.value == "stopped"
        assert fetched.metadata["cancel_reason"] == "不需要"
        assert fetched.error == "不需要"

    @pytest.mark.asyncio
    async def test_cancel_missing_task_silent(self, svc: Any) -> None:
        assert await svc.cancel_task("missing") is None

    @pytest.mark.asyncio
    async def test_cancel_appends_error_chain(self, svc: Any) -> None:
        task = await svc.create_task(title="c2")
        await svc.start_task(task.id)
        await svc.fail_task(task.id, reason="先错")
        await svc.cancel_task(task.id, reason="后取消")
        fetched = svc.get_task(task.id)
        assert fetched.error == "先错 → 后取消"

    @pytest.mark.asyncio
    async def test_fail_emit_exception_isolated(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """fail_task 三步隔离：状态变更通知失败不阻断级联与容器检查。"""
        task = await svc.create_task(title="f-iso")

        async def boom(task_id: str, old: str, new: str) -> None:
            raise RuntimeError("emit down")

        monkeypatch.setattr(svc, "_emit_state_change", boom)
        monkeypatch.setattr(svc, "fail_task_cascade", lambda task_id, reason="": 0)
        monkeypatch.setattr(svc, "_try_destroy_container_if_idle", lambda task_id: None)
        await svc.fail_task(task.id, reason="x")
        assert svc.get_task(task.id).status.value == "failed"  # 状态仍落盘

    @pytest.mark.asyncio
    async def test_fail_cascade_exception_isolated(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="f-iso2")

        async def boom(task_id: str, reason: str = "") -> int:
            raise RuntimeError("cascade down")

        monkeypatch.setattr(svc, "fail_task_cascade", boom)
        monkeypatch.setattr(svc, "_try_destroy_container_if_idle", lambda task_id: None)
        await svc.fail_task(task.id, reason="x")
        assert svc.get_task(task.id).status.value == "failed"

    @pytest.mark.asyncio
    async def test_fail_destroy_exception_isolated(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="f-iso3")

        async def boom(task_id: str) -> None:
            raise RuntimeError("destroy down")

        monkeypatch.setattr(svc, "fail_task_cascade", lambda task_id, reason="": 0)
        monkeypatch.setattr(svc, "_try_destroy_container_if_idle", boom)
        await svc.fail_task(task.id, reason="x")
        assert svc.get_task(task.id).status.value == "failed"

    @pytest.mark.asyncio
    async def test_try_destroy_container_if_idle_success(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destroyed: list[str] = []

        class FakeManager:
            async def destroy_if_workspace_idle(self, task_id: str) -> None:
                destroyed.append(task_id)

        async def _get_manager(self: Any) -> Any:
            return FakeManager()

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": _get_manager})()
        )
        await svc._try_destroy_container_if_idle("t-1")
        assert destroyed == ["t-1"]

    @pytest.mark.asyncio
    async def test_try_destroy_container_if_idle_failure_non_fatal(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> Any:
            raise RuntimeError("manager down")

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        await svc._try_destroy_container_if_idle("t-2")  # 不抛异常即通过

    @pytest.mark.asyncio
    async def test_cancel_cascade_storage_none(self) -> None:
        from service import TaskService

        s = TaskService(task_id="t")
        assert await s.cancel_task_cascade("x") == 0
        assert await s.fail_task_cascade("x") == 0

    @pytest.mark.asyncio
    async def test_complete_task(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="done")
        await svc.start_task(task.id)
        await svc.complete_task(task.id)
        assert svc.get_task(task.id).status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_complete_missing_task_silent(self, svc: Any) -> None:
        assert await svc.complete_task("missing") is None


class TestCompleteEvaluation:
    @pytest.mark.asyncio
    async def test_passed_completes_with_result(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="e")
        await svc.start_task(task.id)
        await svc.complete_evaluation(task.id, passed=True, result={"score": 0.9})
        fetched = svc.get_task(task.id)
        assert fetched.status == TaskStatus.COMPLETED
        assert fetched.result == {"score": 0.9}

    @pytest.mark.asyncio
    async def test_failed_with_summary_reason(self, svc: Any) -> None:
        task = await svc.create_task(title="e2")
        await svc.start_task(task.id)
        await svc.complete_evaluation(
            task.id, passed=False, result={"summary": "输出不完整"},
        )
        fetched = svc.get_task(task.id)
        assert fetched.status.value == "failed"
        assert fetched.error == "评估未通过: 输出不完整"

    @pytest.mark.asyncio
    async def test_failed_with_metric_reasons(self, svc: Any) -> None:
        task = await svc.create_task(title="e3")
        await svc.start_task(task.id)
        await svc.complete_evaluation(
            task.id,
            passed=False,
            result={
                "metrics": [
                    {"metric_id": "m1", "passed": False, "message": "差"},
                    {"metric_id": "m2", "passed": True},
                    {"metric_id": "m3", "passed": False, "error": "崩"},
                ]
            },
        )
        fetched = svc.get_task(task.id)
        assert fetched.status.value == "failed"
        assert fetched.error == "评估未通过: m1: 差, m3: 崩"

    @pytest.mark.asyncio
    async def test_failed_default_reason(self, svc: Any) -> None:
        task = await svc.create_task(title="e4")
        await svc.start_task(task.id)
        await svc.complete_evaluation(task.id, passed=False)
        assert svc.get_task(task.id).error == "评估未通过"

    @pytest.mark.asyncio
    async def test_missing_task_silent(self, svc: Any) -> None:
        assert await svc.complete_evaluation("missing", passed=True) is None


class TestRecoverReset:
    @pytest.mark.asyncio
    async def test_recover_to_completed(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="rec")
        await svc.start_task(task.id)
        await svc.fail_task(task.id, reason="误判")
        await svc.recover_to_completed(task.id, result={"ok": True})
        fetched = svc.get_task(task.id)
        assert fetched.status == TaskStatus.COMPLETED
        assert fetched.result == {"ok": True}
        assert fetched.completed_at is not None

    @pytest.mark.asyncio
    async def test_recover_missing_task_silent(self, svc: Any) -> None:
        assert await svc.recover_to_completed("missing") is None

    def test_recover_to_completed_persists_to_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """恢复为 completed 必须落盘：新 storage 实例重读同一目录可见。

        语义上等价于进程重启后任务不得复活为 failed（对照 complete_task /
        reset_to_pending 均持久化）。task_evaluate 放行恢复路径的下游消费者。
        """
        import asyncio

        from service import TaskService

        data_dir = str(tmp_path / "tasks")
        first = TaskService(data_dir=data_dir)
        task = asyncio.run(first.create_task(title="rec-disk"))
        asyncio.run(first.start_task(task.id))
        asyncio.run(first.fail_task(task.id, reason="误判"))

        reloaded = TaskService(data_dir=data_dir)
        assert reloaded.get_task(task.id).status.value == "failed"

        asyncio.run(first.recover_to_completed(task.id, result={"ok": True}))

        after = TaskService(data_dir=data_dir)
        fetched = after.get_task(task.id)
        assert fetched.status.value == "completed"
        assert fetched.result == {"ok": True}
        assert fetched.completed_at is not None

    @pytest.mark.asyncio
    async def test_reset_to_pending_clears_started_at(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(title="rst")
        await svc.start_task(task.id)
        await svc.fail_task(task.id)
        reset = await svc.reset_to_pending(task.id)
        assert reset is not None
        assert reset.status == TaskStatus.PENDING
        assert reset.started_at is None

    @pytest.mark.asyncio
    async def test_reset_missing_task_returns_none(self, svc: Any) -> None:
        assert await svc.reset_to_pending("missing") is None


class TestContextUsageInjection:
    @pytest.mark.asyncio
    async def test_inject_context_usage(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="ctx")
        await svc.bind_pipeline_run(task.id, "pipe-ctx-1")

        class FakeState:
            def get(self, key: str, default: Any = None) -> Any:
                return {
                    "context_window": 1000,
                    "llm_usage": {"input_tokens": 250},
                }.get(key, default)

        class FakeEngine:
            _current_state = FakeState()

        class FakeEntry:
            engine = FakeEngine()

        class FakeRegistry:
            def get(self, pipeline_id: str) -> Any:
                return FakeEntry()

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": lambda self: FakeRegistry()})()
        )
        await svc.complete_evaluation(task.id, passed=True)
        meta = svc.get_task(task.id).metadata
        assert meta["context_usage"]["pct"] == 25.0
        assert meta["context_usage"]["input_tokens"] == 250
        assert meta["context_usage"]["context_window"] == 1000

    @pytest.mark.asyncio
    async def test_inject_context_usage_no_pipeline_run(self, svc: Any) -> None:
        task = await svc.create_task(title="ctx2")
        await svc.complete_evaluation(task.id, passed=True)
        assert "context_usage" not in svc.get_task(task.id).metadata
        task = await svc.create_task(title="ctx2")
        await svc.complete_evaluation(task.id, passed=True)
        assert "context_usage" not in svc.get_task(task.id).metadata

    @pytest.mark.asyncio
    async def test_inject_context_usage_registry_failure(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="ctx3")
        await svc.bind_pipeline_run(task.id, "pipe-ctx-3")

        def boom() -> Any:
            raise RuntimeError("registry down")

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": boom})()
        )
        await svc.complete_evaluation(task.id, passed=True)
        assert "context_usage" not in svc.get_task(task.id).metadata  # 遥测失败不阻断

    @pytest.mark.asyncio
    async def test_inject_context_usage_no_entry(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="ctx4")
        await svc.bind_pipeline_run(task.id, "pipe-ctx-4")

        class FakeRegistry:
            def get(self, pipeline_id: str) -> Any:
                return None

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": lambda self: FakeRegistry()})()
        )
        await svc.complete_evaluation(task.id, passed=True)
        assert "context_usage" not in svc.get_task(task.id).metadata

    @pytest.mark.asyncio
    async def test_inject_context_usage_no_state(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="ctx5")
        await svc.bind_pipeline_run(task.id, "pipe-ctx-5")

        class FakeEngine:
            _current_state = None

        class FakeEntry:
            engine = FakeEngine()

        class FakeRegistry:
            def get(self, pipeline_id: str) -> Any:
                return FakeEntry()

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": lambda self: FakeRegistry()})()
        )
        await svc.complete_evaluation(task.id, passed=True)
        assert "context_usage" not in svc.get_task(task.id).metadata

    @pytest.mark.asyncio
    async def test_inject_context_usage_zero_window(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="ctx6")
        await svc.bind_pipeline_run(task.id, "pipe-ctx-6")

        class FakeState:
            def get(self, key: str, default: Any = None) -> Any:
                return {"context_window": 0, "llm_usage": {"input_tokens": 10}}.get(key, default)

        class FakeEngine:
            _current_state = FakeState()

        class FakeEntry:
            engine = FakeEngine()

        class FakeRegistry:
            def get(self, pipeline_id: str) -> Any:
                return FakeEntry()

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": lambda self: FakeRegistry()})()
        )
        await svc.complete_evaluation(task.id, passed=True)
        assert "context_usage" not in svc.get_task(task.id).metadata  # 窗口为 0 跳过

    @pytest.mark.asyncio
    async def test_inject_context_usage_metadata_none(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        task = await svc.create_task(title="ctx7")
        await svc.bind_pipeline_run(task.id, "pipe-ctx-7")
        task.metadata = None
        await svc.save_task(task)

        class FakeState:
            def get(self, key: str, default: Any = None) -> Any:
                return {"context_window": 100, "llm_usage": {"input_tokens": 50}}.get(key, default)

        class FakeEngine:
            _current_state = FakeState()

        class FakeEntry:
            engine = FakeEngine()

        class FakeRegistry:
            def get(self, pipeline_id: str) -> Any:
                return FakeEntry()

        _install_fake_package(
            monkeypatch, "pipeline.registry", type("R", (), {"get_engine_registry": lambda self: FakeRegistry()})()
        )
        await svc.complete_evaluation(task.id, passed=True)
        meta = svc.get_task(task.id).metadata
        assert meta["context_usage"]["pct"] == 50.0  # metadata None 时新建


class TestCleanupHelpers:
    def test_cancel_pipeline_no_task_worker(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """provider 无 task_worker 时静默跳过。"""
        class FakeProvider:
            def get(self, key: str) -> Any:
                return None

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        svc._cancel_pipeline("t-1")  # 不抛异常即通过

    def test_cancel_pipeline_cancels(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        cancelled: list[str] = []

        class FakeWorker:
            def cancel_pipeline(self, task_id: str) -> bool:
                cancelled.append(task_id)
                return True

        class FakeProvider:
            def get(self, key: str) -> Any:
                return FakeWorker() if key == "task_worker" else None

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        svc._cancel_pipeline("t-2")
        assert cancelled == ["t-2"]

    def test_cancel_pipeline_exception_non_fatal(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise RuntimeError("provider down")

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": boom})(),
        )
        svc._cancel_pipeline("t-3")  # 不抛异常即通过

    def test_is_child_of_container(self, svc: Any) -> None:
        import asyncio

        container = asyncio.run(svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        ))
        child = asyncio.run(svc.create_task(title="子", parent_task_id=container.id))
        assert svc._is_child_of_container(child) is True
        assert svc._is_child_of_container(container) is False
        assert svc._is_child_of_container(asyncio.run(svc.create_task(title="独立"))) is False

    def test_is_child_of_non_container_root(self, svc: Any) -> None:
        """根任务存在但非容器 → False。"""
        import asyncio

        root = asyncio.run(svc.create_task(title="普通根"))
        child = asyncio.run(svc.create_task(title="子", parent_task_id=root.id))
        assert svc._is_child_of_container(child) is False

    def test_is_child_of_missing_root(self, svc: Any) -> None:
        """根任务记录已删（get_task 返回 None）→ False。"""
        import asyncio

        root = asyncio.run(svc.create_task(title="根"))
        child = asyncio.run(svc.create_task(title="子", parent_task_id=root.id))
        svc.hard_delete_sync(root.id)  # 直接删根记录，不级联
        assert svc._is_child_of_container(child) is False

    def test_get_execution_record_storage_import(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """_get_execution_record_storage 委托 infrastructure.service_access。"""
        sentinel = object()

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_access",
            type("SA", (), {"get_execution_record_storage": lambda self: sentinel})(),
        )
        assert svc._get_execution_record_storage() is sentinel

    def test_cleanup_pipeline_file_empty(self, svc: Any) -> None:
        assert svc._cleanup_pipeline_file("") is False

    def test_cleanup_pipeline_file_no_storage(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc, "_get_execution_record_storage", lambda: None)
        assert svc._cleanup_pipeline_file("pipe-1") is False

    def test_cleanup_pipeline_file_deletes(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeStorage:
            def delete_by_session(self, session_id: str) -> int:
                return 2

        monkeypatch.setattr(svc, "_get_execution_record_storage", lambda: FakeStorage())
        assert svc._cleanup_pipeline_file("pipe-1") is True

    def test_cleanup_pipeline_file_zero_deleted(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeStorage:
            def delete_by_session(self, session_id: str) -> int:
                return 0

        monkeypatch.setattr(svc, "_get_execution_record_storage", lambda: FakeStorage())
        assert svc._cleanup_pipeline_file("pipe-1") is False

    def test_cleanup_pipeline_file_exception(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise RuntimeError("storage down")

        monkeypatch.setattr(svc, "_get_execution_record_storage", boom)
        assert svc._cleanup_pipeline_file("pipe-1") is False

    @pytest.mark.asyncio
    async def test_cascade_cleanup_no_descendants(self, svc: Any) -> None:
        task = await svc.create_task(title="孤")
        stats = await svc._cascade_cleanup_subtasks(task.id)
        assert stats["subtasks_deleted"] == 0

    @pytest.mark.asyncio
    async def test_cascade_cleanup_full(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = await svc.create_task(title="父")
        child = await svc.create_task(
            title="子", parent_task_id=parent.id,
            metadata={"workspace": str(Path("ws-child"))},
        )
        await svc.bind_pipeline_run(child.id, "pipe-child")

        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: True)
        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            return {"workspace_cleaned": True, "errors": []}

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)
        stats = await svc._cascade_cleanup_subtasks(parent.id)
        assert stats["subtasks_deleted"] == 1
        assert stats["pipeline_files_cleaned"] == 1
        assert stats["workspaces_cleaned"] == 1
        assert svc.get_task(child.id) is None

    @pytest.mark.asyncio
    async def test_cascade_cleanup_skip_workspace(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = await svc.create_task(title="父2")
        child = await svc.create_task(
            title="子2", parent_task_id=parent.id,
            metadata={"workspace": "ws-same"},
        )
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)
        stats = await svc._cascade_cleanup_subtasks(
            parent.id, skip_workspace=True, container_workspace="ws-same",
        )
        assert stats["subtasks_deleted"] == 1
        assert stats["workspaces_cleaned"] == 0

    @pytest.mark.asyncio
    async def test_cascade_cleanup_same_workspace_skipped(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """子任务 workspace 与容器相同 → 跳过清理。"""
        parent = await svc.create_task(title="父3")
        child = await svc.create_task(
            title="子3", parent_task_id=parent.id,
            metadata={"workspace": "ws-shared"},
        )
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)
        stats = await svc._cascade_cleanup_subtasks(
            parent.id, skip_workspace=False, container_workspace="ws-shared",
        )
        assert stats["subtasks_deleted"] == 1
        assert stats["workspaces_cleaned"] == 0

    @pytest.mark.asyncio
    async def test_cascade_cleanup_cleanup_exception(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = await svc.create_task(title="父4")
        child = await svc.create_task(
            title="子4", parent_task_id=parent.id,
            metadata={"workspace": "ws-child4"},
        )
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            raise RuntimeError("cleanup crash")

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)
        stats = await svc._cascade_cleanup_subtasks(parent.id)
        assert stats["subtasks_deleted"] == 1  # 记录仍删除
        assert any("工作空间清理失败" in e for e in stats["errors"])

    @pytest.mark.asyncio
    async def test_cascade_cleanup_hard_delete_exception(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = await svc.create_task(title="父5")
        child = await svc.create_task(title="子5", parent_task_id=parent.id)
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)

        async def _boom(task_id: str) -> bool:
            raise RuntimeError("delete crash")

        monkeypatch.setattr(svc, "hard_delete", _boom)
        stats = await svc._cascade_cleanup_subtasks(parent.id)
        assert stats["subtasks_deleted"] == 0
        assert any("记录删除失败" in e for e in stats["errors"])

    @pytest.mark.asyncio
    async def test_cascade_cleanup_missing_descendant_skipped(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """后代任务记录已不存在 → 跳过该后代。"""
        parent = await svc.create_task(title="父6")
        child = await svc.create_task(title="子6", parent_task_id=parent.id)
        svc.hard_delete_sync(child.id)  # 记录已删，但父的级联仍会枚举
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)
        stats = await svc._cascade_cleanup_subtasks(parent.id)
        assert stats["subtasks_deleted"] == 0

    @pytest.mark.asyncio
    async def test_soft_delete_container_missing(self, svc: Any) -> None:
        result = await svc.soft_delete_container("missing")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_soft_delete_container_metadata_none(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """容器任务 metadata 为 None 时软删除仍落 soft_deleted 标记。"""
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        container.metadata = None
        await svc.save_task(container)
        monkeypatch.setattr(svc, "_cancel_pipeline_recursive", lambda task_id: None)

        async def _no_cascade(task_id: str, reason: str = "") -> int:
            return 0

        async def _no_cleanup(task_id: str, skip_workspace: bool = False, container_workspace: str = "") -> dict[str, Any]:
            return {"subtasks_deleted": 0}

        monkeypatch.setattr(svc, "cancel_task_cascade", _no_cascade)
        monkeypatch.setattr(svc, "_cascade_cleanup_subtasks", _no_cleanup)
        result = await svc.soft_delete_container(container.id)
        assert result["soft_deleted"] is True
        fetched = svc.get_task(container.id)
        assert fetched.metadata["soft_deleted"] is True
        assert fetched.error == "已取消: 用户请求删除"

    @pytest.mark.asyncio
    async def test_hard_delete_task_missing(self, svc: Any) -> None:
        result = await svc.hard_delete_task("missing")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_hard_delete_task_with_subtasks_and_notifier(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """硬删除带子任务：级联清理 + 管道文件 + ws_notifier 推送。"""
        parent = await svc.create_task(
            title="父", metadata={"user_id": "u-1", "workspace": "ws-parent"},
        )
        child = await svc.create_task(title="子", parent_task_id=parent.id)
        await svc.bind_pipeline_run(parent.id, "pipe-parent")

        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: True)

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            return {"workspace_cleaned": True, "errors": []}

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)
        sent: list[tuple[str, dict]] = []

        class FakeNotifier:
            async def send_to_user(self, user_id: str, payload: dict) -> None:
                sent.append((user_id, payload))

        class FakeProvider:
            def get(self, key: str) -> Any:
                return FakeNotifier() if key == "ws_interaction_notifier" else None

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        result = await svc.hard_delete_task(parent.id)
        assert result["deleted"] is True
        assert result["pipeline_file_cleaned"] is True
        assert result["cascade_cleanup"]["subtasks_deleted"] == 1
        assert sent == [("u-1", {"type": "task_deleted", "data": {"task_id": parent.id, "title": "父"}})]
        assert svc.get_task(parent.id) is None
        assert svc.get_task(child.id) is None

    @pytest.mark.asyncio
    async def test_hard_delete_task_notifier_missing_user_id(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """metadata 缺 user_id → 不推送（不抛异常）。"""
        task = await svc.create_task(title="无用户")
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            return {"workspace_cleaned": True, "errors": []}

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)

        class FakeNotifier:
            async def send_to_user(self, user_id: str, payload: dict) -> None:
                raise AssertionError("不应推送")

        class FakeProvider:
            def get(self, key: str) -> Any:
                return FakeNotifier() if key == "ws_interaction_notifier" else None

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        result = await svc.hard_delete_task(task.id)
        assert result["deleted"] is True

    @pytest.mark.asyncio
    async def test_hard_delete_task_notifier_exception_non_fatal(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = await svc.create_task(title="通知炸", metadata={"user_id": "u-2"})
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            return {"workspace_cleaned": True, "errors": []}

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)

        class FakeNotifier:
            async def send_to_user(self, user_id: str, payload: dict) -> None:
                raise RuntimeError("notifier down")

        class FakeProvider:
            def get(self, key: str) -> Any:
                return FakeNotifier() if key == "ws_interaction_notifier" else None

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        result = await svc.hard_delete_task(task.id)
        assert result["deleted"] is True  # 通知失败不阻断删除

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_empty(self, svc: Any) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        result = await svc._cleanup_subtask_worktrees(container, [])
        assert result["total_subtasks"] == 0
        assert result["cleaned_count"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_skips_no_workspace(self, svc: Any) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        child = await svc.create_task(title="无工作空间", parent_task_id=container.id)
        result = await svc._cleanup_subtask_worktrees(container, [child])
        assert result["skipped_count"] == 1
        assert result["cleaned_count"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_skips_same_workspace(self, svc: Any) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container", "workspace": "ws-shared"},
        )
        child = await svc.create_task(
            title="同工作空间", parent_task_id=container.id, metadata={"workspace": "ws-shared"},
        )
        result = await svc._cleanup_subtask_worktrees(container, [child])
        assert result["skipped_count"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_lifecycle_path(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container", "workspace": "ws-container"},
        )
        child = await svc.create_task(
            title="子", parent_task_id=container.id, metadata={"workspace": "ws-child"},
        )

        class FakeLifecycle:
            def restore_ws_meta(self, task_id: str) -> None:
                pass

            def cleanup_workspace(self, task_id: str) -> dict:
                return {"worktree_removed": True, "dir_removed": False}

        class FakeProvider:
            def get(self, key: str) -> Any:
                return FakeLifecycle() if key == "workspace_lifecycle_manager" else None

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        result = await svc._cleanup_subtask_worktrees(container, [child])
        assert result["cleaned_count"] == 1
        assert result["error_count"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_fallback_path(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """lifecycle 不可用 → 回退 _cleanup_task_resources。"""
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container", "workspace": "ws-container"},
        )
        child = await svc.create_task(
            title="子", parent_task_id=container.id, metadata={"workspace": "ws-child"},
        )

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            return {"workspace_cleaned": True, "errors": []}

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)
        result = await svc._cleanup_subtask_worktrees(container, [child])
        assert result["cleaned_count"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_fallback_errors(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container", "workspace": "ws-container"},
        )
        child = await svc.create_task(
            title="子", parent_task_id=container.id, metadata={"workspace": "ws-child"},
        )

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            return {"workspace_cleaned": False, "errors": ["boom"]}

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)
        result = await svc._cleanup_subtask_worktrees(container, [child])
        assert result["error_count"] == 1
        assert any("子任务" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_fallback_skipped(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container", "workspace": "ws-container"},
        )
        child = await svc.create_task(
            title="子", parent_task_id=container.id, metadata={"workspace": "ws-child"},
        )

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            return {"workspace_cleaned": False, "errors": []}

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)
        result = await svc._cleanup_subtask_worktrees(container, [child])
        assert result["skipped_count"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_subtask_worktrees_exception(self, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container", "workspace": "ws-container"},
        )
        child = await svc.create_task(
            title="子", parent_task_id=container.id, metadata={"workspace": "ws-child"},
        )

        async def _fake_cleanup(task_id: str, workspace: str | None) -> dict[str, Any]:
            raise RuntimeError("cleanup crash")

        monkeypatch.setattr(svc, "_cleanup_task_resources", _fake_cleanup)
        result = await svc._cleanup_subtask_worktrees(container, [child])
        assert result["error_count"] == 1
        assert any("子任务" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_hard_delete_task_skips_workspace_for_container_child(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        child = await svc.create_task(
            title="子", parent_task_id=container.id,
            metadata={"workspace": "ws-child"},
        )
        monkeypatch.setattr(svc, "_cleanup_pipeline_file", lambda pid: False)
        result = await svc.hard_delete_task(child.id)
        assert result["deleted"] is True
        assert result["cleanup"] == {"skipped": "容器子任务不清理工作空间"}
        assert svc.get_task(child.id) is None

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_isolation_path(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """隔离管理器路径：destroy_by_task_id 成功 + lifecycle 不可用回退。"""
        destroyed: list[str] = []

        class FakeManager:
            async def destroy_by_task_id(self, task_id: str) -> None:
                destroyed.append(task_id)

        async def _get_manager(self: Any) -> Any:
            return FakeManager()

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": _get_manager})()
        )
        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: type("P", (), {"get": lambda self2, k: None})()})(),
        )
        result = await svc._cleanup_task_resources("t-1", workspace=None)
        assert destroyed == ["t-1"]
        assert result["container_destroyed"] is True
        assert result["workspace_cleaned"] is False

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_workspace_rmtree(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """lifecycle 不可用 + 无隔离管理器 → 回退目录删除（安全路径校验）。"""
        ws_dir = tmp_path / "ws-1"
        ws_dir.mkdir()
        (ws_dir / "f.txt").write_text("x", encoding="utf-8")

        def boom() -> Any:
            raise RuntimeError("no isolation")

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: type("P", (), {"get": lambda self2, k: None})()})(),
        )
        _install_fake_package(
            monkeypatch,
            "isolation.workspace",
            type("W", (), {"get_workspace_config_root": lambda self: str(tmp_path)})(),
        )
        result = await svc._cleanup_task_resources("t-2", workspace=str(ws_dir))
        assert result["workspace_cleaned"] is True
        assert not ws_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_rejects_outside_root(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        outside = tmp_path.parent / "outside-ws"
        outside.mkdir(exist_ok=True)

        def boom() -> Any:
            raise RuntimeError("no isolation")

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: type("P", (), {"get": lambda self2, k: None})()})(),
        )
        _install_fake_package(
            monkeypatch,
            "isolation.workspace",
            type("W", (), {"get_workspace_config_root": lambda self: str(tmp_path)})(),
        )
        result = await svc._cleanup_task_resources("t-3", workspace=str(outside))
        assert result["workspace_cleaned"] is False
        assert any("安全拦截" in e for e in result["errors"])
        assert outside.exists()  # 未删除

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_relative_workspace(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """相对 workspace 拼配置根后删除。"""
        ws_dir = tmp_path / "ws-rel"
        ws_dir.mkdir()

        def boom() -> Any:
            raise RuntimeError("no isolation")

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: type("P", (), {"get": lambda self2, k: None})()})(),
        )
        _install_fake_package(
            monkeypatch,
            "isolation.workspace",
            type("W", (), {"get_workspace_config_root": lambda self: str(tmp_path)})(),
        )
        result = await svc._cleanup_task_resources("t-4", workspace="ws-rel")
        assert result["workspace_cleaned"] is True
        assert not ws_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_workspace_missing(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def boom() -> Any:
            raise RuntimeError("no isolation")

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: type("P", (), {"get": lambda self2, k: None})()})(),
        )
        _install_fake_package(
            monkeypatch,
            "isolation.workspace",
            type("W", (), {"get_workspace_config_root": lambda self: str(tmp_path)})(),
        )
        result = await svc._cleanup_task_resources("t-5", workspace="ws-not-exist")
        assert result["workspace_cleaned"] is False

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_lifecycle_path(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lifecycle 可用 → 经 lifecycle 清理工作空间（不落回退目录删除）。"""
        cleaned: list[str] = []

        class FakeLifecycle:
            def restore_ws_meta(self, task_id: str) -> None:
                pass

            def cleanup_workspace(self, task_id: str) -> bool:
                cleaned.append(task_id)
                return True

        class FakeProvider:
            def get(self, key: str) -> Any:
                return FakeLifecycle() if key == "workspace_lifecycle_manager" else None

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        result = await svc._cleanup_task_resources("t-6", workspace="ws-any")
        assert cleaned == ["t-6"]
        assert result["workspace_cleaned"] is True

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_lifecycle_exception_falls_back(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """lifecycle 抛异常 → 回退目录删除。"""
        ws_dir = tmp_path / "ws-fb"
        ws_dir.mkdir()

        class FakeLifecycle:
            def restore_ws_meta(self, task_id: str) -> None:
                raise RuntimeError("lifecycle down")

            def cleanup_workspace(self, task_id: str) -> bool:
                raise RuntimeError("lifecycle down")

        class FakeProvider:
            def get(self, key: str) -> Any:
                return FakeLifecycle() if key == "workspace_lifecycle_manager" else None

        def boom() -> Any:
            raise RuntimeError("no isolation")

        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: FakeProvider()})(),
        )
        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        _install_fake_package(
            monkeypatch,
            "isolation.workspace",
            type("W", (), {"get_workspace_config_root": lambda self: str(tmp_path)})(),
        )
        result = await svc._cleanup_task_resources("t-7", workspace=str(ws_dir))
        assert result["workspace_cleaned"] is True
        assert not ws_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_worktree_file(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """回退路径命中 .git 文件 → 走 _remove_worktree。"""
        ws_dir = tmp_path / "ws-wt"
        ws_dir.mkdir()
        (ws_dir / ".git").write_text("gitdir: /nonexistent/wt", encoding="utf-8")

        def boom() -> Any:
            raise RuntimeError("no isolation")

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: type("P", (), {"get": lambda self2, k: None})()})(),
        )
        _install_fake_package(
            monkeypatch,
            "isolation.workspace",
            type("W", (), {"get_workspace_config_root": lambda self: str(tmp_path)})(),
        )
        removed: list[Path] = []
        monkeypatch.setattr(svc, "_remove_worktree", lambda p, r: removed.append(p))
        result = await svc._cleanup_task_resources("t-8", workspace=str(ws_dir))
        assert removed == [ws_dir]
        assert result["workspace_cleaned"] is False  # _remove_worktree 未置位

    @pytest.mark.asyncio
    async def test_cleanup_task_resources_rmtree_exception(
        self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ws_dir = tmp_path / "ws-err"
        ws_dir.mkdir()

        def boom() -> Any:
            raise RuntimeError("no isolation")

        _install_fake_package(
            monkeypatch, "isolation.manager", type("IM", (), {"get_isolation_manager": boom})()
        )
        _install_fake_package(
            monkeypatch,
            "infrastructure.service_provider",
            type("SP", (), {"get_service_provider": lambda self: type("P", (), {"get": lambda self2, k: None})()})(),
        )
        _install_fake_package(
            monkeypatch,
            "isolation.workspace",
            type("W", (), {"get_workspace_config_root": lambda self: str(tmp_path)})(),
        )
        monkeypatch.setattr("shutil.rmtree", lambda p: (_ for _ in ()).throw(PermissionError("locked")))
        result = await svc._cleanup_task_resources("t-9", workspace=str(ws_dir))
        assert result["workspace_cleaned"] is False
        assert any("清理工作空间失败" in e for e in result["errors"])

    def test_remove_worktree_gitdir_file(self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """worktree .git 为文件（gitdir: 指向）→ 反查分支 + remove + 删分支。"""
        ws = tmp_path / "wt"
        ws.mkdir()
        gitdir = tmp_path / "main" / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (ws / ".git").write_text(f"gitdir: {gitdir.as_posix()}", encoding="utf-8")

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            calls.append(cmd)
            if cmd[1] == "rev-parse":
                return type("R", (), {"returncode": 0, "stdout": "task/abc\n", "stderr": ""})()
            if cmd[1] == "worktree":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if cmd[1] == "branch":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            raise AssertionError(f"unexpected cmd: {cmd}")

        monkeypatch.setattr("subprocess.run", fake_run)
        results: dict[str, Any] = {}
        svc._remove_worktree(ws, results)
        assert results["workspace_cleaned"] is True
        assert any(c[1] == "branch" and c[2] == "-D" and c[3] == "task/abc" for c in calls)

    def test_remove_worktree_branch_delete_failure(self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        ws = tmp_path / "wt2"
        ws.mkdir()
        (ws / ".git").write_text("gitdir: /nonexistent/gitdir", encoding="utf-8")

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[1] == "rev-parse":
                return type("R", (), {"returncode": 0, "stdout": "task/keep\n", "stderr": ""})()
            if cmd[1] == "worktree":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if cmd[1] == "branch":
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": "branch not found"})()
            raise AssertionError(f"unexpected cmd: {cmd}")

        monkeypatch.setattr("subprocess.run", fake_run)
        results: dict[str, Any] = {"errors": []}
        svc._remove_worktree(ws, results)
        assert results["workspace_cleaned"] is True
        assert any("删除分支失败" in e for e in results["errors"])

    def test_remove_worktree_worktree_remove_failure(self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        ws = tmp_path / "wt3"
        ws.mkdir()
        (ws / ".git").write_text("gitdir: /nonexistent/gitdir3", encoding="utf-8")

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[1] == "rev-parse":
                return type("R", (), {"returncode": 0, "stdout": "HEAD\n", "stderr": ""})()
            raise type("CPE", (Exception,), {})("git worktree remove failed")

        monkeypatch.setattr("subprocess.run", fake_run)
        results: dict[str, Any] = {"errors": []}
        svc._remove_worktree(ws, results)
        assert "workspace_cleaned" not in results or results["workspace_cleaned"] is False
        assert len(results["errors"]) >= 1

    def test_remove_worktree_called_process_error(self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """git worktree remove 抛 CalledProcessError → 错误留痕。"""
        import subprocess

        ws = tmp_path / "wt5"
        ws.mkdir()
        (ws / ".git").write_text("gitdir: /nonexistent/gitdir5", encoding="utf-8")

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[1] == "rev-parse":
                return type("R", (), {"returncode": 0, "stdout": "task/x\n", "stderr": ""})()
            raise subprocess.CalledProcessError(1, cmd, stderr="fatal: not a worktree")

        monkeypatch.setattr("subprocess.run", fake_run)
        results: dict[str, Any] = {"errors": []}
        svc._remove_worktree(ws, results)
        assert any("git worktree remove 失败" in e for e in results["errors"])

    def test_remove_worktree_plain_git_dir(self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """.git 文件内容非 gitdir 声明 → main_repo 取 workspace 父目录。"""
        ws = tmp_path / "wt6"
        ws.mkdir()
        (ws / ".git").write_text("not a gitdir pointer", encoding="utf-8")

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[1] == "rev-parse":
                return type("R", (), {"returncode": 0, "stdout": "task/y\n", "stderr": ""})()
            if cmd[1] == "worktree":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if cmd[1] == "branch":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            raise AssertionError(f"unexpected cmd: {cmd}")

        monkeypatch.setattr("subprocess.run", fake_run)
        results: dict[str, Any] = {"errors": []}
        svc._remove_worktree(ws, results)
        assert results["workspace_cleaned"] is True

    def test_remove_worktree_detached_head_skips_branch(self, svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        ws = tmp_path / "wt4"
        ws.mkdir()
        (ws / ".git").write_text("gitdir: /nonexistent/gitdir4", encoding="utf-8")

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[1] == "rev-parse":
                return type("R", (), {"returncode": 0, "stdout": "HEAD\n", "stderr": ""})()
            if cmd[1] == "worktree":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            raise AssertionError(f"unexpected cmd: {cmd}")

        monkeypatch.setattr("subprocess.run", fake_run)
        results: dict[str, Any] = {"errors": []}
        svc._remove_worktree(ws, results)
        assert results["workspace_cleaned"] is True
        assert results["errors"] == []  # detached HEAD 无分支可删，无错误
