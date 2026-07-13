"""评估系统单元测试。

覆盖范围：
- MetricLoader: YAML 指标文件加载和解析
- ExpectEvaluator: 期望条件判定
- EvaluationEngine: 评估引擎分发逻辑
- ResultMapper: 结果映射
- EvaluationExecutor: 执行器编排 + TaskService 集成
- EvaluationResult: 综合判定计算
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from evaluation.engine import EvaluationEngine
from evaluation.expect import ExpectEvaluator
from evaluation.executor import EvaluationExecutor
from evaluation.loader import MetricLoader
from evaluation.mapper import ResultMapper
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)


# ── Fixture: 测试用 YAML 指标 ──────────────────────────────


def _write_yaml(directory: Path, filename: str, data: dict) -> Path:
    """写入测试 YAML 文件。"""
    path = directory / filename
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True)
    return path


@pytest.fixture
def metrics_dir(tmp_path: Path) -> Path:
    """创建包含测试指标的临时目录。"""
    # tool 类型指标
    _write_yaml(tmp_path, "bash_check.yaml", {
        "id": "bash_check",
        "name": "命令检查",
        "description": "执行自定义命令",
        "evaluator_id": "bash_execute",
        "evaluator_type": "tool",
        "default_config": {"action": "execute", "use_isolation": True},
        "expect": {
            "conditions": [
                {"field": "success", "operator": "is_true"},
                {"field": "data.exit_code", "operator": "equals", "value": 0},
            ],
            "logic": "and",
            "pass_message": "命令执行成功",
            "fail_message": "命令执行失败",
        },
        "is_red_line": False,
        "default_weight": 1.0,
        "level": 2,
        "tags": ["bash", "command"],
    })

    # agent 类型指标
    _write_yaml(tmp_path, "semantic_check.yaml", {
        "id": "semantic_check",
        "name": "质量评估",
        "description": "对内容进行质量评估",
        "evaluator_id": "evaluator_agent",
        "evaluator_type": "agent",
        "default_config": {"check": "semantic"},
        "expect": {
            "conditions": [
                {"field": "passed", "operator": "is_true"},
            ],
            "logic": "and",
            "pass_message": "质量评估通过",
            "fail_message": "质量评估未通过",
        },
        "is_red_line": False,
        "default_weight": 1.0,
        "level": 3,
        "tags": ["semantic", "quality"],
    })

    # human 类型指标
    _write_yaml(tmp_path, "human_review.yaml", {
        "id": "human_review",
        "name": "人工审核",
        "description": "人工审核",
        "evaluator_id": "human_interaction",
        "evaluator_type": "human",
        "default_config": {"mode": "choice"},
        "expect": {
            "conditions": [
                {"field": "passed", "operator": "is_true"},
            ],
            "logic": "and",
            "pass_message": "审核通过",
            "fail_message": "审核未通过",
        },
        "is_red_line": False,
        "default_weight": 1.0,
        "level": 4,
        "tags": ["human", "approval"],
    })

    return tmp_path


@pytest.fixture
def loader(metrics_dir: Path) -> MetricLoader:
    """创建并加载测试指标的 MetricLoader。"""
    ldr = MetricLoader(metrics_dir=metrics_dir)
    ldr.load_all()
    return ldr


# ── MetricLoader 测试 ────────────────────────────────────


class TestMetricLoader:
    """指标加载器测试。"""

    def test_load_all_metrics(self, loader: MetricLoader) -> None:
        """加载目录下所有指标文件。"""
        assert len(loader.metrics) == 3
        assert "bash_check" in loader.metrics
        assert "semantic_check" in loader.metrics
        assert "human_review" in loader.metrics

    def test_metric_type_parsed_correctly(self, loader: MetricLoader) -> None:
        """evaluator_type 正确映射到 MetricType。"""
        assert loader.metrics["bash_check"].metric_type == MetricType.TOOL
        assert loader.metrics["semantic_check"].metric_type == MetricType.AGENT
        assert loader.metrics["human_review"].metric_type == MetricType.HUMAN

    def test_expect_conditions_parsed(self, loader: MetricLoader) -> None:
        """expect 条件正确解析。"""
        bash = loader.metrics["bash_check"]
        assert len(bash.expect.conditions) == 2
        assert bash.expect.conditions[0].field == "success"
        assert bash.expect.conditions[0].operator == "is_true"
        assert bash.expect.conditions[1].field == "data.exit_code"
        assert bash.expect.conditions[1].operator == "equals"
        assert bash.expect.conditions[1].value == 0

    def test_load_nonexistent_dir(self, tmp_path: Path) -> None:
        """不存在的目录抛出 FileNotFoundError。"""
        ldr = MetricLoader(metrics_dir=tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError):
            ldr.load_all()

    def test_get_metric(self, loader: MetricLoader) -> None:
        """get 方法返回已加载的指标。"""
        m = loader.get("bash_check")
        assert m is not None
        assert m.id == "bash_check"

    def test_get_nonexistent_metric(self, loader: MetricLoader) -> None:
        """get 不存在的指标返回 None。"""
        assert loader.get("nonexistent") is None

    def test_list_metrics(self, loader: MetricLoader) -> None:
        """list_metrics 返回所有已加载指标 ID。"""
        ids = loader.list_metrics()
        assert set(ids) == {"bash_check", "semantic_check", "human_review"}

    def test_load_one(self, metrics_dir: Path) -> None:
        """单独加载一个指标文件。"""
        ldr = MetricLoader(metrics_dir=metrics_dir)
        m = ldr.load_one("bash_check")
        assert m is not None
        assert m.id == "bash_check"
        assert len(ldr.metrics) == 1


# ── ExpectEvaluator 测试 ─────────────────────────────────


class TestExpectEvaluator:
    """期望值评估器测试。"""

    def test_tool_output_pass(self) -> None:
        """工具输出满足期望条件时通过。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="data.exit_code", operator="equals", value=0),
            ],
            logic="and",
            pass_message="通过",
            fail_message="失败",
        )
        result = evaluator.evaluate(
            metric_id="bash_check",
            expect=expect,
            output={"success": True, "data": {"exit_code": 0}},
        )
        assert result.passed is True
        assert result.message == "通过"

    def test_tool_output_fail(self) -> None:
        """工具输出不满足期望条件时失败。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="data.exit_code", operator="equals", value=0),
            ],
            logic="and",
        )
        result = evaluator.evaluate(
            metric_id="bash_check",
            expect=expect,
            output={"success": True, "data": {"exit_code": 1}},
        )
        assert result.passed is False

    def test_or_logic(self) -> None:
        """or 逻辑：任一条件满足即通过。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="a", operator="is_true"),
                ExpectCondition(field="b", operator="is_true"),
            ],
            logic="or",
        )
        result = evaluator.evaluate(
            metric_id="test",
            expect=expect,
            output={"a": False, "b": True},
        )
        assert result.passed is True

    def test_no_conditions_pass(self) -> None:
        """无条件定义时默认通过。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(conditions=[])
        result = evaluator.evaluate(
            metric_id="test", expect=expect, output={},
        )
        assert result.passed is True

    def test_nested_field_resolution(self) -> None:
        """嵌套字段路径正确解析。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="data.status", operator="equals", value="completed"),
            ],
        )
        result = evaluator.evaluate(
            metric_id="test",
            expect=expect,
            output={"data": {"status": "completed"}},
        )
        assert result.passed is True

    def test_missing_field_fails(self) -> None:
        """字段不存在时条件失败。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="nonexistent", operator="is_true"),
            ],
        )
        result = evaluator.evaluate(
            metric_id="test", expect=expect, output={},
        )
        assert result.passed is False

    def test_in_operator(self) -> None:
        """in 操作符正确工作。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="status", operator="in", value=[200, 201, 204]),
            ],
        )
        result = evaluator.evaluate(
            metric_id="test",
            expect=expect,
            output={"status": 200},
        )
        assert result.passed is True

    def test_contains_operator(self) -> None:
        """contains 操作符对字符串正确工作。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="output", operator="contains", value="passed"),
            ],
        )
        result = evaluator.evaluate(
            metric_id="test",
            expect=expect,
            output={"output": "2 tests passed"},
        )
        assert result.passed is True


# ── EvaluationEngine 测试 ─────────────────────────────────


class TestEvaluationEngine:
    """评估引擎测试。

    单指标测试通过 register_evaluator 注入 Mock 评估器（与 test_custom_evaluator
    同模式），避免依赖真实 bash_execute 工具 / agent_registry / human_interaction——
    这些在生产环境由对应注册表提供，单测不应触发真实链路。
    """

    async def test_evaluate_single_tool_metric(self, loader: MetricLoader) -> None:
        """评估单个 tool 类型指标。"""
        engine = EvaluationEngine(loader=loader)

        async def mock_tool(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            # bash_check 的 expect: success=True 且 data.exit_code==0
            return {"success": True, "data": {"exit_code": 0}}

        engine.register_evaluator(MetricType.TOOL, mock_tool)
        result = await engine.evaluate_single(task_id="task_test", metric_id="bash_check")
        assert result.metric_id == "bash_check"
        assert result.passed is True

    async def test_evaluate_single_agent_metric(self, loader: MetricLoader) -> None:
        """评估单个 agent 类型指标。"""
        engine = EvaluationEngine(loader=loader)

        async def mock_agent(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            # semantic_check 的 expect: passed is_true
            return {"passed": True, "score": 95}

        engine.register_evaluator(MetricType.AGENT, mock_agent)
        result = await engine.evaluate_single(task_id="task_test", metric_id="semantic_check")
        assert result.metric_id == "semantic_check"
        assert result.passed is True

    async def test_evaluate_single_human_metric(self, loader: MetricLoader) -> None:
        """评估单个 human 类型指标。"""
        engine = EvaluationEngine(loader=loader)

        async def mock_human(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            # human_review 的 expect: passed is_true
            return {"passed": True}

        engine.register_evaluator(MetricType.HUMAN, mock_human)
        result = await engine.evaluate_single(task_id="task_test", metric_id="human_review")
        assert result.metric_id == "human_review"
        assert result.passed is True

    async def test_evaluate_nonexistent_metric(self, loader: MetricLoader) -> None:
        """评估不存在的指标抛出 KeyError。"""
        engine = EvaluationEngine(loader=loader)
        with pytest.raises(KeyError):
            await engine.evaluate_single(task_id="task_test", metric_id="nonexistent")

    async def test_evaluate_multiple_metrics(self, loader: MetricLoader) -> None:
        """评估多个指标。"""
        engine = EvaluationEngine(loader=loader)

        # 注入 Mock 评估器，避免触发真实工具/agent 链路（见类 docstring）
        async def mock_tool(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            return {"success": True, "data": {"exit_code": 0}}

        async def mock_agent(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            return {"passed": True}

        engine.register_evaluator(MetricType.TOOL, mock_tool)
        engine.register_evaluator(MetricType.AGENT, mock_agent)

        config = EvaluationConfig(
            metric_ids=["bash_check", "semantic_check"],
        )
        result = await engine.evaluate(task_id="task1", config=config)
        assert len(result.results) == 2
        assert result.overall_passed is True

    async def test_evaluate_fail_fast(self, loader: MetricLoader) -> None:
        """fail_fast 配置生效。"""
        engine = EvaluationEngine(loader=loader)

        # 注册一个返回失败的评估器
        async def fail_evaluator(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            return {"success": False, "data": {"exit_code": 1}}

        engine.register_evaluator(MetricType.TOOL, fail_evaluator)

        config = EvaluationConfig(
            metric_ids=["bash_check", "semantic_check"],
            fail_fast=True,
        )
        result = await engine.evaluate(task_id="task1", config=config)
        # bash_check 失败后应停止，不评估 semantic_check
        assert len(result.results) == 1
        assert result.overall_passed is False

    async def test_custom_evaluator(self, loader: MetricLoader) -> None:
        """自定义评估器函数生效。"""
        engine = EvaluationEngine(loader=loader)

        custom_called = False

        async def custom_eval(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            nonlocal custom_called
            custom_called = True
            return {"success": True, "data": {"exit_code": 0}}

        engine.register_evaluator(MetricType.TOOL, custom_eval)
        await engine.evaluate_single(task_id="task_test", metric_id="bash_check")
        assert custom_called is True

    async def test_evaluate_all_when_no_metric_ids(self, loader: MetricLoader) -> None:
        """metric_ids 为空时评估所有已加载指标。"""
        engine = EvaluationEngine(loader=loader)

        # 注入 Mock 评估器，避免触发真实工具/agent 链路（见类 docstring）
        async def mock_tool(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            return {"success": True, "data": {"exit_code": 0}}

        async def mock_agent(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            return {"passed": True}

        async def mock_human(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            return {"passed": True}

        engine.register_evaluator(MetricType.TOOL, mock_tool)
        engine.register_evaluator(MetricType.AGENT, mock_agent)
        engine.register_evaluator(MetricType.HUMAN, mock_human)

        config = EvaluationConfig(metric_ids=[])
        result = await engine.evaluate(task_id="task1", config=config)
        assert len(result.results) == 3  # bash_check + semantic_check + human_review


# ── ResultMapper 测试 ─────────────────────────────────────


class TestResultMapper:
    """结果映射器测试。"""

    def test_all_pass_maps_to_true(self) -> None:
        """所有指标通过映射为 True。"""
        mapper = ResultMapper()
        result = EvaluationResult(
            task_id="t1",
            results=[
                MetricResult(metric_id="m1", passed=True),
                MetricResult(metric_id="m2", passed=True),
            ],
        )
        assert mapper.map_to_task_status(result) is True

    def test_any_fail_maps_to_false(self) -> None:
        """任一指标失败映射为 False。"""
        mapper = ResultMapper()
        result = EvaluationResult(
            task_id="t1",
            results=[
                MetricResult(metric_id="m1", passed=True),
                MetricResult(metric_id="m2", passed=False),
            ],
        )
        assert mapper.map_to_task_status(result) is False

    def test_build_summary(self) -> None:
        """摘要构建正确。"""
        mapper = ResultMapper()
        result = EvaluationResult(
            task_id="t1",
            results=[
                MetricResult(metric_id="m1", passed=True, message="OK"),
                MetricResult(metric_id="m2", passed=False, message="FAIL"),
            ],
        )
        summary = mapper.build_summary(result)
        assert "1/2" in summary
        assert "PASS" in summary
        assert "FAIL" in summary

    def test_map_single_result(self) -> None:
        """单个结果映射正确。"""
        mapper = ResultMapper()
        r = MetricResult(metric_id="m1", passed=True, message="OK")
        mapped = mapper.map_single_result(r, is_red_line=True)
        assert mapped["passed"] is True
        assert mapped["is_red_line"] is True
        assert mapped["metric_id"] == "m1"


# ── EvaluationExecutor 测试 ───────────────────────────────


class TestEvaluationExecutor:
    """评估执行器测试。

    注入带 Mock 评估器的 EvaluationEngine，避免触发真实工具/agent 链路
    （见 TestEvaluationEngine docstring）。complete_evaluation 真实签名为
    complete_evaluation(task_id, overall_passed, result=eval_data)，故断言
    检查前两个位置参数，result 用调用记录校验存在性。
    """

    @staticmethod
    def _make_mock_engine(loader: MetricLoader, *, passed: bool = True) -> EvaluationEngine:
        """构造注入 Mock tool 评估器的引擎（bash_check 的 expect 要求 exit_code==0）。"""
        engine = EvaluationEngine(loader=loader)

        async def mock_tool(metric_def: MetricDefinition, params: dict, task_id: str = "") -> dict:
            return (
                {"success": True, "data": {"exit_code": 0}}
                if passed
                else {"success": False, "data": {"exit_code": 1}}
            )

        engine.register_evaluator(MetricType.TOOL, mock_tool)
        return engine

    async def test_run_evaluation(self, loader: MetricLoader) -> None:
        """执行评估返回正确结果。"""
        executor = EvaluationExecutor(
            loader=loader, engine=self._make_mock_engine(loader, passed=True),
        )
        result = await executor.run_evaluation(
            task_id="task1",
            metric_ids=["bash_check"],
        )
        assert result.task_id == "task1"
        assert len(result.results) == 1
        assert result.overall_passed is True

    async def test_run_with_task_service(self, loader: MetricLoader) -> None:
        """评估完成后回写任务状态。"""
        mock_service = MagicMock()
        executor = EvaluationExecutor(
            task_service=mock_service,
            loader=loader,
            engine=self._make_mock_engine(loader, passed=True),
        )
        await executor.run_evaluation(
            task_id="task1",
            metric_ids=["bash_check"],
        )
        # complete_evaluation(task_id, overall_passed, result=eval_data)
        mock_service.complete_evaluation.assert_called_once()
        call_args = mock_service.complete_evaluation.call_args
        assert call_args.args[0] == "task1"
        assert call_args.args[1] is True

    async def test_run_evaluation_failed(self, loader: MetricLoader) -> None:
        """评估失败时回写 failed 状态。"""
        mock_service = MagicMock()
        executor = EvaluationExecutor(
            task_service=mock_service,
            loader=loader,
            engine=self._make_mock_engine(loader, passed=False),
        )
        result = await executor.run_evaluation(
            task_id="task1",
            metric_ids=["bash_check"],
        )
        assert result.overall_passed is False
        mock_service.complete_evaluation.assert_called_once()
        call_args = mock_service.complete_evaluation.call_args
        assert call_args.args[0] == "task1"
        assert call_args.args[1] is False


# ── EvaluationResult 综合判定测试 ─────────────────────────


class TestEvaluationResult:
    """评估结果综合判定测试。"""

    def test_compute_overall_all_pass(self) -> None:
        """所有指标通过 → overall_passed=True。"""
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

    def test_compute_overall_partial_fail(self) -> None:
        """部分指标失败 → overall_passed=False。"""
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

    def test_compute_overall_empty(self) -> None:
        """空结果 → overall_passed=False（无指标时判定为不通过）。"""
        result = EvaluationResult(task_id="t1", results=[])
        result.compute_overall()
        assert result.overall_passed is False
        assert "无评估指标" in result.summary
