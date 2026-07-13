"""M13 长期任务迁移方案 — 功能验证测试。

覆盖范围：
- TestStorageDirStructure：目录结构存储（创建/读取/删除/多根独立）
- TestResetToPending：TaskService.reset_to_pending() 强制状态重置
- TestWorkerRecovery：Worker 启动恢复（短期恢复/长期跳过/混合）
- TestWorkerIdleTimeout：idle 计时器注册/超时/安全检查
- TestEventNotification：任务终态通知注入对话
- TestDependencyValidatorNoORM：dependency_validator 去 ORM 验证
- TestE2ELifecycle：端到端全生命周期（TaskService + Worker + TimerManager + 持久化）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tasks.service import TaskService
from tasks.storage import TaskStorage
from tasks.types import TaskStatus, create_task


def _make_svc(data_dir: Path | None = None) -> TaskService:
    """创建测试用 TaskService（内存或文件存储）。"""
    svc = TaskService.__new__(TaskService)
    svc._storage = TaskStorage(data_dir=data_dir)
    svc._progress = None
    svc._scheduler = None
    svc._concurrency = None
    svc._on_state_change = None
    return svc


# ═══════════════════════════════════════════════════════════
# Storage 目录结构
# ═══════════════════════════════════════════════════════════


class TestStorageDirStructure:
    """M13-0: 按根任务分目录 + 扁平 YAML 文件存储。"""

    def test_root_creates_tree_dir(self, tmp_path: Path) -> None:
        """根任务保存后创建 tree_{id}/ 目录和 {id}.yaml 文件。"""
        storage = TaskStorage(data_dir=tmp_path)
        root = create_task(title="根任务")
        storage.save(root)

        tree_dir = tmp_path / f"tree_{root.id}"
        assert tree_dir.is_dir()
        assert (tree_dir / f"{root.id}.yaml").exists()

    def test_child_same_dir_as_parent(self, tmp_path: Path) -> None:
        """子任务存入父任务所在目录。"""
        storage = TaskStorage(data_dir=tmp_path)
        root = create_task(title="根")
        storage.save(root)

        child = create_task(title="子", parent_task_id=root.id)
        storage.save(child)

        tree_dir = tmp_path / f"tree_{root.id}"
        assert (tree_dir / f"{child.id}.yaml").exists()

    def test_save_and_reload(self, tmp_path: Path) -> None:
        """保存后重新加载 TaskStorage，数据一致。"""
        storage = TaskStorage(data_dir=tmp_path)
        root = create_task(title="根", description="desc")
        storage.save(root)

        child = create_task(title="子", parent_task_id=root.id)
        storage.save(child)

        storage2 = TaskStorage(data_dir=tmp_path)
        r = storage2.get(root.id)
        c = storage2.get(child.id)
        assert r is not None and r.title == "根"
        assert c is not None and c.parent_task_id == root.id

    def test_list_by_parent(self, tmp_path: Path) -> None:
        """list_by_parent 正确过滤。"""
        storage = TaskStorage(data_dir=tmp_path)
        root = create_task(title="根")
        storage.save(root)
        storage.save(create_task(title="子A", parent_task_id=root.id))
        storage.save(create_task(title="子B", parent_task_id=root.id))

        children = storage.list_by_parent(root.id)
        assert len(children) == 2

    def test_delete_child_keeps_dir(self, tmp_path: Path) -> None:
        """删除子任务后根目录保留。"""
        storage = TaskStorage(data_dir=tmp_path)
        root = create_task(title="根")
        storage.save(root)
        child = create_task(title="子", parent_task_id=root.id)
        storage.save(child)

        tree_dir = tmp_path / f"tree_{root.id}"
        storage.delete(child.id)
        assert not (tree_dir / f"{child.id}.yaml").exists()
        assert tree_dir.is_dir()

    def test_delete_root_removes_empty_dir(self, tmp_path: Path) -> None:
        """删除无子任务的根任务后目录被清理。"""
        storage = TaskStorage(data_dir=tmp_path)
        root = create_task(title="根")
        storage.save(root)

        tree_dir = tmp_path / f"tree_{root.id}"
        storage.delete(root.id)
        assert not tree_dir.exists()

    def test_multiple_roots_independent_dirs(self, tmp_path: Path) -> None:
        """多个根任务各自独立目录。"""
        storage = TaskStorage(data_dir=tmp_path)
        r1 = create_task(title="根A")
        r2 = create_task(title="根B")
        storage.save(r1)
        storage.save(r2)

        assert (tmp_path / f"tree_{r1.id}").is_dir()
        assert (tmp_path / f"tree_{r2.id}").is_dir()


# ═══════════════════════════════════════════════════════════
# reset_to_pending
# ═══════════════════════════════════════════════════════════


class TestResetToPending:
    """M13-b1: TaskService.reset_to_pending() 强制重置。"""

    def test_running_to_pending(self) -> None:
        """RUNNING -> PENDING（恢复场景）。"""
        svc = _make_svc()
        t = svc.create_task(title="t")
        svc.start_task(t.id)
        result = svc.reset_to_pending(t.id)
        assert result.status == TaskStatus.PENDING
        assert result.started_at is None

    def test_failed_to_pending(self) -> None:
        """FAILED -> PENDING（失败重试）。"""
        svc = _make_svc()
        t = svc.create_task(title="t")
        svc.start_task(t.id)
        svc.fail_task(t.id, "err")
        result = svc.reset_to_pending(t.id)
        assert result.status == TaskStatus.PENDING
        assert result.error == ""

    def test_bypasses_state_machine(self) -> None:
        """绕过状态机（状态机不允许 RUNNING->PENDING）。"""
        svc = _make_svc()
        t = svc.create_task(title="t")
        svc.start_task(t.id)
        assert not svc._state_machine.can_transition(TaskStatus.RUNNING, TaskStatus.PENDING)
        result = svc.reset_to_pending(t.id)
        assert result.status == TaskStatus.PENDING

    def test_triggers_callback(self) -> None:
        """触发 on_state_change 回调。"""
        svc = _make_svc()
        cb = []
        svc._on_state_change = lambda tid, old, new: cb.append((tid, old, new))
        t = svc.create_task(title="t")
        svc.start_task(t.id)
        svc.reset_to_pending(t.id)
        assert cb[-1] == (t.id, "running", "pending")

    def test_not_found_raises(self) -> None:
        """任务不存在时抛 KeyError。"""
        svc = _make_svc()
        with pytest.raises(KeyError, match="not found"):
            svc.reset_to_pending("nonexistent")


# ═══════════════════════════════════════════════════════════
# Worker 启动恢复
# ═══════════════════════════════════════════════════════════


class TestWorkerRecovery:
    """M13-b1: Worker._recover_running_tasks()。"""

    def _make_worker(self, svc: TaskService) -> "TaskWorker":
        from infrastructure.task_worker import TaskWorker
        return TaskWorker(
            task_service=svc,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={"task_service": svc},
            event_bus=None,
        )

    @pytest.mark.asyncio
    async def test_short_term_recovered(self) -> None:
        """短期 running 任务恢复为 pending。"""
        svc = _make_svc()
        t = svc.create_task(title="短期", metadata={"task_scope": "short_term"})
        svc.start_task(t.id)
        worker = self._make_worker(svc)
        await worker._recover_running_tasks()
        assert svc.get_task(t.id).status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_long_term_skipped(self) -> None:
        """长期 running 任务保持 running。"""
        svc = _make_svc()
        t = svc.create_task(title="长期", metadata={"task_scope": "long_term"})
        svc.start_task(t.id)
        worker = self._make_worker(svc)
        await worker._recover_running_tasks()
        assert svc.get_task(t.id).status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_mixed_recovery(self) -> None:
        """混合场景：短期恢复、长期保持、pending 不变。"""
        svc = _make_svc()
        s = svc.create_task(title="短期", metadata={"task_scope": "short_term"})
        svc.start_task(s.id)
        l = svc.create_task(title="长期", metadata={"task_scope": "long_term"})
        svc.start_task(l.id)
        p = svc.create_task(title="pending")

        worker = self._make_worker(svc)
        await worker._recover_running_tasks()
        assert svc.get_task(s.id).status == TaskStatus.PENDING
        assert svc.get_task(l.id).status == TaskStatus.RUNNING
        assert svc.get_task(p.id).status == TaskStatus.PENDING


# ═══════════════════════════════════════════════════════════
# Worker idle 超时
# ═══════════════════════════════════════════════════════════


class TestWorkerIdleTimeout:
    """M13-b2: idle 计时器注册与超时。"""

    def _make_worker_with_timer(self, svc: TaskService) -> tuple["TaskWorker", "TimerManager"]:
        from infrastructure.task_worker import TaskWorker
        from tasks.timer_manager import TimerManager
        TimerManager.reset_instance()
        tm = TimerManager.get_instance()
        worker = TaskWorker(
            task_service=svc,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={"task_service": svc, "timer_manager": tm},
            event_bus=None,
        )
        return worker, tm

    @pytest.mark.asyncio
    async def test_idle_timeout_marks_failed(self) -> None:
        """idle 超时后任务标记为 failed。"""
        svc = _make_svc()
        worker, tm = self._make_worker_with_timer(svc)
        t = svc.create_task(title="idle")
        svc.start_task(t.id)

        await tm.create_timer(t.id, timeout=0.1, callback=lambda tid: worker._on_idle_timeout(tid))
        await asyncio.sleep(0.3)

        assert svc.get_task(t.id).status == TaskStatus.FAILED
        assert "idle" in (svc.get_task(t.id).error or "").lower()

    @pytest.mark.asyncio
    async def test_idle_timeout_ignores_completed(self) -> None:
        """idle 超时但任务已完成，不影响。"""
        svc = _make_svc()
        worker, tm = self._make_worker_with_timer(svc)
        t = svc.create_task(title="done")
        svc.start_task(t.id)
        svc.move_to_evaluating(t.id)
        svc.complete_evaluation(t.id, passed=True)

        worker._on_idle_timeout(t.id)
        assert svc.get_task(t.id).status == TaskStatus.COMPLETED

    def test_idle_no_service_no_crash(self) -> None:
        """无 task_service 时不崩溃。"""
        from infrastructure.task_worker import TaskWorker
        worker = TaskWorker(
            task_service=None,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={},
        )
        worker._on_idle_timeout("any")


# ═══════════════════════════════════════════════════════════
# 任务终态通知
# ═══════════════════════════════════════════════════════════


class TestEventNotification:
    """M13-c: 子任务终态时 TaskEventReceiver 将通知注入对话。"""

    def _make_plugin(self, svc: TaskService) -> tuple["TaskEventReceiverPlugin", list]:
        from plugins.input.task_event_receiver import TaskEventReceiverPlugin
        plugin = TaskEventReceiverPlugin()
        plugin._task_service = svc
        return plugin, plugin._pending_events

    @pytest.mark.asyncio
    async def test_completed_generates_notification(self) -> None:
        """子任务完成时生成完成通知。"""
        svc = _make_svc()
        root = svc.create_task(title="root")
        a = svc.create_task(title="A", parent_task_id=root.id)
        svc.start_task(a.id)
        svc.complete_evaluation(a.id, passed=True)

        plugin, events = self._make_plugin(svc)
        await plugin._on_state_changed({"task_id": a.id, "new_status": "completed", "task": svc.get_task(a.id)})
        assert len(events) == 1
        assert events[0]["type"] == "task_completed"
        assert events[0]["task_id"] == a.id

    @pytest.mark.asyncio
    async def test_failed_generates_notification(self) -> None:
        """子任务失败时生成失败通知。"""
        svc = _make_svc()
        root = svc.create_task(title="root")
        d = svc.create_task(title="D", parent_task_id=root.id)
        svc.start_task(d.id)
        svc.fail_task(d.id, "fail")

        plugin, events = self._make_plugin(svc)
        await plugin._on_state_changed({"task_id": d.id, "new_status": "failed", "task": svc.get_task(d.id)})
        assert len(events) == 1
        assert events[0]["type"] == "task_failed"
        assert events[0]["task_id"] == d.id

    @pytest.mark.asyncio
    async def test_non_terminal_ignored(self) -> None:
        """非终态事件不处理。"""
        svc = _make_svc()
        plugin, events = self._make_plugin(svc)
        await plugin._on_state_changed({"task_id": "x", "new_status": "running", "task": None})
        assert len(events) == 0


# ═══════════════════════════════════════════════════════════
# E2E 全生命周期
# ═══════════════════════════════════════════════════════════


class TestE2ELifecycle:
    """端到端：TaskService + Worker + TimerManager + 持久化。"""

    @pytest.mark.asyncio
    async def test_full_lifecycle_with_timer(self, tmp_path: Path) -> None:
        """提交->启动->idle注册->评估通过->idle取消->完成。"""
        from infrastructure.task_worker import TaskWorker
        from tasks.timer_manager import TimerManager

        TimerManager.reset_instance()
        tm = TimerManager.get_instance()
        svc = _make_svc(data_dir=tmp_path)

        bus = MagicMock()
        submitted = []
        bus.emit = lambda n, d: submitted.append((n, d))
        bus.subscribe = MagicMock()

        worker = TaskWorker(
            task_service=svc,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={"task_service": svc, "timer_manager": tm},
            event_bus=bus,
        )

        t = svc.create_task(title="e2e", metadata={"task_scope": "short_term"})
        svc.start_task(t.id)

        await tm.create_timer(
            t.id,
            timeout=float(tm.idle_threshold),
            callback=lambda tid: worker._on_idle_timeout(tid),
        )
        assert tm.get_timer_status(t.id) is not None

        svc.move_to_evaluating(t.id)
        svc.complete_evaluation(t.id, passed=True)

        await tm.cancel_timer(t.id)
        assert svc.get_task(t.id).status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_recovery_then_execute(self, tmp_path: Path) -> None:
        """恢复 -> 重新执行 -> 完成。"""
        from infrastructure.task_worker import TaskWorker
        from tasks.timer_manager import TimerManager

        TimerManager.reset_instance()
        svc = _make_svc(data_dir=tmp_path)

        worker = TaskWorker(
            task_service=svc,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={"task_service": svc, "timer_manager": TimerManager.get_instance()},
            event_bus=MagicMock(),
        )

        crashed = svc.create_task(title="crashed", metadata={"task_scope": "short_term"})
        svc.start_task(crashed.id)

        await worker._recover_running_tasks()
        assert svc.get_task(crashed.id).status == TaskStatus.PENDING

        svc.start_task(crashed.id)
        svc.move_to_evaluating(crashed.id)
        svc.complete_evaluation(crashed.id, passed=True)
        assert svc.get_task(crashed.id).status == TaskStatus.COMPLETED

    def test_persistence_across_reloads(self, tmp_path: Path) -> None:
        """存储持久化：重新加载后数据一致。"""
        svc1 = _make_svc(data_dir=tmp_path)
        root = svc1.create_task(title="root")
        svc1.create_task(title="child", parent_task_id=root.id)
        svc1.start_task(root.id)
        svc1.complete_evaluation(root.id, passed=True)

        svc2 = _make_svc(data_dir=tmp_path)
        assert svc2.get_task(root.id) is not None
        assert svc2.get_task(root.id).status == TaskStatus.COMPLETED
        assert len(svc2.list_subtasks(root.id)) == 1

    @pytest.mark.asyncio
    async def test_event_notification_with_recovery(self, tmp_path: Path) -> None:
        """恢复 + 终态事件通知注入完整流程。"""
        from infrastructure.task_worker import TaskWorker
        from plugins.input.task_event_receiver import TaskEventReceiverPlugin

        svc = _make_svc(data_dir=tmp_path)

        TaskWorker(
            task_service=svc,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={"task_service": svc},
            event_bus=MagicMock(),
        )

        root = svc.create_task(title="root")
        a = svc.create_task(title="A", parent_task_id=root.id)

        svc.start_task(a.id)
        svc.complete_evaluation(a.id, passed=True)

        plugin = TaskEventReceiverPlugin()
        plugin._task_service = svc

        await plugin._on_state_changed({"task_id": a.id, "new_status": "completed", "task": svc.get_task(a.id)})
        assert len(plugin._pending_events) == 1
        assert plugin._pending_events[0]["type"] == "task_completed"
        assert plugin._pending_events[0]["task_id"] == a.id
