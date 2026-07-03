"""
评估结果查询 API

提供查询评估结果的接口

注意：评估指标已迁移到文件存储，统计功能已移除。
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.evaluation.metric_loader import get_metric_loader
from src.tools.evaluators.result_storage import EvaluationResultStorage

logger = logging.getLogger(__name__)


class EvaluationResultQuery:
    """
    评估结果查询 API

    提供查询评估结果的各种方法
    """

    def __init__(self, session: AsyncSession):
        """
        初始化查询 API

        Args:
            session: 数据库会话
        """
        self.session = session
        self.storage = EvaluationResultStorage(session)
        self.metric_loader = get_metric_loader()

    async def get_metric_stats(
        self,
        metric_name: str,
    ) -> dict[str, Any] | None:
        """
        获取指标统计信息

        注意：统计功能已移除，返回基本信息。

        Args:
            metric_name: 指标名称

        Returns:
            基本指标信息
        """
        return await self.storage.query_metric_stats(metric_name)

    async def get_metric_details(
        self,
        metric_name: str,
    ) -> dict[str, Any] | None:
        """
        获取指标详细信息

        Args:
            metric_name: 指标名称

        Returns:
            指标详细信息字典
        """
        return await self.storage.get_metric_details(metric_name)

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
        return await self.storage.list_popular_metrics(limit=limit)

    async def list_metrics_by_category(
        self,
        category: str,
    ) -> list[dict[str, Any]]:
        """
        按分类列出指标

        Args:
            category: 分类名称

        Returns:
            指标列表
        """
        try:
            metrics = await self.metric_loader.list_metrics(category=category)

            return [
                {
                    "id": m.get("id"),
                    "name": m.get("name", ""),
                    "description": m.get("description", ""),
                    "category": m.get("category", ""),
                    "evaluator_type": m.get("evaluator_type", "tool"),
                    "evaluator_id": m.get("evaluator_id", ""),
                    "usage_count": 0,
                    "success_count": 0,
                    "success_rate": 0,
                }
                for m in metrics
            ]

        except Exception as e:
            logger.error(f"列出分类指标失败: {e}")
            return []

    async def search_metrics(
        self,
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        搜索指标

        注意：搜索功能简化为名称匹配。

        Args:
            keyword: 搜索关键词
            limit: 返回数量限制

        Returns:
            指标列表
        """
        try:
            # 获取所有指标，然后进行名称匹配
            all_metrics = await self.metric_loader.list_metrics(limit=1000)

            # 简单的名称匹配
            keyword_lower = keyword.lower()
            matched = [
                m
                for m in all_metrics
                if keyword_lower in m.get("name", "").lower() or keyword_lower in m.get("description", "").lower()
            ]

            return [
                {
                    "id": m.get("id"),
                    "name": m.get("name", ""),
                    "description": m.get("description", ""),
                    "category": m.get("category", ""),
                    "evaluator_type": m.get("evaluator_type", "tool"),
                    "evaluator_id": m.get("evaluator_id", ""),
                    "usage_count": 0,
                    "success_count": 0,
                    "success_rate": 0,
                }
                for m in matched[:limit]
            ]

        except Exception as e:
            logger.error(f"搜索指标失败: {e}")
            return []

    async def get_all_categories(
        self,
    ) -> list[str]:
        """
        获取所有指标分类

        Returns:
            分类列表
        """
        try:
            return await self.metric_loader.get_categories()

        except Exception as e:
            logger.error(f"获取分类列表失败: {e}")
            return []

    async def compare_metrics(
        self,
        metric_names: list[str],
    ) -> dict[str, dict[str, Any]]:
        """
        比较多个指标的统计信息

        Args:
            metric_names: 指标名称列表

        Returns:
            指标统计信息字典
        """
        comparison = {}

        for metric_name in metric_names:
            stats = await self.get_metric_stats(metric_name)
            if stats:
                comparison[metric_name] = stats

        return comparison

    async def get_metrics_summary(
        self,
    ) -> dict[str, Any]:
        """
        获取所有指标的汇总信息

        注意：统计功能已移除，返回基本信息。

        Returns:
            汇总信息字典
        """
        try:
            # 获取所有活跃指标
            metrics = await self.metric_loader.list_metrics(limit=1000)

            total_metrics = len(metrics)

            # 分类统计
            category_breakdown: dict[str, dict[str, Any]] = {}
            for metric in metrics:
                category = metric.get("category", "unknown")
                if category not in category_breakdown:
                    category_breakdown[category] = {
                        "count": 0,
                    }

                category_breakdown[category]["count"] += 1

            return {
                "total_metrics": total_metrics,
                "total_usage": 0,
                "total_success": 0,
                "overall_success_rate": 0,
                "category_breakdown": category_breakdown,
            }

        except Exception as e:
            logger.error(f"获取指标汇总失败: {e}")
            return {}
