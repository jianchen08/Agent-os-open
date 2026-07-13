"""ChildTaskGuard 子任务守护插件单元测试。

验证插件在以下场景中的行为：
1. 有活跃子任务 → wait 挂起
2. 无活跃子任务 → 不拦截
3. 有工具调用 → 不拦截
4. 非 llm_call → 不拦截
5. 无 task_id → 不拦截
6. 各种子任务状态组合
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, create_initial_state
from plugins.output.child_task_guard import ChildTaskGuard


@pytest.fixture
def guard() -> ChildTaskGuard:
    """创建默认配置的 ChildTaskGuard 实例。"""
    return ChildTaskGuard({"idle_remind_limit": 3, "priority": 28})


@pytest.fixture
def llm_state() -> dict:
    """创建 LLM 调用阶段的基础状态。"""
    return create_initial_state(
        session_id="test-session",
        task_id="parent-task-001",
        core_type="llm_call",
        raw_result="我在等待子任务完成",
        raw_tool_calls=[],
    )


@pytest.fixture
def make_ctx():
    """创建上下文工厂。"""
    def _make(state: dict, task_service=None, timer_manager=None):
        ctx = PluginContext(state=state, config={}, _services={})
        ctx._services["task_service"] = task_service or MagicMock()
        ctx._services["timer_manager"] = timer_manager or MagicMock()
        return ctx
    return _make


def _mock_subtask(status_value: str) -> MagicMock:
    """创建模拟子任务。"""
    st = MagicMock()
    st.status.value = status_value
    return st


def _mock_task_service(subtasks: list) -> MagicMock:
    """创建模拟 TaskService。"""
    svc = MagicMock()
    svc.list_subtasks.return_value = subtasks
    return svc


def _mock_timer_manager() -> MagicMock:
    """创建模拟 TimerManager。"""
    mgr = MagicMock()
    mgr.reset_timer = AsyncMock(return_value=None)
    return mgr


# ── 基础属性 ──


class TestChildTaskGuardProperties:
    """测试插件基础属性。"""

    def test_name(self, guard):
        assert guard.name == "child_task_guard"

    def test_priority(self, guard):
        assert guard.priority == 28

    def test_error_policy(self, guard):
        assert guard.error_policy == ErrorPolicy.SKIP

    def test_custom_priority(self):
        g = ChildTaskGuard({"priority": 15})
        assert g.priority == 15

    def test_default_idle_remind_limit(self):
        g = ChildTaskGuard({})
        assert g._idle_remind_limit == 3


# ── 有活跃子任务 ──


class TestActiveChildren:
    """测试有活跃子任务时的行为。"""

    @pytest.mark.asyncio
    async def test_running_child_triggers_wait(self, guard, llm_state, make_ctx):
        """有 running 子任务时返回 wait 信号。"""
        task_svc = _mock_task_service([_mock_subtask("running")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"

    @pytest.mark.asyncio
    async def test_pending_child_triggers_wait(self, guard, llm_state, make_ctx):
        """有 pending 子任务时返回 wait 信号。"""
        task_svc = _mock_task_service([_mock_subtask("pending")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"

    @pytest.mark.asyncio
    async def test_evaluating_child_triggers_wait(self, guard, llm_state, make_ctx):
        """有 evaluating 子任务时返回 wait 信号。"""
        task_svc = _mock_task_service([_mock_subtask("evaluating")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"

    @pytest.mark.asyncio
    async def test_scheduled_child_triggers_wait(self, guard, llm_state, make_ctx):
        """有 scheduled 子任务时返回 wait 信号。"""
        task_svc = _mock_task_service([_mock_subtask("scheduled")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"

    @pytest.mark.asyncio
    async def test_mixed_children_one_active_triggers_wait(self, guard, llm_state, make_ctx):
        """多个子任务中只要有一个活跃就触发 wait。"""
        task_svc = _mock_task_service([
            _mock_subtask("completed"),
            _mock_subtask("running"),
            _mock_subtask("failed"),
        ])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"


# ── 无活跃子任务 ──


class TestNoActiveChildren:
    """测试无活跃子任务时的行为。"""

    @pytest.mark.asyncio
    async def test_completed_children_no_wait(self, guard, llm_state, make_ctx):
        """所有子任务已完成时不拦截。"""
        task_svc = _mock_task_service([
            _mock_subtask("completed"),
            _mock_subtask("completed"),
        ])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_failed_children_no_wait(self, guard, llm_state, make_ctx):
        """所有子任务已失败时不拦截。"""
        task_svc = _mock_task_service([_mock_subtask("failed")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_no_children_no_wait(self, guard, llm_state, make_ctx):
        """没有子任务时不拦截。"""
        task_svc = _mock_task_service([])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None



# ── 前置条件检查 ──


class TestPreconditions:
    """测试前置条件不满足时的行为。"""

    @pytest.mark.asyncio
    async def test_tool_execute_core_type_ignored(self, guard, make_ctx):
        """core_type 为 tool_execute 时不触发。"""
        state = create_initial_state(
            session_id="test",
            task_id="task-001",
            core_type="tool_execute",
            raw_result="result",
            raw_tool_calls=[],
        )
        task_svc = _mock_task_service([_mock_subtask("running")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_no_task_id_ignored(self, guard, make_ctx):
        """无 task_id 时不触发。"""
        state = create_initial_state(
            session_id="test",
            core_type="llm_call",
            raw_result="result",
            raw_tool_calls=[],
        )
        task_svc = _mock_task_service([_mock_subtask("running")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_has_tool_calls_ignored(self, guard, llm_state, make_ctx):
        """有工具调用时不触发。"""
        llm_state["raw_tool_calls"] = [{"function": {"name": "task_manage"}}]
        task_svc = _mock_task_service([_mock_subtask("running")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_none_raw_result_ignored(self, guard, make_ctx):
        """raw_result 为 None 时不触发（无 LLM 输出）。"""
        state = create_initial_state(
            session_id="test",
            task_id="task-001",
            core_type="llm_call",
            raw_tool_calls=[],
        )
        state["raw_result"] = None
        task_svc = _mock_task_service([_mock_subtask("running")])
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None


# ── 服务不可用 ──


class TestServiceUnavailable:
    """测试服务不可用时的降级行为。"""

    @pytest.mark.asyncio
    async def test_no_task_service_no_crash(self, guard, llm_state, make_ctx):
        """TaskService 不可用时不崩溃，返回空结果。"""
        ctx = make_ctx(llm_state)
        ctx._services = {}
        ctx.get_service = lambda name: (_ for _ in ()).throw(KeyError(name))

        result = await guard.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_list_subtasks_exception_no_crash(self, guard, llm_state, make_ctx):
        """list_subtasks 抛异常时不崩溃。"""
        task_svc = MagicMock()
        task_svc.list_subtasks.side_effect = RuntimeError("db error")
        timer_mgr = _mock_timer_manager()
        ctx = make_ctx(llm_state, task_svc, timer_mgr)

        result = await guard.execute(ctx)

        assert result.route_signal is None
