"""长期任务容器闭环测试。

使用真实代码（不 mock）验证完整业务闭环：
1. 创建容器 → 挂载子任务 → 子任务执行 → 终态通知 → 灵汐标记容器完成
2. 子任务失败 → 终态通知 → 灵汐重试 → 重试后完成 → 容器完成
3. 子任务失败 → 灵汐标记容器失败
4. 容器超时安全网

涉及真实组件：TaskService, EventBus, TaskEventReceiverPlugin, task_manage_func
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline.event_bus import EventBus
from tasks.service import SimpleStateMachine, TaskService
from tasks.storage import TaskStorage
from tasks.types import TaskStatus


def _make_svc(data_dir: Path | None = None) -> TaskService:
    """创建真实 TaskService（内存存储）。"""
    svc = TaskService.__new__(TaskService)
    svc._storage = TaskStorage(data_dir=data_dir)
    svc._state_machine = SimpleStateMachine()
    svc._progress = None
    svc._scheduler = None
    svc._concurrency = None
    svc._on_state_change = None
    return svc


def _make_svc_with_event_bus(event_bus: EventBus) -> TaskService:
    """创建带 EventBus 回调的 TaskService。

    每次状态变更时通过 EventBus 发出 task_state_changed 事件，
    与生产环境行为一致。
    """
    svc = _make_svc()

    def on_state_change(task_id: str, old_status: str, new_status: str) -> None:
        task = svc.get_task(task_id)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(event_bus.emit("task_state_changed", {
                    "task_id": task_id,
                    "old_status": old_status,
                    "new_status": new_status,
                    "task": task,
                }))
            else:
                loop.run_until_complete(event_bus.emit("task_state_changed", {
                    "task_id": task_id,
                    "old_status": old_status,
                    "new_status": new_status,
                    "task": task,
                }))
        except RuntimeError:
            asyncio.run(event_bus.emit("task_state_changed", {
                "task_id": task_id,
                "old_status": old_status,
                "new_status": new_status,
                "task": task,
            }))

    svc._on_state_change = on_state_change
    return svc


def _make_event_receiver(svc: TaskService, event_bus: EventBus):
    """创建真实 TaskEventReceiverPlugin 并绑定 EventBus。"""
    from plugins.input.task_event_receiver import TaskEventReceiverPlugin

    plugin = TaskEventReceiverPlugin()
    plugin._event_bus = event_bus
    plugin._task_service = svc
    event_bus.subscribe("task_state_changed", plugin._on_state_changed)
    return plugin


def _complete_subtask(svc: TaskService, task_id: str) -> None:
    """将子任务完整流转到 COMPLETED。"""
    svc.start_task(task_id)
    svc.move_to_evaluating(task_id)
    svc.complete_evaluation(task_id, passed=True)


def _fail_subtask(svc: TaskService, task_id: str, error: str = "测试失败") -> None:
    """将子任务流转到 FAILED。"""
    svc.start_task(task_id)
    svc.fail_task(task_id, error=error)


# ═══════════════════════════════════════════════════════════
# 场景一：正常闭环 — 全部成功
# ═══════════════════════════════════════════════════════════


class TestNormalClosedLoop:
    """正常流程：创建容器 → 3个子任务完成 → 通知灵汐 → 灵汐标记容器完成。"""

    @pytest.mark.asyncio
    async def test_full_success_closed_loop(self, tmp_path: Path) -> None:
        """完整成功闭环。"""
        event_bus = EventBus()
        svc = _make_svc_with_event_bus(event_bus)
        receiver = _make_event_receiver(svc, event_bus)

        # ── 1. 灵汐创建容器 ──
        container = svc.create_task(
            title="开发待办事项App",
            metadata={"task_scope": "long_term"},
        )
        assert container.status == TaskStatus.PENDING

        # ── 2. 灵汐挂载3个子任务 ──
        prep = svc.create_task(
            title="方案准备",
            parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        refine = svc.create_task(
            title="方案细化",
            parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        validate = svc.create_task(
            title="最终验证",
            parent_task_id=container.id,
            metadata={"task_role": "final_validation"},
        )

        assert len(svc.list_subtasks(container.id)) == 3
        assert svc.get_progress(container.id) == 0.0

        # ── 3. 方案准备完成 → 通知灵汐 ──
        _complete_subtask(svc, prep.id)
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        assert receiver._pending_events[0]["type"] == "task_completed"
        assert receiver._pending_events[0]["task_id"] == prep.id
        assert svc.get_progress(container.id) == pytest.approx(33.33, abs=0.01)

        # 灵汐读取通知后提交方案细化（清空通知）
        receiver._pending_events.clear()

        # ── 4. 方案细化完成 → 通知灵汐 ──
        _complete_subtask(svc, refine.id)
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        assert receiver._pending_events[0]["type"] == "task_completed"
        assert receiver._pending_events[0]["task_id"] == refine.id
        assert svc.get_progress(container.id) == pytest.approx(66.67, abs=0.01)
        receiver._pending_events.clear()

        # ── 5. 最终验证完成 → 通知灵汐 ──
        _complete_subtask(svc, validate.id)
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        assert receiver._pending_events[0]["type"] == "task_completed"
        assert receiver._pending_events[0]["task_id"] == validate.id
        assert svc.get_progress(container.id) == 100.0
        receiver._pending_events.clear()

        # ── 6. 灵汐调用 task_manage complete_container ──
        import tools.builtin.task_manage as tm
        tm._task_service_instance = svc

        result = tm.task_manage_func({
            "action": "complete_container",
            "task_id": container.id,
            "parent_agent_level": 1,
        })

        assert result["success"] is True
        assert result["status"] == "completed"
        assert result["subtask_count"] == 3
        assert result["completed_subtasks"] == 3

        container_check = svc.get_task(container.id)
        assert container_check.status == TaskStatus.COMPLETED
        assert container_check.completed_at is not None


# ═══════════════════════════════════════════════════════════
# 场景二：子任务失败 → 灵汐重试 → 重试后成功 → 容器完成
# ═══════════════════════════════════════════════════════════


class TestRetryClosedLoop:
    """子任务失败 → 灵汐重试 → 成功 → 容器完成。"""

    @pytest.mark.asyncio
    async def test_retry_and_complete(self, tmp_path: Path) -> None:
        """子任务失败后重试成功，最终容器完成。"""
        event_bus = EventBus()
        svc = _make_svc_with_event_bus(event_bus)
        receiver = _make_event_receiver(svc, event_bus)

        container = svc.create_task(
            title="测试项目",
            metadata={"task_scope": "long_term"},
        )
        prep = svc.create_task(
            title="方案准备",
            parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        refine = svc.create_task(
            title="方案细化",
            parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        validate = svc.create_task(
            title="最终验证",
            parent_task_id=container.id,
            metadata={"task_role": "final_validation"},
        )

        # ── 方案准备失败 ──
        _fail_subtask(svc, prep.id, error="管道迭代耗尽")
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        assert receiver._pending_events[0]["type"] == "task_failed"
        assert "管道迭代耗尽" in receiver._pending_events[0]["error"]
        receiver._pending_events.clear()

        # ── 灵汐决定重试 ──
        import tools.builtin.task_manage as tm
        tm._task_service_instance = svc

        retry_result = tm.task_manage_func({
            "action": "retry",
            "task_id": prep.id,
        })
        assert retry_result["success"] is True
        assert retry_result["status"] == "running"

        # ── 重试后成功完成 ──
        svc.move_to_evaluating(prep.id)
        svc.complete_evaluation(prep.id, passed=True)
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        assert receiver._pending_events[0]["type"] == "task_completed"
        receiver._pending_events.clear()

        # ── 后续子任务正常完成 ──
        _complete_subtask(svc, refine.id)
        await asyncio.sleep(0.05)
        receiver._pending_events.clear()

        _complete_subtask(svc, validate.id)
        await asyncio.sleep(0.05)
        receiver._pending_events.clear()

        # ── 灵汐标记容器完成 ──
        result = tm.task_manage_func({
            "action": "complete_container",
            "task_id": container.id,
            "parent_agent_level": 1,
        })
        assert result["success"] is True
        assert result["status"] == "completed"

        container_check = svc.get_task(container.id)
        assert container_check.status == TaskStatus.COMPLETED
        assert svc.get_progress(container.id) == 100.0


# ═══════════════════════════════════════════════════════════
# 场景三：子任务失败 → 灵汐标记容器失败
# ═══════════════════════════════════════════════════════════


class TestFailContainerClosedLoop:
    """子任务失败且不可恢复 → 灵汐标记容器失败。"""

    @pytest.mark.asyncio
    async def test_fail_container(self, tmp_path: Path) -> None:
        """子任务失败，灵汐决定标记容器失败。"""
        event_bus = EventBus()
        svc = _make_svc_with_event_bus(event_bus)
        receiver = _make_event_receiver(svc, event_bus)

        container = svc.create_task(
            title="失败项目",
            metadata={"task_scope": "long_term"},
        )
        prep = svc.create_task(
            title="方案准备",
            parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        refine = svc.create_task(
            title="方案细化",
            parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )

        # ── 方案准备完成 ──
        _complete_subtask(svc, prep.id)
        await asyncio.sleep(0.05)
        receiver._pending_events.clear()

        # ── 方案细化失败 ──
        _fail_subtask(svc, refine.id, error="方案不可行")
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        assert receiver._pending_events[0]["type"] == "task_failed"
        assert "方案不可行" in receiver._pending_events[0]["error"]
        receiver._pending_events.clear()

        # ── 灵汐决定标记容器失败 ──
        import tools.builtin.task_manage as tm
        tm._task_service_instance = svc

        result = tm.task_manage_func({
            "action": "fail_container",
            "task_id": container.id,
            "parent_agent_level": 1,
            "container_reason": "方案细化失败，无法继续",
        })
        assert result["success"] is True
        assert result["status"] == "failed"

        container_check = svc.get_task(container.id)
        assert container_check.status == TaskStatus.FAILED
        assert "方案细化失败" in container_check.error


# ═══════════════════════════════════════════════════════════
# 场景四：权限验证 — L2 不能操作容器
# ═══════════════════════════════════════════════════════════


class TestContainerPermission:
    """容器操作权限验证。"""

    @pytest.mark.asyncio
    async def test_l2_cannot_complete_container(self, tmp_path: Path) -> None:
        """L2 Agent 不能标记容器完成。"""
        svc = _make_svc()
        container = svc.create_task(
            title="容器",
            metadata={"task_scope": "long_term"},
        )
        svc.create_task(title="子任务", parent_task_id=container.id)

        import tools.builtin.task_manage as tm
        tm._task_service_instance = svc

        result = tm.task_manage_func({
            "action": "complete_container",
            "task_id": container.id,
            "parent_agent_level": 2,
        })
        assert result["success"] is False
        assert result["error_code"] == "PERMISSION_DENIED"

        container_check = svc.get_task(container.id)
        assert container_check.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_non_container_cannot_use_container_op(self, tmp_path: Path) -> None:
        """非容器任务不能使用容器操作。"""
        svc = _make_svc()
        normal_task = svc.create_task(title="普通任务")

        import tools.builtin.task_manage as tm
        tm._task_service_instance = svc

        result = tm.task_manage_func({
            "action": "complete_container",
            "task_id": normal_task.id,
            "parent_agent_level": 1,
        })
        assert result["success"] is False
        assert result["error_code"] == "NOT_A_CONTAINER"

    @pytest.mark.asyncio
    async def test_non_pending_container_cannot_operate(self, tmp_path: Path) -> None:
        """非 PENDING 状态的容器不能操作。"""
        svc = _make_svc()
        container = svc.create_task(
            title="已完成容器",
            metadata={"task_scope": "long_term"},
        )
        svc.create_task(title="子任务", parent_task_id=container.id)

        svc._transition_with_callback(container, TaskStatus.COMPLETED)
        container.completed_at = datetime.now().isoformat()
        svc._storage.save(container)

        import tools.builtin.task_manage as tm
        tm._task_service_instance = svc

        result = tm.task_manage_func({
            "action": "complete_container",
            "task_id": container.id,
            "parent_agent_level": 1,
        })
        assert result["success"] is False
        assert result["error_code"] == "INVALID_STATUS"


# ═══════════════════════════════════════════════════════════
# 场景五：并行子任务
# ═══════════════════════════════════════════════════════════


class TestParallelSubtasks:
    """无依赖的子任务可以并行提交和完成。"""

    @pytest.mark.asyncio
    async def test_parallel_completion(self, tmp_path: Path) -> None:
        """多个无依赖子任务并行完成后，灵汐标记容器完成。"""
        event_bus = EventBus()
        svc = _make_svc_with_event_bus(event_bus)
        receiver = _make_event_receiver(svc, event_bus)

        container = svc.create_task(
            title="并行任务",
            metadata={"task_scope": "long_term"},
        )

        task_a = svc.create_task(
            title="任务A",
            parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        task_b = svc.create_task(
            title="任务B",
            parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        task_c = svc.create_task(
            title="任务C",
            parent_task_id=container.id,
            metadata={"task_role": "final_validation"},
        )

        # ── 并行完成（模拟无依赖同时结束）──
        _complete_subtask(svc, task_a.id)
        _complete_subtask(svc, task_b.id)
        _complete_subtask(svc, task_c.id)
        await asyncio.sleep(0.1)

        # 灵汐收到3条通知
        assert len(receiver._pending_events) == 3
        types = [e["type"] for e in receiver._pending_events]
        assert types.count("task_completed") == 3

        assert svc.get_progress(container.id) == 100.0

        # ── 灵汐标记容器完成 ──
        import tools.builtin.task_manage as tm
        tm._task_service_instance = svc

        result = tm.task_manage_func({
            "action": "complete_container",
            "task_id": container.id,
            "parent_agent_level": 1,
        })
        assert result["success"] is True

        container_check = svc.get_task(container.id)
        assert container_check.status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════
# 场景六：通知注入到 user_input 闭环
# ═══════════════════════════════════════════════════════════


class TestNotificationInjection:
    """验证终态通知正确注入到灵汐的对话中。"""

    @pytest.mark.asyncio
    async def test_notification_text_format(self, tmp_path: Path) -> None:
        """验证通知文本格式正确。"""
        event_bus = EventBus()
        svc = _make_svc_with_event_bus(event_bus)
        receiver = _make_event_receiver(svc, event_bus)

        container = svc.create_task(title="容器", metadata={"task_scope": "long_term"})
        task_a = svc.create_task(title="调研分析", parent_task_id=container.id)

        # 完成
        _complete_subtask(svc, task_a.id)
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        event = receiver._pending_events[0]
        assert event["type"] == "task_completed"
        assert event["title"] == "调研分析"

        # 模拟管道 execute 注入到 user_input
        event_messages = []
        for e in receiver._pending_events:
            if e["type"] == "task_completed":
                event_messages.append(
                    f"[系统通知] 任务 '{e['title']}' 已完成"
                )
            elif e["type"] == "task_failed":
                event_messages.append(
                    f"[系统通知] 任务 '{e['title']}' 失败: {e.get('error', '未知错误')}"
                )

        notification = "\n".join(event_messages)
        assert "[系统通知] 任务 '调研分析' 已完成" in notification

    @pytest.mark.asyncio
    async def test_failed_notification_includes_error(self, tmp_path: Path) -> None:
        """失败通知包含错误信息。"""
        event_bus = EventBus()
        svc = _make_svc_with_event_bus(event_bus)
        receiver = _make_event_receiver(svc, event_bus)

        container = svc.create_task(title="容器", metadata={"task_scope": "long_term"})
        task_b = svc.create_task(title="方案细化", parent_task_id=container.id)

        _fail_subtask(svc, task_b.id, error="迭代耗尽，Agent 未完成")
        await asyncio.sleep(0.05)

        assert len(receiver._pending_events) == 1
        event = receiver._pending_events[0]
        assert event["type"] == "task_failed"
        assert event["error"] == "迭代耗尽，Agent 未完成"
