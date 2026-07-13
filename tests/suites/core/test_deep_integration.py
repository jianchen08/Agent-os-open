"""深度集成测试 — 测试真实代码路径，暴露隐藏 bug。

与 test_evaluation_stability.py 的浅层单元测试不同，
本文件测试 EvaluationEngine 的完整集成路径：
evaluate() → _evaluate_metric() → _evaluate_agent() → _parse_evaluation_result()

以及 EvaluationExecutor 的完整编排链路。

这些测试直接调用真实源码而非辅助函数，旨在发现隐藏 bug。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from evaluation.engine import EvaluationEngine
from evaluation.executor import EvaluationExecutor
from evaluation.types import (
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)


# ---------------------------------------------------------------------------
# 深度集成：EvaluationEngine.evaluate() 完整链路
# ---------------------------------------------------------------------------


class TestEvaluateAgentIntegration:
    """测试 Agent 型评估器的集成路径。

    变更说明（接口重构对齐）：
    早期 EvaluationEngine 支持注入 ``pipeline_factory``，由测试用 mock 管道
    驱动 _evaluate_agent。重构后 _evaluate_agent 改为经全局 service_provider /
    engine_registry 注册真实评估子管道（CollectingSink + send_pipeline_message），
    ``pipeline_factory`` 参数已移除，__init__ 不再接受该关键字。
    因此本类不再用 mock 管道测完整链路，而是断言重构后的真实契约：
    1. 评估引擎不接受 pipeline_factory；
    2. agent_registry 缺失 / 找不到 evaluator_agent 时，_evaluate_agent 直接抛
       RuntimeError 暴露配置问题（不再静默 fallback）。
    """

    @pytest.mark.core
    @pytest.mark.unit
    def test_evaluate_agent_no_pipeline_factory_kwarg_anymore(self):
        """验证 pipeline_factory 关键字已从 EvaluationEngine.__init__ 移除。

        变更原因：_evaluate_agent 重构为经全局注册表创建真实评估子管道，
        不再接受外部 pipeline_factory 注入。此处断言新接口契约，
        防止误用旧关键字。
        """
        loader = MagicMock()
        with pytest.raises(TypeError, match="pipeline_factory"):
            EvaluationEngine(loader=loader, pipeline_factory=MagicMock())

    @pytest.mark.core
    @pytest.mark.unit
    async def test_evaluate_agent_missing_registry_raises_runtime_error(self):
        """测试 agent_registry=None 时 _evaluate_agent 抛 RuntimeError。

        变更原因：原 mock 管道版测试当 pipeline 输出无 JSON 时返回 fallback；
        重构后无 agent_registry 即视为配置错误，_evaluate_agent 直接抛
        RuntimeError（而非静默 fallback），暴露问题。这里断言新行为。
        """
        metric = MetricDefinition(
            id="semantic_check",
            name="语义评估",
            metric_type=MetricType.AGENT,
            evaluator_id="system_evaluator_agent",
        )

        loader = MagicMock()
        engine = EvaluationEngine(loader=loader, agent_registry=None)

        with pytest.raises(RuntimeError, match="agent_registry"):
            await engine._evaluate_agent(metric, {"criteria": "测试"})

    @pytest.mark.core
    @pytest.mark.unit
    async def test_evaluate_agent_not_found_in_registry_raises_runtime_error(self):
        """测试 evaluator_agent 在 registry 中找不到时抛 RuntimeError。

        变更原因：原 mock 管道版测试当 pipeline 抛异常时返回 fallback；
        重构后 evaluator_agent 未注册即视为配置错误，_evaluate_agent 直接抛
        RuntimeError（而非捕获返回 fallback），暴露问题。这里断言新行为。
        """
        metric = MetricDefinition(
            id="semantic_check",
            name="语义评估",
            metric_type=MetricType.AGENT,
            evaluator_id="system_evaluator_agent",
        )

        from tests.suites.conftest import MockAgentRegistry

        empty_registry = MockAgentRegistry(configs=[])

        loader = MagicMock()
        engine = EvaluationEngine(
            loader=loader,
            agent_registry=empty_registry,
        )

        with pytest.raises(RuntimeError, match="not found in registry"):
            await engine._evaluate_agent(metric, {"criteria": "测试"})


class TestEvaluateToolIntegration:
    """测试 Tool 型评估器的集成路径。"""

    @pytest.mark.core
    @pytest.mark.unit
    async def test_evaluate_tool_no_registry_falls_back_to_builtin(self):
        """测试 tool_registry=None 时 _evaluate_tool 回退到内置工具发现。

        变更原因：_evaluate_tool 重构后，当 tool_registry 未注入时不再抛
        RuntimeError，而是经 DynamicToolLoader 自动发现 src/tools/builtin 下的
        内置工具（evaluator_id 即工具 name）。此处用真实存在的 bash_execute
        断言新行为：无 registry 也能解析到内置 BashTool 并成功执行。
        （_evaluate_tool 为 async 方法，需 await。）
        """
        metric = MetricDefinition(
            id="bash_check",
            name="命令检查",
            metric_type=MetricType.TOOL,
            evaluator_id="bash_execute",
        )

        loader = MagicMock()
        engine = EvaluationEngine(loader=loader, tool_registry=None)

        result = await engine._evaluate_tool(
            metric, {"action": "execute", "command": "echo test"},
        )

        # 内置 BashTool 执行成功，返回 success=True 的结果字典
        assert result["success"] is True

    @pytest.mark.core
    @pytest.mark.unit
    async def test_evaluate_tool_handler_not_found_raises_runtime_error(self):
        """测试 tool 存在于 registry 但 handler 不存在时抛 RuntimeError。

        验证：当 tool_registry 与内置工具发现都找不到对应 handler 时，
        应抛出 RuntimeError 而非静默 fallback。
        （_evaluate_tool 为 async 方法，需 await。）
        """
        metric = MetricDefinition(
            id="bash_check",
            name="命令检查",
            metric_type=MetricType.TOOL,
            evaluator_id="nonexistent_tool",
        )

        mock_registry = MagicMock()
        mock_registry.get_handler.return_value = None

        loader = MagicMock()
        engine = EvaluationEngine(loader=loader, tool_registry=mock_registry)

        with pytest.raises(RuntimeError, match="Tool 'nonexistent_tool' not found in registry"):
            await engine._evaluate_tool(metric, {"action": "execute"})


class TestEvaluateMetricIntegration:
    """测试 _evaluate_metric 的完整路径。"""

    @pytest.mark.core
    @pytest.mark.unit
    async def test_evaluate_metric_agent_type_exception_becomes_metric_result(self):
        """测试 _evaluate_metric 中 Agent 评估异常被捕获为 MetricResult。

        验证：当 _evaluate_agent 内部抛出未预期的异常（如 evaluator_agent 未
        在 registry 注册）时，_evaluate_metric 应捕获异常并返回 passed=False
        的 MetricResult，而非让异常向上传播导致整个评估崩溃。
        （_evaluate_metric 为 async 方法，需 await。）
        """
        metric = MetricDefinition(
            id="semantic_check",
            name="语义评估",
            metric_type=MetricType.AGENT,
            evaluator_id="system_evaluator_agent",
        )

        from tests.suites.conftest import MockAgentRegistry

        empty_registry = MockAgentRegistry(configs=[])

        loader = MagicMock()
        engine = EvaluationEngine(
            loader=loader,
            agent_registry=empty_registry,
        )

        result = await engine._evaluate_metric(metric, {"criteria": "测试"})

        assert isinstance(result, MetricResult)
        assert result.metric_id == "semantic_check"
        assert result.passed is False
        assert "not found in registry" in result.error

    @pytest.mark.core
    @pytest.mark.unit
    async def test_evaluate_metric_with_expect_conditions(self):
        """测试带 expect 条件的评估判定逻辑。

        验证：当 metric 定义了 expect.conditions 时，
        _evaluate_metric 会通过 ExpectEvaluator 进行条件判定，
        而非直接返回 passed=True。
        （_evaluate_metric 为 async 方法，需 await。）
        """
        metric = MetricDefinition(
            id="bash_check",
            name="命令检查",
            metric_type=MetricType.TOOL,
            evaluator_id="bash_execute",
            expect=ExpectSpec(
                conditions=[
                    ExpectCondition(field="success", operator="is_true"),
                    ExpectCondition(field="data.exit_code", operator="equals", value=0),
                ],
                logic="and",
                pass_message="命令执行成功",
                fail_message="命令执行失败",
            ),
        )

        mock_registry = MagicMock()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "success": True,
            "data": {"exit_code": 0, "stdout": "hello"},
        }
        mock_registry.get_handler.return_value = lambda params: mock_result

        loader = MagicMock()
        engine = EvaluationEngine(loader=loader, tool_registry=mock_registry)

        result = await engine._evaluate_metric(
            metric,
            {"action": "execute", "command": "echo hello"},
        )

        assert result.passed is True
        assert result.message == "命令执行成功"

    @pytest.mark.core
    @pytest.mark.unit
    async def test_evaluate_metric_expect_condition_fails(self):
        """测试 expect 条件不满足时评估结果为 failed。

        验证：当工具返回 exit_code=1 但期望 exit_code=0 时，
        ExpectEvaluator 应正确判定为 failed。
        （_evaluate_metric 为 async 方法，需 await。）
        """
        metric = MetricDefinition(
            id="bash_check",
            name="命令检查",
            metric_type=MetricType.TOOL,
            evaluator_id="bash_execute",
            expect=ExpectSpec(
                conditions=[
                    ExpectCondition(field="data.exit_code", operator="equals", value=0),
                ],
                pass_message="通过",
                fail_message="退出码非零",
            ),
        )

        mock_registry = MagicMock()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "success": False,
            "data": {"exit_code": 1, "stderr": "error"},
        }
        mock_registry.get_handler.return_value = lambda params: mock_result

        loader = MagicMock()
        engine = EvaluationEngine(loader=loader, tool_registry=mock_registry)

        result = await engine._evaluate_metric(
            metric,
            {"action": "execute", "command": "false"},
        )

        assert result.passed is False


class TestExecutorIntegration:
    """测试 EvaluationExecutor 的完整编排链路。"""

    @pytest.mark.core
    @pytest.mark.unit
    async def test_executor_run_evaluation_with_real_engine(self):
        """测试 executor 使用真实 EvaluationEngine 执行评估。

        验证：EvaluationExecutor 能正确编排 loader → engine → mapper 的完整流程。
        （run_evaluation 为 async 方法，需 await。）
        """
        metric = MetricDefinition(
            id="bash_check",
            name="命令检查",
            metric_type=MetricType.TOOL,
            evaluator_id="bash_execute",
            expect=ExpectSpec(
                conditions=[
                    ExpectCondition(field="success", operator="is_true"),
                ],
            ),
        )

        mock_loader = MagicMock()
        mock_loader.get.return_value = metric
        mock_loader.metrics = {"bash_check": metric}

        mock_registry = MagicMock()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"success": True, "data": {"exit_code": 0}}
        mock_registry.get_handler.return_value = lambda params: mock_result

        mock_task_service = MagicMock()

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            loader=mock_loader,
            tool_registry=mock_registry,
        )

        result = await executor.run_evaluation(
            task_id="test_task_001",
            metric_ids=["bash_check"],
            input_params={"bash_check": {"action": "execute", "command": "echo test"}},
            skip_state_update=True,
        )

        assert isinstance(result, EvaluationResult)
        assert result.overall_passed is True
        assert len(result.results) == 1
        mock_task_service.complete_evaluation.assert_not_called()

    @pytest.mark.core
    @pytest.mark.unit
    async def test_executor_state_update_when_not_skipped(self):
        """测试 skip_state_update=False 时 executor 回写状态。

        验证：当 skip_state_update=False 且有 task_service 时，
        executor 应调用 task_service.complete_evaluation 回写结果。
        （run_evaluation 为 async 方法，需 await。task_service.complete_evaluation
        在源码中以 await 调用，故用 AsyncMock 模拟协程方法。）
        """
        metric = MetricDefinition(
            id="bash_check",
            name="命令检查",
            metric_type=MetricType.TOOL,
            evaluator_id="bash_execute",
            expect=ExpectSpec(
                conditions=[ExpectCondition(field="success", operator="is_true")],
            ),
        )

        mock_loader = MagicMock()
        mock_loader.get.return_value = metric
        mock_loader.metrics = {"bash_check": metric}

        mock_registry = MagicMock()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"success": True}
        mock_registry.get_handler.return_value = lambda params: mock_result

        mock_task_service = MagicMock()
        mock_task_service.complete_evaluation = AsyncMock()

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            loader=mock_loader,
            tool_registry=mock_registry,
        )

        result = await executor.run_evaluation(
            task_id="test_task_001",
            metric_ids=["bash_check"],
            skip_state_update=False,
        )

        assert result.overall_passed is True
        mock_task_service.complete_evaluation.assert_called_once()
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args[0][0] == "test_task_001"
        assert call_args[0][1] is True
        assert call_args[1]["result"]["overall_passed"] is True

    @pytest.mark.core
    @pytest.mark.unit
    async def test_executor_no_metrics_returns_empty_result(self):
        """测试当 loader 中无匹配指标时返回空结果。

        验证：当 metric_ids 中的 ID 在 loader 中不存在时，
        evaluate() 应返回 overall_passed=False 的空结果。
        （run_evaluation 为 async 方法，需 await。）
        """
        mock_loader = MagicMock()
        mock_loader.get.return_value = None
        mock_loader.metrics = {}

        executor = EvaluationExecutor(
            task_service=MagicMock(),
            loader=mock_loader,
        )

        result = await executor.run_evaluation(
            task_id="test_task_001",
            metric_ids=["nonexistent_metric"],
        )

        assert result.overall_passed is False
        assert len(result.results) == 0


class TestParseEdgeCases:
    """测试 _parse_evaluation_result 的边界情况。"""

    @pytest.mark.core
    @pytest.mark.unit
    def test_parse_multiple_json_blocks_takes_first_valid(self):
        """测试文本中有多个 JSON 块时取第一个有效的。

        验证：当 LLM 输出中包含多个 JSON 块时，
        解析器应取第一个包含 evaluation_result 的有效 JSON。
        """
        text = (
            '前一段 {"evaluation_result": {"passed": true, "score": 80, "feedback": "A"}}'
            ' 后一段 {"evaluation_result": {"passed": false, "score": 30, "feedback": "B"}}'
        )
        result = EvaluationEngine._parse_evaluation_result(text)

        assert result is not None
        assert result["passed"] is True
        assert result["score"] == 80.0

    @pytest.mark.core
    @pytest.mark.unit
    def test_parse_nested_with_extra_fields(self):
        """测试嵌套 JSON 包含额外字段时只提取 passed/score/feedback。

        验证：当 evaluation_result 中包含 suggestions 等额外字段时，
        解析器应正确提取 suggestions。
        """
        text = (
            '{"evaluation_result": {"passed": true, "score": 85, '
            '"feedback": "通过", "suggestions": ["建议1", "建议2"]}}'
        )
        result = EvaluationEngine._parse_evaluation_result(text)

        assert result is not None
        assert result["passed"] is True
        assert result["score"] == 85.0
        assert result["suggestions"] == ["建议1", "建议2"]

    @pytest.mark.core
    @pytest.mark.unit
    def test_parse_boolean_true_lowercase_only(self):
        """测试 JSON 中 passed 值必须为 true/false（小写布尔值）。

        验证：JSON 标准中布尔值为 true/false（小写），
        解析器应正确处理。
        """
        text_passed = '{"evaluation_result": {"passed": true, "score": 90, "feedback": "OK"}}'
        text_failed = '{"evaluation_result": {"passed": false, "score": 30, "feedback": "NO"}}'

        result_passed = EvaluationEngine._parse_evaluation_result(text_passed)
        result_failed = EvaluationEngine._parse_evaluation_result(text_failed)

        assert result_passed["passed"] is True
        assert result_failed["passed"] is False

    @pytest.mark.core
    @pytest.mark.unit
    def test_parse_score_as_float(self):
        """测试 score 字段正确转换为 float 类型。

        验证：即使 JSON 中 score 是整数（如 95），
        解析器应返回 float 类型（95.0）。
        """
        text = '{"evaluation_result": {"passed": true, "score": 95, "feedback": "OK"}}'
        result = EvaluationEngine._parse_evaluation_result(text)

        assert isinstance(result["score"], float)
        assert result["score"] == 95.0

    @pytest.mark.core
    @pytest.mark.unit
    def test_parse_missing_score_defaults_to_zero(self):
        """测试当 JSON 中缺少 score 字段时默认为 0。

        验证：当 evaluation_result 中没有 score 字段时，
        解析器应返回 score=0.0。
        """
        text = '{"evaluation_result": {"passed": true, "feedback": "OK"}}'
        result = EvaluationEngine._parse_evaluation_result(text)

        assert result is not None
        assert result["passed"] is True
        assert result["score"] == 0.0

    @pytest.mark.core
    @pytest.mark.unit
    def test_parse_missing_feedback_defaults_to_empty(self):
        """测试当 JSON 中缺少 feedback 字段时默认为空字符串。

        验证：当 evaluation_result 中没有 feedback 字段时，
        解析器应返回 feedback=""。
        """
        text = '{"evaluation_result": {"passed": true, "score": 90}}'
        result = EvaluationEngine._parse_evaluation_result(text)

        assert result is not None
        assert result["feedback"] == ""
