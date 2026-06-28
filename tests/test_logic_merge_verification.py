"""B-1/B-2/B-4/B-5 逻辑合并验证测试。

验证 4 项重复逻辑合并后提取的方法行为不变：
- B-1: evaluation/engine.py — _evaluate_core() 核心评估流程
- B-2: routes_missing.py — _with_fallback_strategies() 降级链
- B-4: pipeline/engine.py — _create_log_handler() FileHandler 工厂
- B-5: routes_threads.py — _safe_get_service() 服务安全获取

约束：不改变现有功能行为，验证提取后行为与原始逻辑一致。
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from evaluation.engine import EvaluationEngine
from evaluation.types import (
    EvaluationConfig,
    MetricDefinition,
    MetricResult,
    MetricType,
)


# ============================================================
# B-1: _evaluate_core() 核心评估流程验证
# ============================================================


class _TestableEvaluationEngine(EvaluationEngine):
    """测试用评估引擎，覆盖 _evaluate_metric 以隔离外部依赖。

    记录每次 _evaluate_metric 调用的 metric_id 和 input_params，
    用于验证 resolve_params 回调和指标执行顺序。
    """

    def __init__(
        self, metric_results: dict[str, MetricResult] | None = None
    ) -> None:
        loader = MagicMock()
        loader.metrics = {}
        super().__init__(loader=loader)
        self._metric_results_map = metric_results or {}
        self.eval_call_log: list[tuple[str, dict[str, Any]]] = []

    async def _evaluate_metric(
        self,
        metric_def: MetricDefinition,
        input_params: dict[str, Any],
        task_id: str,
    ) -> MetricResult:
        self.eval_call_log.append((metric_def.id, dict(input_params)))
        if metric_def.id in self._metric_results_map:
            return self._metric_results_map[metric_def.id]
        return MetricResult(metric_id=metric_def.id, passed=True)


class TestB1EvaluateCore:
    """B-1: _evaluate_core() 核心评估流程验证。"""

    @pytest.fixture
    def engine(self) -> _TestableEvaluationEngine:
        return _TestableEvaluationEngine()

    @pytest.fixture
    def engine_with_failure(self) -> _TestableEvaluationEngine:
        """第一个指标失败，后续指标通过。"""
        return _TestableEvaluationEngine(
            metric_results={
                "fail_metric": MetricResult(
                    metric_id="fail_metric", passed=False, message="评估失败"
                ),
            }
        )

    def _make_metrics(self) -> list[MetricDefinition]:
        """创建三种类型的指标，故意打乱顺序（HUMAN → TOOL → AGENT）。"""
        return [
            MetricDefinition(id="m_human", metric_type=MetricType.HUMAN),
            MetricDefinition(id="m_tool", metric_type=MetricType.TOOL),
            MetricDefinition(id="m_agent", metric_type=MetricType.AGENT),
        ]

    async def test_empty_metrics_returns_not_passed(self, engine):
        """测试: 空指标列表返回 overall_passed=False 且 summary 为提示信息。"""
        result = await engine._evaluate_core(
            task_id="t1",
            metrics_to_run=[],
            fail_fast=False,
            resolve_params=lambda m: {},
        )
        assert result.overall_passed is False
        assert "无可评估指标" in result.summary
        assert result.results == []

    async def test_metrics_sorted_by_type_priority(self, engine):
        """测试: 指标按类型优先级排序 (TOOL→AGENT→HUMAN)。"""
        metrics = self._make_metrics()
        await engine._evaluate_core(
            task_id="t1",
            metrics_to_run=metrics,
            fail_fast=False,
            resolve_params=lambda m: {},
        )
        executed_ids = [mid for mid, _ in engine.eval_call_log]
        assert executed_ids == ["m_tool", "m_agent", "m_human"]

    async def test_fail_fast_stops_on_first_failure(self, engine_with_failure):
        """测试: fail_fast=True 时首次失败即停止，后续指标不执行。"""
        metrics = [
            MetricDefinition(id="fail_metric", metric_type=MetricType.TOOL),
            MetricDefinition(id="pass_metric", metric_type=MetricType.AGENT),
        ]
        result = await engine_with_failure._evaluate_core(
            task_id="t1",
            metrics_to_run=metrics,
            fail_fast=True,
            resolve_params=lambda m: {},
        )
        assert len(result.results) == 1
        assert result.results[0].metric_id == "fail_metric"
        assert len(engine_with_failure.eval_call_log) == 1

    async def test_no_fail_fast_executes_all(self, engine_with_failure):
        """测试: fail_fast=False 时全部指标均执行。"""
        metrics = [
            MetricDefinition(id="fail_metric", metric_type=MetricType.TOOL),
            MetricDefinition(id="pass_metric", metric_type=MetricType.AGENT),
        ]
        result = await engine_with_failure._evaluate_core(
            task_id="t1",
            metrics_to_run=metrics,
            fail_fast=False,
            resolve_params=lambda m: {},
        )
        assert len(result.results) == 2
        assert len(engine_with_failure.eval_call_log) == 2

    async def test_overall_result_all_passed(self, engine):
        """测试: 全部通过时 overall_passed=True。"""
        metrics = [
            MetricDefinition(id="m1", metric_type=MetricType.TOOL),
            MetricDefinition(id="m2", metric_type=MetricType.AGENT),
        ]
        result = await engine._evaluate_core(
            task_id="t1",
            metrics_to_run=metrics,
            fail_fast=False,
            resolve_params=lambda m: {},
        )
        assert result.overall_passed is True
        assert "2/2" in result.summary

    async def test_overall_result_has_failure(self, engine_with_failure):
        """测试: 存在失败时 overall_passed=False。"""
        metrics = [
            MetricDefinition(id="fail_metric", metric_type=MetricType.TOOL),
            MetricDefinition(id="pass_metric", metric_type=MetricType.AGENT),
        ]
        result = await engine_with_failure._evaluate_core(
            task_id="t1",
            metrics_to_run=metrics,
            fail_fast=False,
            resolve_params=lambda m: {},
        )
        assert result.overall_passed is False

    async def test_resolve_params_called_per_metric(self, engine):
        """测试: resolve_params 回调对每个指标被调用且传入正确参数。"""
        metrics = [
            MetricDefinition(id="m1", metric_type=MetricType.TOOL),
            MetricDefinition(id="m2", metric_type=MetricType.AGENT),
        ]
        param_map = {"m1": {"key1": "val1"}, "m2": {"key2": "val2"}}
        await engine._evaluate_core(
            task_id="t1",
            metrics_to_run=metrics,
            fail_fast=False,
            resolve_params=lambda m: param_map.get(m.id, {}),
        )
        call_params = dict(engine.eval_call_log)
        assert call_params["m1"] == {"key1": "val1"}
        assert call_params["m2"] == {"key2": "val2"}


class TestB1EvaluateResolveParams:
    """B-1: evaluate() 和 evaluate_with_metrics() 的 resolve_params 差异验证。"""

    async def test_evaluate_uses_config_input_params_by_metric_id(self):
        """测试: evaluate() 的 resolve_params 从 config.input_params 按 metric_id 取参数。"""
        engine = _TestableEvaluationEngine()
        engine._loader.get = MagicMock(return_value=MetricDefinition(
            id="m1", metric_type=MetricType.TOOL
        ))

        config = EvaluationConfig(
            metric_ids=["m1"],
            input_params={"m1": {"file": "result.py"}},
        )
        await engine.evaluate(task_id="t1", config=config)

        _, params = engine.eval_call_log[0]
        assert params == {"file": "result.py"}

    async def test_evaluate_missing_metric_id_returns_empty_params(self):
        """测试: evaluate() 中 metric_id 不在 input_params 时返回空 dict。"""
        engine = _TestableEvaluationEngine()
        engine._loader.get = MagicMock(return_value=MetricDefinition(
            id="m1", metric_type=MetricType.TOOL
        ))

        config = EvaluationConfig(metric_ids=["m1"])
        await engine.evaluate(task_id="t1", config=config)

        _, params = engine.eval_call_log[0]
        assert params == {}

    async def test_evaluate_with_metrics_merges_default_config_and_params(self):
        """测试: evaluate_with_metrics() 合并 default_config 和全局 input_params。"""
        engine = _TestableEvaluationEngine()
        metrics = [
            MetricDefinition(
                id="m1",
                metric_type=MetricType.TOOL,
                default_config={"default_key": "default_val"},
            ),
        ]

        await engine.evaluate_with_metrics(
            task_id="t1",
            metrics=metrics,
            input_params={"global_key": "global_val"},
        )

        _, params = engine.eval_call_log[0]
        assert params == {
            "default_key": "default_val",
            "global_key": "global_val",
        }

    async def test_evaluate_with_metrics_global_params_overrides_default(self):
        """测试: evaluate_with_metrics() 中全局参数覆盖 default_config。"""
        engine = _TestableEvaluationEngine()
        metrics = [
            MetricDefinition(
                id="m1",
                metric_type=MetricType.TOOL,
                default_config={"shared_key": "from_default"},
            ),
        ]

        await engine.evaluate_with_metrics(
            task_id="t1",
            metrics=metrics,
            input_params={"shared_key": "from_global"},
        )

        _, params = engine.eval_call_log[0]
        assert params["shared_key"] == "from_global"

    async def test_evaluate_with_metrics_fail_fast_is_false(self):
        """测试: evaluate_with_metrics() 始终使用 fail_fast=False。"""
        engine = _TestableEvaluationEngine(
            metric_results={
                "m1": MetricResult(metric_id="m1", passed=False),
            }
        )
        metrics = [
            MetricDefinition(id="m1", metric_type=MetricType.TOOL),
            MetricDefinition(id="m2", metric_type=MetricType.AGENT),
        ]

        result = await engine.evaluate_with_metrics(
            task_id="t1", metrics=metrics
        )

        # fail_fast=False 时即使 m1 失败也会执行 m2
        assert len(result.results) == 2


# ============================================================
# B-2: _with_fallback_strategies() 降级链验证
# ============================================================


class TestB2WithFallbackStrategies:
    """B-2: _with_fallback_strategies() 降级链验证。"""

    @pytest.fixture(autouse=True)
    def _import_func(self):
        from channels.api.routes_missing import _with_fallback_strategies
        self._func = _with_fallback_strategies

    def test_first_strategy_success_returns_directly(self):
        """测试: 第一个策略成功时直接返回，不执行后续策略。"""
        call_log: list[str] = []

        def strategy_a():
            call_log.append("a")
            return {"data": "from_a"}

        def strategy_b():
            call_log.append("b")
            return {"data": "from_b"}

        result = self._func(
            strategies=[strategy_a, strategy_b],
            default={"data": "default"},
        )
        assert result == {"data": "from_a"}
        assert call_log == ["a"]

    def test_first_fails_second_succeeds(self):
        """测试: 主策略失败(None)时尝试降级策略并返回其结果。"""
        call_log: list[str] = []

        def strategy_a():
            call_log.append("a")
            return None

        def strategy_b():
            call_log.append("b")
            return {"data": "from_b"}

        result = self._func(
            strategies=[strategy_a, strategy_b],
            default={"data": "default"},
        )
        assert result == {"data": "from_b"}
        assert call_log == ["a", "b"]

    def test_all_strategies_fail_returns_default(self):
        """测试: 全部策略失败时返回默认响应。"""
        def strategy_a():
            return None

        def strategy_b():
            return None

        result = self._func(
            strategies=[strategy_a, strategy_b],
            default={"fallback": True},
        )
        assert result == {"fallback": True}

    def test_empty_strategies_returns_default(self):
        """测试: 空策略列表直接返回默认响应。"""
        result = self._func(
            strategies=[],
            default={"empty": True},
        )
        assert result == {"empty": True}

    def test_empty_dict_treated_as_success(self):
        """测试: 返回空 dict (非 None) 视为成功。"""
        def strategy():
            return {}

        result = self._func(
            strategies=[strategy],
            default={"fallback": True},
        )
        assert result == {}

    def test_strategy_order_respected(self):
        """测试: 策略按列表顺序依次尝试。"""
        call_order: list[int] = []

        def make_strategy(idx, succeeds):
            def strategy():
                call_order.append(idx)
                return {"idx": idx} if succeeds else None
            return strategy

        result = self._func(
            strategies=[
                make_strategy(1, False),
                make_strategy(2, False),
                make_strategy(3, True),
                make_strategy(4, True),
            ],
            default={"fallback": True},
        )
        assert result == {"idx": 3}
        assert call_order == [1, 2, 3]


# ============================================================
# B-4: _create_log_handler() FileHandler 工厂验证
# ============================================================


class TestB4CreateLogHandler:
    """B-4: _create_log_handler() FileHandler 工厂验证。"""

    @pytest.fixture(autouse=True)
    def _import_factory(self):
        # _create_log_handler 已从 PipelineEngine 静态方法迁移到 engine_logging 模块函数
        from pipeline.engine_logging import _create_log_handler
        self._create_log_handler = _create_log_handler

    def _create_handler(self, tmp_path, **overrides):
        """创建 handler 并在测试后自动关闭。"""
        defaults = dict(
            file_path=str(tmp_path / "test.log"),
            mode="w",
            level=logging.DEBUG,
            formatter=logging.Formatter("%(message)s"),
            filters=[],
        )
        defaults.update(overrides)
        handler = self._create_log_handler(**defaults)
        yield handler
        handler.close()

    def test_correct_level(self, tmp_path):
        """测试: handler 设置正确的日志级别。"""
        for gen in self._create_handler(tmp_path, level=logging.WARNING):
            assert gen.level == logging.WARNING

    def test_correct_formatter(self, tmp_path):
        """测试: handler 设置正确的格式化器。"""
        fmt = logging.Formatter("%(asctime)s %(message)s")
        for gen in self._create_handler(tmp_path, formatter=fmt):
            assert gen.formatter is fmt

    def test_no_filters(self, tmp_path):
        """测试: 空 filters 列表时 handler 无过滤器。"""
        for gen in self._create_handler(tmp_path, filters=[]):
            assert len(gen.filters) == 0

    def test_filters_added(self, tmp_path):
        """测试: 所有 filter 被正确添加到 handler。"""
        f1 = logging.Filter()
        f2 = logging.Filter()
        for gen in self._create_handler(tmp_path, filters=[f1, f2]):
            assert f1 in gen.filters
            assert f2 in gen.filters
            assert len(gen.filters) == 2

    def test_correct_file_path(self, tmp_path):
        """测试: handler 指向正确的文件路径。"""
        import os
        log_dir = tmp_path / "custom"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / "output.log")
        for gen in self._create_handler(
            tmp_path, file_path=log_path, mode="w"
        ):
            assert os.path.normpath(gen.baseFilename) == os.path.normpath(log_path)

    def test_write_mode_creates_new_file(self, tmp_path):
        """测试: mode="w" 时覆盖写入已有文件。"""
        log_path = tmp_path / "overwrite.log"
        log_path.write_text("old content")

        for gen in self._create_handler(
            tmp_path, file_path=str(log_path), mode="w"
        ):
            pass  # handler 已关闭

        # mode="w" 会截断文件
        assert log_path.read_text() == ""

    def test_append_mode_preserves_content(self, tmp_path):
        """测试: mode="a" 时保留已有内容。"""
        log_path = tmp_path / "append.log"
        log_path.write_text("existing line\n")

        for gen in self._create_handler(
            tmp_path, file_path=str(log_path), mode="a"
        ):
            pass

        assert log_path.read_text() == "existing line\n"

    def test_handler_is_file_handler(self, tmp_path):
        """测试: 返回类型为 logging.FileHandler。"""
        for gen in self._create_handler(tmp_path):
            assert isinstance(gen, logging.FileHandler)

    def test_three_call_point_configs(self, tmp_path):
        """测试: 模拟 _setup_pipeline_logging 中 3 个调用点的配置。"""
        pipeline_id = "test_pipe_001"

        # 创建子目录
        (tmp_path / "error").mkdir(parents=True, exist_ok=True)
        (tmp_path / "task").mkdir(parents=True, exist_ok=True)

        _pipeline_filter = MagicMock(spec=logging.Filter)
        main_handler = self._create_log_handler(
            file_path=str(tmp_path / f"pipeline_{pipeline_id}.log"),
            mode="w",
            level=logging.DEBUG,
            formatter=logging.Formatter("%(asctime)s [%(name)s] %(message)s"),
            filters=[_pipeline_filter, lambda r: r.levelno < logging.WARNING],
        )
        assert main_handler.level == logging.DEBUG
        assert len(main_handler.filters) == 2
        main_handler.close()

        # 2. 错误日志: WARNING, 仅 pipeline_filter
        error_handler = self._create_log_handler(
            file_path=str(tmp_path / "error" / f"pipeline_{pipeline_id}.log"),
            mode="w",
            level=logging.WARNING,
            formatter=logging.Formatter("%(asctime)s [%(name)s] %(message)s"),
            filters=[_pipeline_filter],
        )
        assert error_handler.level == logging.WARNING
        assert len(error_handler.filters) == 1
        error_handler.close()

        # 3. 任务日志: DEBUG, 仅 task_filter
        _task_filter = MagicMock(spec=logging.Filter)
        task_handler = self._create_log_handler(
            file_path=str(tmp_path / "task" / f"pipeline_{pipeline_id}.log"),
            mode="w",
            level=logging.DEBUG,
            formatter=logging.Formatter("%(asctime)s [%(name)s] %(message)s"),
            filters=[_task_filter],
        )
        assert task_handler.level == logging.DEBUG
        assert len(task_handler.filters) == 1
        task_handler.close()


# ============================================================
# B-5: _safe_get_service() 服务安全获取验证
# ============================================================


class TestB5SafeGetService:
    """B-5: _safe_get_service() 服务安全获取验证。"""

    @pytest.fixture(autouse=True)
    def _import_func(self):
        from channels.api.routes_threads import _safe_get_service
        self._func = _safe_get_service

    @patch("channels.api.routes_threads.get_service_provider")
    def test_service_exists_returns_instance(self, mock_get_provider):
        """测试: 服务存在时返回实例。"""
        mock_service = MagicMock(name="task_service")
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_service
        mock_get_provider.return_value = mock_provider

        result = self._func("task_service")

        assert result is mock_service
        mock_provider.get.assert_called_once_with("task_service")

    @patch("channels.api.routes_threads.get_service_provider")
    def test_service_not_found_returns_none(self, mock_get_provider):
        """测试: provider.get 返回 None 时返回 None。"""
        mock_provider = MagicMock()
        mock_provider.get.return_value = None
        mock_get_provider.return_value = mock_provider

        result = self._func("nonexistent_service")

        assert result is None

    @patch("channels.api.routes_threads.get_service_provider")
    def test_provider_raises_returns_none(self, mock_get_provider):
        """测试: get_service_provider 抛异常时返回 None。"""
        mock_get_provider.side_effect = RuntimeError("provider not initialized")

        result = self._func("task_service")

        assert result is None

    @patch("channels.api.routes_threads.get_service_provider")
    def test_get_raises_returns_none(self, mock_get_provider):
        """测试: provider.get 抛异常时返回 None。"""
        mock_provider = MagicMock()
        mock_provider.get.side_effect = KeyError("service not registered")
        mock_get_provider.return_value = mock_provider

        result = self._func("bad_service")

        assert result is None

    @patch("channels.api.routes_threads.get_service_provider")
    def test_different_service_names(self, mock_get_provider):
        """测试: 不同服务名传递给 provider.get。"""
        mock_provider = MagicMock()
        mock_provider.get.return_value = MagicMock()
        mock_get_provider.return_value = mock_provider

        self._func("task_service")
        self._func("task_worker")

        assert mock_provider.get.call_args_list[0] == (("task_service",),)
        assert mock_provider.get.call_args_list[1] == (("task_worker",),)
