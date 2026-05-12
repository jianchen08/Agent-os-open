"""
评估指标服务

提供评估指标的 CRUD 操作和业务逻辑

支持可复用的评估指标管理：
- 创建和管理评估指标
- 按分类、状态查询指标
- 更新指标使用统计
- 初始化内置指标
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.repositories.evaluation_metric_repo import EvaluationMetricRepository
from src.db.repositories.task_repo import TaskRepository

logger = logging.getLogger(__name__)


class EvaluationMetricService:
    """评估指标服务类

    提供评估指标的 CRUD 操作和业务逻辑。
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.metric_repo = EvaluationMetricRepository(session)
        self.task_repo = TaskRepository(session)

    async def create_metric(self, metric_data: dict[str, Any]) -> dict[str, Any]:
        """
        创建评估指标

        Args:
            metric_data: 指标数据字典，包含:
                - name: 指标名称（唯一）
                - description: 指标描述
                - category: 指标分类 (file/schema/test/code/api/performance/semantic/human)
                - evaluator_type: 评估器类型 (tool/workflow/human)
                - evaluator_id: 评估器 ID
                - default_config: 默认配置（可选）
                - input_schema: 输入 Schema（可选）
                - source: 来源（可选，默认 custom）
                - status: 状态（可选，默认 active）
                - tags: 标签（可选）

        Returns:
            创建的指标信息字典
        """
        # 检查名称是否已存在
        existing = await self.metric_repo.get_metric_by_name(metric_data["name"])
        if existing:
            raise ValueError(f"评估指标名称已存在: {metric_data['name']}")

        # 创建指标
        metric = await self.metric_repo.create_metric(metric_data)

        logger.info(
            f"创建评估指标成功 | metric_id={metric.id} | name={metric.name} | "
            f"category={metric.category}"
        )

        return {
            "id": metric.id,
            "name": metric.name,
            "description": metric.description,
            "category": metric.category,
            "evaluator_type": metric.evaluator_type,
            "evaluator_id": metric.evaluator_id,
            "default_config": metric.default_config,
            "input_schema": metric.input_schema,
            "source": metric.source,
            "status": metric.status,
            "tags": metric.tags,
            "includes": metric.includes,
            "requires": metric.requires,
            "level": metric.level,
            "usage_count": metric.usage_count,
            "success_count": metric.success_count,
            "created_at": metric.created_at.isoformat() if metric.created_at else "",
        }

    async def get_metric(self, metric_id: str) -> dict[str, Any] | None:
        """
        获取评估指标

        Args:
            metric_id: 指标 ID

        Returns:
            指标信息字典，不存在返回 None
        """
        metric = await self.metric_repo.get_metric(metric_id)

        if not metric:
            return None

        return {
            "id": metric.id,
            "name": metric.name,
            "description": metric.description,
            "category": metric.category,
            "evaluator_type": metric.evaluator_type,
            "evaluator_id": metric.evaluator_id,
            "default_config": metric.default_config,
            "input_schema": metric.input_schema,
            "source": metric.source,
            "status": metric.status,
            "tags": metric.tags,
            "includes": metric.includes,
            "requires": metric.requires,
            "level": metric.level,
            "usage_count": metric.usage_count,
            "success_count": metric.success_count,
            "avg_execution_time": metric.avg_execution_time,
            "created_at": metric.created_at.isoformat() if metric.created_at else "",
            "updated_at": metric.updated_at.isoformat() if metric.updated_at else None,
        }

    async def get_metric_by_name(self, name: str) -> dict[str, Any] | None:
        """
        按名称获取评估指标

        Args:
            name: 指标名称

        Returns:
            指标信息字典，不存在返回 None
        """
        metric = await self.metric_repo.get_metric_by_name(name)

        if not metric:
            return None

        return {
            "id": metric.id,
            "name": metric.name,
            "description": metric.description,
            "category": metric.category,
            "evaluator_type": metric.evaluator_type,
            "evaluator_id": metric.evaluator_id,
            "default_config": metric.default_config,
            "input_schema": metric.input_schema,
            "source": metric.source,
            "status": metric.status,
            "tags": metric.tags,
            "includes": metric.includes,
            "requires": metric.requires,
            "level": metric.level,
            "usage_count": metric.usage_count,
            "success_count": metric.success_count,
            "avg_execution_time": metric.avg_execution_time,
            "created_at": metric.created_at.isoformat() if metric.created_at else "",
            "updated_at": metric.updated_at.isoformat() if metric.updated_at else None,
        }

    async def list_metrics(
        self,
        category: str | None = None,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        列出评估指标

        Args:
            category: 指标分类（可选）
            status: 状态（默认 active）
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            指标信息字典列表
        """
        metrics = await self.metric_repo.list_metrics(
            category=category, status=status, limit=limit, offset=offset
        )

        return [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "category": m.category,
                "evaluator_type": m.evaluator_type,
                "evaluator_id": m.evaluator_id,
                "source": m.source,
                "status": m.status,
                "tags": m.tags,
                "includes": m.includes,
                "requires": m.requires,
                "level": m.level,
                "usage_count": m.usage_count,
                "success_count": m.success_count,
                "created_at": m.created_at.isoformat() if m.created_at else "",
                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            }
            for m in metrics
        ]

    async def update_metric_stats(
        self, metric_id: str, success: bool, execution_time_ms: float
    ) -> None:
        """
        更新指标使用统计

        Args:
            metric_id: 指标 ID
            success: 是否成功
            execution_time_ms: 执行时间（毫秒）
        """
        await self.metric_repo.update_metric_stats(
            metric_id=metric_id, success=success, execution_time_ms=execution_time_ms
        )

        logger.debug(
            f"更新指标统计 | metric_id={metric_id} | success={success} | "
            f"execution_time_ms={execution_time_ms}"
        )

    async def initialize_builtin_metrics(self) -> int:
        """
        初始化内置评估指标

        如果内置指标不存在则创建，存在则跳过。

        Returns:
            创建的指标数量
        """
        created_count = await self.metric_repo.initialize_builtin_metrics()

        logger.info(f"初始化内置评估指标完成 | 创建数量={created_count}")

        return created_count

    async def get_metrics_for_task(self, task_id: str) -> list[dict[str, Any]]:
        """
        获取任务的评估指标

        Args:
            task_id: 任务 ID

        Returns:
            指标信息字典列表
        """
        # 获取任务
        task = await self.task_repo.get(task_id)
        if not task:
            return []

        # 获取评估指标
        metric_ids = task.evaluation_metric_ids or []
        if not metric_ids:
            return []

        metrics = await self.metric_repo.get_metrics_by_ids(metric_ids)

        return [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "category": m.category,
                "evaluator_type": m.evaluator_type,
                "evaluator_id": m.evaluator_id,
                "default_config": m.default_config,
                "input_schema": m.input_schema,
                "includes": m.includes,
                "requires": m.requires,
                "level": m.level,
            }
            for m in metrics
        ]

    async def get_metric_usage_stats(self, metric_id: str) -> dict[str, Any] | None:
        """
        获取指标使用统计

        Args:
            metric_id: 指标 ID

        Returns:
            统计信息字典，不存在返回 None
        """
        return await self.metric_repo.get_metric_usage_stats(metric_id)

    async def search_metrics(
        self, keyword: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """
        搜索指标（按名称或描述）

        Args:
            keyword: 搜索关键词
            limit: 返回数量限制

        Returns:
            指标信息字典列表
        """
        metrics = await self.metric_repo.search_metrics(keyword=keyword, limit=limit)

        return [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "category": m.category,
                "evaluator_type": m.evaluator_type,
                "evaluator_id": m.evaluator_id,
                "includes": m.includes,
                "requires": m.requires,
                "level": m.level,
                "usage_count": m.usage_count,
                "source": m.source,
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            }
            for m in metrics
        ]

    async def get_categories(self) -> list[str]:
        """
        获取所有指标分类

        Returns:
            分类列表
        """
        return await self.metric_repo.get_categories()

    async def get_popular_metrics(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        获取常用指标（按使用次数排序）

        Args:
            limit: 返回数量限制

        Returns:
            指标信息字典列表
        """
        metrics = await self.metric_repo.get_popular_metrics(limit=limit)

        return [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "category": m.category,
                "evaluator_type": m.evaluator_type,
                "evaluator_id": m.evaluator_id,
                "includes": m.includes,
                "requires": m.requires,
                "level": m.level,
                "usage_count": m.usage_count,
                "success_count": m.success_count,
                "source": m.source,
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            }
            for m in metrics
        ]

    async def update_metric(
        self, metric_id: str, metric_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        更新评估指标

        Args:
            metric_id: 指标 ID
            metric_data: 更新数据

        Returns:
            更新后的指标信息，失败返回 None
        """
        success = await self.metric_repo.update_metric(metric_id, metric_data)

        if not success:
            return None

        updated_metric = await self.metric_repo.get_metric(metric_id)

        return {
            "id": updated_metric.id,
            "name": updated_metric.name,
            "description": updated_metric.description,
            "category": updated_metric.category,
            "evaluator_type": updated_metric.evaluator_type,
            "evaluator_id": updated_metric.evaluator_id,
            "default_config": updated_metric.default_config,
            "input_schema": updated_metric.input_schema,
            "status": updated_metric.status,
            "tags": updated_metric.tags,
            "includes": updated_metric.includes,
            "requires": updated_metric.requires,
            "level": updated_metric.level,
            "updated_at": updated_metric.updated_at.isoformat() if updated_metric.updated_at else None,
        }

    async def delete_metric(self, metric_id: str) -> bool:
        """
        删除评估指标（软删除，设置状态为 deprecated）

        Args:
            metric_id: 指标 ID

        Returns:
            是否删除成功
        """
        return await self.metric_repo.delete_metric(metric_id)
