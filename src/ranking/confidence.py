"""
置信度计算器

评估工具/工作流执行成功概率，考虑历史表现和上下文匹配度
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgentConfig, ToolLibrary, Workflow

# 兼容别名
Agent = AgentConfig


class ConfidenceResult:
    """置信度评估结果"""

    def __init__(
        self,
        item_id: uuid.UUID,
        item_type: str,
        confidence_score: float,
        success_probability: float,
        reasons: list[str],
        risk_factors: list[str],
        suggestions: list[str],
    ):
        """
        初始化置信度结果

        Args:
            item_id: 项目 ID
            item_type: 项目类型
            confidence_score: 置信度分数（0-1）
            success_probability: 成功概率（0-1）
            reasons: 置信度理由列表
            risk_factors: 风险因素列表
            suggestions: 建议列表
        """
        self.item_id = item_id
        self.item_type = item_type
        self.confidence_score = confidence_score
        self.success_probability = success_probability
        self.reasons = reasons
        self.risk_factors = risk_factors
        self.suggestions = suggestions

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": str(self.item_id),
            "type": self.item_type,
            "confidence_score": self.confidence_score,
            "success_probability": self.success_probability,
            "reasons": self.reasons,
            "risk_factors": self.risk_factors,
            "suggestions": self.suggestions,
        }


class ConfidenceCalculator:
    """
    置信度计算器

    综合历史表现、上下文匹配度等因素评估执行置信度
    """

    def __init__(self, session: AsyncSession):
        """
        初始化置信度计算器

        Args:
            session: 数据库会话
        """
        self.session = session

    async def calculate_tool_confidence(
        self,
        tool_id: uuid.UUID,
        user_context: dict[str, Any] | None = None,
    ) -> ConfidenceResult:
        """
        计算工具执行置信度

        Args:
            tool_id: 工具 ID
            user_context: 用户上下文信息

        Returns:
            置信度评估结果
        """
        # 查询工具信息
        stmt = select(ToolLibrary).where(ToolLibrary.id == str(tool_id))
        result = await self.session.execute(stmt)
        tool = result.scalar_one_or_none()

        if not tool:
            return ConfidenceResult(
                item_id=tool_id,
                item_type="tool",
                confidence_score=0.0,
                success_probability=0.0,
                reasons=["工具不存在"],
                risk_factors=["工具未注册"],
                suggestions=[],
            )

        # 评估因素
        factors = {
            "history": await self._evaluate_tool_history(tool),
            "freshness": self._evaluate_freshness(tool.last_used_at),
            "approval": self._evaluate_approval_requirement(tool),
            "context": await self._evaluate_context_match(tool, user_context),
        }

        # 计算综合置信度
        weights = {
            "history": 0.4,
            "freshness": 0.2,
            "approval": 0.1,
            "context": 0.3,
        }

        confidence_score = sum(factors[factor] * weights[factor] for factor in factors)

        # 生成理由和风险
        reasons, risk_factors, suggestions = self._generate_tool_assessment(
            tool, factors
        )

        # 估算成功概率
        success_probability = self._estimate_success_probability(tool, factors)

        return ConfidenceResult(
            item_id=tool.id,
            item_type="tool",
            confidence_score=confidence_score,
            success_probability=success_probability,
            reasons=reasons,
            risk_factors=risk_factors,
            suggestions=suggestions,
        )

    async def calculate_workflow_confidence(
        self,
        workflow_id: uuid.UUID,
        user_context: dict[str, Any] | None = None,
    ) -> ConfidenceResult:
        """
        计算工作流执行置信度

        Args:
            workflow_id: 工作流 ID
            user_context: 用户上下文

        Returns:
            置信度评估结果
        """
        # 查询工作流
        stmt = select(Workflow).where(Workflow.id == str(workflow_id))
        result = await self.session.execute(stmt)
        workflow = result.scalar_one_or_none()

        if not workflow:
            return ConfidenceResult(
                item_id=workflow_id,
                item_type="workflow",
                confidence_score=0.0,
                success_probability=0.0,
                reasons=["工作流不存在"],
                risk_factors=["工作流未注册"],
                suggestions=[],
            )

        # 评估因素
        factors = {
            "history": await self._evaluate_workflow_history(workflow),
            "freshness": self._evaluate_freshness(workflow.last_used_at),
            "complexity": self._evaluate_workflow_complexity(workflow),
            "context": await self._evaluate_context_match(workflow, user_context),
        }

        # 加权计算
        weights = {
            "history": 0.4,
            "freshness": 0.2,
            "complexity": 0.2,
            "context": 0.2,
        }

        confidence_score = sum(factors[factor] * weights[factor] for factor in factors)

        # 生成评估
        reasons, risk_factors, suggestions = self._generate_workflow_assessment(
            workflow, factors
        )

        success_probability = self._estimate_success_probability(workflow, factors)

        return ConfidenceResult(
            item_id=workflow.id,
            item_type="workflow",
            confidence_score=confidence_score,
            success_probability=success_probability,
            reasons=reasons,
            risk_factors=risk_factors,
            suggestions=suggestions,
        )

    async def calculate_agent_confidence(
        self,
        agent_id: uuid.UUID,
        task_description: str,
        user_context: dict[str, Any] | None = None,
    ) -> ConfidenceResult:
        """
        计算 Agent 执行置信度

        Args:
            agent_id: Agent ID
            task_description: 任务描述
            user_context: 用户上下文

        Returns:
            置信度评估结果
        """
        # 查询 Agent
        stmt = select(Agent).where(Agent.id == str(agent_id))
        result = await self.session.execute(stmt)
        agent = result.scalar_one_or_none()

        if not agent:
            return ConfidenceResult(
                item_id=agent_id,
                item_type="agent",
                confidence_score=0.0,
                success_probability=0.0,
                reasons=["Agent 不存在"],
                risk_factors=["Agent 未注册"],
                suggestions=[],
            )

        # 评估因素
        factors = {
            "history": await self._evaluate_agent_history(agent),
            "freshness": self._evaluate_freshness(agent.last_used_at),
            "match": self._evaluate_task_match(agent, task_description),
            "context": await self._evaluate_context_match(agent, user_context),
        }

        # 加权
        weights = {
            "history": 0.3,
            "freshness": 0.2,
            "match": 0.3,
            "context": 0.2,
        }

        confidence_score = sum(factors[factor] * weights[factor] for factor in factors)

        # 生成评估
        reasons, risk_factors, suggestions = self._generate_agent_assessment(
            agent, factors
        )

        success_probability = self._estimate_success_probability(agent, factors)

        return ConfidenceResult(
            item_id=agent.id,
            item_type="agent",
            confidence_score=confidence_score,
            success_probability=success_probability,
            reasons=reasons,
            risk_factors=risk_factors,
            suggestions=suggestions,
        )

    async def _evaluate_tool_history(self, tool: ToolLibrary) -> float:
        """评估工具历史表现"""
        # 基于成功次数
        if tool.success_count >= 50:
            return 1.0
        elif tool.success_count >= 20:
            return 0.8
        elif tool.success_count >= 10:
            return 0.6
        elif tool.success_count >= 5:
            return 0.4
        else:
            return 0.2

    async def _evaluate_workflow_history(self, workflow: Workflow) -> float:
        """评估工作流历史表现"""
        # 综合成功次数和平均分数
        history_score = 0.0

        # 成功次数评分
        if workflow.success_count >= 50:
            history_score += 0.5
        elif workflow.success_count >= 20:
            history_score += 0.4
        elif workflow.success_count >= 10:
            history_score += 0.3
        elif workflow.success_count >= 5:
            history_score += 0.2
        else:
            history_score += 0.1

        # 平均分数评分
        if workflow.avg_score:
            history_score += workflow.avg_score * 0.5

        return min(history_score, 1.0)

    async def _evaluate_agent_history(self, agent: Agent) -> float:
        """评估 Agent 历史表现"""
        # 类似工作流评估
        history_score = 0.0

        if agent.success_count >= 20:
            history_score += 0.5
        elif agent.success_count >= 10:
            history_score += 0.4
        elif agent.success_count >= 5:
            history_score += 0.3
        elif agent.success_count >= 2:
            history_score += 0.2
        else:
            history_score += 0.1

        if agent.avg_score:
            history_score += agent.avg_score * 0.5

        return min(history_score, 1.0)

    def _evaluate_freshness(self, last_used_at: datetime | None) -> float:
        """评估时效性（最近使用情况）"""
        if not last_used_at:
            return 0.3  # 从未使用，给予较低分

        days_since_use = (datetime.now() - last_used_at).days

        if days_since_use <= 1:
            return 1.0
        elif days_since_use <= 7:
            return 0.8
        elif days_since_use <= 30:
            return 0.6
        elif days_since_use <= 90:
            return 0.4
        else:
            return 0.2

    def _evaluate_approval_requirement(self, tool: ToolLibrary) -> float:
        """评估审批要求（不需要审批的置信度更高）"""
        if tool.requires_approval:
            return 0.7  # 需要审批，降低置信度
        else:
            return 1.0  # 不需要审批，置信度高

    def _evaluate_workflow_complexity(self, workflow: Workflow) -> float:
        """评估工作流复杂度"""
        # 简单评估：基于定义的大小
        definition_str = str(workflow.definition)

        # 复杂度与置信度成反比
        if len(definition_str) < 500:
            return 1.0  # 简单工作流
        elif len(definition_str) < 2000:
            return 0.8
        elif len(definition_str) < 5000:
            return 0.6
        else:
            return 0.4  # 复杂工作流

    def _evaluate_task_match(self, agent: Agent, task_description: str) -> float:
        """评估任务与 Agent 的匹配度"""
        # 简单的关键词匹配
        agent_text = f"{agent.name} {agent.description or ''}".lower()
        task_text = task_description.lower()

        # 计算关键词重叠
        agent_words = set(agent_text.split())
        task_words = set(task_text.split())

        if not agent_words or not task_words:
            return 0.5

        intersection = agent_words.intersection(task_words)
        match_ratio = len(intersection) / len(task_words)

        return min(match_ratio * 2, 1.0)  # 放大匹配度

    async def _evaluate_context_match(
        self,
        item: Any,
        user_context: dict[str, Any] | None,
    ) -> float:
        """评估上下文匹配度"""
        if not user_context:
            return 0.7  # 无上下文，给予中等分数

        # 简单评估：检查用户权限和偏好
        score = 0.7  # 基础分

        # 检查创建者匹配
        if hasattr(item, "created_by") and item.created_by:
            if user_context.get("user_id") == item.created_by:
                score += 0.3  # 创建者使用自己的工具

        # 检查类型偏好
        if "preferred_types" in user_context:
            item_type = getattr(item, "source_type", None) or getattr(
                item, "type", None
            )
            if item_type in user_context["preferred_types"]:
                score += 0.1

        return min(score, 1.0)

    def _estimate_success_probability(
        self,
        item: Any,
        factors: dict[str, float],
    ) -> float:
        """估算成功概率"""
        # 基础概率
        base_prob = 0.5

        # 历史表现调整
        history_factor = factors.get("history", 0.5)
        if hasattr(item, "avg_score") and item.avg_score:
            base_prob = item.avg_score
        else:
            base_prob = 0.5 + history_factor * 0.3

        # 其他因素调整
        adjustments = sum(
            factors.get(f, 0.5) * 0.1
            for f in ["freshness", "context", "match", "complexity"]
            if f in factors
        )

        return min(base_prob + adjustments, 1.0)

    def _generate_tool_assessment(
        self,
        tool: ToolLibrary,
        factors: dict[str, float],
    ) -> tuple[list[str], list[str], list[str]]:
        """生成工具评估文本"""
        reasons = []
        risk_factors = []
        suggestions = []

        # 历史表现
        if factors["history"] >= 0.8:
            reasons.append(f"工具经过充分验证(已使用{tool.success_count}次)")
        elif factors["history"] < 0.4:
            risk_factors.append("工具使用次数较少，可能存在未知问题")

        # 时效性
        if factors["freshness"] >= 0.8:
            reasons.append("工具最近使用频繁，活跃度高")
        elif factors["freshness"] < 0.4:
            risk_factors.append("工具长期未使用，可能存在兼容性问题")
            suggestions.append("建议先在测试环境验证")

        # 审批要求
        if factors["approval"] < 1.0:
            risk_factors.append("工具需要审批后使用")
            suggestions.append("确保已获得必要权限")

        # 上下文匹配
        if factors["context"] >= 0.9:
            reasons.append("工具与当前上下文高度匹配")

        return reasons, risk_factors, suggestions

    def _generate_workflow_assessment(
        self,
        workflow: Workflow,
        factors: dict[str, float],
    ) -> tuple[list[str], list[str], list[str]]:
        """生成工作流评估文本"""
        reasons = []
        risk_factors = []
        suggestions = []

        # 历史表现
        if factors["history"] >= 0.8:
            reasons.append(
                f"工作流表现优异(成功率{workflow.success_count}次, "
                f"平均分{workflow.avg_score or 0:.2f})"
            )
        elif factors["history"] < 0.4:
            risk_factors.append("工作流使用较少，稳定性未知")

        # 复杂度
        if factors["complexity"] < 0.6:
            risk_factors.append("工作流较复杂，执行时间可能较长")
            suggestions.append("建议分步执行，监控执行状态")

        # 时效性
        if factors["freshness"] >= 0.8:
            reasons.append("工作流最近频繁使用")

        return reasons, risk_factors, suggestions

    def _generate_agent_assessment(
        self,
        agent: Agent,
        factors: dict[str, float],
    ) -> tuple[list[str], list[str], list[str]]:
        """生成 Agent 评估文本"""
        reasons = []
        risk_factors = []
        suggestions = []

        # 历史表现
        if factors["history"] >= 0.8:
            reasons.append(
                f"Agent经验丰富(已执行{agent.success_count}次, "
                f"平均分{agent.avg_score or 0:.2f})"
            )
        elif factors["history"] < 0.4:
            risk_factors.append("Agent执行次数较少")

        # 任务匹配
        if factors["match"] >= 0.7:
            reasons.append("Agent与任务高度匹配")
        elif factors["match"] < 0.4:
            risk_factors.append("Agent与任务匹配度较低")
            suggestions.append("考虑使用其他Agent或自定义配置")

        # 时效性
        if factors["freshness"] >= 0.8:
            reasons.append("Agent最近活跃度高")

        return reasons, risk_factors, suggestions
