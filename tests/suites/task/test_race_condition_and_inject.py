"""双重调度竞态条件与 task_id 注入链测试。

验证两个问题：
1. task_state_changed(pending) 和 task.submitted 双重事件导致同一任务被调度两次
2. ParamInjectPlugin 在管道中正确注入 task_id

测试覆盖：
- create_task 触发 on_state_change → task_state_changed 事件
- task.submitted 事件同时触发 TaskWorker
- 两个协程并发执行导致 idle 计时器冲突
- ParamInjectPlugin 从 state 正确读取 task_id 并注入工具参数
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from pipeline.types import StateKeys


# ── 测试 1: 双重调度竞态条件 ──────────────────────────


class TestDoubleDispatchRaceCondition:
    """验证 create_task + task.submitted 双重触发导致竞态。"""

    @pytest.mark.asyncio
    async def test_create_task_triggers_state_change(self):
        """create_task 应该触发 on_state_change 回调（pending 状态）。

        这是竞态条件的根源：create_task 内部触发 on_state_change，
        而 on_state_change 回调会发出 task_state_changed 事件。
        """
        state_changes = []

        def on_state_change(task_id, old_status, new_status, **kwargs):
            state_changes.append({
                "task_id": task_id,
                "old_status": old_status,
                "new_status": new_status,
            })

        from tasks.service import TaskService
        from tasks.storage import TaskStorage
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = TaskStorage(data_dir=Path(tmpdir))
            svc = TaskService(storage=storage, on_state_change=on_state_change)

            task = svc.create_task(
                title="测试任务",
                description="测试",
                metadata={"target_id": "agent_001"},
            )

            assert len(state_changes) == 1
            assert state_changes[0]["new_status"] == "pending"
            assert state_changes[0]["task_id"] == task.id

    @pytest.mark.asyncio
    async def test_timer_double_create_raises_error(self):
        """对同一个 task_id 调用两次 create_timer 应该抛出 ValueError。"""
        from tasks.timer_manager import TimerManager

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
        """模拟两个协程并发调用 create_timer，验证只有一个成功。"""
        from tasks.timer_manager import TimerManager

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

    @pytest.mark.asyncio
    async def test_task_worker_dedup_on_submitted(self):
        """验证 _on_task_submitted 有去重机制，同一 task_id 只创建一次协程。

        BUG-FIX-fix_20260516_double_dispatch:
        修复前：_on_task_submitted 无去重，同一 task_id 收到两次事件会创建两个协程。
        修复后：通过 _task_id_to_bg_task 检查，已有未完成协程则跳过。
        """
        from infrastructure.task_worker import TaskWorker

        executed_count = 0

        async def mock_execute(task_data):
            nonlocal executed_count
            executed_count += 1

        event_bus = MagicMock()
        event_bus.subscribe = MagicMock()

        services = {"task_service": MagicMock(), "timer_manager": MagicMock()}

        with patch.object(TaskWorker, '__init__', lambda self, **kwargs: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._running = True
            worker._tasks = set()
            worker._terminal_events = {}
            worker._task_id_to_bg_task = {}
            worker._services = services
            worker._event_bus = event_bus

            # 模拟收到两次 task.submitted 事件
            event1 = MagicMock()
            event1.data = {"task_id": "task_001"}

            event2 = MagicMock()
            event2.data = {"task_id": "task_001"}

            original_create_task = asyncio.create_task

            created_tasks = []

            def track_create_task(coro):
                t = original_create_task(coro)
                created_tasks.append(t)
                return t

            with patch("asyncio.create_task", side_effect=track_create_task):
                with patch.object(worker, "_execute_background_task", side_effect=mock_execute):
                    await worker._on_task_submitted(event1)
                    await worker._on_task_submitted(event2)

            # 修复后应该只创建一个协程（第二次被去重跳过）
            assert len(created_tasks) == 1, (
                f"_on_task_submitted 应该只创建 1 个协程（去重），实际创建了 {len(created_tasks)}"
            )


# ── 测试 2: task_id 注入链 ──────────────────────────────


class TestTaskIdInjection:
    """验证 ParamInjectPlugin 正确注入 task_id。"""

    @pytest.mark.asyncio
    async def test_param_inject_reads_task_id_from_state(self):
        """ParamInjectPlugin 应该从 state 中读取 task_id 并注入到工具参数。"""
        from plugins.input.param_inject import ParamInjectPlugin
        from pipeline.plugin import PluginContext

        plugin = ParamInjectPlugin()

        ctx = MagicMock(spec=PluginContext)
        ctx.state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.SESSION_ID: "session_001",
            StateKeys.TASK_ID: "task_abc123",
            "user_id": "user_001",
            StateKeys.RAW_TOOL_CALLS: [
                {
                    "name": "task_evaluate",
                    "args": {
                        "action": "auto_complete",
                    },
                },
            ],
        }

        result = await plugin._do_work(ctx)

        injected_calls = result.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(injected_calls) == 1
        assert injected_calls[0]["args"]["task_id"] == "task_abc123", (
            "task_id 应该从 state 注入到工具参数"
        )

    @pytest.mark.asyncio
    async def test_param_inject_skips_when_no_task_id_in_state(self):
        """state 中没有 task_id 时不应该注入空值。"""
        from plugins.input.param_inject import ParamInjectPlugin
        from pipeline.plugin import PluginContext

        plugin = ParamInjectPlugin()

        ctx = MagicMock(spec=PluginContext)
        ctx.state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.SESSION_ID: "session_001",
            StateKeys.TASK_ID: "",  # 空 task_id
            "user_id": "user_001",
            StateKeys.RAW_TOOL_CALLS: [
                {
                    "name": "task_evaluate",
                    "args": {"action": "auto_complete"},
                },
            ],
        }

        result = await plugin._do_work(ctx)

        injected_calls = result.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(injected_calls) == 1
        assert "task_id" not in injected_calls[0]["args"], (
            "task_id 为空时不应该注入"
        )

    @pytest.mark.asyncio
    async def test_param_inject_does_not_overwrite_existing_task_id(self):
        """不覆盖 LLM 显式传入的 task_id。"""
        from plugins.input.param_inject import ParamInjectPlugin
        from pipeline.plugin import PluginContext

        plugin = ParamInjectPlugin()

        ctx = MagicMock(spec=PluginContext)
        ctx.state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.SESSION_ID: "session_001",
            StateKeys.TASK_ID: "state_task_id",
            "user_id": "user_001",
            StateKeys.RAW_TOOL_CALLS: [
                {
                    "name": "task_evaluate",
                    "args": {"action": "auto_complete", "task_id": "explicit_task_id"},
                },
            ],
        }

        result = await plugin._do_work(ctx)

        injected_calls = result.get(StateKeys.RAW_TOOL_CALLS, [])
        assert injected_calls[0]["args"]["task_id"] == "explicit_task_id", (
            "不应覆盖显式传入的 task_id"
        )

    @pytest.mark.asyncio
    async def test_engine_run_passes_task_id_via_extra_state(self):
        """验证 engine.run(task_id=xxx) 通过 extra_state 传入 state。"""
        from pipeline.engine import PipelineEngine

        engine = PipelineEngine(
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            plugin_registry=MagicMock(),
            services={},
        )

        # Mock _run_loop 以避免真正执行
        captured_state = {}

        async def mock_run_loop(state, **kwargs):
            nonlocal captured_state
            captured_state = dict(state)
            return state

        engine._run_loop = mock_run_loop
        engine._load_system_default_agent = MagicMock()

        mock_config = MagicMock()
        mock_config.to_state.return_value = {"system_prompt": "test"}

        await engine.run(
            user_input="test",
            agent_config=mock_config,
            task_id="test_task_001",
        )

        assert captured_state.get(StateKeys.TASK_ID) == "test_task_001", (
            f"task_id 应该通过 extra_state 写入 state，实际: {captured_state.get(StateKeys.TASK_ID)}"
        )


# ── 测试 3: _on_task_state_changed 去重检查 ──────────────


class TestTaskWorkerDedupCheck:
    """验证 _on_task_state_changed 不再处理 pending 状态（修复后）。"""

    @pytest.mark.asyncio
    async def test_pending_state_no_longer_triggers_execution(self):
        """pending 状态变更不再触发执行（已删除该分支）。"""
        from infrastructure.task_worker import TaskWorker

        with patch.object(TaskWorker, '__init__', lambda self, **kwargs: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._running = True
            worker._tasks = set()
            worker._terminal_events = {}
            worker._services = {"task_service": MagicMock()}

            mock_task = MagicMock()
            mock_task.metadata = {"target_id": "agent_001", "task_scope": "short_term"}
            mock_task.target_type = "agent"
            mock_task.title = "测试"
            mock_task.description = "描述"

            mock_task_service = MagicMock()
            mock_task_service.get_task.return_value = mock_task
            worker._services = {"task_service": mock_task_service}

            event = MagicMock()
            event.data = {
                "task_id": "task_001",
                "new_status": "pending",
            }

            with patch("asyncio.create_task") as mock_create:
                await worker._on_task_state_changed(event)
                assert not mock_create.called, (
                    "修复后 pending 状态变更不应触发执行"
                )

    @pytest.mark.asyncio
    async def test_terminal_state_still_sets_event(self):
        """终态事件仍然正确设置 asyncio.Event。"""
        from infrastructure.task_worker import TaskWorker

        with patch.object(TaskWorker, '__init__', lambda self, **kwargs: None):
            worker = TaskWorker.__new__(TaskWorker)
            worker._running = True
            worker._tasks = set()
            worker._terminal_events = {"task_001": asyncio.Event()}
            worker._services = {}

            event = MagicMock()
            event.data = {
                "task_id": "task_001",
                "new_status": "completed",
            }

            with patch.object(worker, "_check_stale_containers", new_callable=AsyncMock):
                await worker._on_task_state_changed(event)

            assert worker._terminal_events["task_001"].is_set(), (
                "终态事件应该被 set"
            )


# ── 测试 4: 端到端竞态模拟 ──────────────────────────────


class TestEndToEndRaceSimulation:
    """模拟完整的双重调度竞态场景。"""

    @pytest.mark.asyncio
    async def test_double_dispatch_simulation(self):
        """模拟 create_task → on_state_change + task.submitted 双重触发。

        场景：
        1. task_submit 调用 task_service.create_task() → 触发 on_state_change(pending)
        2. on_state_change → emit("task_state_changed", {new_status: "pending"})
        3. TaskWorker._on_task_state_changed → 检查 _terminal_events（空）→ 触发执行
        4. task_submit 调用 event_bus.emit("task.submitted")
        5. TaskWorker._on_task_submitted → 再次触发执行
        6. 两个协程并发 → 第二个 create_timer 失败
        """
        from tasks.timer_manager import TimerManager

        TimerManager._instance = None
        TimerManager._initialized = False

        tm = TimerManager.get_instance()

        task_id = "race_test_task"
        execution_log = []

        async def simulate_worker_execution(label: str):
            """模拟 TaskWorker._execute_background_task 的计时器注册部分。"""
            execution_log.append(f"{label}: 开始执行")
            try:
                await tm.create_timer(
                    task_id=task_id,
                    timeout=60.0,
                    callback=lambda tid: None,
                )
                execution_log.append(f"{label}: 计时器注册成功")
            except ValueError as e:
                execution_log.append(f"{label}: 计时器注册失败 - {e}")

        # 并发执行两个协程（模拟两个事件同时触发）
        await asyncio.gather(
            simulate_worker_execution("路径A(task_state_changed)"),
            simulate_worker_execution("路径B(task.submitted)"),
        )

        # 验证结果
        success_count = sum(1 for log in execution_log if "注册成功" in log)
        conflict_count = sum(1 for log in execution_log if "注册失败" in log)

        assert success_count == 1, f"应该只有一个路径成功，实际: {execution_log}"
        assert conflict_count == 1, f"应该有一个路径冲突，实际: {execution_log}"

        print("\n执行日志:")
        for log in execution_log:
            print(f"  {log}")

        # 清理
        try:
            await tm.cancel_timer(task_id)
        except Exception:
            pass

        TimerManager._instance = None
        TimerManager._initialized = False
