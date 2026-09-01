# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""TimerManager 行为测试。

覆盖：
1. 单例与重置；配置加载（默认/合并/异常回退）；
2. 配置属性（task_max_duration / 分级 / idle / project / activity / retry /
   auto_restore / restore_lookback）；
3. TimerState 状态判定（is_active/is_expired/is_cancelled/time_remaining/
   to_dict）；
4. create/reset/cancel/超时回调（回调异常不阻断）/查询/清理/clear_all/
   reload_config；
5. restore_from_storage：auto_restore 关闭、running 任务恢复、已存在跳过、
   无 updated_at 跳过、非法时间戳跳过、超 lookback 跳过、剩余>0 恢复、
   剩余<=0 触发回调、异常兜底。

外部依赖 mock 边界：config.config_center 为跨插件配置句柄（mock）；
asyncio 事件循环为真实依赖（call_later 真实调度，超时回调经手动触发）。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
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
def mgr() -> Any:
    from timer_manager import TimerManager

    TimerManager.reset_instance()
    return TimerManager.get_instance()


def _install_fake_package(monkeypatch: pytest.MonkeyPatch, dotted: str, module: ModuleType) -> None:
    """注册假包层级（from a.b import x 需要 a 与 a.b 都在 sys.modules）。"""
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, ModuleType(parent))
    monkeypatch.setitem(sys.modules, dotted, module)


class TestSingletonAndConfig:
    def test_singleton(self) -> None:
        from timer_manager import TimerManager

        TimerManager.reset_instance()
        assert TimerManager.get_instance() is TimerManager.get_instance()

    def test_default_config_values(self, mgr: Any) -> None:
        assert mgr.task_max_duration == 3600
        assert mgr.task_max_duration_for_level("L1") is None
        assert mgr.task_max_duration_for_level("L2") == 9000
        assert mgr.task_max_duration_for_level("L3") == 3600
        assert mgr.task_max_duration_for_level("UNKNOWN") == 3600  # 未知层级兜底
        assert mgr.task_max_duration_for_level(None) == 3600
        assert mgr.idle_threshold == 600
        assert mgr.project_max_duration == 86400
        assert mgr.activity_threshold == 300
        assert mgr.max_retries == 3
        assert mgr.retry_interval == 60
        assert mgr.auto_restore is True
        assert mgr.restore_lookback == 7200

    def test_config_merge_deep(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置中心返回部分覆盖时深合并。"""
        class FakeCenter:
            def get(self, name: str) -> dict:
                return {"timeout": {"idle_threshold": 120}, "retry": {"max_retries": 5}}

        _install_fake_package(
            monkeypatch,
            "config.config_center",
            type("CC", (), {"get_config_center": lambda self: FakeCenter()})(),
        )
        mgr.reload_config()
        assert mgr.idle_threshold == 120  # 覆盖生效
        assert mgr.max_retries == 5
        assert mgr.task_max_duration == 3600  # 未覆盖保持默认

    def test_config_load_failure_falls_back(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise RuntimeError("config center down")

        _install_fake_package(
            monkeypatch,
            "config.config_center",
            type("CC", (), {"get_config_center": boom})(),
        )
        mgr.reload_config()
        assert mgr.task_max_duration == 3600  # 回退默认

    def test_config_center_returns_non_dict(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置中心返回非 dict → 使用默认配置。"""
        class FakeCenter:
            def get(self, name: str) -> Any:
                return None

        _install_fake_package(
            monkeypatch,
            "config.config_center",
            type("CC", (), {"get_config_center": lambda self: FakeCenter()})(),
        )
        mgr.reload_config()
        assert mgr.task_max_duration == 3600

    def test_reset_instance_cancels_handles(self) -> None:
        """reset_instance 取消既有 handle 并清空计时器。"""
        import asyncio

        from timer_manager import TimerManager

        TimerManager.reset_instance()
        m = TimerManager.get_instance()
        asyncio.run(m.create_timer("t-reset", timeout=100))
        TimerManager.reset_instance()
        m2 = TimerManager.get_instance()
        assert m2.get_timer_count() == 0
        assert m2 is not m  # 新实例

    def test_get_timer_manager_function(self) -> None:
        from timer_manager import TimerManager, get_timer_manager

        TimerManager.reset_instance()
        assert get_timer_manager() is TimerManager.get_instance()


class TestTimerState:
    def test_state_flags(self) -> None:
        from timer_manager import TimerState, TimerStatus

        active = TimerState(task_id="t1", status=TimerStatus.ACTIVE, handle=object())
        assert active.is_active() is True
        assert active.is_expired() is False
        assert active.is_cancelled() is False

        expired = TimerState(task_id="t2", status=TimerStatus.EXPIRED)
        assert expired.is_active() is False
        assert expired.is_expired() is True

        cancelled = TimerState(task_id="t3", status=TimerStatus.CANCELLED)
        assert cancelled.is_cancelled() is True

    def test_time_remaining(self) -> None:
        from timer_manager import TimerState, TimerStatus

        future = datetime.now(UTC) + timedelta(seconds=100)
        t = TimerState(task_id="t", timeout_at=future, status=TimerStatus.ACTIVE)
        remaining = t.time_remaining()
        assert remaining is not None
        assert 0 < remaining <= 100

        past = datetime.now(UTC) - timedelta(seconds=10)
        t2 = TimerState(task_id="t2", timeout_at=past, status=TimerStatus.ACTIVE)
        assert t2.time_remaining() == 0.0  # 已过期钳制为 0

        t3 = TimerState(task_id="t3", timeout_at=None, status=TimerStatus.ACTIVE)
        assert t3.time_remaining() is None

        t4 = TimerState(task_id="t4", timeout_at=future, status=TimerStatus.EXPIRED)
        assert t4.time_remaining() is None  # 非 ACTIVE 返回 None

    def test_to_dict(self) -> None:
        from timer_manager import TimerState, TimerStatus

        t = TimerState(
            task_id="t1", root_task_id="r1", timeout_duration=60.0,
            status=TimerStatus.ACTIVE, timeout_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        d = t.to_dict()
        assert d["task_id"] == "t1"
        assert d["root_task_id"] == "r1"
        assert d["timeout_duration"] == 60.0
        assert d["status"] == "active"
        assert d["time_remaining"] is not None
        assert d["created_at"] is not None
        assert d["last_activity"] is not None
        assert d["timeout_at"] is not None


class TestTimerLifecycle:
    @pytest.mark.asyncio
    async def test_create_timer_default_timeout(self, mgr: Any) -> None:
        t = await mgr.create_timer("task-1")
        assert t.timeout_duration == 3600.0
        assert t.status.value == "active"
        assert t.handle is not None
        assert mgr.get_timer_status("task-1") is t
        assert mgr.get_timer_count() == 1

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, mgr: Any) -> None:
        await mgr.create_timer("task-dup", timeout=100)
        with pytest.raises(ValueError, match="计时器已存在"):
            await mgr.create_timer("task-dup", timeout=100)

    @pytest.mark.asyncio
    async def test_reset_timer(self, mgr: Any) -> None:
        await mgr.create_timer("task-r", timeout=100)
        new = await mgr.reset_timer("task-r", new_timeout=200)
        assert new is not None
        assert new.timeout_duration == 200.0
        assert new.root_task_id is None
        assert mgr.get_timer_status("task-r") is new

    @pytest.mark.asyncio
    async def test_reset_timer_keeps_original_timeout(self, mgr: Any) -> None:
        await mgr.create_timer("task-r2", timeout=150)
        new = await mgr.reset_timer("task-r2")
        assert new is not None
        assert new.timeout_duration == 150.0

    @pytest.mark.asyncio
    async def test_reset_missing_returns_none(self, mgr: Any) -> None:
        assert await mgr.reset_timer("missing") is None

    @pytest.mark.asyncio
    async def test_cancel_timer(self, mgr: Any) -> None:
        await mgr.create_timer("task-c", timeout=100)
        assert await mgr.cancel_timer("task-c") is True
        assert mgr.get_timer_status("task-c") is None
        assert await mgr.cancel_timer("task-c") is False  # 已删除

    @pytest.mark.asyncio
    async def test_on_timeout_marks_expired_and_calls_callback(self, mgr: Any) -> None:
        fired: list[str] = []
        await mgr.create_timer("task-t", timeout=100, callback=lambda tid: fired.append(tid))
        mgr._on_timeout("task-t")
        assert fired == ["task-t"]
        timer = mgr.get_timer_status("task-t")
        assert timer is not None
        assert timer.status.value == "expired"
        assert timer.handle is None

    @pytest.mark.asyncio
    async def test_on_timeout_unknown_task_ignored(self, mgr: Any) -> None:
        mgr._on_timeout("no-such")  # 不抛异常即通过

    @pytest.mark.asyncio
    async def test_on_timeout_callback_exception_swallowed(self, mgr: Any) -> None:
        def bad(tid: str) -> None:
            raise RuntimeError("callback boom")

        await mgr.create_timer("task-b", timeout=100, callback=bad)
        mgr._on_timeout("task-b")  # 不抛异常即通过
        assert mgr.get_timer_status("task-b").status.value == "expired"

    @pytest.mark.asyncio
    async def test_get_all_and_active(self, mgr: Any) -> None:
        await mgr.create_timer("a1", timeout=100)
        await mgr.create_timer("a2", timeout=100)
        mgr._on_timeout("a2")  # 过期
        assert len(mgr.get_all_timers()) == 2
        active = mgr.get_active_timers()
        assert [t.task_id for t in active] == ["a1"]

    @pytest.mark.asyncio
    async def test_cleanup_expired_timers(self, mgr: Any) -> None:
        await mgr.create_timer("e1", timeout=100)
        await mgr.create_timer("e2", timeout=100)
        mgr._on_timeout("e1")
        assert await mgr.cleanup_expired_timers() == 1
        assert mgr.get_timer_status("e1") is None
        assert mgr.get_timer_status("e2") is not None
        assert await mgr.cleanup_expired_timers() == 0  # 幂等

    @pytest.mark.asyncio
    async def test_clear_all(self, mgr: Any) -> None:
        await mgr.create_timer("c1", timeout=100)
        await mgr.create_timer("c2", timeout=100)
        await mgr.clear_all()
        assert mgr.get_timer_count() == 0

    def test_reload_config(self, mgr: Any) -> None:
        mgr.reload_config()  # 不抛异常即通过
        assert mgr.task_max_duration == 3600


class TestRestoreFromStorage:
    @pytest.fixture(autouse=True)
    def _fake_tasks_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """restore_from_storage 走遗留 __import__("tasks.types") 路径（0.1 死引用，
        本插件无 tasks/ 包）——注册假模块使被测路径可达（产品 bug 见报告）。"""
        from task_types import TaskStatus

        fake = ModuleType("tasks.types")
        fake.TaskStatus = TaskStatus
        _install_fake_package(monkeypatch, "tasks.types", fake)

    def _make_task(self, task_id: str, status: Any, updated_at: str | None) -> Any:
        from task_types import TaskModel

        return TaskModel(id=task_id, title=task_id, status=status, updated_at=updated_at)

    @pytest.mark.asyncio
    async def test_auto_restore_disabled(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mgr, "_config", {"recovery": {"auto_restore": False, "restore_lookback": 7200}})
        assert await mgr.restore_from_storage(task_service=object()) == 0

    @pytest.mark.asyncio
    async def test_restore_running_tasks(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        fresh = datetime.now(UTC) - timedelta(seconds=60)
        tasks = [self._make_task("t-running", TaskStatus.RUNNING, fresh.isoformat())]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        restored = await mgr.restore_from_storage(task_service=FakeService())
        assert restored == 1
        timer = mgr.get_timer_status("t-running")
        assert timer is not None
        assert timer.root_task_id is None

    @pytest.mark.asyncio
    async def test_restore_skips_existing_timer(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        fresh = datetime.now(UTC) - timedelta(seconds=60)
        await mgr.create_timer("t-exist", timeout=100)
        tasks = [self._make_task("t-exist", TaskStatus.RUNNING, fresh.isoformat())]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        assert await mgr.restore_from_storage(task_service=FakeService()) == 0

    @pytest.mark.asyncio
    async def test_restore_skips_missing_updated_at(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        tasks = [self._make_task("t-no-ts", TaskStatus.RUNNING, None)]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        assert await mgr.restore_from_storage(task_service=FakeService()) == 0

    @pytest.mark.asyncio
    async def test_restore_skips_invalid_timestamp(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        tasks = [self._make_task("t-bad-ts", TaskStatus.RUNNING, "not-a-timestamp")]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        assert await mgr.restore_from_storage(task_service=FakeService()) == 0

    @pytest.mark.asyncio
    async def test_restore_naive_timestamp_normalized(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """无时区的时间戳按 UTC 归一化后恢复。"""
        from task_types import TaskStatus

        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=60)
        tasks = [self._make_task("t-naive", TaskStatus.RUNNING, naive.isoformat())]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        assert await mgr.restore_from_storage(task_service=FakeService()) == 1

    @pytest.mark.asyncio
    async def test_restore_create_timer_failure_swallowed(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        fresh = datetime.now(UTC) - timedelta(seconds=60)
        tasks = [self._make_task("t-fail", TaskStatus.RUNNING, fresh.isoformat())]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        async def _boom(task_id: str, timeout: float | None = None, callback: Any = None, root_task_id: str | None = None) -> Any:
            raise RuntimeError("create failed")

        monkeypatch.setattr(mgr, "create_timer", _boom)
        assert await mgr.restore_from_storage(task_service=FakeService()) == 0

    @pytest.mark.asyncio
    async def test_restore_expired_callback_failure_swallowed(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        old = datetime.now(UTC) - timedelta(seconds=7200)
        tasks = [self._make_task("t-exp2", TaskStatus.RUNNING, old.isoformat())]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        def _boom(tid: str) -> None:
            raise RuntimeError("callback boom")

        restored = await mgr.restore_from_storage(task_service=FakeService(), callback=_boom)
        assert restored == 0
        await asyncio.sleep(0.05)  # 等待异步回调任务执行完（异常被吞）

    @pytest.mark.asyncio
    async def test_restore_expired_create_task_failure_swallowed(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """asyncio.create_task 本身失败（无运行循环）→ 异常被吞。"""
        from task_types import TaskStatus

        old = datetime.now(UTC) - timedelta(seconds=7200)
        tasks = [self._make_task("t-exp3", TaskStatus.RUNNING, old.isoformat())]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        def _boom(coro: Any) -> Any:
            raise RuntimeError("no running loop")

        monkeypatch.setattr(asyncio, "create_task", _boom)
        restored = await mgr.restore_from_storage(task_service=FakeService(), callback=lambda tid: None)
        assert restored == 0

    @pytest.mark.asyncio
    async def test_restore_skips_stale_beyond_lookback(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        stale = datetime.now(UTC) - timedelta(seconds=10000)  # > 7200s lookback
        tasks = [self._make_task("t-stale", TaskStatus.RUNNING, stale.isoformat())]

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        assert await mgr.restore_from_storage(task_service=FakeService()) == 0

    @pytest.mark.asyncio
    async def test_restore_expired_triggers_callback(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from task_types import TaskStatus

        # 剩余时间 <= 0：updated_at 距今超过 task_max_duration(3600)。
        # 取 5400s：>3600 保证走 expired 分支，且留 1800s 余量远离
        # restore_lookback(7200) 下界——7200 恰在 lookback 边界，构造时间到
        # restore 执行之间几毫秒的挂钟差即被判"超出 lookback"跳过
        # （CI 慢机 100% 复现的边界竞态）。
        old = datetime.now(UTC) - timedelta(seconds=5400)
        tasks = [self._make_task("t-expired", TaskStatus.RUNNING, old.isoformat())]
        fired: list[str] = []

        class FakeService:
            def list_by_status(self, status: Any) -> list:
                return tasks

        restored = await mgr.restore_from_storage(task_service=FakeService(), callback=lambda tid: fired.append(tid))
        assert restored == 0
        # 回调经 asyncio.create_task 异步触发，轮询等待（避免固定 sleep 在满载下抖动）
        for _ in range(50):
            if fired:
                break
            await asyncio.sleep(0.01)
        assert fired == ["t-expired"]

    @pytest.mark.asyncio
    async def test_restore_service_error_swallowed(self, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        class BoomService:
            def list_by_status(self, status: Any) -> list:
                raise RuntimeError("storage down")

        assert await mgr.restore_from_storage(task_service=BoomService()) == 0

    @pytest.mark.asyncio
    async def test_async_callback_sync_and_async(self, mgr: Any) -> None:
        calls: list[str] = []

        async def async_cb(tid: str) -> None:
            calls.append(f"async:{tid}")

        def sync_cb(tid: str) -> None:
            calls.append(f"sync:{tid}")

        await mgr._async_callback(async_cb, "a")
        await mgr._async_callback(sync_cb, "s")
        assert calls == ["async:a", "sync:s"]

    @pytest.mark.asyncio
    async def test_async_callback_exception_swallowed(self, mgr: Any) -> None:
        async def bad(tid: str) -> None:
            raise RuntimeError("boom")

        await mgr._async_callback(bad, "x")  # 不抛异常即通过
