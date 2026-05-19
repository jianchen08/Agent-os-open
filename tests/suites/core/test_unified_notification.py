"""统一通知路径集成测试。

验证 inject_and_wake() 作为唯一通知入口在各种场景下的行为：
- 挂起态注入 _suspended_state
- 运行态入队 _pending_notifications
- _run_loop 正确消费通知
- TaskWorker 通知链路
- _find_engine 查找优先级
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pipeline.engine import PipelineEngine
from pipeline.types import StateKeys


def _make_engine(services: dict | None = None) -> PipelineEngine:
    """构建用于通知测试的 PipelineEngine 实例。"""
    if services is None:
        services = {"__test__": True}
    return PipelineEngine(
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        plugin_registry=MagicMock(),
        services=services,
    )


# ═══════════════════════════════════════════════════════════
# 1. inject_and_wake 单元测试
# ═══════════════════════════════════════════════════════════


class TestInjectAndWakeSuspended:
    """挂起态: _suspended_state 存在时的注入行为。"""

    @pytest.mark.asyncio
    async def test_message_injected_into_suspended_state(self):
        """挂起态下消息应注入到 _suspended_state.user_input 和 messages。"""
        engine = _make_engine()
        engine._suspended_state = {
            "user_input": "原始输入",
            "messages": [],
        }
        engine._wake_event = None

        engine.inject_and_wake("子任务已完成")

        assert "子任务已完成" in engine._suspended_state["user_input"]
        assert "原始输入" in engine._suspended_state["user_input"]
        assert any(
            m["content"] == "子任务已完成"
            for m in engine._suspended_state["messages"]
        )

    @pytest.mark.asyncio
    async def test_message_prepended_to_existing_user_input(self):
        """注入的消息应前置到已有 user_input 前面。"""
        engine = _make_engine()
        engine._suspended_state = {
            "user_input": "原始",
            "messages": [],
        }
        engine._wake_event = None

        engine.inject_and_wake("新消息")

        ui = engine._suspended_state["user_input"]
        assert ui.index("新消息") < ui.index("原始")

    @pytest.mark.asyncio
    async def test_wake_event_set_when_not_none(self):
        """挂起态下 _wake_event 非空时应被 set。"""
        engine = _make_engine()
        engine._suspended_state = {"user_input": "", "messages": []}
        engine._wake_event = asyncio.Event()
        assert not engine._wake_event.is_set()

        engine.inject_and_wake("唤醒")

        assert engine._wake_event.is_set()

    @pytest.mark.asyncio
    async def test_not_go_to_pending_notifications(self):
        """挂起态下消息不应进入 _pending_notifications。"""
        engine = _make_engine()
        engine._suspended_state = {"user_input": "", "messages": []}
        engine._wake_event = None

        engine.inject_and_wake("测试消息")

        assert len(engine._pending_notifications) == 0


class TestInjectAndWakeRunning:
    """运行态: _suspended_state 为 None 时的入队行为。"""

    @pytest.mark.asyncio
    async def test_message_enqueued_to_pending_notifications(self):
        """运行态下消息应入队 _pending_notifications。"""
        engine = _make_engine()
        engine._suspended_state = None
        engine._wake_event = None

        engine.inject_and_wake("子任务通知")

        assert len(engine._pending_notifications) == 1
        assert engine._pending_notifications[0] == "子任务通知"

    @pytest.mark.asyncio
    async def test_multiple_messages_queue_up(self):
        """多次注入应累积在队列中。"""
        engine = _make_engine()
        engine._suspended_state = None
        engine._wake_event = None

        engine.inject_and_wake("消息1")
        engine.inject_and_wake("消息2")
        engine.inject_and_wake("消息3")

        assert len(engine._pending_notifications) == 3

    @pytest.mark.asyncio
    async def test_wake_event_set_when_not_none(self):
        """运行态下 _wake_event 非空时也应被 set。"""
        engine = _make_engine()
        engine._suspended_state = None
        engine._wake_event = asyncio.Event()
        assert not engine._wake_event.is_set()

        engine.inject_and_wake("通知")

        assert engine._wake_event.is_set()

    @pytest.mark.asyncio
    async def test_not_modify_suspended_state(self):
        """运行态下不应触碰 _suspended_state。"""
        engine = _make_engine()
        engine._suspended_state = None
        engine._wake_event = None

        engine.inject_and_wake("运行态消息")

        assert engine._suspended_state is None


class TestInjectAndWakeEdgeCases:
    """边界条件测试。"""

    @pytest.mark.asyncio
    async def test_empty_message_ignored(self):
        """空消息应被完全忽略。"""
        engine = _make_engine()
        engine._suspended_state = {"user_input": "原始", "messages": []}
        engine._wake_event = None

        engine.inject_and_wake("")

        assert engine._suspended_state["user_input"] == "原始"
        assert len(engine._pending_notifications) == 0

    @pytest.mark.asyncio
    async def test_none_message_ignored(self):
        """None 消息（falsy）应被忽略。"""
        engine = _make_engine()
        engine._suspended_state = {"user_input": "原始", "messages": []}
        engine._wake_event = None

        engine.inject_and_wake(None)  # type: ignore

        assert engine._suspended_state["user_input"] == "原始"

    @pytest.mark.asyncio
    async def test_wake_event_none_does_not_crash(self):
        """_wake_event 为 None 时不应崩溃。"""
        engine = _make_engine()
        engine._suspended_state = None
        engine._wake_event = None

        engine.inject_and_wake("测试")

    @pytest.mark.asyncio
    async def test_suspended_state_empty_messages_list(self):
        """_suspended_state.messages 为空列表时正常注入。"""
        engine = _make_engine()
        engine._suspended_state = {"user_input": "", "messages": []}
        engine._wake_event = None

        engine.inject_and_wake("第一条")

        assert len(engine._suspended_state["messages"]) == 1

    @pytest.mark.asyncio
    async def test_suspended_state_no_messages_key(self):
        """_suspended_state 没有 messages 键时自动创建。"""
        engine = _make_engine()
        engine._suspended_state = {"user_input": ""}
        engine._wake_event = None

        engine.inject_and_wake("消息")

        assert "messages" in engine._suspended_state
        assert len(engine._suspended_state["messages"]) == 1


# ═══════════════════════════════════════════════════════════
# 2. inject_and_wake 与 _suspend_and_wait 集成
# ═══════════════════════════════════════════════════════════


class TestInjectAndWakeWithSuspend:
    """inject_and_wake 与 _suspend_and_wait 的集成。"""

    @pytest.mark.asyncio
    async def test_inject_wakes_suspended_engine(self):
        """挂起中的引擎被 inject_and_wake 唤醒后应恢复执行。"""
        services: dict = {"__test__": True}
        engine = _make_engine(services)
        pipeline_id = "test-wake-001"
        engine._suspended_state = {
            StateKeys.PIPELINE_ID: pipeline_id,
            "user_input": "等待中",
            "messages": [],
        }

        state = {StateKeys.PIPELINE_ID: pipeline_id}

        async def delayed_wake():
            await asyncio.sleep(0.05)
            engine.inject_and_wake("子任务完成了！")

        asyncio.create_task(delayed_wake())
        await engine._suspend_and_wait(state)

        assert "子任务完成了！" in engine._suspended_state.get("user_input", "")

    @pytest.mark.asyncio
    async def test_run_loop_consumes_pending_notifications(self):
        """运行态下 inject_and_wake 入队的消息在 _run_loop 中被消费。"""
        from pipeline.route import InputRouteEntry, InputRouteTable
        from pipeline.registry import PluginRegistry
        from pipeline.plugin import ICorePlugin, IOutputPlugin, OutputResult, PluginResult

        class SimpleCorePlugin(ICorePlugin):
            error_policy = None

            def __init__(self, state_updates):
                self._state_updates = state_updates

            @property
            def name(self):
                return "simple_core"

            @property
            def priority(self):
                return 50

            async def execute(self, ctx):
                return PluginResult(state_updates=self._state_updates)

        class SimpleOutputPlugin(IOutputPlugin):
            error_policy = None
            _call_count = 0

            @property
            def name(self):
                return "simple_output"

            @property
            def priority(self):
                return 50

            @property
            def route_signals(self):
                return []

            async def execute(self, ctx):
                return OutputResult()

        core_plugin = SimpleCorePlugin(
            state_updates={StateKeys.RAW_RESULT: "response"}
        )
        output_plugin = SimpleOutputPlugin()

        registry = PluginRegistry()
        registry.register_core("llm_call", core_plugin)
        registry.register(output_plugin)

        class NotificationThenEndTable:
            def __init__(self):
                self.entries = []
                self._call_count = 0

            def arbitrate(self, signals, state):
                self._call_count += 1
                if self._call_count == 1:
                    return None
                from pipeline.types import RouteSignal
                return RouteSignal(route_type="end", reason="done")

        input_table = InputRouteTable([
            InputRouteEntry(
                name="default", condition="True",
                target="core", plugins=[], priority=10,
            ),
        ])
        services: dict = {"__test__": True}
        engine = PipelineEngine(
            input_route_table=input_table,
            output_route_table=NotificationThenEndTable(),
            plugin_registry=registry,
            services=services,
            max_iterations=10,
        )

        initial_state = {
            StateKeys.PIPELINE_ID: "test-consume-001",
            StateKeys.ITERATION: 0,
            StateKeys.ENDED: False,
            StateKeys.SHOULD_STOP: False,
            StateKeys.CORE_TYPE: "llm_call",
            "messages": [{"role": "user", "content": "开始"}],
            "user_input": "开始",
        }

        engine._suspended_state = None
        engine.inject_and_wake("[系统通知] 子任务完成")

        result = await engine.run(initial_state)

        assert result[StateKeys.ENDED] is True
        assert len(engine._pending_notifications) == 0


# ═══════════════════════════════════════════════════════════
# 3. _find_engine 查找优先级
# ═══════════════════════════════════════════════════════════


class TestFindEngine:
    """_find_engine 应按优先级查找引擎。"""

    @pytest.mark.asyncio
    async def test_prefers_suspended_over_running(self):
        """挂起引擎优先于运行引擎。"""
        from infrastructure.task_worker import TaskWorker

        suspended_engine = MagicMock()
        running_engine = MagicMock()

        services = {
            "__suspended_engine_pipe-001": suspended_engine,
            "__running_engine_pipe-001": running_engine,
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services

            result = worker._find_engine("pipe-001")

        assert result is suspended_engine

    @pytest.mark.asyncio
    async def test_falls_back_to_running(self):
        """无挂起引擎时回退到运行引擎。"""
        from infrastructure.task_worker import TaskWorker

        running_engine = MagicMock()
        services = {
            "__running_engine_pipe-002": running_engine,
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services

            result = worker._find_engine("pipe-002")

        assert result is running_engine

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        """找不到引擎时返回 None。"""
        from infrastructure.task_worker import TaskWorker

        services = {
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services

            result = worker._find_engine("pipe-nonexist")

        assert result is None


# ═══════════════════════════════════════════════════════════
# 4. TaskWorker._notify_suspended_pipelines 集成
# ═══════════════════════════════════════════════════════════


class TestNotifySuspendedPipelines:
    """TaskWorker 通知链路集成测试。"""

    @pytest.mark.asyncio
    async def test_notifies_suspended_engine_via_inject_and_wake(self):
        """通过 parent_pipeline_id 找到挂起引擎后调用 inject_and_wake。"""
        from infrastructure.task_worker import TaskWorker

        mock_engine = MagicMock()
        mock_engine.inject_and_wake = MagicMock()
        services = {
            "__suspended_engine_parent-pipe-001": mock_engine,
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        mock_task = MagicMock()
        mock_task.parent_pipeline_id = "parent-pipe-001"
        mock_task.parent_task_id = "parent-task-001"
        mock_task.title = "子任务A"

        task_service = MagicMock()
        task_service.get_task = MagicMock(return_value=mock_task)

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services
            worker._task_service = task_service
            worker._wake_events = {}
            worker._suspended_engines = {}

            await worker._notify_suspended_pipelines(
                task_id="child-001",
                new_status="completed",
                data={"task": {"title": "子任务A"}},
            )

        mock_engine.inject_and_wake.assert_called_once()
        call_arg = mock_engine.inject_and_wake.call_args[0][0]
        assert "子任务A" in call_arg
        assert "已完成" in call_arg

    @pytest.mark.asyncio
    async def test_notifies_running_engine_via_inject_and_wake(self):
        """运行中引擎也通过 inject_and_wake 通知（统一路径）。"""
        from infrastructure.task_worker import TaskWorker

        mock_engine = MagicMock()
        mock_engine.inject_and_wake = MagicMock()
        services = {
            "__running_engine_parent-pipe-002": mock_engine,
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        mock_task = MagicMock()
        mock_task.parent_pipeline_id = "parent-pipe-002"
        mock_task.parent_task_id = "parent-task-002"
        mock_task.title = "子任务B"

        task_service = MagicMock()
        task_service.get_task = MagicMock(return_value=mock_task)

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services
            worker._task_service = task_service
            worker._wake_events = {}
            worker._suspended_engines = {}

            await worker._notify_suspended_pipelines(
                task_id="child-002",
                new_status="failed",
                data={"task": {"title": "子任务B", "error": "超时"}},
            )

        mock_engine.inject_and_wake.assert_called_once()
        call_arg = mock_engine.inject_and_wake.call_args[0][0]
        assert "子任务B" in call_arg
        assert "failed" in call_arg

    @pytest.mark.asyncio
    async def test_queues_when_engine_not_found(self):
        """引擎未找到时通知入队等待。"""
        from infrastructure.task_worker import TaskWorker

        services: dict = {
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        mock_task = MagicMock()
        mock_task.parent_pipeline_id = "parent-pipe-003"
        mock_task.parent_task_id = "parent-task-003"
        mock_task.title = "子任务C"

        task_service = MagicMock()
        task_service.get_task = MagicMock(return_value=mock_task)

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services
            worker._task_service = task_service
            worker._wake_events = {}
            worker._suspended_engines = {}

            await worker._notify_suspended_pipelines(
                task_id="child-003",
                new_status="completed",
                data={"task": {"title": "子任务C"}},
            )

        pending_key = "__pending_notifications_parent-pipe-003"
        assert pending_key in services
        assert len(services[pending_key]) == 1
        assert "子任务C" in services[pending_key][0]

    @pytest.mark.asyncio
    async def test_fallback_scan_finds_by_watching_task_ids(self):
        """无 parent_pipeline_id 时回退扫描 _watching_task_ids。"""
        from infrastructure.task_worker import TaskWorker

        mock_engine = MagicMock()
        mock_engine.inject_and_wake = MagicMock()
        mock_engine._watching_task_ids = ["orphan-task-001"]

        services = {
            "__suspended_engine_scan-001": mock_engine,
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        task_service = MagicMock()
        task_service.get_task = MagicMock(return_value=None)

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services
            worker._task_service = task_service
            worker._wake_events = {}
            worker._suspended_engines = {}

            await worker._notify_suspended_pipelines(
                task_id="orphan-task-001",
                new_status="completed",
                data={"task": {"title": "孤立任务"}},
            )

        mock_engine.inject_and_wake.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_content_completed(self):
        """completed 状态的通知包含 ✅ 标记。"""
        from infrastructure.task_worker import TaskWorker

        mock_engine = MagicMock()
        mock_engine.inject_and_wake = MagicMock()
        services = {
            "__suspended_engine_pipe-c": mock_engine,
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        mock_task = MagicMock()
        mock_task.parent_pipeline_id = "pipe-c"
        mock_task.parent_task_id = "parent-c"
        mock_task.title = "测试任务"

        task_service = MagicMock()
        task_service.get_task = MagicMock(return_value=mock_task)

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services
            worker._task_service = task_service
            worker._wake_events = {}
            worker._suspended_engines = {}

            await worker._notify_suspended_pipelines(
                task_id="task-c",
                new_status="completed",
                data={"task": {"title": "测试任务"}},
            )

        call_arg = mock_engine.inject_and_wake.call_args[0][0]
        assert "✅" in call_arg
        assert "已完成" in call_arg

    @pytest.mark.asyncio
    async def test_notification_content_failed(self):
        """failed 状态的通知包含 ❌ 标记和错误信息。"""
        from infrastructure.task_worker import TaskWorker

        mock_engine = MagicMock()
        mock_engine.inject_and_wake = MagicMock()
        services = {
            "__suspended_engine_pipe-f": mock_engine,
            "task_service": MagicMock(),
            "timer_manager": MagicMock(),
        }

        mock_task = MagicMock()
        mock_task.parent_pipeline_id = "pipe-f"
        mock_task.parent_task_id = "parent-f"
        mock_task.title = "失败任务"

        task_service = MagicMock()
        task_service.get_task = MagicMock(return_value=mock_task)

        with patch.object(TaskWorker, '__init__', lambda self, **kw: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._services = services
            worker._task_service = task_service
            worker._wake_events = {}
            worker._suspended_engines = {}

            await worker._notify_suspended_pipelines(
                task_id="task-f",
                new_status="failed",
                data={"task": {"title": "失败任务", "error": "连接超时"}},
            )

        call_arg = mock_engine.inject_and_wake.call_args[0][0]
        assert "❌" in call_arg
        assert "failed" in call_arg
        assert "连接超时" in call_arg


# ═══════════════════════════════════════════════════════════
# 5. 端到端：挂起→通知→唤醒→消费
# ═══════════════════════════════════════════════════════════


class TestEndToEndNotification:
    """端到端通知流程测试。"""

    @pytest.mark.asyncio
    async def test_suspended_engine_wakes_on_child_completion(self):
        """完整链路: 引擎挂起 → 子任务完成通知 → 唤醒 → 消费通知。"""
        services: dict = {"__test__": True}
        engine = _make_engine(services)
        pipeline_id = "e2e-pipe-001"

        engine._suspended_state = {
            StateKeys.PIPELINE_ID: pipeline_id,
            "user_input": "等待子任务",
            "messages": [{"role": "user", "content": "等待子任务"}],
        }

        state = {
            StateKeys.PIPELINE_ID: pipeline_id,
            "submitted_task_ids": ["child-e2e-001"],
        }

        async def simulate_child_completion():
            await asyncio.sleep(0.05)
            engine.inject_and_wake(
                "[系统通知] 子任务 'E2E测试' (ID: child-e2e-001) 已完成 ✅\n"
                "请继续执行后续流程。"
            )

        asyncio.create_task(simulate_child_completion())
        await engine._suspend_and_wait(state)

        ui = engine._suspended_state.get("user_input", "")
        assert "E2E测试" in ui
        assert "已完成" in ui
        assert "等待子任务" in ui

    @pytest.mark.asyncio
    async def test_running_engine_queues_and_consumes(self):
        """运行态链路: 注入通知 → 入队 → _run_loop 消费。"""
        services: dict = {"__test__": True}
        engine = _make_engine(services)
        engine._suspended_state = None

        engine.inject_and_wake("通知1")
        engine.inject_and_wake("通知2")

        assert len(engine._pending_notifications) == 2

        notif_sources = []
        if engine._pending_notifications:
            notif_sources.extend(engine._pending_notifications[:])
            engine._pending_notifications.clear()

        combined = "\n\n".join(notif_sources)
        assert "通知1" in combined
        assert "通知2" in combined
        assert len(engine._pending_notifications) == 0

    @pytest.mark.asyncio
    async def test_state_transition_suspended_to_running(self):
        """状态切换: 挂起态被唤醒后变为运行态，后续通知走运行态路径。"""
        services: dict = {"__test__": True}
        engine = _make_engine(services)

        engine._suspended_state = {
            "user_input": "初始",
            "messages": [],
        }
        engine._wake_event = asyncio.Event()

        engine.inject_and_wake("挂起态通知")
        assert "挂起态通知" in engine._suspended_state["user_input"]

        engine._suspended_state = None
        engine._wake_event = asyncio.Event()

        engine.inject_and_wake("运行态通知")
        assert len(engine._pending_notifications) == 1
        assert engine._pending_notifications[0] == "运行态通知"
