"""任务总超时硬墙（L1 无，L2 2.5h，L3 1h）单元测试。

BUG-FIX-fix_20260629_total_timeout_dead_code:
旧实现 task_max_duration 写在 timer_manager.DEFAULT_CONFIG，但没有任何
代码路径在任务启动时真正按它创建计时器——idle_timer 用的是 idle_threshold
而且会无限续期，活跃任务可以跑 24h+ 不被强制 fail。

新实现：
- TimerManager.task_max_duration_for_level("L1") 返回 None（不限制）
- TimerManager.task_max_duration_for_level("L2") = 9000
- TimerManager.task_max_duration_for_level("L3") = 3600
- TaskExecutionContext 新增 total_timeout_handle，cleanup 时一并取消
- _register_total_timeout 按 task.agent_level 取值并 loop.call_later 注册
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from infrastructure.task_context import TaskExecutionContext
from tasks.timer_manager import TimerManager


class TestTaskMaxDurationByLevel:
    """配置层：按 agent_level 取总超时秒数。"""

    def teardown_method(self) -> None:
        TimerManager.reset_instance()

    def test_l1_returns_none(self) -> None:
        mgr = TimerManager()
        assert mgr.task_max_duration_for_level("L1") is None

    def test_l2_returns_9000(self) -> None:
        mgr = TimerManager()
        assert mgr.task_max_duration_for_level("L2") == 9000

    def test_l3_returns_3600(self) -> None:
        mgr = TimerManager()
        assert mgr.task_max_duration_for_level("L3") == 3600

    def test_unknown_level_returns_default(self) -> None:
        mgr = TimerManager()
        # 兜底走 task_max_duration（默认 3600）
        assert mgr.task_max_duration_for_level("L99") == 3600

    def test_none_level_returns_default(self) -> None:
        mgr = TimerManager()
        assert mgr.task_max_duration_for_level(None) == 3600

    def test_property_compat(self) -> None:
        """旧属性 task_max_duration 兼容（兜底默认值）。"""
        mgr = TimerManager()
        assert mgr.task_max_duration == 3600


class TestTotalTimeoutHandleOnCtx:
    """TaskExecutionContext.cleanup 必须取消 total_timeout_handle。"""

    @pytest.mark.asyncio
    async def test_cleanup_cancels_total_timer(self) -> None:
        ctx = TaskExecutionContext("t1")
        # 模拟一个尚未触发的 call_later handle
        handle = MagicMock()
        ctx.total_timeout_handle = handle
        ctx.cleanup(timer_manager=None)
        handle.cancel.assert_called_once()
        assert ctx.total_timeout_handle is None

    @pytest.mark.asyncio
    async def test_cleanup_no_handle_is_safe(self) -> None:
        ctx = TaskExecutionContext("t2")
        ctx.cleanup(timer_manager=None)  # 不抛异常
        assert ctx.total_timeout_handle is None


class TestRegisterTotalTimeout:
    """_register_total_timeout 行为：per-agent 优先，level fallback。"""

    @pytest.mark.asyncio
    async def test_agent_timeout_minus_one_skips_even_if_level_l2(self) -> None:
        """agent.timeout_seconds=-1（不限）→ 即便 L2 fallback=9000s 也不注册。"""
        from infrastructure.task_executor import TaskExecutorMixin

        class _Holder(TaskExecutorMixin):
            pass

        holder = _Holder()
        ctx = TaskExecutionContext("t-unsuppressed")
        mgr = TimerManager()
        agent = MagicMock(timeout_seconds=-1, name="my_agent")
        holder._register_total_timeout(
            "t-unsuppressed", {"agent_level": "L2"}, agent, MagicMock(), mgr, ctx,
        )
        assert ctx.total_timeout_handle is None

    @pytest.mark.asyncio
    async def test_agent_timeout_positive_overrides_level(self) -> None:
        """agent.timeout_seconds=120 → 用 120s 而非 L3 fallback 的 3600s。

        通过 call_later 注册的 TimerHandle._when（绝对时间戳）减当前 loop
        时间，换算回 duration 验证。
        """
        from infrastructure.task_executor import TaskExecutorMixin

        class _Holder(TaskExecutorMixin):
            pass

        holder = _Holder()
        ctx = TaskExecutionContext("t-override")
        mgr = TimerManager()
        agent = MagicMock(timeout_seconds=120, name="custom_agent")
        holder._register_total_timeout(
            "t-override", {"agent_level": "L3"}, agent, MagicMock(), mgr, ctx,
        )
        try:
            handle = ctx.total_timeout_handle
            assert handle is not None
            # call_later 的 TimerHandle 暴露 _when（绝对单调时间），换算 duration
            loop = asyncio.get_running_loop()
            duration_approx = handle._when - loop.time()
            # 120s 左右，远小于 L3 fallback 的 3600s
            assert 100 <= duration_approx <= 130, (
                f"agent.timeout_seconds 应覆盖 level fallback，"
                f"实际 duration={duration_approx}"
            )
        finally:
            if ctx.total_timeout_handle:
                ctx.total_timeout_handle.cancel()

    @pytest.mark.asyncio
    async def test_l1_skips_registration_no_agent(self) -> None:
        """无 agent_config + L1 → task_max_duration_for_level=None → 不注册。"""
        from infrastructure.task_executor import TaskExecutorMixin

        class _Holder(TaskExecutorMixin):
            pass

        holder = _Holder()
        ctx = TaskExecutionContext("t-l1")
        mgr = TimerManager()
        holder._register_total_timeout(
            "t-l1", {"agent_level": "L1"}, None, MagicMock(), mgr, ctx,
        )
        assert ctx.total_timeout_handle is None

    @pytest.mark.asyncio
    async def test_l3_registers_handle_no_agent(self) -> None:
        """无 agent_config + L3 → 用 fallback 3600s。"""
        from infrastructure.task_executor import TaskExecutorMixin

        class _Holder(TaskExecutorMixin):
            pass

        holder = _Holder()
        ctx = TaskExecutionContext("t-l3")
        mgr = TimerManager()
        holder._register_total_timeout(
            "t-l3", {"agent_level": "L3"}, None, MagicMock(), mgr, ctx,
        )
        try:
            assert ctx.total_timeout_handle is not None
        finally:
            if ctx.total_timeout_handle:
                ctx.total_timeout_handle.cancel()

    @pytest.mark.asyncio
    async def test_handle_fires_fail_task(self) -> None:
        """超短超时 0.05s 验证回调真触发 fail_task。"""
        from infrastructure.task_executor import TaskExecutorMixin

        class _Holder(TaskExecutorMixin):
            pass

        holder = _Holder()
        ctx = TaskExecutionContext("t-fast")
        ctx.set_terminal = MagicMock()  # type: ignore
        ctx.cleanup = MagicMock()  # type: ignore

        # 用 mock TimerManager，duration 强制返回 0.05
        mgr = MagicMock()
        mgr.task_max_duration_for_level = MagicMock(return_value=0.05)

        # mock task_service.fail_task 为 coroutine
        called: list[str] = []

        async def _fail_task(tid: str, reason: str) -> None:
            called.append(f"{tid}|{reason}")

        ts = MagicMock()
        ts.fail_task = _fail_task

        holder._register_total_timeout(
            "t-fast", {"agent_level": "L3"}, None, ts, mgr, ctx,
        )
        # 等待回调触发 + 异步 fail_task 完成
        await asyncio.sleep(0.15)
        assert called, "total_timeout 到点后 fail_task 未被调用"
        assert "t-fast" in called[0]
        assert "total_timeout" in called[0]
        ctx.set_terminal.assert_called_once()


class TestCancelPipelineCancelsTotalTimeout:
    """cancel_pipeline（pause/cancel 终态冻结引擎的统一路径）必须取消
    total_timeout_handle。

    BUG-FIX-fix_20260713_cancel_pipeline_leaks_total_timer:
    pause/cancel 都走 cancel_pipeline 冻结引擎，但旧 cancel_pipeline 手动清理了
    active/suspended_engine/idle_timer，唯独漏了 total_timeout_handle（它的取消
    在 ctx.cleanup 里）。结果 pause 后引擎虽停，2400s 硬墙定时器仍倒计时，到点
    照常 fail_task 把 STOPPED 改成 FAILED，触发通知唤醒父任务重试。
    """

    @pytest.mark.asyncio
    async def test_cancel_pipeline_cancels_total_timeout_handle(self) -> None:
        from infrastructure.task_executor import TaskExecutorMixin

        class _Holder(TaskExecutorMixin):
            pass

        holder = _Holder()
        holder._task_service = None  # 跳过 pipeline_id 解析与 message_bus.stop
        holder._contexts = {}
        holder._cancel_idle_timer_async = MagicMock()  # type: ignore[method-assign]

        ctx = TaskExecutionContext("t-freeze")
        handle = MagicMock()
        ctx.total_timeout_handle = handle
        holder._contexts["t-freeze"] = ctx

        holder.cancel_pipeline("t-freeze")

        handle.cancel.assert_called_once()
        assert ctx.total_timeout_handle is None
