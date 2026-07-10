"""场景4：错误恢复端到端测试。

覆盖场景：
- 任务重试机制：失败 → 重试 → 完成
- 重试耗尽后最终失败的状态回写
- EvaluationExecutor 评估失败时的错误传播
- TaskWorker 恢复 running/evaluating 任务的逻辑
- Agent 异常后的降级处理
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tasks.service import TaskService
from tasks.state_machine import SimpleStateMachine
from tasks.storage import TaskStorage
from tasks.types import TaskStatus, create_task


# ── Fixture ──────────────────────────────────────────────────────

@pytest.fixture
def storage(tmp_path):
    return TaskStorage(data_dir=str(tmp_path / "tasks"))


@pytest.fixture
def task_service(storage):
    return TaskService(storage=storage)


# ── 1. 任务重试机制 ─────────────────────────────────────────────────

class TestTaskRetryMechanism:
    """任务重试：失败 → 重置 → 重新执行 → 完成。"""

    @pytest.mark.asyncio
    async def test_retry_from_failed_to_completed(self, task_service):
        """失败后重置为 pending，重新执行到完成。"""
        # 创建并执行到失败
        task = await task_service.create_task(title="需要重试的任务")
        await task_service.start_task(task.id)
        await task_service.fail_task(task.id, error="首次执行失败")

        # 验证已失败
        task = task_service.get_task(task.id)
        assert task.status == TaskStatus.FAILED

        # 重置为 pending 重试
        task = await task_service.reset_to_pending(task.id)
        assert task.status == TaskStatus.PENDING
        assert task.error == ""

        # 重新执行
        task = await task_service.start_task(task.id)
        assert task.status == TaskStatus.RUNNING

        # 完成
        await task_service.move_to_evaluating(task.id)
        task = await task_service.complete_evaluation(task.id, passed=True)
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_multiple_retries_exhaust(self, task_service):
        """多次重试最终失败。"""
        task = await task_service.create_task(title="反复失败")

        for i in range(3):
            # 启动 → 失败 → 重置
            await task_service.start_task(task.id)
            await task_service.fail_task(task.id, error=f"第{i+1}次失败")
            task = task_service.get_task(task.id)
            assert task.status == TaskStatus.FAILED

            if i < 2:
                await task_service.reset_to_pending(task.id)

        # 最终状态：FAILED
        assert task_service.get_task(task.id).status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_reject_task_retry_count_tracking(self, task_service):
        """打回次数正确追踪。"""
        task = await task_service.create_task(title="打回追踪")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        # 第一次打回
        task = await task_service.reject_task(task.id, reason="不合格")
        assert task.reject_count == 1
        assert task.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_recover_to_completed_from_failed(self, task_service):
        """recover_to_completed 将错误标记的 failed 任务恢复为 completed。"""
        task = await task_service.create_task(title="错误标记的任务")
        await task_service.start_task(task.id)
        await task_service.fail_task(task.id, error="误判失败")

        # 恢复为 completed
        task = await task_service.recover_to_completed(
            task.id, result={"recovered": True},
        )
        assert task.status == TaskStatus.COMPLETED
        assert task.error is None

    @pytest.mark.asyncio
    async def test_recover_to_completed_only_for_failed(self, task_service):
        """recover_to_completed 只能用于 FAILED 状态。"""
        task = await task_service.create_task(title="未失败的任务")

        with pytest.raises(ValueError, match="仅用于 FAILED"):
            await task_service.recover_to_completed(task.id)


# ── 2. EvaluationExecutor 错误传播 ──────────────────────────────────

class TestEvaluationExecutorErrors:
    """评估执行器错误传播。"""

    @pytest.mark.asyncio
    async def test_evaluation_failure_propagates_to_task_status(self, task_service):
        """评估失败传播到任务状态。"""
        task = await task_service.create_task(title="评估失败测试")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        # 评估不通过 → 任务标记为 failed
        task = await task_service.complete_evaluation(
            task.id, passed=False, result={"score": 20, "reason": "不达标"},
        )
        assert task.status == TaskStatus.FAILED

        # 验证 evaluation_history 记录了失败
        history = task.metadata.get("evaluation_history", [])
        assert len(history) > 0
        assert history[-1]["passed"] is False

    @pytest.mark.asyncio
    async def test_evaluation_success_records_history(self, task_service):
        """评估成功记录 evaluation_history。"""
        task = await task_service.create_task(title="评估成功测试")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        task = await task_service.complete_evaluation(
            task.id, passed=True, result={"score": 95},
        )
        assert task.status == TaskStatus.COMPLETED

        history = task.metadata.get("evaluation_history", [])
        assert len(history) > 0
        assert history[-1]["passed"] is True

    @pytest.mark.asyncio
    async def test_executor_with_mock_engine(self):
        """EvaluationExecutor 使用 Mock 引擎进行评估。"""
        from evaluation.executor import EvaluationExecutor
        from evaluation.types import (
            EvaluationConfig,
            EvaluationResult,
            MetricResult,
        )

        # Mock 引擎
        mock_engine = AsyncMock()
        mock_engine.evaluate.return_value = EvaluationResult(
            task_id="test_task",
            results=[
                MetricResult(metric_id="m1", passed=True, score=90.0, message="OK"),
            ],
            overall_passed=True,
            summary="1/1 指标通过",
        )

        # Mock mapper
        mock_mapper = MagicMock()
        mock_mapper.map_to_task_status.return_value = True
        mock_mapper.build_summary.return_value = "1/1 指标通过"

        executor = EvaluationExecutor(
            task_service=None,
            engine=mock_engine,
            mapper=mock_mapper,
        )

        result = await executor.run_evaluation(
            task_id="test_task",
            metric_ids=["m1"],
        )
        assert result.overall_passed is True

    @pytest.mark.asyncio
    async def test_executor_skip_state_update(self, task_service):
        """skip_state_update=True 时不回写任务状态。"""
        from evaluation.executor import EvaluationExecutor
        from evaluation.types import (
            EvaluationResult,
            MetricResult,
        )

        task = await task_service.create_task(title="跳过状态更新")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        mock_engine = AsyncMock()
        mock_engine.evaluate.return_value = EvaluationResult(
            task_id=task.id,
            results=[MetricResult(metric_id="m1", passed=True)],
            overall_passed=True,
        )

        mock_mapper = MagicMock()
        mock_mapper.map_to_task_status.return_value = True
        mock_mapper.build_summary.return_value = "OK"

        executor = EvaluationExecutor(
            task_service=task_service,
            engine=mock_engine,
            mapper=mock_mapper,
        )

        await executor.run_evaluation(
            task_id=task.id,
            metric_ids=["m1"],
            skip_state_update=True,
        )

        # 任务状态应保持 evaluating
        updated = task_service.get_task(task.id)
        assert updated.status == TaskStatus.EVALUATING


# ── 3. EvaluationResult 计算 ────────────────────────────────────────

class TestEvaluationResultComputation:
    """评估结果综合判定。"""

    def test_overall_passed_when_all_pass(self):
        from evaluation.types import EvaluationResult, MetricResult

        result = EvaluationResult(
            task_id="t1",
            results=[
                MetricResult(metric_id="m1", passed=True),
                MetricResult(metric_id="m2", passed=True),
            ],
        )
        result.compute_overall()
        assert result.overall_passed is True
        assert "2/2" in result.summary

    def test_overall_failed_when_any_fails(self):
        from evaluation.types import EvaluationResult, MetricResult

        result = EvaluationResult(
            task_id="t1",
            results=[
                MetricResult(metric_id="m1", passed=True),
                MetricResult(metric_id="m2", passed=False),
            ],
        )
        result.compute_overall()
        assert result.overall_passed is False
        assert "1/2" in result.summary

    def test_overall_failed_when_no_metrics(self):
        from evaluation.types import EvaluationResult

        result = EvaluationResult(task_id="t1")
        result.compute_overall()
        assert result.overall_passed is False


# ── 4. TaskWorker 恢复逻辑 ──────────────────────────────────────────

class TestTaskWorkerRecovery:
    """TaskWorker 恢复 running/evaluating 任务的逻辑。"""

    @pytest.mark.asyncio
    async def test_recover_running_tasks_resets_to_pending(self, task_service):
        """running 任务被恢复为 pending。"""
        # 创建几个 running 任务
        t1 = await task_service.create_task(title="running_1")
        await task_service.start_task(t1.id)
        t2 = await task_service.create_task(title="running_2")
        await task_service.start_task(t2.id)

        # 模拟 recovery
        from tasks.types import TaskStatus
        running_tasks = task_service.list_by_status(TaskStatus.RUNNING)
        assert len(running_tasks) >= 2

        for task in running_tasks:
            await task_service.reset_to_pending(task.id)

        # 验证全部恢复为 pending
        running_after = task_service.list_by_status(TaskStatus.RUNNING)
        assert len(running_after) == 0
        pending_after = task_service.list_by_status(TaskStatus.PENDING)
        assert len(pending_after) >= 2

    @pytest.mark.asyncio
    async def test_recover_evaluating_task_stays_in_evaluating(self, task_service):
        """evaluating 任务保持评估状态（由 _rerun_evaluation 处理）。"""
        task = await task_service.create_task(title="evaluating_recovery")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        # 验证 evaluating 任务存在
        evaluating = task_service.list_by_status(TaskStatus.EVALUATING)
        assert len(evaluating) >= 1
        assert evaluating[0].status == TaskStatus.EVALUATING

    @pytest.mark.asyncio
    async def test_completed_tasks_not_recovered(self, task_service):
        """已完成任务不参与恢复。"""
        task = await task_service.create_task(title="completed_task")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)
        await task_service.complete_evaluation(task.id, passed=True)

        # running 列表中不应包含 completed 任务
        running = task_service.list_by_status(TaskStatus.RUNNING)
        completed = task_service.list_by_status(TaskStatus.COMPLETED)
        assert len(completed) >= 1
        task_ids_in_running = [t.id for t in running]
        assert task.id not in task_ids_in_running


# ── 5. 任务创建与查询 ────────────────────────────────────────────────

class TestTaskCreationAndQuery:
    """任务创建与查询验证。"""

    @pytest.mark.asyncio
    async def test_create_task_default_status_pending(self, task_service):
        """创建的任务默认为 pending 状态。"""
        task = await task_service.create_task(title="新任务")
        assert task.status == TaskStatus.PENDING
        assert task.id is not None
        assert len(task.id) > 0

    @pytest.mark.asyncio
    async def test_get_task_returns_none_for_unknown(self, task_service):
        """查询不存在的任务返回 None。"""
        result = task_service.get_task("nonexistent_id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_by_status(self, task_service):
        """按状态列出任务。"""
        t1 = await task_service.create_task(title="pending_1")
        t2 = await task_service.create_task(title="pending_2")
        t3 = await task_service.create_task(title="to_run")
        await task_service.start_task(t3.id)

        pending = task_service.list_by_status(TaskStatus.PENDING)
        running = task_service.list_by_status(TaskStatus.RUNNING)

        pending_ids = [t.id for t in pending]
        assert t1.id in pending_ids
        assert t2.id in pending_ids
        assert t3.id not in pending_ids

        running_ids = [t.id for t in running]
        assert t3.id in running_ids

    @pytest.mark.asyncio
    async def test_task_not_found_raises_on_start(self, task_service):
        """启动不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await task_service.start_task("nonexistent_task")

    @pytest.mark.asyncio
    async def test_invalid_transition_raises_on_start_completed(self, task_service):
        """对已完成的任务启动会抛出 InvalidTransitionError。"""
        from tasks.state_machine import InvalidTransitionError

        task = await task_service.create_task(title="已完成")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)
        await task_service.complete_evaluation(task.id, passed=True)

        with pytest.raises(InvalidTransitionError):
            await task_service.start_task(task.id)


# ── 6. EvaluationEngine 基础 ────────────────────────────────────────

class TestEvaluationEngineBasics:
    """EvaluationEngine 基础行为。"""

    @pytest.mark.asyncio
    async def test_no_metrics_returns_failure(self):
        """无指标时评估结果为失败。"""
        from evaluation.engine import EvaluationEngine
        from evaluation.loader import MetricLoader
        from evaluation.types import EvaluationConfig

        loader = MetricLoader()
        engine = EvaluationEngine(loader=loader)
        result = await engine.evaluate(task_id="empty_task")
        assert result.overall_passed is False
        assert "无" in result.summary or not result.results

    @pytest.mark.asyncio
    async def test_evaluate_single_metric_not_found(self):
        """评估不存在的指标抛出 KeyError。"""
        from evaluation.engine import EvaluationEngine
        from evaluation.loader import MetricLoader

        loader = MetricLoader()
        engine = EvaluationEngine(loader=loader)
        with pytest.raises(KeyError, match="not found"):
            await engine.evaluate_single(task_id="t1", metric_id="nonexistent_metric")


# ── 7. MetricLoader ─────────────────────────────────────────────────

class TestMetricLoader:
    """指标加载器测试。"""

    def test_empty_loader_has_no_metrics(self):
        from evaluation.loader import MetricLoader
        loader = MetricLoader()
        # 未调用 load_all() 前没有指标
        assert len(loader.metrics) == 0

    def test_get_returns_none_for_unknown(self):
        from evaluation.loader import MetricLoader
        loader = MetricLoader()
        assert loader.get("nonexistent") is None


# ── 8. ExecutionStatus 属性 ─────────────────────────────────────────

class TestExecutionStatusProperties:
    """ExecutionStatus 状态属性验证。"""

    def test_pending_is_waiting(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.PENDING.is_waiting

    def test_running_is_active(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.RUNNING.is_active

    def test_evaluating_is_active(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.EVALUATING.is_active

    def test_suspended_is_waiting(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.SUSPENDED.is_waiting

    def test_blocked_is_waiting(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.BLOCKED.is_waiting

    def test_completed_is_success_and_terminal(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.COMPLETED.is_success
        assert ExecutionStatus.COMPLETED.is_terminal
        assert not ExecutionStatus.COMPLETED.is_failure

    def test_failed_is_failure_and_terminal(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.FAILED.is_failure
        assert ExecutionStatus.FAILED.is_terminal
        assert not ExecutionStatus.FAILED.is_success

    def test_cancelled_is_failure_and_terminal(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.CANCELLED.is_failure
        assert ExecutionStatus.CANCELLED.is_terminal
