"""
推荐器

基于语义相似度和历史成功率智能推荐工具/工作流
"""

import uuid
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgentConfig, EpisodesMemory, ToolLibrary, Workflow

# 兼容别名
Agent = AgentConfig
from src.memory.retriever import HybridRetriever
from src.memory.types import RetrievalConfig, SearchResult


class RecommendationResult:
    """推荐结果"""

    def __init__(
        self,
        item_id: uuid.UUID,
        item_type: str,
        name: str,
        description: str,
        score: float,
        confidence: float,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ):
        self.item_id = item_id
        self.item_type = item_type
        self.name = name
        self.description = description
        self.score = score
        self.confidence = confidence
        self.reason = reason
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": str(self.item_id),
            "type": self.item_type,
            "name": self.name,
            "description": self.description,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
            "metadata": self.metadata,
        }


class Recommender:
    """推荐器，综合考虑语义相似度、历史成功率和用户行为进行推荐"""

    def __init__(self, session: AsyncSession, retriever: HybridRetriever | None = None):
        self.session = session
        self.retriever = retriever

    async def recommend_tools(
        self,
        user_intent: str,
        user_id: uuid.UUID | None = None,
        limit: int = 5,
        min_success_rate: float = 0.5,
    ) -> list[RecommendationResult]:
        """推荐工具"""
        semantic_results = []
        if self.retriever:
            semantic_results = await self.retriever.search_tools(
                requirement=user_intent,
                config=RetrievalConfig(top_k=limit * 2, min_score=0.6),
            )

        stmt = (
            select(ToolLibrary)
            .where(ToolLibrary.status == "active")
            .order_by(ToolLibrary.success_count.desc())
            .limit(limit * 2)
        )

        if user_id:
            stmt = stmt.where(ToolLibrary.created_by == str(user_id))

        result = await self.session.execute(stmt)
        popular_tools = result.scalars().all()

        recommendations = self._fuse_tool_recommendations(
            semantic_results=semantic_results,
            popular_tools=popular_tools,
            user_intent=user_intent,
            limit=limit,
        )

        filtered = [
            r for r in recommendations
            if r.metadata.get("success_rate", 1.0) >= min_success_rate
        ]

        return filtered[:limit]

    async def recommend_workflows(
        self,
        user_intent: str,
        user_id: uuid.UUID | None = None,
        limit: int = 5,
        min_avg_score: float = 0.6,
    ) -> list[RecommendationResult]:
        """推荐工作流"""
        stmt = (
            select(Workflow)
            .where(
                and_(
                    Workflow.status == "active",
                    Workflow.avg_score.isnot(None),
                    Workflow.avg_score >= min_avg_score,
                )
            )
            .order_by(Workflow.avg_score.desc())
            .limit(limit * 2)
        )

        if user_id:
            stmt = stmt.where(Workflow.created_by == str(user_id))

        result = await self.session.execute(stmt)
        workflows = result.scalars().all()

        recommendations = []
        for wf in workflows:
            semantic_score = self._compute_keyword_similarity(
                user_intent, f"{wf.name} {wf.description or ''}"
            )
            combined_score = semantic_score * 0.6 + (wf.avg_score or 0.0) * 0.4

            reason_parts = []
            if wf.avg_score and wf.avg_score >= 0.8:
                reason_parts.append(f"高评分工作流({wf.avg_score:.2f})")
            if semantic_score >= 0.7:
                reason_parts.append("与需求高度匹配")
            if wf.success_count > 10:
                reason_parts.append(f"已被使用{wf.success_count}次")

            recommendations.append(
                RecommendationResult(
                    item_id=wf.id,
                    item_type="workflow",
                    name=wf.name,
                    description=wf.description or "",
                    score=combined_score,
                    confidence=min(semantic_score + 0.2, 1.0),
                    reason="; ".join(reason_parts) if reason_parts else "符合基本要求",
                    metadata={
                        "avg_score": wf.avg_score,
                        "success_count": wf.success_count,
                        "type": wf.type,
                        "source": wf.source,
                    },
                )
            )

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:limit]

    async def recommend_agents(
        self,
        task_description: str,
        user_id: uuid.UUID | None = None,
        limit: int = 5,
    ) -> list[RecommendationResult]:
        """推荐 Agent"""
        stmt = (
            select(Agent)
            .where(Agent.is_active)
            .order_by(Agent.created_at.desc())
            .limit(limit * 2)
        )

        if user_id:
            stmt = stmt.where(Agent.created_by == str(user_id))

        result = await self.session.execute(stmt)
        agents = result.scalars().all()

        recommendations = []
        for agent in agents:
            semantic_score = self._compute_keyword_similarity(
                task_description, f"{agent.name} {agent.description or ''}"
            )
            combined_score = semantic_score * 0.7 + ((agent.avg_score or 0.5) * 0.3)

            reason_parts = []
            if agent.avg_score and agent.avg_score >= 0.8:
                reason_parts.append(f"高评分Agent({agent.avg_score:.2f})")
            if semantic_score >= 0.7:
                reason_parts.append("与任务高度匹配")
            if agent.success_count > 5:
                reason_parts.append(f"已执行{agent.success_count}次")

            recommendations.append(
                RecommendationResult(
                    item_id=agent.id,
                    item_type="agent",
                    name=agent.name,
                    description=agent.description or "",
                    score=combined_score,
                    confidence=min(semantic_score + 0.1, 1.0),
                    reason="; ".join(reason_parts) if reason_parts else "基本符合要求",
                    metadata={
                        "avg_score": agent.avg_score,
                        "success_count": agent.success_count,
                        "type": agent.type,
                        "source": agent.source,
                    },
                )
            )

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:limit]

    async def collaborative_recommend(
        self,
        user_id: uuid.UUID,
        item_type: str = "tool",
        limit: int = 5,
    ) -> list[RecommendationResult]:
        """协同过滤推荐（基于用户行为相似性）"""
        stmt = select(EpisodesMemory).where(EpisodesMemory.user_id == str(user_id))
        result = await self.session.execute(stmt)
        user_episodes = result.scalars().all()

        used_tags = set()
        for ep in user_episodes:
            if ep.tags:
                used_tags.update(ep.tags)

        if item_type == "tool":
            stmt = select(ToolLibrary).where(ToolLibrary.status == "active")
        elif item_type == "workflow":
            stmt = select(Workflow).where(Workflow.status == "active")
        else:
            stmt = select(Agent).where(Agent.is_active)

        result = await self.session.execute(stmt)
        items = result.scalars().all()

        recommendations = []
        for item in items:
            item_desc = f"{item.name} {item.description or ''}"
            match_score = min(sum(0.2 for tag in used_tags if tag.lower() in item_desc.lower()), 1.0)

            if match_score > 0:
                recommendations.append(
                    RecommendationResult(
                        item_id=item.id,
                        item_type=item_type,
                        name=item.name,
                        description=item.description or "",
                        score=match_score,
                        confidence=match_score * 0.8,
                        reason="基于您的历史使用偏好",
                        metadata={"match_tags": list(used_tags)},
                    )
                )

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:limit]

    def _fuse_tool_recommendations(
        self,
        semantic_results: list[SearchResult],
        popular_tools: list[ToolLibrary],
        user_intent: str,
        limit: int,
    ) -> list[RecommendationResult]:
        """
        融合语义搜索和热门工具推荐

        Args:
            semantic_results: 语义搜索结果
            popular_tools: 热门工具列表
            user_intent: 用户意图
            limit: 返回数量

        Returns:
            融合后的推荐列表
        """
        # 构建工具映射
        tool_map = {t.id: t for t in popular_tools}
        scores: dict[uuid.UUID, float] = {}

        # 语义搜索得分（权重 0.7）
        for result in semantic_results:
            if result.id in tool_map:
                scores[result.id] = result.score * 0.7

        # 热门度得分（权重 0.3）
        max_count = max((t.success_count for t in popular_tools), default=1)
        for tool in popular_tools:
            popularity_score = (tool.success_count / max_count) * 0.3
            scores[tool.id] = scores.get(tool.id, 0) + popularity_score

        # 生成推荐结果
        recommendations = []
        sorted_tools = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        for tool_id in sorted_tools[:limit]:
            tool = tool_map[tool_id]
            score = scores[tool_id]

            # 推荐理由
            reason_parts = []
            if tool.success_count > 10:
                reason_parts.append(f"热门工具(已用{tool.success_count}次)")
            if score >= 0.7:
                reason_parts.append("与需求高度匹配")

            reason = "; ".join(reason_parts) if reason_parts else "符合基本要求"

            recommendations.append(
                RecommendationResult(
                    item_id=tool.id,
                    item_type="tool",
                    name=tool.name,
                    description=tool.description or "",
                    score=score,
                    confidence=min(score + 0.1, 1.0),
                    reason=reason,
                    metadata={
                        "success_count": tool.success_count,
                        "source_type": tool.source_type,
                    },
                )
            )

        return recommendations

    def _compute_keyword_similarity(
        self,
        text1: str,
        text2: str,
    ) -> float:
        """
        计算关键词相似度（简单的词重叠）

        Args:
            text1: 文本1
            text2: 文本2

        Returns:
            相似度分数（0-1）
        """
        # 分词（简单按空格和标点）
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 or not words2:
            return 0.0

        # Jaccard 相似度
        intersection = words1.intersection(words2)
        union = words1.union(words2)

        return len(intersection) / len(union) if union else 0.0
