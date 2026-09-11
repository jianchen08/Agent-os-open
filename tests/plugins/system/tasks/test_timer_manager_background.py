# @feature: FP-0.2.五 资源治理 | @ci: python-coverage
"""timer_manager 后台回调任务强引用测试（D6：create_task 弱引用可被 GC）。

行为契约：
- 派发的后台回调任务持模块级强引用（运行中不被 GC 静默丢弃）；
- 回调真实执行（sync/async 两种 callback 形态，≥2 组区分输入）；
- 任务完成后 done callback 自移除引用（集合不无界增长）。

直接驱动 ``_spawn_background_callback(_async_callback(...))``——与
restore_from_storage 超时分支的派发调用完全同形（后者需真实 TaskService
存储，重基建不属本单测范围）。
"""

from __future__ import annotations

import asyncio

import pytest

import timer_manager as tm_mod
from timer_manager import TimerManager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton():
    """单例 _initialized 复位（TimerManager 构造有类级初始化门）。"""
    TimerManager._initialized = False
    yield
    TimerManager._initialized = False


@pytest.fixture(autouse=True)
def _clean_background_tasks():
    """每用例前后清空模块级强引用集合，防跨用例泄漏。"""
    tm_mod._background_tasks.clear()
    yield
    tm_mod._background_tasks.clear()


def test_async_callback_task_held_runs_and_self_removes():
    """async 回调：运行中持强引用 → 回调真实执行 → 完成后自移除。"""

    async def async_cb(task_id: str) -> None:
        await asyncio.sleep(0)
        fired.append(task_id)

    fired: list[str] = []

    async def _scenario() -> None:
        mgr = TimerManager()
        task = tm_mod._spawn_background_callback(mgr._async_callback(async_cb, "task-async"))
        assert task in tm_mod._background_tasks, "运行中任务必须被强引用（否则 CPython 弱引用可被 GC）"
        await asyncio.wait_for(task, timeout=2)
        assert fired == ["task-async"], "回调必须真实执行（引用丢失即无声不执行）"
        assert task not in tm_mod._background_tasks, "完成后 done callback 应自移除"

    asyncio.run(_scenario())


def test_sync_callback_task_held_runs_and_self_removes():
    """sync 回调（≥2 组区分输入）：同契约——持引用、真实执行、完成自移除。"""

    def sync_cb(task_id: str) -> None:
        fired.append(task_id)

    fired: list[str] = []

    async def _scenario() -> None:
        mgr = TimerManager()
        task = tm_mod._spawn_background_callback(mgr._async_callback(sync_cb, "task-sync"))
        assert task in tm_mod._background_tasks
        await asyncio.wait_for(task, timeout=2)
        assert fired == ["task-sync"]
        assert task not in tm_mod._background_tasks

    asyncio.run(_scenario())
