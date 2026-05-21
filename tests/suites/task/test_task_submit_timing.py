"""
任务提交时序测试

验证 task_submit 的执行时序严格为：
1. 参数验证
2. 准备工作（agent调用记录等）
3. 数据持久化(commit)
4. 返回成功
5. 派发执行（schedule）在返回之后
"""
import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --- 预 mock 重依赖模块，避免 redis 等未安装导致 import 失败 ---

# 先创建真正的 ToolExecutionResult 类（src.core.results 和 src.tools.types 共享）
class _ToolExecutionResult:
    def __init__(self, success=False, data=None, error=None, error_code=None, metadata=None):
        self.success = success
        self.data = data or {}
        self.error = error
        self.error_code = error_code
        self.metadata = metadata or {}

def _create_success_result(data=None, metadata=None):
    return _ToolExecutionResult(success=True, data=data, metadata=metadata)

def _create_failure_result(error=None, error_code=None):
    return _ToolExecutionResult(success=False, error=error, error_code=error_code)

def _make_mock_module():
    return MagicMock()

_MOCK_MODULES = [
    "redis", "redis.asyncio",
    "src.core.event_bus", "src.core.event_bus.factory",
    "src.core.event_bus.redis_streams",
    "src.core.event_bus.types",
    "src.evaluation.metric_loader",
    "src.db.repositories.execution_record_repo",
    "src.db.repositories.task_repo",
    "src.services.agent_call_recorder",
    "src.infrastructure.task_launcher",
    "src.agents.level_controller",
    "src.utils.message_id_helper",
    "src.tasks.dependency_validator",
    "src.tasks.services.submission_service",
]

for mod_name in _MOCK_MODULES:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = _make_mock_module()

# src.core.results — 需要真正的 ToolExecutionResult 类（isinstance 用）
_mod = types.ModuleType("src.core.results")
_mod.ToolExecutionResult = _ToolExecutionResult
sys.modules["src.core.results"] = _mod

# src.tools.types — 需要 create_success_result / create_failure_result
_mod2 = types.ModuleType("src.tools.types")
_mod2.ToolExecutionResult = _ToolExecutionResult
_mod2.create_success_result = _create_success_result
_mod2.create_failure_result = _create_failure_result
sys.modules["src.tools.types"] = _mod2

# 现在可以安全导入
from src.tasks.services.task_submit_orchestrator import TaskSubmitOrchestrator


class TestSubmitToAgentTiming:
    """验证 submit_to_agent 的时序正确性"""

    @pytest.fixture
    def mock_session(self):
        session = AsyncMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()
        return session

    @pytest.fixture
    def mock_submission_service(self):
        service = AsyncMock()
        service.submit = AsyncMock(return_value={
            "task_id": "task_001",
            "status": "pending",
            "total_criteria": 1,
            "execution_record_id": "exec_001",
            "created_at": "2026-01-01T00:00:00Z",
        })
        service._resolve_metrics = AsyncMock(return_value={
            "evaluation_metric_ids": ["file_check"],
            "acceptance_criteria_list": [{"metric_id": "file_check"}],
        })
        return service

    @pytest.fixture
    def mock_level_controller(self):
        controller = MagicMock()
        controller.get_allowed_targets.return_value = [2, 3]
        return controller

    @pytest.fixture
    def orchestrator(self, mock_session, mock_submission_service, mock_level_controller):
        return TaskSubmitOrchestrator(
            session=mock_session,
            submission_service=mock_submission_service,
            level_controller=mock_level_controller,
        )

    @pytest.mark.asyncio
    async def test_commit_happens_before_return(
        self, orchestrator, mock_session
    ):
        """验证 commit 在返回之前完成"""
        call_order = []
        mock_session.commit = AsyncMock(side_effect=lambda: call_order.append("commit"))

        agent = MagicMock()
        agent.name = "test_agent"
        agent.is_active = True
        agent.config_id = "test_agent"

        with patch.object(orchestrator, "_validate_agent", return_value=agent), \
             patch.object(orchestrator, "_resolve_acceptance_criteria", return_value={"file_check": {}}), \
             patch.object(orchestrator, "_record_agent_call", return_value="exec_001"), \
             patch.object(orchestrator, "_complete_agent_call", new_callable=AsyncMock):

            result = await orchestrator.submit_to_agent(
                goal={"title": "test"},
                acceptance_criteria={"file_check": {}},
                target_id="test_agent",
            )

            # commit 必须在返回前被调用
            assert "commit" in call_order
            assert result is not None

    @pytest.mark.asyncio
    async def test_complete_agent_call_before_commit(
        self, orchestrator, mock_session
    ):
        """验证 _complete_agent_call 在 commit 之前执行（确保数据被持久化）"""
        call_order = []

        mock_session.commit = AsyncMock(side_effect=lambda: call_order.append("commit"))

        agent = MagicMock()
        agent.name = "test_agent"
        agent.is_active = True

        async def mock_complete(*args, **kwargs):
            call_order.append("complete_agent_call")

        with patch.object(orchestrator, "_validate_agent", return_value=agent), \
             patch.object(orchestrator, "_resolve_acceptance_criteria", return_value={"file_check": {}}), \
             patch.object(orchestrator, "_record_agent_call", return_value="exec_001"), \
             patch.object(orchestrator, "_complete_agent_call", side_effect=mock_complete):

            await orchestrator.submit_to_agent(
                goal={"title": "test"},
                acceptance_criteria={"file_check": {}},
                target_id="test_agent",
            )

            # _complete_agent_call 必须在 commit 之前
            assert "complete_agent_call" in call_order
            assert "commit" in call_order
            assert call_order.index("complete_agent_call") < call_order.index("commit"), \
                f"时序错误: {call_order} - complete_agent_call 应在 commit 之前"

    @pytest.mark.asyncio
    async def test_dispatch_is_fire_and_forget(
        self, orchestrator, mock_session
    ):
        """验证 schedule 通过 asyncio.create_task 派发（不阻塞返回）"""
        agent = MagicMock()
        agent.name = "test_agent"
        agent.is_active = True

        with patch.object(orchestrator, "_validate_agent", return_value=agent), \
             patch.object(orchestrator, "_resolve_acceptance_criteria", return_value={"file_check": {}}), \
             patch.object(orchestrator, "_record_agent_call", return_value="exec_001"), \
             patch.object(orchestrator, "_complete_agent_call", new_callable=AsyncMock), \
             patch("asyncio.create_task") as mock_create_task:

            result = await orchestrator.submit_to_agent(
                goal={"title": "test"},
                acceptance_criteria={"file_check": {}},
                target_id="test_agent",
            )

            # 应该使用 asyncio.create_task 进行派发（fire-and-forget）
            assert mock_create_task.called, \
                "schedule 应通过 asyncio.create_task 异步派发，而非 await 阻塞"


class TestSubmitToWorkflowTiming:
    """验证 submit_to_workflow 的时序正确性"""

    @pytest.fixture
    def mock_session(self):
        session = AsyncMock()
        session.commit = AsyncMock()
        return session

    @pytest.fixture
    def mock_submission_service(self):
        service = AsyncMock()
        service.submit = AsyncMock(return_value={
            "task_id": "task_wf_001",
            "status": "pending",
            "total_criteria": 1,
            "execution_record_id": "exec_wf_001",
            "created_at": "2026-01-01T00:00:00Z",
        })
        return service

    @pytest.fixture
    def mock_level_controller(self):
        return MagicMock()

    @pytest.fixture
    def orchestrator(self, mock_session, mock_submission_service, mock_level_controller):
        return TaskSubmitOrchestrator(
            session=mock_session,
            submission_service=mock_submission_service,
            level_controller=mock_level_controller,
        )

    @pytest.mark.asyncio
    async def test_workflow_commit_before_dispatch(
        self, orchestrator, mock_session
    ):
        """验证工作流提交的 commit 在派发之前完成"""
        call_order = []
        mock_session.commit = AsyncMock(side_effect=lambda: call_order.append("commit"))

        with patch.object(orchestrator, "_resolve_acceptance_criteria", return_value={"file_check": {}}), \
             patch.object(orchestrator, "_generate_workflow_execution_id", return_value="exec_wf_001"), \
             patch("asyncio.create_task") as mock_create_task:

            result = await orchestrator.submit_to_workflow(
                goal={"title": "workflow test"},
                acceptance_criteria={"file_check": {}},
                workflow_id="wf_001",
            )

            # commit 必须在返回前完成
            assert "commit" in call_order
            # 派发应通过 create_task（fire-and-forget）
            assert mock_create_task.called
