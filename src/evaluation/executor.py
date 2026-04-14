"""评估执行器 — 执行评估 + 结果收集 + 任务状态回写。

EvaluationExecutor 是评估系统的顶层编排器，职责：
1. 接收任务完成信号
2. 创建 EvaluationEngine 执行评估
3. 使用 ResultMapper 映射评估结果
4. 通过 TaskService 回写任务状态

这是评估系统与任务系统的集成点。

用法：
    executor = EvaluationExecutor(task_service=svc)
    result = executor.run_evaluation(task_id="abc123", metric_ids=["code_check"])
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
    ) -> None:
        """初始化评估执行器。

        Args:
            task_service: 任务服务实例（可选），提供 complete_evaluation 方法
            loader: 指标加载器，None 时创建默认实例并加载所有指标
            engine: 评估引擎，None 时根据 loader 创建默认实例
            mapper: 结果映射器，None 时创建默认实例
        """
        self._task_service = task_service
        self._loader = loader or MetricLoader()
        self._engine = engine or EvaluationEngine(loader=self._loader)
        self._mapper = mapper or ResultMapper()

    def run_evaluation(
        self,
        task_id: str,
        metric_ids: list[str] | None = None,
        input_params: dict[str, dict[str, Any]] | None = None,
        fail_fast: bool = False,
    ) -> EvaluationResult:
        """执行评估并可选回写任务状态。

        流程：
        1. 确保指标已加载
        2. 构建评估配置
        3. 调用评估引擎执行评估
        4. 映射评估结果
        5. 通过 TaskService 回写状态（如果注入了 task_service）

        Args:
            task_id: 任务 ID
            metric_ids: 要评估的指标 ID 列表，None 表示评估所有已加载指标
            input_params: 各指标的输入参数
            fail_fast: 是否在首个指标失败时停止

        Returns:
            评估结果
        """
        # 确保指标已加载
        if not self._loader.metrics:
            self._loader.load_all()

        # 构建评估配置
        config = EvaluationConfig(
            metric_ids=metric_ids or [],
            input_params=input_params or {},
            fail_fast=fail_fast,
        )

        # 执行评估
        result = self._engine.evaluate(task_id=task_id, config=config)

        # 映射结果并回写任务状态
        overall_passed = self._mapper.map_to_task_status(result)

        if self._task_service is not None:
            try:
                self._task_service.complete_evaluation(task_id, overall_passed)
                logger.info(
                    "Task %s evaluation completed: %s",
                    task_id,
                    "passed" if overall_passed else "failed",
                )
            except Exception as e:
                logger.error(
                    "Failed to update task %s status: %s", task_id, e
                )
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
