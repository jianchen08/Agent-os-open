"""重启后残留 running 任务的恢复语义回归测试。

BUG-FIX-fix_20260625_restart_task_becomes_failed:
问题根因: 重启后存在两条处理残留 running 任务的路径，且语义冲突——
  - 路径 A (app_factory._cleanup_ghost_running_tasks, uvicorn.run 之前):
    直接改 storage，把 running 标为 FAILED，且绕过状态回调（前端收不到推送）。
  - 路径 B (TaskWorker.start → _recover_running_tasks, lifespan 内):
    经 pause_task 把 running 标为 STOPPED（可恢复），触发前端推送。
  路径 A 先执行，任务被写成 FAILED，路径 B 查不到 running 任务，STOPPED 语义从未生效。
  用户看到：重启后任务变成「失败」，无法恢复。
修复方案: 删除冗余且语义错误的路径 A，统一由路径 B（TaskWorker._recover_running_tasks）
  处理，running/pending → STOPPED(paused_by=system)，可由用户手动恢复。

本测试锁定正确语义：重启残留的 running 任务恢复后必须是 STOPPED，绝不能是 FAILED。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tasks.service import TaskService
from tasks.types import TaskStatus

TEST_DATA_DIR = Path("data") / "tasks_test_restart_recovery"


@pytest.fixture(autouse=True)
def _setup_teardown():
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


def _make_worker(svc: TaskService):
    """创建最小化 TaskWorker，仅用于触发 _recover_running_tasks。"""
    from infrastructure.task_worker import TaskWorker  # noqa: PLC0415

    return TaskWorker(
        task_service=svc,
        plugin_registry=MagicMock(),
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        services={"task_service": svc},
        event_bus=None,
    )


async def test_restart_running_task_becomes_stopped_not_failed():
    """重启后残留的 running 任务必须恢复为 STOPPED（可恢复），而非 FAILED。"""
    svc = TaskService(event_bus=None, data_dir=str(TEST_DATA_DIR))

    # 模拟重启前正在执行、重启时引擎状态丢失的任务
    task = await svc.create_task(title="重启前 running 任务")
    await svc.start_task(task.id)
    assert svc.get_task(task.id).status == TaskStatus.RUNNING

    # 模拟重启：TaskWorker.start() → _recover_running_tasks
    worker = _make_worker(svc)
    await worker._recover_running_tasks()

    recovered = svc.get_task(task.id)
    # 核心回归断言：恢复后是 STOPPED（可恢复），且绝不是 FAILED（终态、不可恢复）
    assert recovered.status == TaskStatus.STOPPED, (
        f"重启残留任务应为 STOPPED，实际为 {recovered.status}"
    )
    assert recovered.status != TaskStatus.FAILED
    # paused_by=system 标记来源，与 pause_task 语义一致
    assert (recovered.metadata or {}).get("paused_by") == "system"


async def test_pending_task_also_recovered_to_stopped():
    """重启前 pending 的任务同样恢复为 STOPPED（系统未执行，等待用户决定）。"""
    svc = TaskService(event_bus=None, data_dir=str(TEST_DATA_DIR))

    task = await svc.create_task(title="重启前 pending 任务")
    assert svc.get_task(task.id).status == TaskStatus.PENDING

    worker = _make_worker(svc)
    await worker._recover_running_tasks()

    recovered = svc.get_task(task.id)
    assert recovered.status == TaskStatus.STOPPED
    assert recovered.status != TaskStatus.FAILED


async def test_completed_task_not_touched_on_restart():
    """已完成任务在重启恢复时保持原样，不被改成 STOPPED。"""
    svc = TaskService(event_bus=None, data_dir=str(TEST_DATA_DIR))

    task = await svc.create_task(title="已完成任务")
    await svc.start_task(task.id)
    await svc.complete_task(task.id)
    assert svc.get_task(task.id).status == TaskStatus.COMPLETED

    worker = _make_worker(svc)
    await worker._recover_running_tasks()

    assert svc.get_task(task.id).status == TaskStatus.COMPLETED
