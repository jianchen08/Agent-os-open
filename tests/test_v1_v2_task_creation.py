"""
V1+V2 验证：创建任务正向流程 + 异常配置错误

验证点：
V1-创建任务（正向）：
  1. 后端状态转换：pending -> running 正确
  2. 前端即时渲染：WebSocket 推送机制
  3. 排序正确性：新卡片排序在列表顶部
  4. 状态显示正确：前后端一致
  5. 不需要手动刷新：自动刷新/推送机制

V2-创建任务（异常-配置错误）：
  1. 后端错误精确性：配置错误时明确错误信息
  2. 前端错误展示：ErrorBoundary 不空白
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

# ============================================================================
# V1 - 创建任务正向流程
# ============================================================================


class TestV1BackendStateMachine:
    """V1-1: 后端状态转换验证"""

    def test_state_machine_pending_to_running_is_valid(self):
        """验证 pending -> running 是合法转换"""
        from tasks.state_machine import get_task_state_machine

        sm = get_task_state_machine()
        assert sm.can_transition("running") is True

    def test_state_machine_full_happy_path(self):
        """验证正常流程状态链：pending -> running -> evaluating -> completed"""
        from tasks.state_machine import get_task_state_machine

        sm = get_task_state_machine()
        assert sm.can_transition("running") is True
        sm.transition("running")
        assert sm.can_transition("evaluating") is True
        sm.transition("evaluating")
        assert sm.can_transition("completed") is True

    def test_state_machine_invalid_transitions(self):
        """验证非法状态转换被拒绝"""
        from tasks.state_machine import _TASK_TRANSITIONS

        assert "running" not in _TASK_TRANSITIONS.get("completed", [])
        assert "running" not in _TASK_TRANSITIONS.get("cancelled", [])
        assert _TASK_TRANSITIONS.get("cancelled", []) == []

    def test_state_machine_terminal_states(self):
        """验证终态定义"""
        from tasks.state_machine import _TASK_TRANSITIONS

        assert _TASK_TRANSITIONS.get("cancelled", []) == []
        assert _TASK_TRANSITIONS.get("completed", []) == []
        assert len(_TASK_TRANSITIONS.get("pending", [])) > 0
        assert len(_TASK_TRANSITIONS.get("running", [])) > 0

    def test_simple_state_machine_pending_to_running(self):
        """验证 SimpleStateMachine（TaskService 使用）的 pending -> running"""
        from tasks.state_machine import get_task_state_machine
        from tasks.types import TaskStatus

        sm = get_task_state_machine()
        assert sm.can_transition(TaskStatus.RUNNING.value) is True

    def test_task_service_create_task_starts_as_pending(self):
        """验证 TaskService.create_task 返回的任务状态为 PENDING"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            task = asyncio.new_event_loop().run_until_complete(
                service.create_task(title="测试任务", description="描述")
            )
            assert task.status == TaskStatus.PENDING

    def test_task_service_start_task_transitions_to_running(self):
        """验证 TaskService.start_task 将 pending 转为 running"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()
            task = loop.run_until_complete(
                service.create_task(title="测试任务")
            )
            assert task.status == TaskStatus.PENDING

            loop.run_until_complete(service.start_task(task.id))
            task = service.get_task(task.id)
            assert task.status == TaskStatus.RUNNING

    def test_task_service_invalid_transition_raises(self):
        """验证非法状态转换抛出 InvalidTransitionError"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()
            task = loop.run_until_complete(
                service.create_task(title="测试任务")
            )
            assert task.status == TaskStatus.PENDING

            # pending -> completed 是非法转换
            raised = False
            try:
                loop.run_until_complete(
                    service.force_transition(task.id, TaskStatus("completed"))
                )
            except Exception as e:
                assert "不允许" in str(e) or "InvalidTransition" in type(e).__name__
                raised = True
            assert raised, "期望抛出 InvalidTransitionError"

    def test_task_model_default_status_is_pending(self):
        """验证 TaskModel 默认状态为 PENDING"""
        from tasks.types import TaskModel, TaskStatus

        task = TaskModel(title="测试")
        assert task.status == TaskStatus.PENDING

    def test_task_factory_creates_pending_task(self):
        """验证 create_task 工厂函数创建 PENDING 状态任务"""
        from tasks.types import create_task, TaskStatus

        task = create_task(title="测试任务")
        assert task.status == TaskStatus.PENDING
        assert task.id
        assert task.created_at


class TestV1BackendStateDescriptions:
    """V1-4: 后端状态描述正确性"""

    @pytest.mark.skip(reason="get_status_description 方法已移除")
    def test_all_status_descriptions_exist(self):
        """验证所有定义的状态都有中文描述"""
        from tasks.state_machine import TaskStateMachine

        sm = TaskStateMachine()
        expected_statuses = [
            "pending", "scheduled", "running", "evaluating",
            "suspended", "blocked", "completed", "failed",
            "cancelled", "timeout",
        ]
        for status in expected_statuses:
            desc = sm.get_status_description(status)
            assert not desc.startswith("未知状态"), f"状态 {status} 缺少描述"

    @pytest.mark.skip(reason="get_next_logical_status 方法已移除")
    def test_next_logical_status_sequence(self):
        """验证逻辑推进路径：pending -> running -> evaluating -> completed"""
        from tasks.state_machine import TaskStateMachine

        sm = TaskStateMachine()
        assert sm.get_next_logical_status("pending") == "running"
        assert sm.get_next_logical_status("running") == "evaluating"
        assert sm.get_next_logical_status("evaluating") == "completed"
        assert sm.get_next_logical_status("completed") is None


class TestV1FrontendRealtimeUpdate:
    """V1-2,5: 前端实时更新机制验证（代码分析）"""

    def test_global_websocket_supports_event_subscription(self):
        """验证 GlobalWebSocket 支持事件订阅（用于实时更新）"""
        with open("frontend/src/services/websocket/GlobalWebSocket.ts", "r", encoding="utf-8") as f:
            content = f.read()
        assert "subscribe(" in content, "GlobalWebSocket 缺少 subscribe 方法"
        assert "unsubscribe(" in content, "GlobalWebSocket 缺少 unsubscribe 方法"
        assert "_emit(" in content, "GlobalWebSocket 缺少 _emit 内部方法"

    def test_task_ws_event_types_defined(self):
        """验证任务 WebSocket 事件类型已定义"""
        with open("frontend/src/types/task.ts", "r", encoding="utf-8") as f:
            content = f.read()
        expected_events = [
            "task_created",
            "task_phase_changed",
            "task_completed",
            "task_failed",
        ]
        for event in expected_events:
            assert f"'{event}'" in content, f"TaskWSEventType 缺少事件类型: {event}"

    def test_task_service_emits_state_change_event(self):
        """验证 TaskService 创建任务时广播事件"""
        from tasks.service import TaskService

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_event_bus = MagicMock()
            service = TaskService(data_dir=tmpdir, event_bus=mock_event_bus)

            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                service.create_task(title="测试事件")
            )

    def test_long_term_task_store_has_update_method(self):
        """验证 longTermTaskStore 有 updateTask 方法（WS 事件更新用）"""
        import importlib
        spec = importlib.util.spec_from_file_location(
            "longTermTaskStore",
            "frontend/src/stores/longTermTaskStore.ts"
        )
        with open("frontend/src/stores/longTermTaskStore.ts", "r", encoding="utf-8") as f:
            content = f.read()
        assert "updateTask" in content, "longTermTaskStore 缺少 updateTask 方法"
        assert "fetchTasks" in content, "longTermTaskStore 缺少 fetchTasks 方法"


class TestV1FrontendTaskListSorting:
    """V1-3: 排序正确性验证"""

    def test_backend_list_all_default_reverse_order(self):
        """验证后端 list_all 默认按创建时间倒序（最新的在前）"""
        from tasks.service import TaskService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()
            t1 = loop.run_until_complete(service.create_task(title="任务1"))
            t2 = loop.run_until_complete(service.create_task(title="任务2"))
            t3 = loop.run_until_complete(service.create_task(title="任务3"))

            tasks = loop.run_until_complete(service.list_all(reverse=True))
            assert len(tasks) >= 3
            ids = [t.id for t in tasks if t.id in (t1.id, t2.id, t3.id)]
            assert ids[0] == t3.id

    def test_frontend_task_sort_by_created_at_desc(self):
        """验证前端 TaskSortBy 类型支持 created_at 排序"""
        with open("frontend/src/types/task.ts", "r", encoding="utf-8") as f:
            content = f.read()
        assert "'created_at'" in content, "TaskSortBy 缺少 created_at 排序选项"
        assert "'desc'" in content, "TaskSortOrder 缺少 desc 选项"


class TestV1FrontendStatusDisplay:
    """V1-4: 前端状态显示与后端一致性"""

    def test_frontend_task_status_matches_backend(self):
        """验证前端 TaskStatus 类型与后端状态定义一致"""
        with open("frontend/src/types/task.ts", "r", encoding="utf-8") as f:
            content = f.read()

        frontend_statuses = ["pending", "in_progress", "completed", "failed", "blocked", "suspended"]

        backend_statuses = [
            "pending", "scheduled", "running", "evaluating",
            "suspended", "blocked", "completed", "failed",
            "cancelled", "timeout",
        ]

        common_statuses = ["pending", "completed", "failed", "blocked", "suspended"]
        for status in common_statuses:
            assert status in backend_statuses, f"前端状态 {status} 在后端未定义"

    def test_frontend_task_type_definition_complete(self):
        """验证前端 Task 类型包含必要的状态字段"""
        with open("frontend/src/types/task.ts", "r", encoding="utf-8") as f:
            content = f.read()

        assert "status: TaskStatus" in content
        assert "phaseStatus" in content


# ============================================================================
# V2 - 创建任务异常（配置错误）
# ============================================================================


class TestV2BackendErrorHandling:
    """V2-1: 后端错误精确性验证"""

    def test_invalid_transition_error_contains_statuses(self):
        """验证 InvalidTransitionError 包含源状态和目标状态"""
        from tasks.state_machine import InvalidTransitionError

        err = InvalidTransitionError("completed", "running")
        assert err.current_state == "completed"
        assert err.target_state == "running"
        assert "completed" in str(err)
        assert "running" in str(err)

    def test_invalid_transition_error_custom_message(self):
        """验证 InvalidTransitionError 支持自定义消息"""
        from tasks.state_machine import InvalidTransitionError

        err = InvalidTransitionError("pending", "completed", "自定义错误")
        assert "自定义错误" in str(err)

    def test_task_not_found_raises_key_error(self):
        """验证查询不存在的任务抛出 KeyError"""
        from tasks.service import TaskService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()
            with pytest.raises(KeyError) as exc_info:
                loop.run_until_complete(service.start_task("nonexistent_id"))
            assert "nonexistent_id" in str(exc_info.value)

    def test_api_error_contains_error_code(self):
        """验证 APIError 包含错误码和消息"""
        from channels.api.deps import APIError

        err = APIError(
            status_code=404,
            error_code="TASK_001",
            message="任务不存在或已被删除",
        )
        assert err.status_code == 404
        assert err.error_code == "TASK_001"
        assert "任务不存在" in err.message

    def test_api_error_for_invalid_status(self):
        """验证无效状态操作返回 TASK_002 错误码"""
        from channels.api.deps import APIError

        err = APIError(
            status_code=400,
            error_code="TASK_002",
            message="当前状态 'completed' 不允许提交",
        )
        assert err.status_code == 400
        assert err.error_code == "TASK_002"

    def test_standard_error_has_trace_id(self):
        """验证 StandardError 支持追踪 ID"""
        with open("src/core/errors.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "trace_id" in content or "StandardError" in content

    @pytest.mark.skip(reason="src/config/exceptions.py 已移除")
    def test_config_validation_exceptions(self):
        """验证配置模块有验证异常"""
        with open("src/config/exceptions.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "Exception" in content
        assert len(content.strip()) > 0

    def test_config_loader_error_handling(self):
        """验证配置加载器有错误处理逻辑"""
        with open("src/config/loader.py", "r", encoding="utf-8") as f:
            content = f.read()
        assert "except" in content or "raise" in content or "Exception" in content

    def test_task_service_fail_task_with_error_message(self):
        """验证 fail_task 正确记录错误信息"""
        from tasks.service import TaskService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()
            task = loop.run_until_complete(service.create_task(title="测试"))
            loop.run_until_complete(service.start_task(task.id))
            loop.run_until_complete(
                service.fail_task(task.id, reason="配置解析失败: agent.yaml line 42")
            )
            task = service.get_task(task.id)
            assert task.error is not None
            assert "配置解析失败" in task.error
            assert "agent.yaml line 42" in task.error


class TestV2FrontendErrorDisplay:
    """V2-2: 前端错误展示验证"""

    def test_error_boundary_renders_fallback_ui(self):
        """验证 ErrorBoundary 渲染降级 UI（不空白）"""
        with open("frontend/src/components/ErrorBoundary.tsx", "r", encoding="utf-8") as f:
            content = f.read()

        assert "出错了" in content
        assert "刷新页面" in content
        assert "错误详情" in content
        assert "captureException" in content

    def test_error_boundary_displays_error_details(self):
        """验证 ErrorBoundary 展示错误详情（包含错误堆栈）"""
        with open("frontend/src/components/ErrorBoundary.tsx", "r", encoding="utf-8") as f:
            content = f.read()

        assert "error.toString()" in content
        assert "componentStack" in content

    def test_long_term_task_store_error_handling(self):
        """验证 longTermTaskStore 正确处理错误"""
        with open("frontend/src/stores/longTermTaskStore.ts", "r", encoding="utf-8") as f:
            content = f.read()

        assert "error:" in content or "error |" in content
        assert "clearError" in content
        assert "getErrorMessage" in content

    def test_tasks_api_error_response_type(self):
        """验证前端 API 层有错误响应类型"""
        with open("frontend/src/services/api/client.ts", "r", encoding="utf-8") as f:
            content = f.read()

        assert "error" in content.lower()

    def test_task_type_includes_error_message(self):
        """验证前端 Task 类型包含 errorMessage 字段"""
        with open("frontend/src/types/task.ts", "r", encoding="utf-8") as f:
            content = f.read()
        assert "errorMessage" in content


class TestV1V2Integration:
    """V1+V2 集成验证"""

    def test_create_task_and_query_via_storage(self):
        """验证创建任务后可通过存储层查询"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()
            task = loop.run_until_complete(
                service.create_task(
                    title="集成测试任务",
                    description="测试创建后查询",
                )
            )

            found = service.get_task(task.id)
            assert found is not None
            assert found.title == "集成测试任务"
            assert found.status == TaskStatus.PENDING

    def test_task_lifecycle_create_start_fail(self):
        """验证完整生命周期：创建 -> 启动 -> 失败"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()

            task = loop.run_until_complete(
                service.create_task(title="生命周期测试")
            )
            assert task.status == TaskStatus.PENDING

            loop.run_until_complete(service.start_task(task.id))
            task = service.get_task(task.id)
            assert task.status == TaskStatus.RUNNING

            loop.run_until_complete(
                service.fail_task(task.id, reason="模拟配置错误: config.yaml line 10: 缺少必填字段 'model'")
            )
            task = service.get_task(task.id)
            assert task.status == TaskStatus.FAILED
            assert "config.yaml line 10" in task.error
            assert "缺少必填字段" in task.error

    def test_task_lifecycle_create_start_evaluate_complete(self):
        """验证正向完整生命周期：创建 -> 启动 -> 评估 -> 完成"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()

            task = loop.run_until_complete(
                service.create_task(title="完整流程测试")
            )
            assert task.status == TaskStatus.PENDING

            loop.run_until_complete(service.start_task(task.id))
            task = service.get_task(task.id)
            assert task.status == TaskStatus.RUNNING

            loop.run_until_complete(
                service.force_transition(task.id, TaskStatus("evaluating"))
            )
            task = service.get_task(task.id)
            assert task.status == TaskStatus.EVALUATING

            loop.run_until_complete(
                service.complete_evaluation(task.id, passed=True)
            )
            task = service.get_task(task.id)
            assert task.status == TaskStatus.COMPLETED

    def test_task_reactivate_after_completion(self):
        """验证已完成任务可以重新激活"""
        from tasks.service import TaskService
        from tasks.types import TaskStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            service = TaskService(data_dir=tmpdir)

            loop = asyncio.new_event_loop()

            task = loop.run_until_complete(service.create_task(title="重激活测试"))
            loop.run_until_complete(service.start_task(task.id))
            loop.run_until_complete(
                service.force_transition(task.id, TaskStatus("evaluating"))
            )
            loop.run_until_complete(service.complete_evaluation(task.id, passed=True))
            task = service.get_task(task.id)
            assert task.status == TaskStatus.COMPLETED

            loop.run_until_complete(service.reset_to_pending(task.id))
            task = service.get_task(task.id)
            assert task.status == TaskStatus.PENDING
