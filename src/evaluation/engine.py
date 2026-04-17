"""统一评估引擎 — 根据指标类型分发到对应评估器。

评估引擎是评估系统的核心协调器，职责：
1. 接收评估请求（指标 ID 列表 + 输入参数）
2. 从 MetricLoader 获取指标定义
3. 根据指标类型分发评估：
   - tool → 通过 ToolRegistry 调用真实工具执行评估
   - agent → 调用 LLM Agent 评估（当前 Mock 实现）
   - human → 等待人工审核（当前 Mock 实现）
4. 使用 ExpectEvaluator 对评估输出进行期望判定
5. 收集评估结果返回

tool 类型评估器通过注入 ToolRegistry 实现真实工具调用；
当 ToolRegistry 不可用时自动 fallback 到 Mock 实现。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from evaluation.expect import ExpectEvaluator
from evaluation.loader import MetricLoader
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    MetricDefinition,
    MetricResult,
    MetricType,
)

logger = logging.getLogger(__name__)

# 评估器函数签名：接收指标定义和输入参数，返回输出字典
EvaluatorFunc = Callable[[MetricDefinition, dict[str, Any]], dict[str, Any]]


class EvaluationEngine:
    """统一评估引擎。

    根据指标类型分发评估到对应评估器，并使用期望评估器判定结果。

    用法：
        loader = MetricLoader()
        loader.load_all()
        engine = EvaluationEngine(loader=loader)
        result = engine.evaluate(
            task_id="abc123",
            config=EvaluationConfig(metric_ids=["code_check"]),
        )

    Attributes:
        _loader: 指标加载器
        _expect_evaluator: 期望值评估器
        _evaluators: 各类型的评估器函数注册表
    """

    def __init__(
        self,
        loader: MetricLoader,
        expect_evaluator: ExpectEvaluator | None = None,
        tool_registry: Any | None = None,
    ) -> None:
        """初始化评估引擎。

        Args:
            loader: 指标加载器（必须已加载指标）
            expect_evaluator: 期望值评估器，None 时创建默认实例
            tool_registry: 工具注册表，None 时工具型评估器 fallback 到 Mock
        """
        self._loader = loader
        self._expect_evaluator = expect_evaluator or ExpectEvaluator()
        self._tool_registry = tool_registry
        self._evaluators: dict[MetricType, EvaluatorFunc] = {
            MetricType.TOOL: self._evaluate_tool,
            MetricType.AGENT: self._evaluate_agent,
            MetricType.HUMAN: self._evaluate_human,
        }

    def register_evaluator(self, metric_type: MetricType, func: EvaluatorFunc) -> None:
        """注册自定义评估器函数。

        用于替换默认的 Mock 评估器或添加新类型。

        Args:
            metric_type: 指标类型
            func: 评估器函数
        """
        self._evaluators[metric_type] = func
        logger.info("Registered evaluator for %s", metric_type.value)

    def evaluate(
        self,
        task_id: str,
        config: EvaluationConfig | None = None,
    ) -> EvaluationResult:
        """执行评估。

        Args:
            task_id: 关联的任务 ID
            config: 评估配置，None 时使用默认配置

        Returns:
            评估结果
        """
        config = config or EvaluationConfig()

        # 确定要评估的指标列表
        if config.metric_ids:
            metrics_to_run = [
                self._loader.get(mid)
                for mid in config.metric_ids
                if self._loader.get(mid) is not None
            ]
        else:
            metrics_to_run = list(self._loader.metrics.values())

        if not metrics_to_run:
            logger.warning("No metrics to evaluate for task %s", task_id)
            return EvaluationResult(
                task_id=task_id,
                overall_passed=False,
                summary="无可评估指标",
            )

        # 逐指标评估
        results: list[MetricResult] = []
        for metric_def in metrics_to_run:
            result = self._evaluate_metric(
                metric_def=metric_def,
                input_params=config.input_params.get(metric_def.id, {}),
            )
            results.append(result)

            if config.fail_fast and not result.passed:
                logger.info(
                    "Fail-fast triggered: %s failed, stopping evaluation",
                    metric_def.id,
                )
                break

        eval_result = EvaluationResult(
            task_id=task_id,
            results=results,
        )
        eval_result.compute_overall()
        return eval_result

    def evaluate_with_metrics(
        self,
        task_id: str,
        metrics: list[MetricDefinition],
        input_params: dict[str, Any] | None = None,
    ) -> EvaluationResult:
        """使用动态指标列表执行评估（不依赖 loader）。

        适用于模板评估等场景，指标由运行时动态生成而非从 YAML 文件加载。

        Args:
            task_id: 关联的任务 ID
            metrics: 动态指标定义列表
            input_params: 全局输入参数，每个指标共享

        Returns:
            评估结果
        """
        if not metrics:
            logger.warning("No metrics to evaluate for task %s", task_id)
            return EvaluationResult(
                task_id=task_id,
                overall_passed=False,
                summary="无可评估指标",
            )

        input_params = input_params or {}
        results: list[MetricResult] = []

        for metric_def in metrics:
            # 合并全局输入参数和指标默认配置
            merged_params = {**metric_def.default_config, **input_params}
            result = self._evaluate_metric(
                metric_def=metric_def,
                input_params=merged_params,
            )
            results.append(result)

        eval_result = EvaluationResult(
            task_id=task_id,
            results=results,
        )
        eval_result.compute_overall()
        return eval_result

    def evaluate_single(
        self,
        task_id: str,
        metric_id: str,
        input_params: dict[str, Any] | None = None,
    ) -> MetricResult:
        """评估单个指标。

        Args:
            task_id: 关联的任务 ID
            metric_id: 指标 ID
            input_params: 评估输入参数

        Returns:
            单个指标的评估结果

        Raises:
            KeyError: 指标不存在
        """
        metric_def = self._loader.get(metric_id)
        if metric_def is None:
            raise KeyError(f"Metric '{metric_id}' not found")

        return self._evaluate_metric(
            metric_def=metric_def,
            input_params=input_params or {},
        )

    def _evaluate_metric(
        self,
        metric_def: MetricDefinition,
        input_params: dict[str, Any],
    ) -> MetricResult:
        """执行单个指标的评估流程。

        1. 根据指标类型获取评估器
        2. 调用评估器获取输出
        3. 使用期望评估器判定结果

        Args:
            metric_def: 指标定义
            input_params: 输入参数

        Returns:
            评估结果
        """
        evaluator = self._evaluators.get(metric_def.metric_type)
        if evaluator is None:
            return MetricResult(
                metric_id=metric_def.id,
                passed=False,
                message=f"无对应评估器: {metric_def.metric_type.value}",
                error=f"No evaluator registered for type {metric_def.metric_type}",
            )

        try:
            # 合并默认配置和输入参数
            merged_params = {**metric_def.default_config, **input_params}

            # 调用评估器获取输出
            output = evaluator(metric_def, merged_params)

            # 使用期望评估器判定
            result = self._expect_evaluator.evaluate(
                metric_id=metric_def.id,
                expect=metric_def.expect,
                output=output,
            )

            # 尝试提取 score（agent/human 类型可能返回 score）
            if "score" in output:
                result.score = float(output["score"])
            elif "passed" in output and isinstance(output["passed"], bool):
                result.score = 100.0 if output["passed"] else 0.0

            return result

        except Exception as e:
            logger.error(
                "Evaluation failed for metric %s: %s", metric_def.id, e
            )
            return MetricResult(
                metric_id=metric_def.id,
                passed=False,
                message=f"评估执行异常: {e}",
                error=str(e),
            )

    # ── 默认评估器实现（Mock） ────────────────────────────

    def _evaluate_tool(
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """工具型评估器 — 通过 ToolRegistry 调用真实工具。

        当 tool_registry 不可用或工具不存在时，fallback 到 Mock 实现。

        Args:
            metric_def: 指标定义（evaluator_id 指定要调用的工具）
            params: 合并后的输入参数

        Returns:
            工具执行结果字典，格式：{"success": bool, "data": dict, "error": str}
        """
        evaluator_id = metric_def.evaluator_id
        logger.info(
            "Tool evaluation: %s (evaluator_id=%s)",
            metric_def.id, evaluator_id,
        )

        # 尝试通过 ToolRegistry 调用真实工具
        if self._tool_registry is not None and evaluator_id:
            handler = self._tool_registry.get_handler(evaluator_id)
            if handler is not None:
                try:
                    import asyncio

                    tool_result = handler(params)
                    if asyncio.iscoroutine(tool_result):
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = None

                        if loop and loop.is_running():
                            import concurrent.futures

                            def _run_async(coro_factory, params):
                                return asyncio.run(coro_factory(params))

                            with concurrent.futures.ThreadPoolExecutor() as pool:
                                tool_result = pool.submit(
                                    _run_async, handler, params
                                ).result()
                        else:
                            tool_result = asyncio.run(handler(params))

                    # 将 ToolExecutionResult 转为标准 dict
                    if hasattr(tool_result, "to_dict"):
                        result_dict = tool_result.to_dict()
                    elif isinstance(tool_result, dict):
                        result_dict = tool_result
                    else:
                        result_dict = {"success": True, "data": tool_result}

                    # 标准化：确保包含 success 字段
                    if "success" not in result_dict:
                        status = result_dict.get("status", "completed")
                        result_dict["success"] = status == "completed"

                    logger.info(
                        "Tool evaluation completed: %s -> success=%s",
                        metric_def.id, result_dict.get("success"),
                    )
                    return result_dict

                except Exception as e:
                    logger.error(
                        "Tool execution failed for %s (evaluator_id=%s): %s",
                        metric_def.id, evaluator_id, e,
                    )
                    return {
                        "success": False,
                        "error": str(e),
                    }
            else:
                logger.warning(
                    "Tool '%s' not found in registry, falling back to mock",
                    evaluator_id,
                )

        # Fallback: Mock 实现
        logger.info(
            "Mock tool evaluation (fallback): %s (evaluator_id=%s)",
            metric_def.id, evaluator_id,
        )
        return {
            "success": True,
            "data": {
                "status": "completed",
                "exit_code": 0,
            },
        }

    def _evaluate_agent(
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Agent 型评估器的 Mock 实现。

        在当前阶段（M5b），仅返回 Mock 输出。
        真实实现需要创建独立 LLM Agent 执行评估。

        Args:
            metric_def: 指标定义
            params: 合并后的输入参数

        Returns:
            Mock 评估输出
        """
        logger.info(
            "Mock agent evaluation: %s (evaluator_id=%s)",
            metric_def.id, metric_def.evaluator_id,
        )
        # Mock: 返回通过结果
        return {
            "passed": True,
            "score": 85.0,
            "feedback": "Mock agent evaluation passed",
        }

    def _evaluate_human(
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """人工审核型评估器的 Mock 实现。

        在当前阶段（M5b），仅返回 Mock 输出。
        真实实现需要通过 human_interaction 工具等待人工审核。

        Args:
            metric_def: 指标定义
            params: 合并后的输入参数

        Returns:
            Mock 评估输出
        """
        logger.info(
            "Mock human evaluation: %s (evaluator_id=%s)",
            metric_def.id, metric_def.evaluator_id,
        )
        # Mock: 返回审核通过结果
        return {
            "passed": True,
            "score": 100.0,
            "feedback": "Mock human review passed",
        }
