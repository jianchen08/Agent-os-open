"""
评估结果存储层

负责将评估结果存储到数据库

注意：评估指标已迁移到文件存储，统计功能已移除。
此模块保留用于兼容性，但不再更新指标统计。
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.evaluation.metric_loader import get_metric_loader
from src.tools.evaluators.result_wrapper import EvaluationResult, EvaluationSummary

logger = logging.getLogger(__name__)


class EvaluationResultStorage:
    """
    评估结果存储层

    注意：评估指标已迁移到文件存储，统计功能已移除。
    此类保留用于兼容性，但不再更新指标统计。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化存储层

        Args:
            session: 数据库会话
        """
        self.session = session
        self.metric_loader = get_metric_loader()

    async def store_result(
        self,
        result: EvaluationResult,
        task_id: str | None = None,
        execution_id: str | None = None,
    ) -> str:
        """
        存储单个评估结果

        注意：评估指标已迁移到文件存储，不再更新统计信息。

        Args:
            result: 评估结果
            task_id: 任务 ID（可选）
            execution_id: 执行 ID（可选）

        Returns:
            指标 ID（如果找到）
        """
        try:
            # 获取指标信息（只读）
            metric = await self.metric_loader.get_metric_by_name(result.metric_name)

            if metric:
                logger.info(f"评估结果记录: {result.metric_name} (passed={result.passed}, score={result.score})")
                return metric.get("id", "")
            logger.warning(f"评估指标不存在: {result.metric_name}")
            return ""

        except Exception as e:
            logger.error(f"记录评估结果失败: {e}")
            raise

    async def store_summary(
        self,
        summary: EvaluationSummary,
        task_id: str | None = None,
        execution_id: str | None = None,
    ) -> list[str]:
        """
        存储评估摘要

        Args:
            summary: 评估摘要
            task_id: 任务 ID（可选）
            execution_id: 执行 ID（可选）

        Returns:
            存储记录 ID 列表
        """
        stored_ids = []

        for result in summary.results:
            record_id = await self.store_result(
                result=result,
                task_id=task_id,
                execution_id=execution_id,
            )
            if record_id:
                stored_ids.append(record_id)

        logger.info(f"已记录 {len(stored_ids)} 个评估结果")

        return stored_ids

    async def query_metric_stats(
        self,
        metric_name: str,
    ) -> dict[str, Any] | None:
        """
        查询指标统计信息

        注意：统计功能已移除，返回基本信息。

        Args:
            metric_name: 指标名称

        Returns:
            基本指标信息
        """
        try:
            metric = await self.metric_loader.get_metric_by_name(metric_name)

            if not metric:
                return None

            return {
                "metric_id": metric.get("id"),
                "metric_name": metric.get("name", ""),
                "usage_count": 0,
                "success_count": 0,
                "success_rate": 0,
                "avg_execution_time": None,
            }

        except Exception as e:
            logger.error(f"查询指标统计失败: {e}")
            return None

    async def query_metric_history(
        self,
        metric_name: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        查询指标历史记录

        注意：当前版本只返回基本信息，不存储详细历史。

        Args:
            metric_name: 指标名称
            limit: 返回数量限制

        Returns:
            历史记录列表
        """
        # 当前版本：返回基本信息
        stats = await self.query_metric_stats(metric_name)

        if not stats:
            return []

        return [stats]

    async def list_popular_metrics(
        self,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        列出常用指标

        注意：统计功能已移除，返回所有活跃指标。

        Args:
            limit: 返回数量限制

        Returns:
            指标列表
        """
        try:
            metrics = await self.metric_loader.list_metrics(limit=limit)

            return [
                {
                    "id": m.get("id"),
                    "name": m.get("name", ""),
                    "description": m.get("description", ""),
                    "category": m.get("category", ""),
                    "usage_count": 0,
                    "success_count": 0,
                    "success_rate": 0,
                    "avg_execution_time": None,
                }
                for m in metrics
            ]

        except Exception as e:
            logger.error(f"列出常用指标失败: {e}")
            return []

    async def get_metric_details(
        self,
        metric_name: str,
    ) -> dict[str, Any] | None:
        """
        获取指标详细信息

        Args:
            metric_name: 指标名称

        Returns:
            指标详细信息
        """
        try:
            metric = await self.metric_loader.get_metric_by_name(metric_name)

            if not metric:
                return None

            return {
                "id": metric.get("id"),
                "name": metric.get("name", ""),
                "description": metric.get("description", ""),
                "category": metric.get("category", ""),
                "evaluator_type": metric.get("evaluator_type", "tool"),
                "evaluator_id": metric.get("evaluator_id", ""),
                "default_config": metric.get("default_config", {}),
                "input_schema": metric.get("input_schema", {}),
                "default_pass_threshold": metric.get("default_pass_threshold"),
                "is_red_line": metric.get("is_red_line", False),
                "default_weight": metric.get("default_weight", 1.0),
                "source": metric.get("source", "builtin"),
                "status": metric.get("status", "active"),
                "usage_count": 0,
                "success_count": 0,
                "success_rate": 0,
                "avg_execution_time": None,
                "tags": metric.get("tags", []),
                "when_to_use": metric.get("when_to_use", []),
                "when_not_to_use": metric.get("when_not_to_use", []),
                "examples": metric.get("examples", []),
                "caveats": metric.get("caveats", []),
            }

        except Exception as e:
            logger.error(f"获取指标详情失败: {e}")
            return None


async def create_storage_backend(
    session: AsyncSession,
) -> callable:
    """
    创建存储后端函数

    这个函数可以被用作 ResultPropagator 的存储后端。

    Args:
        session: 数据库会话

    Returns:
        存储后端函数
    """
    storage = EvaluationResultStorage(session)

    async def backend(result: EvaluationResult) -> str:
        """存储后端函数"""
        return await storage.store_result(result)

    return backend
