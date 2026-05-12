"""
执行经验记录服务

负责记录和管理执行经验，支持：
1. 记录每次执行的详细经验
2. 同步更新执行单元统计
3. 查询相似场景的历史经验
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    Agent,
    ExecutionExperience,
    ExecutionUnit,
    ToolLibrary,
    Workflow,
    WorkflowComposition,
)


class ExperienceService:
    """
    执行经验服务

    管理执行经验的记录、查询和统计
    """

    def __init__(self, session: AsyncSession):
        """
        初始化服务

        Args:
            session: 数据库会话
        """
        self.session = session

    async def get_or_create_unit(
        self,
        unit_type: str,
        ref_id: str,
        name: str = None,
        description: str = None,
        created_by: str = None,
    ) -> ExecutionUnit:
        """
        获取或创建执行单元

        如果已存在则返回，否则创建新的

        Args:
            unit_type: 单元类型（tool/agent/workflow）
            ref_id: 原始定义 ID
            name: 名称
            description: 描述
            created_by: 创建者

        Returns:
            执行单元对象
        """
        # 查找现有单元
        stmt = select(ExecutionUnit).where(
            and_(
                ExecutionUnit.unit_type == unit_type,
                ExecutionUnit.ref_id == ref_id,
            )
        )
        result = await self.session.execute(stmt)
        unit = result.scalar_one_or_none()

        if unit:
            return unit

        # 如果没有提供名称，从原始表获取
        if not name:
            name, description = await self._get_ref_info(unit_type, ref_id)

        # 创建新单元
        unit = ExecutionUnit(
            id=str(uuid.uuid4()),
            unit_type=unit_type,
            ref_id=ref_id,
            name=name or f"{unit_type}_{ref_id[:8]}",
            description=description,
            created_by=created_by,
        )
        self.session.add(unit)
        await self.session.flush()

        return unit

    async def _get_ref_info(
        self, unit_type: str, ref_id: str
    ) -> tuple[str | None, str | None]:
        """从原始定义表获取名称和描述"""
        if unit_type == "tool":
            stmt = select(ToolLibrary.name, ToolLibrary.description).where(
                ToolLibrary.id == ref_id
            )
        elif unit_type == "agent":
            stmt = select(Agent.name, Agent.description).where(Agent.id == ref_id)
        elif unit_type == "workflow":
            stmt = select(Workflow.name, Workflow.description).where(
                Workflow.id == ref_id
            )
        else:
            return None, None

        result = await self.session.execute(stmt)
        row = result.first()
        if row:
            return row.name, row.description
        return None, None

    async def record_experience(
        self,
        unit_type: str,
        ref_id: str,
        user_id: str,
        status: str,
        intent_text: str = None,
        intent_vector: list[float] = None,
        input_params: dict[str, Any] = None,
        output_summary: str = None,
        error_type: str = None,
        error_message: str = None,
        score: float = None,
        duration_ms: int = None,
        token_usage: int = None,
        session_id: str = None,
        episode_id: str = None,
    ) -> ExecutionExperience:
        """
        记录执行经验

        Args:
            unit_type: 单元类型
            ref_id: 原始定义 ID
            user_id: 用户 ID
            status: 执行状态（success/failed/partial/cancelled）
            intent_text: 用户意图
            intent_vector: 意图向量
            input_params: 输入参数
            output_summary: 输出摘要
            error_type: 错误类型
            error_message: 错误信息
            score: 评分
            duration_ms: 耗时
            token_usage: Token 使用量
            session_id: 会话 ID
            episode_id: 情景记忆 ID

        Returns:
            创建的经验记录
        """
        # 获取或创建执行单元
        unit = await self.get_or_create_unit(unit_type, ref_id)

        # 创建经验记录
        experience = ExecutionExperience(
            id=str(uuid.uuid4()),
            unit_id=unit.id,
            user_id=user_id,
            session_id=session_id,
            episode_id=episode_id,
            intent_text=intent_text,
            intent_vector=intent_vector,
            input_params=input_params,
            output_summary=output_summary,
            status=status,
            error_type=error_type,
            error_message=error_message,
            score=score,
            duration_ms=duration_ms,
            token_usage=token_usage,
        )
        self.session.add(experience)

        # 更新执行单元统计
        if status == "success":
            unit.success_count += 1
        elif status == "failed":
            unit.fail_count += 1

        unit.last_used_at = datetime.now()

        # 更新平均分（增量计算）
        if score is not None:
            total = unit.success_count + unit.fail_count
            if unit.avg_score is None:
                unit.avg_score = score
            else:
                unit.avg_score = (unit.avg_score * (total - 1) + score) / total

        # 更新平均耗时（增量计算）
        if duration_ms is not None:
            total = unit.success_count + unit.fail_count
            if unit.avg_duration_ms is None:
                unit.avg_duration_ms = float(duration_ms)
            else:
                unit.avg_duration_ms = (
                    unit.avg_duration_ms * (total - 1) + duration_ms
                ) / total

        await self.session.commit()

        return experience

    async def find_similar_experiences(
        self,
        unit_id: str,
        intent_vector: list[float],
        limit: int = 5,
        min_score: float = 0.6,
    ) -> list[ExecutionExperience]:
        """
        查找相似场景的历史经验

        基于意图向量的相似度搜索

        Args:
            unit_id: 执行单元 ID
            intent_vector: 查询意图向量
            limit: 返回数量
            min_score: 最小相似度

        Returns:
            相似经验列表
        """
        # 注意：这里简化处理，实际应该使用向量数据库
        # 目前只返回该单元的成功经验
        stmt = (
            select(ExecutionExperience)
            .where(
                and_(
                    ExecutionExperience.unit_id == unit_id,
                    ExecutionExperience.status == "success",
                    ExecutionExperience.intent_vector.isnot(None),
                )
            )
            .order_by(desc(ExecutionExperience.score))
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_best_params_for_scene(
        self,
        unit_id: str,
        intent_text: str,
    ) -> dict[str, Any] | None:
        """
        获取某场景下效果最好的参数组合

        Args:
            unit_id: 执行单元 ID
            intent_text: 用户意图

        Returns:
            最佳参数组合
        """
        # 查找相似意图下评分最高的成功经验
        stmt = (
            select(ExecutionExperience.input_params)
            .where(
                and_(
                    ExecutionExperience.unit_id == unit_id,
                    ExecutionExperience.status == "success",
                    ExecutionExperience.input_params.isnot(None),
                    ExecutionExperience.score.isnot(None),
                )
            )
            .order_by(desc(ExecutionExperience.score))
            .limit(1)
        )

        result = await self.session.execute(stmt)
        row = result.first()
        return row.input_params if row else None

    async def get_common_errors(
        self,
        unit_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """
        获取常见错误类型

        Args:
            unit_id: 执行单元 ID
            limit: 返回数量

        Returns:
            错误类型统计
        """
        stmt = (
            select(
                ExecutionExperience.error_type,
                func.count(ExecutionExperience.id).label("count"),
            )
            .where(
                and_(
                    ExecutionExperience.unit_id == unit_id,
                    ExecutionExperience.status == "failed",
                    ExecutionExperience.error_type.isnot(None),
                )
            )
            .group_by(ExecutionExperience.error_type)
            .order_by(desc("count"))
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        return [
            {"error_type": row.error_type, "count": row.count} for row in result.all()
        ]

    async def register_workflow_composition(
        self,
        workflow_ref_id: str,
        child_units: list[dict[str, Any]],
    ):
        """
        注册工作流的组成单元

        Args:
            workflow_ref_id: 工作流原始 ID
            child_units: 子单元列表，每个包含：
                - unit_type: 单元类型
                - ref_id: 原始 ID
                - node_id: 节点 ID
                - sequence: 顺序
        """
        # 获取工作流的执行单元
        workflow_unit = await self.get_or_create_unit("workflow", workflow_ref_id)

        for child_info in child_units:
            # 获取子单元
            child_unit = await self.get_or_create_unit(
                child_info["unit_type"],
                child_info["ref_id"],
            )

            # 检查是否已存在
            stmt = select(WorkflowComposition).where(
                and_(
                    WorkflowComposition.workflow_unit_id == workflow_unit.id,
                    WorkflowComposition.child_unit_id == child_unit.id,
                    WorkflowComposition.node_id == child_info.get("node_id"),
                )
            )
            result = await self.session.execute(stmt)
            existing = result.scalar_one_or_none()

            if not existing:
                composition = WorkflowComposition(
                    workflow_unit_id=workflow_unit.id,
                    child_unit_id=child_unit.id,
                    node_id=child_info.get("node_id"),
                    node_type=child_info.get("unit_type"),
                    sequence=child_info.get("sequence", 0),
                    is_required=child_info.get("is_required", True),
                )
                self.session.add(composition)

        await self.session.commit()

    async def get_workflow_children(
        self,
        workflow_unit_id: str,
    ) -> list[dict[str, Any]]:
        """
        获取工作流包含的所有子单元

        Args:
            workflow_unit_id: 工作流执行单元 ID

        Returns:
            子单元列表
        """
        stmt = (
            select(
                ExecutionUnit,
                WorkflowComposition.node_id,
                WorkflowComposition.sequence,
            )
            .join(
                WorkflowComposition,
                ExecutionUnit.id == WorkflowComposition.child_unit_id,
            )
            .where(WorkflowComposition.workflow_unit_id == workflow_unit_id)
            .order_by(WorkflowComposition.sequence)
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            {
                "unit_id": str(row.ExecutionUnit.id),
                "unit_type": row.ExecutionUnit.unit_type,
                "ref_id": row.ExecutionUnit.ref_id,
                "name": row.ExecutionUnit.name,
                "node_id": row.node_id,
                "sequence": row.sequence,
                "success_rate": row.ExecutionUnit.success_rate,
            }
            for row in rows
        ]
