"""
任务状态实时同步测试。

覆盖范围：
- _do_push_status_change_ws：ws_interaction_notifier.send_to_user 调用验证
- 全部状态转换触发 WebSocket 推送（state_coverage=100%, transition_coverage=100%）
- 回归验证：原有任务管理功能不受影响
"""

from __future__ import annotations

import asyncio
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from tasks.state_machine import _TASK_TRANSITIONS
from tasks.service import TaskService
from tasks.types import TaskStatus


# ═══════════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════════

def _make_service() -> TaskService:
    """创建使用临时目录的 TaskService 实例。"""
    tmp_dir = tempfile.mkdtemp(prefix="test_task_status_sync_")
    return TaskService(data_dir=tmp_dir)


# 从 pending 到各目标状态的路径（最少步数）
# 基于 _TASK_TRANSITIONS:
#   pending → [running, stopped, completed, failed]
#   running → [evaluating, completed, failed, stopped, timeout]
#   evaluating → [running, completed, failed, stopped]
#   stopped → [running, pending]
#   completed → [pending]
#   failed → [pending, running]
#   timeout → [running, pending, failed]
_PATHS_TO_STATE: dict[str, list[str]] = {
    "pending": [],
    "running": ["running"],
    "evaluating": ["running", "evaluating"],
    "stopped": ["stopped"],
    "completed": ["completed"],
    "failed": ["failed"],
    "timeout": ["running", "timeout"],
}


async def _navigate_to_state(svc: TaskService, task_id: str, target: str) -> None:
    """通过合法路径将任务从 pending 转到 target 状态。"""
    for step in _PATHS_TO_STATE[target]:
        await svc.force_transition(task_id, TaskStatus(step))


# 所有合法转换
_ALL_TRANSITIONS: list[tuple[str, str]] = [
    (src, dst)
    for src, targets in _TASK_TRANSITIONS.items()
    for dst in targets
]


# _do_push_status_change_ws 的 patch 目标：模块级单例的 send_to_user 方法。
# service.py 内通过 `from channels.websocket.ws_handler import ws_interaction_notifier`
# 导入该单例，因此 patch 目标必须是该单例的方法属性本身。
_WS_SEND_PATCH = "channels.websocket.ws_handler.ws_interaction_notifier.send_to_user"


# ═══════════════════════════════════════════════════════════════
# 1. _do_push_status_change_ws 调用 ws_interaction_notifier.send_to_user 验证
# ═══════════════════════════════════════════════════════════════

class TestDoPushStatusChangeWs:
    """验证 _do_push_status_change_ws 正确调用 ws_interaction_notifier.send_to_user。

    推送条件（service.py）：
    1. self._storage 不为 None
    2. task 存在
    3. task.metadata 含 session_id（作为 thread_id）
    4. task.metadata 含 user_id（作为 send_to_user 的目标）
    任一缺失都不推送。

    消息体（内联构造，6 字段）：type + data{task_id, status, previous_status,
    title, updated_at, thread_id}。
    """

    @pytest.mark.asyncio
    async def test_send_to_user_called_with_correct_user_id(self):
        """send_to_user 的第一个参数应为 task.metadata.user_id。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc._do_push_status_change_ws(task.id, "pending", "running")

            mock_send.assert_awaited_once()
            user_id = mock_send.call_args.args[0]
            assert user_id == "user-xyz"

    @pytest.mark.asyncio
    async def test_send_to_user_event_has_correct_message_format(self):
        """推送的 event dict 应包含完整的 task_status_changed 格式（6 字段）。"""
        svc = _make_service()
        task = await svc.create_task(
            title="测试标题",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc._do_push_status_change_ws(task.id, "pending", "running")

            mock_send.assert_awaited_once()
            event = mock_send.call_args.args[1]

            assert event["type"] == "task_status_changed"
            data = event["data"]
            assert data["task_id"] == task.id
            assert data["status"] == "running"
            assert data["previous_status"] == "pending"
            assert data["title"] == "测试标题"
            assert data["thread_id"] == "sess-001"
            assert "updated_at" in data  # 由 storage 填充，非空字符串

    @pytest.mark.asyncio
    async def test_event_data_has_exactly_6_fields(self):
        """data 字段应恰好包含 6 个字段，无多余字段。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc._do_push_status_change_ws(task.id, "pending", "completed")

            event = mock_send.call_args.args[1]
            assert set(event["data"].keys()) == {
                "task_id", "status", "previous_status",
                "title", "updated_at", "thread_id",
            }

    @pytest.mark.asyncio
    async def test_no_push_when_no_session_id(self):
        """任务没有 session_id（thread_id）时不推送。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"user_id": "user-xyz"},  # 缺 session_id
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc._do_push_status_change_ws(task.id, "pending", "running")

            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_push_when_no_user_id(self):
        """任务没有 user_id 时不推送（send_to_user 需要 user_id 路由）。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001"},  # 缺 user_id
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc._do_push_status_change_ws(task.id, "pending", "running")

            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_push_when_task_not_found(self):
        """任务不存在时不推送。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc._do_push_status_change_ws("nonexistent", "pending", "running")

            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_push_when_no_storage(self):
        """无存储层时不推送。"""
        svc = TaskService(task_id="some-id")

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc._do_push_status_change_ws("any", "pending", "running")

            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_push_failure_is_non_fatal(self):
        """send_to_user 抛异常时应被抑制（非致命），不向调用方传播。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            mock_send.side_effect = ConnectionError("连接断开")

            # 不应抛出异常（被 try/except 抑制）
            await svc._do_push_status_change_ws(task.id, "pending", "running")


# ═══════════════════════════════════════════════════════════════
# 2. 全部状态转换触发 WebSocket 推送
# ═══════════════════════════════════════════════════════════════

class TestAllStateTransitionsPushWs:
    """验证所有合法状态转换都触发 WebSocket 推送。

    基于 _TASK_TRANSITIONS 定义，state_coverage=100%, transition_coverage=100%。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "from_status, to_status",
        _ALL_TRANSITIONS,
        ids=[f"{s}->{t}" for s, t in _ALL_TRANSITIONS],
    )
    async def test_transition_triggers_ws_push(self, from_status: str, to_status: str):
        """每个合法状态转换都应触发 _do_push_status_change_ws。"""
        svc = _make_service()
        task = await svc.create_task(
            title=f"测试 {from_status}->{to_status}",
            metadata={"session_id": "sess-ws", "user_id": "user-ws"},
        )
        task_id = task.id

        # 通过合法路径将任务导航到 from_status
        await _navigate_to_state(svc, task_id, from_status)

        # 验证推送被调用
        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.force_transition(task_id, TaskStatus(to_status))

            mock_push.assert_called_once_with(task_id, from_status, to_status)


# ═══════════════════════════════════════════════════════════════
# 3. 高层方法触发推送验证（验证端到端调用链）
# ═══════════════════════════════════════════════════════════════

class TestHighLevelMethodsPushWs:
    """验证 start_task/complete_task/fail_task 等高层方法触发推送。"""

    @pytest.mark.asyncio
    async def test_start_task_pushes_pending_to_running(self):
        """start_task 应推送 pending→running。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.start_task(task.id)
            mock_push.assert_called_once_with(task.id, "pending", "running")

    @pytest.mark.asyncio
    async def test_complete_task_pushes_running_to_completed(self):
        """complete_task 应推送 running→completed。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.complete_task(task.id)
            mock_push.assert_called_once_with(task.id, "running", "completed")

    @pytest.mark.asyncio
    async def test_fail_task_pushes_running_to_failed(self):
        """fail_task 应推送 running→failed。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.fail_task(task.id, reason="测试失败")
            mock_push.assert_called_once_with(task.id, "running", "failed")

    @pytest.mark.asyncio
    async def test_move_to_evaluating_pushes_running_to_evaluating(self):
        """move_to_evaluating 应推送 running→evaluating。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.move_to_evaluating(task.id)
            mock_push.assert_called_once_with(task.id, "running", "evaluating")

    @pytest.mark.asyncio
    async def test_pause_task_pushes_running_to_stopped(self):
        """pause_task 应推送 running→stopped。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.pause_task(task.id)
            mock_push.assert_called_once_with(task.id, "running", "stopped")

    @pytest.mark.asyncio
    async def test_resume_task_pushes_stopped_to_running(self):
        """resume_task 应推送 stopped→running。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)
        await svc.pause_task(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.resume_task(task.id)
            mock_push.assert_called_once_with(task.id, "stopped", "running")

    @pytest.mark.asyncio
    async def test_reset_to_pending_pushes_failed_to_pending(self):
        """reset_to_pending 应推送 failed→pending。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)
        await svc.fail_task(task.id, reason="测试")

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.reset_to_pending(task.id)
            mock_push.assert_called_once_with(task.id, "failed", "pending")

    @pytest.mark.asyncio
    async def test_delete_task_pushes_deleting_to_deleted(self):
        """delete_task 应推送 deleting→deleted。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.delete_task(task.id)
            mock_push.assert_called_once_with(task.id, "deleting", "deleted")

    @pytest.mark.asyncio
    async def test_cancel_task_pushes_running_to_stopped(self):
        """cancel_task 应推送当前状态→stopped。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.cancel_task(task.id, reason="用户取消")
            mock_push.assert_called_once_with(task.id, "running", "stopped")

    @pytest.mark.asyncio
    async def test_complete_evaluation_pass_pushes_evaluating_to_completed(self):
        """complete_evaluation(通过) 应推送 evaluating→completed。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)
        await svc.move_to_evaluating(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.complete_evaluation(task.id, passed=True, result={"score": 1.0})
            mock_push.assert_called_once_with(task.id, "evaluating", "completed")

    @pytest.mark.asyncio
    async def test_complete_evaluation_fail_pushes_evaluating_to_failed(self):
        """complete_evaluation(未通过) 应推送 evaluating→failed。"""
        svc = _make_service()
        task = await svc.create_task(
            title="t",
            metadata={"session_id": "sess-001", "user_id": "user-xyz"},
        )
        await svc.start_task(task.id)
        await svc.move_to_evaluating(task.id)

        with patch.object(
            svc, "_do_push_status_change_ws", new_callable=AsyncMock,
        ) as mock_push:
            await svc.complete_evaluation(
                task.id, passed=False,
                result={"summary": "不满足要求", "metrics": []},
            )
            # complete_evaluation → fail_task 会触发 evaluating→failed
            mock_push.assert_called_once_with(task.id, "evaluating", "failed")


# ═══════════════════════════════════════════════════════════════
# 4. _emit_state_change 回调机制验证
# ═══════════════════════════════════════════════════════════════

class TestEmitStateChangeCallbacks:
    """验证 _emit_state_change 正确调用注册的回调。"""

    @pytest.mark.asyncio
    async def test_callback_receives_task_id_old_new_status(self):
        """回调应接收 (task_id, old_status, new_status) 三个参数。"""
        svc = _make_service()
        callback = AsyncMock()
        svc.register_state_callback(callback)

        task = await svc.create_task(
            title="t",
            metadata={"session_id": "s", "user_id": "u"},
        )

        await svc.start_task(task.id)

        callback.assert_called_once_with(task.id, "pending", "running")

    @pytest.mark.asyncio
    async def test_multiple_callbacks_all_called(self):
        """多个回调都应被调用。"""
        svc = _make_service()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        svc.register_state_callback(cb1)
        svc.register_state_callback(cb2)

        task = await svc.create_task(
            title="t",
            metadata={"session_id": "s", "user_id": "u"},
        )
        await svc.start_task(task.id)

        cb1.assert_called_once_with(task.id, "pending", "running")
        cb2.assert_called_once_with(task.id, "pending", "running")

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_block_others(self):
        """单个回调异常不影响其他回调和主流程。"""
        svc = _make_service()
        failing_cb = AsyncMock(side_effect=ValueError("回调异常"))
        normal_cb = AsyncMock()
        svc.register_state_callback(failing_cb)
        svc.register_state_callback(normal_cb)

        task = await svc.create_task(
            title="t",
            metadata={"session_id": "s", "user_id": "u"},
        )

        # 不应抛出异常
        await svc.start_task(task.id)

        failing_cb.assert_called_once()
        normal_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_unregister_callback_stops_receiving_events(self):
        """注销后回调不应再被调用。"""
        svc = _make_service()
        callback = AsyncMock()
        svc.register_state_callback(callback)
        svc.unregister_state_callback(callback)

        task = await svc.create_task(
            title="t",
            metadata={"session_id": "s", "user_id": "u"},
        )
        await svc.start_task(task.id)

        callback.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 5. 回归验证：原有任务管理功能不受影响
# ═══════════════════════════════════════════════════════════════

class TestRegressionTaskManagement:
    """回归验证：确认 WebSocket 推送不影响原有任务管理功能。"""

    @pytest.mark.asyncio
    async def test_task_full_lifecycle_succeeds_with_ws_push(self):
        """完整任务生命周期：创建→启动→评估→完成，WS 推送不影响。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()):
            task = await svc.create_task(
                title="全生命周期测试",
                metadata={"session_id": "sess-001", "user_id": "user-xyz"},
            )

            await svc.start_task(task.id)
            assert svc.get_task(task.id).status == TaskStatus.RUNNING

            await svc.move_to_evaluating(task.id)
            assert svc.get_task(task.id).status == TaskStatus.EVALUATING

            await svc.complete_evaluation(task.id, passed=True, result={"score": 1.0})
            assert svc.get_task(task.id).status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_task_fail_and_reset_succeeds_with_ws_push(self):
        """失败后重置流程正常。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()):
            task = await svc.create_task(
                title="失败重置测试",
                metadata={"session_id": "sess-001", "user_id": "user-xyz"},
            )

            await svc.start_task(task.id)
            await svc.fail_task(task.id, reason="模拟失败")
            assert svc.get_task(task.id).status == TaskStatus.FAILED

            await svc.reset_to_pending(task.id)
            assert svc.get_task(task.id).status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_pause_resume_succeeds_with_ws_push(self):
        """暂停/恢复流程正常。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()):
            task = await svc.create_task(
                title="暂停恢复测试",
                metadata={"session_id": "sess-001", "user_id": "user-xyz"},
            )

            await svc.start_task(task.id)
            assert svc.get_task(task.id).status == TaskStatus.RUNNING

            await svc.pause_task(task.id)
            assert svc.get_task(task.id).status == TaskStatus.STOPPED

            await svc.resume_task(task.id)
            assert svc.get_task(task.id).status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_invalid_transition_still_raises_with_ws_push(self):
        """非法状态转换仍正确抛出异常。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()):
            task = await svc.create_task(
                title="t",
                metadata={"session_id": "s", "user_id": "u"},
            )

            # completed → running 是非法的（completed 只能 → pending）
            # 先把任务导航到 completed
            await svc.force_transition(task.id, TaskStatus.COMPLETED)

            # 注意：源码 _task_state.py 内部 `from src.tasks.state_machine import
            # InvalidTransitionError`，与测试导入的 `tasks.state_machine` 是两个
            # 不同的模块对象（src 同时作为根包与 sys.path 中的目录）。因此这里必须
            # 捕获源码实际抛出的那个异常类，否则 isinstance 校验不通过。
            from src.tasks.state_machine import InvalidTransitionError as _SrcErr

            with pytest.raises(_SrcErr):
                await svc.force_transition(task.id, TaskStatus.RUNNING)

    @pytest.mark.asyncio
    async def test_list_all_returns_tasks_correctly_with_ws_push(self):
        """list_all 在有推送逻辑时仍正常返回任务列表。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()):
            await svc.create_task(
                title="任务A",
                metadata={"session_id": "s1", "user_id": "u1"},
            )
            await svc.create_task(
                title="任务B",
                metadata={"session_id": "s2", "user_id": "u2"},
            )

            all_tasks = await svc.list_all()
            assert len(all_tasks) >= 2

    @pytest.mark.asyncio
    async def test_delete_task_succeeds_with_ws_push(self):
        """删除任务正常工作。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()):
            task = await svc.create_task(
                title="待删除",
                metadata={"session_id": "s", "user_id": "u"},
            )

            result = await svc.delete_task(task.id)
            assert result is True
            assert svc.get_task(task.id) is None

    @pytest.mark.asyncio
    async def test_force_transition_succeeds_for_all_valid_paths(self):
        """force_transition 对所有合法转换路径都正常工作。"""
        svc = _make_service()

        with patch(_WS_SEND_PATCH, new=AsyncMock()):
            for from_status, to_status in _ALL_TRANSITIONS:
                task = await svc.create_task(
                    title=f"{from_status}->{to_status}",
                    metadata={"session_id": "sess-loop", "user_id": "user-loop"},
                )
                # 通过合法路径导航到 from_status
                await _navigate_to_state(svc, task.id, from_status)
                # 再转换到 to_status
                await svc.force_transition(task.id, TaskStatus(to_status))

                assert svc.get_task(task.id).status == TaskStatus(to_status)


# ═══════════════════════════════════════════════════════════════
# 6. 端到端推送链路验证
# ═══════════════════════════════════════════════════════════════

class TestEndToEndPushChain:
    """验证从高层方法到 ws_interaction_notifier.send_to_user 的完整调用链。"""

    @pytest.mark.asyncio
    async def test_start_task_to_send_to_user(self):
        """start_task → _emit_state_change → _push_status_change_ws → send_to_user。

        注意：_push_status_change_ws 使用 asyncio.create_task（fire-and-forget），
        需要 await asyncio.sleep(0) 让出事件循环控制权以等待异步任务执行。
        """
        svc = _make_service()
        task = await svc.create_task(
            title="端到端测试",
            metadata={"session_id": "sess-e2e", "user_id": "user-e2e"},
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            await svc.start_task(task.id)
            # 让出事件循环，让 fire-and-forget 的 create_task 执行
            await asyncio.sleep(0)

            mock_send.assert_awaited_once()

            # 验证消息内容
            user_id = mock_send.call_args.args[0]
            event = mock_send.call_args.args[1]
            assert user_id == "user-e2e"
            assert event["type"] == "task_status_changed"
            assert event["data"]["task_id"] == task.id
            assert event["data"]["status"] == "running"
            assert event["data"]["previous_status"] == "pending"

    @pytest.mark.asyncio
    async def test_complete_task_e2e_message_payload(self):
        """complete_task 端到端验证消息 payload 完整性（6 字段）。"""
        svc = _make_service()
        task = await svc.create_task(
            title="完整Payload测试",
            metadata={"session_id": "sess-payload", "user_id": "user-payload"},
        )

        with patch(_WS_SEND_PATCH, new=AsyncMock()) as mock_send:
            # start_task 也在 patch 内，避免 fire-and-forget 跨 patch 边界执行
            await svc.start_task(task.id)
            await asyncio.sleep(0)  # 消费 start_task 的 fire-and-forget

            mock_send.reset_mock()  # 重置，只关注 complete_task 的推送

            await svc.complete_task(task.id)
            await asyncio.sleep(0)  # 让 fire-and-forget 执行

            mock_send.assert_awaited_once()
            event = mock_send.call_args.args[1]
            data = event["data"]

            # 验证所有 6 个字段都存在且有效
            assert data["task_id"] == task.id
            assert data["status"] == "completed"
            assert data["previous_status"] == "running"
            assert data["title"] == "完整Payload测试"
            assert data["updated_at"]  # 非空
            assert data["thread_id"] == "sess-payload"
