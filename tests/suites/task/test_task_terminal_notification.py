# -*- coding: utf-8 -*-
"""端到端测试：验证子任务终态通知父管道。

覆盖场景：
1. 子任务 completed → 父管道通过 inject_message 收到通知
2. 子任务 failed  → 父管道通过 inject_message 收到通知
3. 子任务 completed → 父管道运行中，通过 inject_notification 收到通知
4. 子任务 failed → 父管道不可达，级联失败到父任务
5. 终态事件 _terminal_events 正确 set

测试策略：
- 纯 asyncio 驱动，不依赖 LLM API
- Mock PipelineEngine 的 inject_message / inject_notification
- 通过 EventBus 连接 TaskService → TaskWorker → MockEngine
"""

import asyncio
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import pytest

from pipeline.event_bus import EventBus
from tasks.service import TaskService
from tasks.storage import TaskStorage

TEST_DATA_DIR = Path("data") / "tasks_test_terminal_notification"


@pytest.fixture(autouse=True)
def _setup_teardown():
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


def _make_mock_engine():
    """创建 mock PipelineEngine，记录 inject_message / inject_notification 调用。"""
    engine = MagicMock()
    engine.inject_message = MagicMock()
    engine.inject_notification = MagicMock()
    engine._watching_task_ids = []
    engine._wake_event = None
    engine._suspended_state = None
    return engine


def _make_task_worker(services, event_bus):
    """创建最小化 TaskWorker 实例。"""
    from infrastructure.task_worker import TaskWorker

    worker = TaskWorker(
        task_service=services.get("task_service"),
        plugin_registry=None,
        input_route_table=None,
        output_route_table=None,
        services=services,
        event_bus=event_bus,
    )
    return worker


# ═══════════════════════════════════════════════════════════
# Test 1: 子任务 completed → 父管道挂起中被 inject_message
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_child_completed_notifies_suspended_parent():
    """子任务 completed 时，挂起的父管道应通过 inject_message 被唤醒。"""
    event_bus = EventBus()
    storage = TaskStorage(data_dir=str(TEST_DATA_DIR))
    svc = TaskService(storage=storage, event_bus=event_bus)

    parent_pipeline_id = "pipe-parent-001"
    mock_engine = _make_mock_engine()

    services = {
        "task_service": svc,
        f"__suspended_engine_{parent_pipeline_id}": mock_engine,
    }

    worker = _make_task_worker(services, event_bus)
    await worker.start()

    # 创建子任务，带 parent_pipeline_id
    child = svc.create_task(
        title="子任务-完成通知测试",
        description="验证完成通知",
    )
    # 手动设置 parent_pipeline_id（正常由 task_submit 工具设置）
    child.parent_pipeline_id = parent_pipeline_id
    child.parent_task_id = "parent-task-001"
    storage.save(child)

    # 驱动子任务到终态
    svc.start_task(child.id)
    svc.move_to_evaluating(child.id)

    # 需要等待事件传播
    svc.complete_evaluation(child.id, passed=True, result={"done": True})

    # 等待事件循环处理
    await asyncio.sleep(0.3)

    # 验证父管道收到 inject_message
    mock_engine.inject_message.assert_called_once()
    call_args = mock_engine.inject_message.call_args[0][0]
    assert "已完成" in call_args, f"通知内容应包含'已完成'，实际: {call_args}"
    assert child.id in call_args
    assert "✅" in call_args

    await worker.stop()


# ═══════════════════════════════════════════════════════════
# Test 2: 子任务 failed → 父管道挂起中被 inject_message
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_child_failed_notifies_suspended_parent():
    """子任务 failed 时，挂起的父管道应通过 inject_message 收到失败通知。"""
    event_bus = EventBus()
    storage = TaskStorage(data_dir=str(TEST_DATA_DIR))
    svc = TaskService(storage=storage, event_bus=event_bus)

    parent_pipeline_id = "pipe-parent-002"
    mock_engine = _make_mock_engine()

    services = {
        "task_service": svc,
        f"__suspended_engine_{parent_pipeline_id}": mock_engine,
    }

    worker = _make_task_worker(services, event_bus)
    await worker.start()

    child = svc.create_task(
        title="子任务-失败通知测试",
        description="验证失败通知",
    )
    child.parent_pipeline_id = parent_pipeline_id
    child.parent_task_id = "parent-task-002"
    storage.save(child)

    # 直接 fail_task
    svc.start_task(child.id)
    svc.fail_task(child.id, error="模拟执行失败")

    await asyncio.sleep(0.3)

    mock_engine.inject_message.assert_called_once()
    call_args = mock_engine.inject_message.call_args[0][0]
    assert "failed" in call_args or "❌" in call_args, f"通知内容应包含失败信息，实际: {call_args}"
    assert child.id in call_args
    assert "模拟执行失败" in call_args

    await worker.stop()


# ═══════════════════════════════════════════════════════════
# Test 3: 子任务 completed → 父管道运行中，inject_notification
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_child_completed_notifies_running_parent():
    """父管道运行中时，子任务完成应通过 inject_notification 通知。"""
    event_bus = EventBus()
    storage = TaskStorage(data_dir=str(TEST_DATA_DIR))
    svc = TaskService(storage=storage, event_bus=event_bus)

    parent_pipeline_id = "pipe-parent-003"
    mock_engine = _make_mock_engine()

    services = {
        "task_service": svc,
        f"__running_engine_{parent_pipeline_id}": mock_engine,
    }

    worker = _make_task_worker(services, event_bus)
    await worker.start()

    child = svc.create_task(
        title="子任务-运行中父管道测试",
        description="验证运行中通知",
    )
    child.parent_pipeline_id = parent_pipeline_id
    storage.save(child)

    svc.start_task(child.id)
    svc.move_to_evaluating(child.id)
    svc.complete_evaluation(child.id, passed=True, result={"ok": True})

    await asyncio.sleep(0.3)

    # 运行中的引擎应收到 inject_notification（不是 inject_message）
    mock_engine.inject_notification.assert_called_once()
    mock_engine.inject_message.assert_not_called()
    call_args = mock_engine.inject_notification.call_args[0][0]
    assert "已完成" in call_args

    await worker.stop()


# ═══════════════════════════════════════════════════════════
# Test 4: 子任务 failed → 父管道不可达 → 级联失败
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_child_failed_cascade_to_parent_task():
    """父管道不可达时，子任务失败应级联到父任务。

    注意：必须先启动 worker，再创建父任务并 start_task。
    因为 worker.start() 的 _recover_running_tasks 会将所有
    running 任务重置为 pending（模拟进程重启恢复逻辑）。
    """
    event_bus = EventBus()
    storage = TaskStorage(data_dir=str(TEST_DATA_DIR))
    svc = TaskService(storage=storage, event_bus=event_bus)

    parent_pipeline_id = "pipe-orphan-001"

    # 先启动 worker（避免 _recover_running_tasks 重置父任务）
    services = {"task_service": svc}
    worker = _make_task_worker(services, event_bus)
    await worker.start()

    # 创建并启动父任务（在 worker 启动之后）
    parent = svc.create_task(
        title="父任务-级联测试",
        description="验证级联失败",
    )
    svc.start_task(parent.id)
    assert svc.get_task(parent.id).status.value == "running"

    # 创建子任务
    child = svc.create_task(
        title="子任务-级联失败测试",
        description="验证级联",
    )
    child.parent_pipeline_id = parent_pipeline_id
    child.parent_task_id = parent.id
    storage.save(child)

    svc.start_task(child.id)
    svc.fail_task(child.id, error="子任务崩溃")

    await asyncio.sleep(0.5)

    parent = svc.get_task(parent.id)
    assert parent.status.value == "failed", (
        f"父任务应被级联标记为 failed，实际: {parent.status.value}"
    )

    await worker.stop()


# ═══════════════════════════════════════════════════════════
# Test 5: _terminal_events 在终态时被 set
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_terminal_event_set_on_completion():
    """子任务到终态时，_terminal_events 中对应的 Event 应被 set。"""
    event_bus = EventBus()
    storage = TaskStorage(data_dir=str(TEST_DATA_DIR))
    svc = TaskService(storage=storage, event_bus=event_bus)

    services = {"task_service": svc}

    worker = _make_task_worker(services, event_bus)
    await worker.start()

    child = svc.create_task(title="终端事件测试")
    child.parent_pipeline_id = "pipe-terminal-001"
    storage.save(child)

    # 注册 terminal event
    terminal_evt = asyncio.Event()
    worker._terminal_events[child.id] = terminal_evt
    assert not terminal_evt.is_set()

    svc.start_task(child.id)
    svc.move_to_evaluating(child.id)
    svc.complete_evaluation(child.id, passed=True)

    await asyncio.sleep(0.3)

    assert terminal_evt.is_set(), "终态事件应被 set"

    await worker.stop()


# ═══════════════════════════════════════════════════════════
# Test 6: 多个子任务依次完成，父管道分别收到通知
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_multiple_children_complete_sequentially():
    """多个子任务依次完成，父管道应收到多次通知。"""
    event_bus = EventBus()
    storage = TaskStorage(data_dir=str(TEST_DATA_DIR))
    svc = TaskService(storage=storage, event_bus=event_bus)

    parent_pipeline_id = "pipe-multi-001"
    mock_engine = _make_mock_engine()

    services = {
        "task_service": svc,
        f"__suspended_engine_{parent_pipeline_id}": mock_engine,
    }

    worker = _make_task_worker(services, event_bus)
    await worker.start()

    children = []
    for i in range(3):
        child = svc.create_task(
            title=f"子任务-{i}",
            description=f"第 {i} 个子任务",
        )
        child.parent_pipeline_id = parent_pipeline_id
        child.parent_task_id = "parent-multi-001"
        storage.save(child)
        children.append(child)

    # 依次完成
    for child in children:
        svc.start_task(child.id)
        svc.move_to_evaluating(child.id)
        svc.complete_evaluation(child.id, passed=True, result={"idx": children.index(child)})

    await asyncio.sleep(0.5)

    # 应收到 3 次通知
    assert mock_engine.inject_message.call_count == 3, (
        f"应收到 3 次通知，实际: {mock_engine.inject_message.call_count}"
    )

    # 验证每次通知包含对应的 task_id
    for i, child in enumerate(children):
        call_args = mock_engine.inject_message.call_args_list[i][0][0]
        assert child.id in call_args, f"第 {i} 次通知应包含 task_id {child.id}"

    await worker.stop()


# ═══════════════════════════════════════════════════════════
# Test 7: _wake_events 被正确 set 唤醒 while 循环
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_wake_event_set_on_notification():
    """子任务完成通知父管道后，_wake_events 中对应的 Event 应被 set。"""
    event_bus = EventBus()
    storage = TaskStorage(data_dir=str(TEST_DATA_DIR))
    svc = TaskService(storage=storage, event_bus=event_bus)

    parent_pipeline_id = "pipe-wake-001"
    parent_task_id = "parent-task-wake-001"
    mock_engine = _make_mock_engine()

    services = {
        "task_service": svc,
        f"__suspended_engine_{parent_pipeline_id}": mock_engine,
    }

    worker = _make_task_worker(services, event_bus)
    await worker.start()

    # 注册 wake event
    wake_evt = asyncio.Event()
    worker._wake_events[parent_task_id] = wake_evt
    assert not wake_evt.is_set()

    child = svc.create_task(title="唤醒事件测试")
    child.parent_pipeline_id = parent_pipeline_id
    child.parent_task_id = parent_task_id
    storage.save(child)

    svc.start_task(child.id)
    svc.move_to_evaluating(child.id)
    svc.complete_evaluation(child.id, passed=True)

    await asyncio.sleep(0.3)

    assert wake_evt.is_set(), "父任务的 wake_event 应被 set"

    await worker.stop()
