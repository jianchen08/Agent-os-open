"""评估执行器 — 执行评估 + 结果收集 + 任务状态回写。

EvaluationExecutor 是评估系统的顶层编排器，职责：
1. 接收任务完成信号
2. 创建 EvaluationEngine 执行评估
3. 使用 ResultMapper 映射评估结果
4. 通过 TaskService 回写任务状态

这是评估系统与任务系统的集成点。

用法：
    executor = EvaluationExecutor(task_service=svc)
    result = executor.run_evaluation(task_id="abc123", metric_ids=["format_valid"])
"""

from __future__ import annotations

import logging
from typing import Any

from evaluation.engine import EvaluationEngine
from evaluation.loader import MetricLoader
from evaluation.mapper import ResultMapper
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    sanitize_eval_paths,
)

logger = logging.getLogger(__name__)


class EvaluationExecutor:
    """评估执行器。

    编排评估引擎、结果映射器和任务服务，完成从评估触发到状态回写的完整流程。

    Attributes:
        _task_service: 任务服务实例（可选，用于状态回写）
        _engine: 评估引擎实例
        _mapper: 结果映射器实例
        _loader: 指标加载器实例
    """

    def __init__(
        self,
        task_service: Any = None,
        loader: MetricLoader | None = None,
        engine: EvaluationEngine | None = None,
        mapper: ResultMapper | None = None,
        tool_registry: Any = None,
        agent_registry: Any = None,
        main_loop: Any = None,
    ) -> None:
        """初始化评估执行器。

        Args:
            task_service: 任务服务实例（可选），提供 complete_evaluation 方法
            loader: 指标加载器，None 时创建默认实例并加载所有指标
            engine: 评估引擎，None 时根据 loader 和其他参数创建默认实例
            mapper: 结果映射器，None 时创建默认实例
            tool_registry: 工具注册表，传递给 EvaluationEngine 用于真实工具调用
            agent_registry: AgentRegistry 实例，传递给 EvaluationEngine
            main_loop: 主事件循环引用，传递给 EvaluationEngine
        """
        self._task_service = task_service
        self._loader = loader or MetricLoader()
        self._engine = engine or EvaluationEngine(
            loader=self._loader,
            tool_registry=tool_registry,
            agent_registry=agent_registry,
            main_loop=main_loop,
        )
        self._mapper = mapper or ResultMapper()

    async def run_evaluation(
        self,
        task_id: str,
        metric_ids: list[str] | None = None,
        input_params: dict[str, dict[str, Any]] | None = None,
        fail_fast: bool = True,
        skip_state_update: bool = False,
    ) -> EvaluationResult:
        """执行评估并可选回写任务状态。

        流程：
        1. 确保指标已加载
        2. 构建评估配置
        3. 调用评估引擎执行评估
        4. 映射评估结果
        5. 通过 TaskService 回写状态（如果注入了 task_service 且 skip_state_update=False）

        Args:
            task_id: 任务 ID
            metric_ids: 要评估的指标 ID 列表，None 表示评估所有已加载指标
            input_params: 各指标的输入参数
            fail_fast: 是否在首个指标失败时停止
            skip_state_update: 是否跳过任务状态回写（由调用方自行管理状态）

        Returns:
            评估结果
        """
        if not self._loader.metrics:
            self._loader.load_all()

        config = EvaluationConfig(
            metric_ids=metric_ids or [],
            input_params=input_params or {},
            fail_fast=fail_fast,
        )

        result = await self._engine.evaluate(task_id=task_id, config=config)

        overall_passed = self._mapper.map_to_task_status(result)

        if not skip_state_update and self._task_service is not None:
            try:
                eval_data = {
                    "overall_passed": overall_passed,
                    "summary": result.summary or self._mapper.build_summary(result),
                    "metrics": [
                        {
                            "metric_id": r.metric_id,
                            "passed": r.passed,
                            "score": r.score,
                            "message": r.message,
                            "error": r.error,
                            "evaluator_input": sanitize_eval_paths(r.evaluator_input),
                            "evaluator_output": sanitize_eval_paths(r.evaluator_output),
                            "pipeline_run_id": r.pipeline_run_id,
                        }
                        for r in result.results
                    ],
                }
                await self._task_service.complete_evaluation(
                    task_id,
                    overall_passed,
                    result=eval_data,
                )
                logger.info(
                    "Task %s evaluation completed: %s",
                    task_id,
                    "passed" if overall_passed else "failed",
                )
            except Exception as e:
                logger.error("Failed to update task %s status: %s", task_id, e)
                result.summary += f" [状态回写失败: {e}]"

        # 构建摘要
        if not result.summary:
            result.summary = self._mapper.build_summary(result)

        return result

    def get_summary(self, result: EvaluationResult) -> str:
        """获取评估结果的可读摘要。

        Args:
            result: 评估结果

        Returns:
            人类可读的摘要字符串
        """
        return self._mapper.build_summary(result)
