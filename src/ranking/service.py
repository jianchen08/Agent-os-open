"""
排行服务

统计工具/工作流使用次数和成功率，支持多种排行维度
基于统一执行单元表（execution_units）进行统计
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    AgentConfig,
    EpisodesMemory,
    ExecutionExperience,
    ExecutionUnit,
    ToolLibrary,
    Workflow,
)

# 兼容别名
Agent = AgentConfig


class RankingService:
    """
    排行服务

    提供工具、工作流、Agent 的使用排行和成功率排行
    """

    def __init__(self, session: AsyncSession):
        """
        初始化排行服务

        Args:
            session: 数据库会话
        """
        self.session = session

    async def get_tool_ranking(
        self,
        user_id: uuid.UUID | None = None,
        category: str | None = None,
        time_range: int | None = None,
        limit: int = 10,
        order_by: str = "success_count",
    ) -> list[dict[str, Any]]:
        """
        获取工具排行榜

        Args:
            user_id: 用户 ID（None 表示全部用户）
            category: 分类过滤
            time_range: 时间范围（天数），None 表示全部
            limit: 返回数量
            order_by: 排序字段（success_count, avg_score, last_used）

        Returns:
            工具排行列表
        """
        # 构建查询
        stmt = select(
            ToolLibrary.id,
            ToolLibrary.name,
            ToolLibrary.description,
            ToolLibrary.source_type,
            ToolLibrary.success_count,
            ToolLibrary.last_used_at,
            ToolLibrary.created_at,
        ).where(ToolLibrary.status == "active")

        # 用户过滤
        if user_id:
            stmt = stmt.where(ToolLibrary.created_by == str(user_id))

        # 分类过滤
        if category:
            stmt = stmt.where(ToolLibrary.source_type == category)

        # 时间范围过滤
        if time_range:
            cutoff_date = datetime.now() - timedelta(days=time_range)
            stmt = stmt.where(ToolLibrary.last_used_at >= cutoff_date)

        # 排序
        if order_by == "success_count":
            stmt = stmt.order_by(desc(ToolLibrary.success_count))
        elif order_by == "last_used":
            stmt = stmt.order_by(desc(ToolLibrary.last_used_at))
        elif order_by == "created_at":
            stmt = stmt.order_by(desc(ToolLibrary.created_at))

        stmt = stmt.limit(limit)

        # 执行查询
        result = await self.session.execute(stmt)
        tools = result.all()

        # 转换为结果字典
        ranking = []
        for rank, tool in enumerate(tools, 1):
            # 计算成功率（这里简化处理，实际需要失败次数数据）
            success_rate = 1.0  # 默认值

            ranking.append(
                {
                    "rank": rank,
                    "id": str(tool.id),
                    "name": tool.name,
                    "description": tool.description,
                    "category": tool.source_type,
                    "success_count": tool.success_count,
                    "success_rate": success_rate,
                    "last_used_at": (
                        tool.last_used_at.isoformat() if tool.last_used_at else None
                    ),
                    "created_at": tool.created_at.isoformat(),
                }
            )

        return ranking

    async def get_workflow_ranking(
        self,
        user_id: uuid.UUID | None = None,
        workflow_type: str | None = None,
        time_range: int | None = None,
        limit: int = 10,
        order_by: str = "success_count",
    ) -> list[dict[str, Any]]:
        """
        获取工作流排行榜

        Args:
            user_id: 用户 ID
            workflow_type: 工作流类型
            time_range: 时间范围（天数）
            limit: 返回数量
            order_by: 排序字段

        Returns:
            工作流排行列表
        """
        # 构建查询
        stmt = select(
            Workflow.id,
            Workflow.name,
            Workflow.description,
            Workflow.type,
            Workflow.source,
            Workflow.success_count,
            Workflow.avg_score,
            Workflow.last_used_at,
            Workflow.created_at,
        ).where(Workflow.status == "active")

        # 用户过滤
        if user_id:
            stmt = stmt.where(Workflow.created_by == str(user_id))

        # 类型过滤
        if workflow_type:
            stmt = stmt.where(Workflow.type == workflow_type)

        # 时间范围
        if time_range:
            cutoff_date = datetime.now() - timedelta(days=time_range)
            stmt = stmt.where(Workflow.last_used_at >= cutoff_date)

        # 排序
        if order_by == "success_count":
            stmt = stmt.order_by(desc(Workflow.success_count))
        elif order_by == "avg_score":
            stmt = stmt.order_by(desc(Workflow.avg_score))
        elif order_by == "last_used":
            stmt = stmt.order_by(desc(Workflow.last_used_at))

        stmt = stmt.limit(limit)

        # 执行查询
        result = await self.session.execute(stmt)
        workflows = result.all()

        # 转换为结果
        ranking = []
        for rank, wf in enumerate(workflows, 1):
            ranking.append(
                {
                    "rank": rank,
                    "id": str(wf.id),
                    "name": wf.name,
                    "description": wf.description,
                    "type": wf.type,
                    "source": wf.source,
                    "success_count": wf.success_count,
                    "avg_score": wf.avg_score,
                    "last_used_at": (
                        wf.last_used_at.isoformat() if wf.last_used_at else None
                    ),
                    "created_at": wf.created_at.isoformat(),
                }
            )

        return ranking

    async def get_agent_ranking(
        self,
        user_id: uuid.UUID | None = None,
        agent_type: str | None = None,
        time_range: int | None = None,
        limit: int = 10,
        order_by: str = "created_at",
    ) -> list[dict[str, Any]]:
        """
        获取 Agent 排行榜

        Args:
            user_id: 用户 ID
            agent_type: Agent 类型
            time_range: 时间范围（天数）
            limit: 返回数量
            order_by: 排序字段

        Returns:
            Agent 排行列表
        """
        # 构建查询 - 使用 AgentConfig 模型
        stmt = select(
            AgentConfig.id,
            AgentConfig.name,
            AgentConfig.description,
            AgentConfig.agent_type,
            AgentConfig.model_name,
            AgentConfig.is_active,
            AgentConfig.created_at,
            AgentConfig.updated_at,
        ).where(AgentConfig.is_active)

        # 类型过滤
        if agent_type:
            stmt = stmt.where(AgentConfig.agent_type == agent_type)

        # 时间范围
        if time_range:
            cutoff_date = datetime.now() - timedelta(days=time_range)
            stmt = stmt.where(AgentConfig.created_at >= cutoff_date)

        # 排序
        stmt = stmt.order_by(desc(AgentConfig.created_at))

        stmt = stmt.limit(limit)

        # 执行查询
        result = await self.session.execute(stmt)
        agents = result.all()

        # 转换为结果
        ranking = []
        for rank, agent in enumerate(agents, 1):
            ranking.append(
                {
                    "rank": rank,
                    "id": str(agent.id),
                    "name": agent.name,
                    "description": agent.description,
                    "type": agent.agent_type,
                    "model": agent.model_name,
                    "is_active": agent.is_active,
                    "created_at": (
                        agent.created_at.isoformat() if agent.created_at else None
                    ),
                    "updated_at": (
                        agent.updated_at.isoformat() if agent.updated_at else None
                    ),
                }
            )

        return ranking

    async def get_user_success_stats(
        self,
        user_id: uuid.UUID,
        time_range: int = 30,
    ) -> dict[str, Any]:
        """
        获取用户成功统计

        Args:
            user_id: 用户 ID
            time_range: 时间范围（天数）

        Returns:
            统计数据
        """
        cutoff_date = datetime.now() - timedelta(days=time_range)

        # 统计情景记忆数量
        stmt = select(func.count(EpisodesMemory.id)).where(
            and_(
                EpisodesMemory.user_id == str(user_id),
                EpisodesMemory.created_at >= cutoff_date,
            )
        )
        result = await self.session.execute(stmt)
        total_episodes = result.scalar() or 0

        # 统计高评分任务数量（score >= 0.7）
        stmt = select(func.count(EpisodesMemory.id)).where(
            and_(
                EpisodesMemory.user_id == str(user_id),
                EpisodesMemory.created_at >= cutoff_date,
                EpisodesMemory.final_score >= 0.7,
            )
        )
        result = await self.session.execute(stmt)
        success_episodes = result.scalar() or 0

        # 计算平均分数
        stmt = select(func.avg(EpisodesMemory.final_score)).where(
            and_(
                EpisodesMemory.user_id == str(user_id),
                EpisodesMemory.created_at >= cutoff_date,
                EpisodesMemory.final_score.isnot(None),
            )
        )
        result = await self.session.execute(stmt)
        avg_score = result.scalar() or 0.0

        # 统计工具使用次数
        stmt = select(func.sum(ToolLibrary.success_count)).where(
            and_(
                ToolLibrary.created_by == str(user_id),
                ToolLibrary.last_used_at >= cutoff_date,
            )
        )
        result = await self.session.execute(stmt)
        tool_usage = result.scalar() or 0

        return {
            "user_id": str(user_id),
            "time_range_days": time_range,
            "total_tasks": total_episodes,
            "successful_tasks": success_episodes,
            "success_rate": (
                success_episodes / total_episodes if total_episodes > 0 else 0.0
            ),
            "avg_score": float(avg_score),
            "tool_usage_count": tool_usage,
        }

    async def get_trending_tools(
        self,
        user_id: uuid.UUID | None = None,
        days: int = 7,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        获取热门工具（上升趋势）

        Args:
            user_id: 用户 ID
            days: 统计天数
            limit: 返回数量

        Returns:
            热门工具列表
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        stmt = (
            select(
                ToolLibrary.id,
                ToolLibrary.name,
                ToolLibrary.description,
                ToolLibrary.success_count,
            )
            .where(
                and_(
                    ToolLibrary.status == "active",
                    ToolLibrary.last_used_at >= cutoff_date,
                )
            )
            .order_by(desc(ToolLibrary.success_count))
            .limit(limit)
        )

        if user_id:
            stmt = stmt.where(ToolLibrary.created_by == str(user_id))

        result = await self.session.execute(stmt)
        tools = result.all()

        trending = []
        for rank, tool in enumerate(tools, 1):
            trending.append(
                {
                    "rank": rank,
                    "id": str(tool.id),
                    "name": tool.name,
                    "description": tool.description,
                    "recent_usage": tool.success_count,
                }
            )

        return trending

    # ========== 基于统一执行单元的新方法 ==========

    async def get_unified_ranking(
        self,
        unit_type: str | None = None,
        user_id: uuid.UUID | None = None,
        time_range: int | None = None,
        limit: int = 10,
        order_by: str = "successful_executions",
    ) -> list[dict[str, Any]]:
        """
        获取统一执行单元排行榜

        基于 execution_units 表，统一查询 tool/agent/workflow

        Args:
            unit_type: 单元类型（tool/agent/workflow），None 表示全部
            user_id: 用户 ID
            time_range: 时间范围（天数）
            limit: 返回数量
            order_by: 排序字段（successful_executions, success_rate, average_score, last_used）

        Returns:
            排行列表
        """
        stmt = select(
            ExecutionUnit.id,
            ExecutionUnit.unit_type,
            ExecutionUnit.unit_id,
            ExecutionUnit.name,
            ExecutionUnit.description,
            ExecutionUnit.total_executions,
            ExecutionUnit.successful_executions,
            ExecutionUnit.average_score,
            ExecutionUnit.last_used_at,
            ExecutionUnit.created_at,
        )

        # 类型过滤
        if unit_type:
            stmt = stmt.where(ExecutionUnit.unit_type == unit_type)

        # 时间范围过滤
        if time_range:
            cutoff_date = datetime.now() - timedelta(days=time_range)
            stmt = stmt.where(ExecutionUnit.last_used_at >= cutoff_date)

        # 排序
        if order_by == "successful_executions":
            stmt = stmt.order_by(desc(ExecutionUnit.successful_executions))
        elif order_by == "success_rate":
            # 按成功率排序（成功数 / 总数）
            stmt = stmt.order_by(
                desc(
                    case(
                        (
                            ExecutionUnit.total_executions > 0,
                            ExecutionUnit.successful_executions
                            * 1.0
                            / ExecutionUnit.total_executions,
                        ),
                        else_=0,
                    )
                )
            )
        elif order_by == "average_score":
            stmt = stmt.order_by(desc(ExecutionUnit.average_score))
        elif order_by == "last_used":
            stmt = stmt.order_by(desc(ExecutionUnit.last_used_at))

        stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        units = result.all()

        ranking = []
        for rank, unit in enumerate(units, 1):
            total = unit.total_executions
            success_rate = unit.successful_executions / total if total > 0 else 0.0
            fail_count = total - unit.successful_executions

            ranking.append(
                {
                    "rank": rank,
                    "id": str(unit.id),
                    "unit_type": unit.unit_type,
                    "unit_id": str(unit.unit_id),
                    "name": unit.name,
                    "description": unit.description,
                    "total_executions": total,
                    "successful_executions": unit.successful_executions,
                    "fail_count": fail_count,
                    "success_rate": round(success_rate, 4),
                    "average_score": unit.average_score,
                    "last_used_at": (
                        unit.last_used_at.isoformat() if unit.last_used_at else None
                    ),
                    "created_at": unit.created_at.isoformat(),
                }
            )

        return ranking

    async def get_experience_stats(
        self,
        unit_id: str,
        time_range: int | None = None,
    ) -> dict[str, Any]:
        """
        获取执行单元的详细经验统计

        Args:
            unit_id: 执行单元 ID
            time_range: 时间范围（天数）

        Returns:
            详细统计数据
        """
        base_filter = [ExecutionExperience.unit_id == unit_id]

        if time_range:
            cutoff_date = datetime.now() - timedelta(days=time_range)
            base_filter.append(ExecutionExperience.created_at >= cutoff_date)

        # 总执行次数
        stmt = select(func.count(ExecutionExperience.id)).where(and_(*base_filter))
        result = await self.session.execute(stmt)
        total_count = result.scalar() or 0

        # 成功次数
        stmt = select(func.count(ExecutionExperience.id)).where(
            and_(*base_filter, ExecutionExperience.status == "success")
        )
        result = await self.session.execute(stmt)
        success_count = result.scalar() or 0

        # 失败次数
        stmt = select(func.count(ExecutionExperience.id)).where(
            and_(*base_filter, ExecutionExperience.status == "failed")
        )
        result = await self.session.execute(stmt)
        fail_count = result.scalar() or 0

        # 平均评分
        stmt = select(func.avg(ExecutionExperience.score)).where(
            and_(*base_filter, ExecutionExperience.score.isnot(None))
        )
        result = await self.session.execute(stmt)
        avg_score = result.scalar() or 0.0

        # 平均耗时
        stmt = select(func.avg(ExecutionExperience.duration_ms)).where(
            and_(*base_filter, ExecutionExperience.duration_ms.isnot(None))
        )
        result = await self.session.execute(stmt)
        avg_duration = result.scalar() or 0.0

        # 错误类型分布
        stmt = (
            select(
                ExecutionExperience.error_type,
                func.count(ExecutionExperience.id).label("count"),
            )
            .where(and_(*base_filter, ExecutionExperience.error_type.isnot(None)))
            .group_by(ExecutionExperience.error_type)
            .order_by(desc("count"))
            .limit(5)
        )
        result = await self.session.execute(stmt)
        error_distribution = [
            {"error_type": row.error_type, "count": row.count} for row in result.all()
        ]

        return {
            "unit_id": unit_id,
            "time_range_days": time_range,
            "total_executions": total_count,
            "success_count": success_count,
            "fail_count": fail_count,
            "success_rate": success_count / total_count if total_count > 0 else 0.0,
            "avg_score": float(avg_score),
            "avg_duration_ms": float(avg_duration),
            "error_distribution": error_distribution,
        }

    async def get_scene_performance(
        self,
        unit_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        获取执行单元在不同场景下的表现

        Args:
            unit_id: 执行单元 ID
            limit: 返回数量

        Returns:
            场景表现列表
        """
        # 按意图文本分组统计
        stmt = (
            select(
                ExecutionExperience.intent_text,
                func.count(ExecutionExperience.id).label("total"),
                func.sum(
                    case((ExecutionExperience.status == "success", 1), else_=0)
                ).label("success"),
                func.avg(ExecutionExperience.score).label("avg_score"),
            )
            .where(
                and_(
                    ExecutionExperience.unit_id == unit_id,
                    ExecutionExperience.intent_text.isnot(None),
                )
            )
            .group_by(ExecutionExperience.intent_text)
            .order_by(desc("total"))
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        scenes = result.all()

        return [
            {
                "intent": scene.intent_text[:100] if scene.intent_text else "",
                "total_executions": scene.total,
                "success_count": scene.success or 0,
                "success_rate": (
                    (scene.success or 0) / scene.total if scene.total > 0 else 0.0
                ),
                "avg_score": float(scene.avg_score) if scene.avg_score else None,
            }
            for scene in scenes
        ]

    async def update_unit_stats(self, unit_id: str):
        """
        更新执行单元的统计数据（从 execution_experiences 聚合）

        Args:
            unit_id: 执行单元 ID
        """
        # 获取统计数据
        stats = await self.get_experience_stats(unit_id)

        # 更新 execution_units 表
        stmt = select(ExecutionUnit).where(ExecutionUnit.id == unit_id)
        result = await self.session.execute(stmt)
        unit = result.scalar_one_or_none()

        if unit:
            unit.success_count = stats["success_count"]
            unit.fail_count = stats["fail_count"]
            unit.avg_score = stats["avg_score"] if stats["avg_score"] > 0 else None
            unit.avg_duration_ms = (
                stats["avg_duration_ms"] if stats["avg_duration_ms"] > 0 else None
            )
            unit.last_used_at = datetime.now()

            await self.session.commit()
