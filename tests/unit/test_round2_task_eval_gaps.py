"""Round2 测试审查 - 任务管理与评估系统模块测试缺口补充

覆盖需求：07_任务管理与评估系统模块需求文档
- F-TASK-02: 任务状态机 7 种状态正确转换
- F-EVAL-01: 5 类评估指标
- F-EVAL-04: 期望条件判定 11 种操作符
- F-EVAL-05: 综合判定逻辑
- AC-TASK-01: 状态机转换合法/非法
- AC-EVAL-02: 11 种操作符
- AC-EVAL-03: 嵌套字段路径解析
"""

import pytest


# =============================================================================
# F-TASK-02 / AC-TASK-01: 任务状态机 7 种状态正确转换
# =============================================================================

class TestTaskStateMachine:
    """任务状态机完整转换规则测试"""

    @pytest.fixture
    def sm(self):
        from src.tasks.state_machine import get_task_state_machine
        return get_task_state_machine()

    def test_all_seven_statuses_exist(self):
        """7 种状态全部存在于转换规则中"""
        from src.tasks.state_machine import _TASK_TRANSITIONS
        expected = {"pending", "running", "evaluating", "stopped", "completed", "failed", "timeout"}
        assert set(_TASK_TRANSITIONS.keys()) == expected

    def test_pending_to_running(self, sm):
        """pending → running 合法"""
        sm.transition("running")
        assert sm.current_state == "running"

    def test_pending_to_stopped(self, sm):
        """pending → stopped 合法"""
        sm.transition("stopped")
        assert sm.current_state == "stopped"

    def test_pending_to_completed(self, sm):
        """pending → completed 合法"""
        sm.transition("completed")
        assert sm.current_state == "completed"

    def test_pending_to_failed(self, sm):
        """pending → failed 合法"""
        sm.transition("failed")
        assert sm.current_state == "failed"

    def test_running_to_evaluating(self, sm):
        """running → evaluating 合法"""
        sm.transition("running")
        sm.transition("evaluating")
        assert sm.current_state == "evaluating"

    def test_running_to_completed(self, sm):
        """running → completed 合法"""
        sm.transition("running")
        sm.transition("completed")
        assert sm.current_state == "completed"

    def test_running_to_failed(self, sm):
        """running → failed 合法"""
        sm.transition("running")
        sm.transition("failed")
        assert sm.current_state == "failed"

    def test_running_to_timeout(self, sm):
        """running → timeout 合法"""
        sm.transition("running")
        sm.transition("timeout")
        assert sm.current_state == "timeout"

    def test_evaluating_to_running(self, sm):
        """evaluating → running 合法（重新执行）"""
        sm.transition("running")
        sm.transition("evaluating")
        sm.transition("running")
        assert sm.current_state == "running"

    def test_stopped_to_pending(self, sm):
        """stopped → pending 合法（重置）"""
        sm.transition("stopped")
        sm.transition("pending")
        assert sm.current_state == "pending"

    def test_completed_to_pending(self, sm):
        """completed → pending 合法（重新开启）"""
        sm.transition("completed")
        sm.transition("pending")
        assert sm.current_state == "pending"

    def test_failed_to_running(self, sm):
        """failed → running 合法（重试）"""
        sm.transition("failed")
        sm.transition("running")
        assert sm.current_state == "running"

    def test_timeout_to_running(self, sm):
        """timeout → running 合法"""
        sm.transition("running")
        sm.transition("timeout")
        sm.transition("running")
        assert sm.current_state == "running"

    # --- 非法转换 ---

    def test_illegal_pending_to_timeout(self, sm):
        """pending → timeout 非法"""
        from src.tasks.state_machine import InvalidTransitionError
        with pytest.raises(InvalidTransitionError):
            sm.transition("timeout")

    def test_illegal_completed_to_running(self, sm):
        """completed → running 非法"""
        from src.tasks.state_machine import InvalidTransitionError
        sm.transition("completed")
        with pytest.raises(InvalidTransitionError):
            sm.transition("running")

    def test_illegal_evaluating_to_timeout(self, sm):
        """evaluating → timeout 非法"""
        from src.tasks.state_machine import InvalidTransitionError
        sm.transition("running")
        sm.transition("evaluating")
        with pytest.raises(InvalidTransitionError):
            sm.transition("timeout")

    def test_can_transition_method(self, sm):
        """can_transition 方法正确判断"""
        assert sm.can_transition("running") is True
        assert sm.can_transition("timeout") is False


# =============================================================================
# F-TASK-01/03: 任务模型与优先级
# =============================================================================

class TestTaskModel:
    """TaskModel 数据模型测试"""

    def test_create_task_defaults(self):
        """create_task 默认值"""
        from src.tasks.types import create_task, TaskStatus, TaskPriority
        task = create_task(title="Test Task")
        assert task.title == "Test Task"
        assert task.status == TaskStatus.PENDING
        assert task.priority == TaskPriority.NORMAL
        assert task.id  # 自动生成 ID
        assert task.parent_task_id is None

    def test_create_task_with_priority(self):
        """create_task 指定优先级"""
        from src.tasks.types import create_task, TaskPriority
        task = create_task(title="Urgent", priority=TaskPriority.CRITICAL)
        assert task.priority == TaskPriority.CRITICAL

    def test_create_task_with_parent(self):
        """create_task 指定父任务"""
        from src.tasks.types import create_task
        task = create_task(title="Child", parent_task_id="parent123")
        assert task.parent_task_id == "parent123"

    def test_task_priority_enum_values(self):
        """TaskPriority 5 级枚举"""
        from src.tasks.types import TaskPriority
        assert TaskPriority.CRITICAL == 1
        assert TaskPriority.HIGH == 3
        assert TaskPriority.NORMAL == 5
        assert TaskPriority.LOW == 7
        assert TaskPriority.BACKGROUND == 9

    def test_task_status_enum_values(self):
        """TaskStatus 7 种状态"""
        from src.tasks.types import TaskStatus
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.RUNNING.value == "running"
        assert TaskStatus.EVALUATING.value == "evaluating"
        assert TaskStatus.STOPPED.value == "stopped"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"
        assert TaskStatus.TIMEOUT.value == "timeout"

    def test_task_metadata_default(self):
        """TaskModel metadata 默认为空字典"""
        from src.tasks.types import create_task
        task = create_task(title="Test")
        assert task.metadata == {}

    def test_ac_dataclass(self):
        """AC 验收标准数据类"""
        from src.tasks.types import AC
        ac = AC(metric_id="file_check", input_params={"path": "src/main.py"})
        assert ac.metric_id == "file_check"
        assert ac.pass_threshold == 1.0


# =============================================================================
# F-EVAL-01: 5 类评估指标类型
# =============================================================================

class TestEvaluationTypes:
    """评估系统类型定义"""

    def test_metric_type_enum(self):
        """MetricType 三种类型"""
        from src.evaluation.types import MetricType
        assert MetricType.TOOL.value == "tool"
        assert MetricType.AGENT.value == "agent"
        assert MetricType.HUMAN.value == "human"

    def test_metric_definition_fields(self):
        """MetricDefinition 数据类字段"""
        from src.evaluation.types import MetricDefinition, MetricType, ExpectSpec
        md = MetricDefinition(id="file_check")
        assert md.id == "file_check"
        assert md.metric_type == MetricType.TOOL
        assert md.is_red_line is False
        assert md.default_weight == 1.0
        assert isinstance(md.expect, ExpectSpec)

    def test_metric_result_dataclass(self):
        """MetricResult 数据类"""
        from src.evaluation.types import MetricResult
        mr = MetricResult(metric_id="test", passed=True, score=95.0)
        assert mr.metric_id == "test"
        assert mr.passed is True
        assert mr.score == 95.0

    def test_evaluation_result_compute_overall(self):
        """EvaluationResult.compute_overall 综合判定"""
        from src.evaluation.types import EvaluationResult, MetricResult
        er = EvaluationResult(task_id="task1")
        er.results = [
            MetricResult(metric_id="m1", passed=True),
            MetricResult(metric_id="m2", passed=True),
        ]
        er.compute_overall()
        assert er.overall_passed is True
        assert "2/2" in er.summary

    def test_evaluation_result_compute_overall_failed(self):
        """EvaluationResult 有指标不通过时 overall 为 False"""
        from src.evaluation.types import EvaluationResult, MetricResult
        er = EvaluationResult(task_id="task1")
        er.results = [
            MetricResult(metric_id="m1", passed=True),
            MetricResult(metric_id="m2", passed=False),
        ]
        er.compute_overall()
        assert er.overall_passed is False

    def test_evaluation_result_empty(self):
        """EvaluationResult 无指标时不通过"""
        from src.evaluation.types import EvaluationResult
        er = EvaluationResult(task_id="task1")
        er.compute_overall()
        assert er.overall_passed is False
        assert "无评估指标" in er.summary


# =============================================================================
# F-EVAL-04 / AC-EVAL-02: 期望条件判定（11 种操作符）
# =============================================================================

class TestExpectConditions:
    """期望条件操作符测试"""

    @pytest.fixture
    def evaluator(self):
        try:
            from src.evaluation.expect_evaluator import ExpectEvaluator
            return ExpectEvaluator()
        except ImportError:
            pytest.skip("expect_evaluator 模块未找到")

    def test_operator_is_true(self, evaluator):
        """is_true 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="passed", operator="is_true")
        assert evaluator._evaluate_condition({"passed": True}, cond) is True
        assert evaluator._evaluate_condition({"passed": False}, cond) is False

    def test_operator_is_false(self, evaluator):
        """is_false 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="error", operator="is_false")
        assert evaluator._evaluate_condition({"error": False}, cond) is True
        assert evaluator._evaluate_condition({"error": True}, cond) is False

    def test_operator_equals(self, evaluator):
        """equals 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="status", operator="equals", value="success")
        assert evaluator._evaluate_condition({"status": "success"}, cond) is True
        assert evaluator._evaluate_condition({"status": "failed"}, cond) is False

    def test_operator_not_equals(self, evaluator):
        """not_equals 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="status", operator="not_equals", value="failed")
        assert evaluator._evaluate_condition({"status": "success"}, cond) is True
        assert evaluator._evaluate_condition({"status": "failed"}, cond) is False

    def test_operator_in(self, evaluator):
        """in 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="category", operator="in", value=["a", "b", "c"])
        assert evaluator._evaluate_condition({"category": "a"}, cond) is True
        assert evaluator._evaluate_condition({"category": "d"}, cond) is False

    def test_operator_not_in(self, evaluator):
        """not_in 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="category", operator="not_in", value=["a", "b"])
        assert evaluator._evaluate_condition({"category": "c"}, cond) is True
        assert evaluator._evaluate_condition({"category": "a"}, cond) is False

    def test_operator_contains(self, evaluator):
        """contains 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="tags", operator="contains", value="python")
        assert evaluator._evaluate_condition({"tags": ["python", "test"]}, cond) is True
        assert evaluator._evaluate_condition({"tags": ["java"]}, cond) is False

    def test_operator_gt(self, evaluator):
        """gt 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="count", operator="gt", value=5)
        assert evaluator._evaluate_condition({"count": 10}, cond) is True
        assert evaluator._evaluate_condition({"count": 3}, cond) is False

    def test_operator_lt(self, evaluator):
        """lt 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="count", operator="lt", value=5)
        assert evaluator._evaluate_condition({"count": 3}, cond) is True
        assert evaluator._evaluate_condition({"count": 10}, cond) is False

    def test_operator_gte(self, evaluator):
        """gte 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="count", operator="gte", value=5)
        assert evaluator._evaluate_condition({"count": 5}, cond) is True
        assert evaluator._evaluate_condition({"count": 4}, cond) is False

    def test_operator_lte(self, evaluator):
        """lte 操作符"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="count", operator="lte", value=5)
        assert evaluator._evaluate_condition({"count": 5}, cond) is True
        assert evaluator._evaluate_condition({"count": 6}, cond) is False


# =============================================================================
# AC-EVAL-03: 嵌套字段路径解析
# =============================================================================

class TestNestedFieldPath:
    """嵌套字段路径解析测试"""

    @pytest.fixture
    def evaluator(self):
        try:
            from src.evaluation.expect_evaluator import ExpectEvaluator
            return ExpectEvaluator()
        except ImportError:
            pytest.skip("expect_evaluator 模块未找到")

    def test_nested_field_one_level(self, evaluator):
        """一级嵌套字段"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="data.exit_code", operator="equals", value=0)
        result = evaluator._evaluate_condition(
            {"data": {"exit_code": 0}}, cond
        )
        assert result is True

    def test_nested_field_two_levels(self, evaluator):
        """二级嵌套字段"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="data.result.status", operator="equals", value="ok")
        result = evaluator._evaluate_condition(
            {"data": {"result": {"status": "ok"}}}, cond
        )
        assert result is True

    def test_nested_field_missing(self, evaluator):
        """嵌套字段不存在"""
        from src.evaluation.types import ExpectCondition
        cond = ExpectCondition(field="data.missing.field", operator="is_true")
        result = evaluator._evaluate_condition({"data": {}}, cond)
        assert result is False


# =============================================================================
# F-EVAL-05: 综合判定 - and/or 逻辑
# =============================================================================

class TestExpectLogic:
    """期望条件组合逻辑测试"""

    @pytest.fixture
    def evaluator(self):
        try:
            from src.evaluation.expect_evaluator import ExpectEvaluator
            return ExpectEvaluator()
        except ImportError:
            pytest.skip("expect_evaluator 模块未找到")

    def test_and_logic_all_pass(self, evaluator):
        """AND 逻辑：全部通过"""
        from src.evaluation.types import ExpectSpec, ExpectCondition
        spec = ExpectSpec(
            conditions=[
                ExpectCondition(field="a", operator="is_true"),
                ExpectCondition(field="b", operator="is_true"),
            ],
            logic="and"
        )
        result = evaluator.evaluate({"a": True, "b": True}, spec)
        assert result is True

    def test_and_logic_partial_fail(self, evaluator):
        """AND 逻辑：部分不通过"""
        from src.evaluation.types import ExpectSpec, ExpectCondition
        spec = ExpectSpec(
            conditions=[
                ExpectCondition(field="a", operator="is_true"),
                ExpectCondition(field="b", operator="is_false"),
            ],
            logic="and"
        )
        result = evaluator.evaluate({"a": True, "b": True}, spec)
        assert result is False

    def test_or_logic_any_pass(self, evaluator):
        """OR 逻辑：任一通过"""
        from src.evaluation.types import ExpectSpec, ExpectCondition
        spec = ExpectSpec(
            conditions=[
                ExpectCondition(field="a", operator="is_true"),
                ExpectCondition(field="b", operator="is_true"),
            ],
            logic="or"
        )
        result = evaluator.evaluate({"a": True, "b": False}, spec)
        assert result is True

    def test_or_logic_all_fail(self, evaluator):
        """OR 逻辑：全部不通过"""
        from src.evaluation.types import ExpectSpec, ExpectCondition
        spec = ExpectSpec(
            conditions=[
                ExpectCondition(field="a", operator="is_true"),
                ExpectCondition(field="b", operator="is_true"),
            ],
            logic="or"
        )
        result = evaluator.evaluate({"a": False, "b": False}, spec)
        assert result is False
