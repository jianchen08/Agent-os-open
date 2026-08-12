import tests._tasks_path  # noqa: F401  注入 tasks 插件目录到 sys.path

"""计时器竞态条件回归测试（0.2 精简版）。

原 0.1 版本覆盖：双重调度竞态、ParamInject task_id 注入、TaskWorker 去重等。
0.2 架构下 TaskWorker（infrastructure.*）、ParamInjectPlugin（plugins.input.*）、
PipelineEngine（pipeline.engine）均不存在或已重构，相关集成级用例已移除。
保留对 TimerManager 的单元级竞态回归（0.2 TimerManager 单例 + create_timer
冲突检测契约不变）。
"""

import asyncio

import pytest

pytestmark = pytest.mark.unit


# ── TimerManager 竞态回归 ──────────────────────────────────


class TestTimerManagerRaceCondition:
    """验证 TimerManager 单例的 create_timer 竞态保护。"""

    @pytest.mark.asyncio
    async def test_timer_double_create_raises_error(self):
        """对同一个 task_id 调用两次 create_timer 应该抛出 ValueError。"""
        from timer_manager import TimerManager

        # 重置单例
        TimerManager._instance = None
        TimerManager._initialized = False

        tm = TimerManager.get_instance()

        await tm.create_timer(
            task_id="test_task_001",
            timeout=60.0,
            callback=lambda tid: None,
        )

        with pytest.raises(ValueError, match="计时器已存在"):
            await tm.create_timer(
                task_id="test_task_001",
                timeout=60.0,
                callback=lambda tid: None,
            )

        # 清理
        await tm.cancel_timer("test_task_001")

        # 重置单例
        TimerManager._instance = None
        TimerManager._initialized = False

    @pytest.mark.asyncio
    async def test_concurrent_create_timer_race(self):
        """模拟两个协程并发调用 create_timer，验证只有一个成功。

        create_timer 内部对同一 task_id 的二次创建抛 ValueError，
        并发场景下只能有一个协程成功注册。
        """
        from timer_manager import TimerManager

        TimerManager._instance = None
        TimerManager._initialized = False

        tm = TimerManager.get_instance()

        results = {"success": 0, "conflict": 0}

        async def try_create_timer():
            try:
                await tm.create_timer(
                    task_id="race_task",
                    timeout=60.0,
                    callback=lambda tid: None,
                )
                results["success"] += 1
            except ValueError:
                results["conflict"] += 1

        await asyncio.gather(
            try_create_timer(),
            try_create_timer(),
        )

        assert results["success"] == 1, "应该只有一个协程成功创建计时器"
        assert results["conflict"] == 1, "另一个协程应该遇到冲突"

        await tm.cancel_timer("race_task")

        TimerManager._instance = None
        TimerManager._initialized = False
