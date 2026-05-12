"""
评估指标仓储

提供 EvaluationMetric 的 CRUD 操作和查询功能
"""

import uuid
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import EvaluationMetric
from src.db.repositories.base import BaseRepository


class EvaluationMetricRepository(BaseRepository[EvaluationMetric]):
    """评估指标仓储"""

    def __init__(self, session: AsyncSession):
        super().__init__(session, EvaluationMetric)

    async def create_metric(self, metric_data: dict[str, Any]) -> EvaluationMetric:
        """
        创建评估指标

        Args:
            metric_data: 指标数据字典，包含:
                - name: 指标名称（唯一）
                - description: 指标描述
                - category: 指标分类
                - evaluator_type: 评估器类型
                - evaluator_id: 评估器 ID
                - default_config: 默认配置（可选）
                - input_schema: 输入 Schema（可选）
                - source: 来源（可选）
                - status: 状态（可选）
                - tags: 标签（可选）

        Returns:
            创建的指标对象
        """
        metric_id = metric_data.get("id", str(uuid.uuid4()))

        metric = EvaluationMetric(
            id=metric_id,
            name=metric_data["name"],
            description=metric_data["description"],
            category=metric_data["category"],
            evaluator_type=metric_data["evaluator_type"],
            evaluator_id=metric_data["evaluator_id"],
            default_config=metric_data.get("default_config", {}),
            input_schema=metric_data.get("input_schema", {}),
            source=metric_data.get("source", "custom"),
            status=metric_data.get("status", "active"),
            usage_count=0,
            success_count=0,
            avg_execution_time=None,
            tags=metric_data.get("tags", []),
        )

        self.session.add(metric)
        await self.session.flush()
        await self.session.refresh(metric)

        return metric

    async def get_metric(self, metric_id: str) -> EvaluationMetric | None:
        """
        根据 ID 获取评估指标

        Args:
            metric_id: 指标 ID

        Returns:
            指标对象，不存在返回 None
        """
        return await self.get(metric_id)

    async def get_metric_by_name(self, name: str) -> EvaluationMetric | None:
        """
        根据名称获取评估指标

        Args:
            name: 指标名称

        Returns:
            指标对象，不存在返回 None
        """
        query = select(EvaluationMetric).where(EvaluationMetric.name == name)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_metrics_by_ids(self, metric_ids: list[str]) -> list[EvaluationMetric]:
        """
        根据 ID 列表获取评估指标

        Args:
            metric_ids: 指标 ID 列表

        Returns:
            指标对象列表
        """
        if not metric_ids:
            return []

        query = (
            select(EvaluationMetric)
            .where(EvaluationMetric.id.in_(metric_ids))
            .order_by(EvaluationMetric.name)
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def list_metrics(
        self,
        category: str | None = None,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvaluationMetric]:
        """
        列出评估指标

        Args:
            category: 指标分类（可选）
            status: 状态（默认 active）
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            指标对象列表
        """
        query = select(EvaluationMetric).where(EvaluationMetric.status == status)

        if category:
            query = query.where(EvaluationMetric.category == category)

        query = (
            query.order_by(EvaluationMetric.category, EvaluationMetric.name)
            .limit(limit)
            .offset(offset)
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_metric(self, metric_id: str, metric_data: dict[str, Any]) -> bool:
        """
        更新评估指标

        Args:
            metric_id: 指标 ID
            metric_data: 更新数据

        Returns:
            是否更新成功
        """
        # 允许更新的字段
        allowed_fields = {
            "description",
            "category",
            "evaluator_type",
            "evaluator_id",
            "default_config",
            "input_schema",
            "status",
            "tags",
        }

        update_data = {k: v for k, v in metric_data.items() if k in allowed_fields}

        if not update_data:
            return False

        query = (
            update(EvaluationMetric)
            .where(EvaluationMetric.id == metric_id)
            .values(**update_data)
        )

        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

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
        # 获取当前统计
        metric = await self.get(metric_id)
        if not metric:
            return

        # 更新统计
        new_usage_count = metric.usage_count + 1
        new_success_count = metric.success_count + (1 if success else 0)

        # 计算新的平均执行时间
        if metric.avg_execution_time is not None:
            new_avg = (
                metric.avg_execution_time * metric.usage_count + execution_time_ms
            ) / new_usage_count
        else:
            new_avg = execution_time_ms

        query = (
            update(EvaluationMetric)
            .where(EvaluationMetric.id == metric_id)
            .values(
                usage_count=new_usage_count,
                success_count=new_success_count,
                avg_execution_time=new_avg,
            )
        )

        await self.session.execute(query)
        await self.session.flush()

    async def get_metrics_by_category(self, category: str) -> list[EvaluationMetric]:
        """
        按分类获取指标

        Args:
            category: 分类名称

        Returns:
            指标列表
        """
        query = (
            select(EvaluationMetric)
            .where(
                and_(
                    EvaluationMetric.category == category,
                    EvaluationMetric.status == "active",
                )
            )
            .order_by(EvaluationMetric.name)
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_metrics_by_evaluator(
        self, evaluator_type: str, evaluator_id: str | None = None
    ) -> list[EvaluationMetric]:
        """
        按评估器获取指标

        Args:
            evaluator_type: 评估器类型
            evaluator_id: 评估器 ID（可选）

        Returns:
            指标列表
        """
        query = select(EvaluationMetric).where(
            and_(
                EvaluationMetric.evaluator_type == evaluator_type,
                EvaluationMetric.status == "active",
            )
        )

        if evaluator_id:
            query = query.where(EvaluationMetric.evaluator_id == evaluator_id)

        query = query.order_by(EvaluationMetric.name)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def search_metrics(
        self, keyword: str, limit: int = 20
    ) -> list[EvaluationMetric]:
        """
        搜索指标（按名称或描述）

        Args:
            keyword: 搜索关键词
            limit: 返回数量限制

        Returns:
            指标列表
        """
        query = (
            select(EvaluationMetric)
            .where(
                and_(
                    EvaluationMetric.status == "active",
                    or_(
                        EvaluationMetric.name.ilike(f"%{keyword}%"),
                        EvaluationMetric.description.ilike(f"%{keyword}%"),
                    ),
                )
            )
            .order_by(EvaluationMetric.usage_count.desc())
            .limit(limit)
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_categories(self) -> list[str]:
        """
        获取所有指标分类

        Returns:
            分类列表
        """
        query = (
            select(EvaluationMetric.category)
            .where(EvaluationMetric.status == "active")
            .distinct()
            .order_by(EvaluationMetric.category)
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_popular_metrics(self, limit: int = 10) -> list[EvaluationMetric]:
        """
        获取常用指标（按使用次数排序）

        Args:
            limit: 返回数量限制

        Returns:
            指标列表
        """
        query = (
            select(EvaluationMetric)
            .where(EvaluationMetric.status == "active")
            .order_by(EvaluationMetric.usage_count.desc())
            .limit(limit)
        )

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def initialize_builtin_metrics(self) -> int:
        """
        初始化内置评估指标

        如果内置指标不存在则创建，存在则跳过。
        使用批量查询优化性能，避免 N+1 查询问题。

        Returns:
            创建的指标数量
        """
        # 内置指标定义
        builtin_metrics = [
            {
                "name": "file_exists",
                "description": "检查文件是否存在",
                "category": "file",
                "evaluator_type": "tool",
                "evaluator_id": "file_checker",
                "default_config": {"check_type": "exists"},
                "source": "builtin",
            },
            {
                "name": "schema_validation",
                "description": "验证数据结构是否符合 Schema",
                "category": "schema",
                "evaluator_type": "tool",
                "evaluator_id": "schema_validator",
                "default_config": {"strict": True},
                "source": "builtin",
            },
            {
                "name": "unit_test_pass",
                "description": "运行单元测试并检查是否通过",
                "category": "test",
                "evaluator_type": "tool",
                "evaluator_id": "test_runner",
                "default_config": {"test_type": "unit"},
                "source": "builtin",
            },
            {
                "name": "code_quality",
                "description": "检查代码质量（复杂度、规范等）",
                "category": "code",
                "evaluator_type": "tool",
                "evaluator_id": "code_linter",
                "default_config": {"rules": ["pep8", "complexity"]},
                "source": "builtin",
            },
            {
                "name": "api_response",
                "description": "测试 API 接口响应",
                "category": "api",
                "evaluator_type": "tool",
                "evaluator_id": "api_tester",
                "default_config": {"check_status": True, "check_schema": True},
                "source": "builtin",
            },
            {
                "name": "performance_benchmark",
                "description": "性能基准测试",
                "category": "performance",
                "evaluator_type": "tool",
                "evaluator_id": "benchmark_runner",
                "default_config": {"metrics": ["response_time", "throughput"]},
                "source": "builtin",
            },
            {
                "name": "semantic_similarity",
                "description": "语义相似度评估",
                "category": "semantic",
                "evaluator_type": "tool",
                "evaluator_id": "embedder",
                "default_config": {"threshold": 0.8},
                "source": "builtin",
            },
        ]

        # 批量查询已存在的指标，避免 N+1 查询问题
        metric_names = [m["name"] for m in builtin_metrics]
        query = select(EvaluationMetric.name).where(
            EvaluationMetric.name.in_(metric_names)
        )
        result = await self.session.execute(query)
        existing_names = set(result.scalars().all())

        created_count = 0

        for metric_def in builtin_metrics:
            # 检查是否已存在（使用内存集合，避免数据库查询）
            if metric_def["name"] in existing_names:
                continue

            # 创建新指标
            await self.create_metric(metric_def)
            created_count += 1

        await self.session.flush()
        return created_count

    async def delete_metric(self, metric_id: str) -> bool:
        """
        删除评估指标（软删除，设置状态为 deprecated）

        Args:
            metric_id: 指标 ID

        Returns:
            是否删除成功
        """
        query = (
            update(EvaluationMetric)
            .where(EvaluationMetric.id == metric_id)
            .values(status="deprecated")
        )

        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

    async def get_metric_usage_stats(self, metric_id: str) -> dict[str, Any] | None:
        """
        获取指标使用统计

        注意：TaskMetric 表已删除，现在只使用 EvaluationMetric 表中的统计信息。

        Args:
            metric_id: 指标 ID

        Returns:
            统计信息字典
        """
        metric = await self.get(metric_id)
        if not metric:
            return None

        return {
            "metric_id": metric_id,
            "metric_name": metric.name,
            "usage_count": metric.usage_count,
            "success_count": metric.success_count,
            "success_rate": (
                (metric.success_count / metric.usage_count * 100)
                if metric.usage_count > 0
                else 0
            ),
            "avg_execution_time": metric.avg_execution_time,
        }
