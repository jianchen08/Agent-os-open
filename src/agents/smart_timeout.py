"""
智能等待时间控制器

基于多维度因素智能计算任务执行等待时间
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class TaskComplexity(Enum):
    """任务复杂度级别"""

    SIMPLE = "simple"  # 简单任务
    MEDIUM = "medium"  # 中等任务
    COMPLEX = "complex"  # 复杂任务
    HEAVY = "heavy"  # 重型任务


class AgentType(Enum):
    """Agent 类型"""

    FAST = "fast"  # 快速执行型
    STANDARD = "standard"  # 标准型
    SLOW = "slow"  # 慢速执行型


@dataclass
class TaskProfile:
    """任务画像"""

    acceptance_criteria_count: int
    target_agent_type: AgentType
    task_type: str  # "file_operation", "code_generation", "data_processing", etc.
    estimated_complexity: TaskComplexity
    historical_avg_time: float | None = None
    priority: int = 5  # 1-10, 10最高优先级


class SmartTimeoutController:
    """智能等待时间控制器"""

    def __init__(self):
        # 基础等待时间配置 (秒)
        self.base_timeouts = {
            TaskComplexity.SIMPLE: 1.0,
            TaskComplexity.MEDIUM: 3.0,
            TaskComplexity.COMPLEX: 5.0,
            TaskComplexity.HEAVY: 8.0,
        }

        # Agent 类型调整系数
        self.agent_multipliers = {
            AgentType.FAST: 0.7,  # 快速 Agent 减少 30%
            AgentType.STANDARD: 1.0,  # 标准 Agent 不调整
            AgentType.SLOW: 1.5,  # 慢速 Agent 增加 50%
        }

        # 任务类型调整系数
        self.task_type_multipliers = {
            "file_operation": 0.5,  # 文件操作很快
            "simple_query": 0.3,  # 简单查询最快
            "code_generation": 1.2,  # 代码生成稍慢
            "data_processing": 1.5,  # 数据处理较慢
            "api_call": 0.8,  # API 调用中等
            "database_operation": 1.0,  # 数据库操作标准
            "complex_analysis": 2.0,  # 复杂分析很慢
        }

        # 历史执行时间缓存
        self.execution_history: dict[str, list[float]] = {}

    def calculate_timeout(self, task_profile: TaskProfile) -> float:
        """
        计算智能等待时间

        Args:
            task_profile: 任务画像

        Returns:
            等待时间（秒）
        """
        # 1. 基于复杂度的基础时间
        complexity = self._estimate_complexity(task_profile)
        base_time = self.base_timeouts[complexity]

        # 2. Agent 类型调整
        agent_multiplier = self.agent_multipliers.get(
            task_profile.target_agent_type, 1.0
        )

        # 3. 任务类型调整
        task_multiplier = self.task_type_multipliers.get(task_profile.task_type, 1.0)

        # 4. 历史数据调整
        history_multiplier = self._get_history_multiplier(task_profile)

        # 5. 优先级调整（高优先级任务等待更久）
        priority_multiplier = 1.0 + (task_profile.priority - 5) * 0.1

        # 综合计算
        timeout = (
            base_time
            * agent_multiplier
            * task_multiplier
            * history_multiplier
            * priority_multiplier
        )

        # 限制在合理范围内 (0.5秒 - 10秒)
        timeout = max(0.5, min(10.0, timeout))

        logger.info(
            f"[SmartTimeout] 计算等待时间 | "
            f"complexity={complexity.value} | "
            f"base={base_time}s | "
            f"agent_mult={agent_multiplier} | "
            f"task_mult={task_multiplier} | "
            f"history_mult={history_multiplier} | "
            f"priority_mult={priority_multiplier} | "
            f"final={timeout:.1f}s"
        )

        return timeout

    def _estimate_complexity(self, task_profile: TaskProfile) -> TaskComplexity:
        """估算任务复杂度"""
        if task_profile.estimated_complexity:
            return task_profile.estimated_complexity

        # 基于 AC 数量估算
        ac_count = task_profile.acceptance_criteria_count
        if ac_count <= 1:
            return TaskComplexity.SIMPLE
        elif ac_count <= 3:
            return TaskComplexity.MEDIUM
        elif ac_count <= 6:
            return TaskComplexity.COMPLEX
        else:
            return TaskComplexity.HEAVY

    def _get_history_multiplier(self, task_profile: TaskProfile) -> float:
        """基于历史执行时间调整"""
        if task_profile.historical_avg_time is None:
            return 1.0

        # 如果历史平均时间很短，减少等待时间
        if task_profile.historical_avg_time < 1.0:
            return 0.8
        elif task_profile.historical_avg_time < 2.0:
            return 0.9
        elif task_profile.historical_avg_time > 5.0:
            return 1.3
        elif task_profile.historical_avg_time > 8.0:
            return 1.5
        else:
            return 1.0

    def record_execution_time(self, task_key: str, execution_time: float):
        """记录任务执行时间，用于历史数据分析"""
        if task_key not in self.execution_history:
            self.execution_history[task_key] = []

        # 保留最近 10 次记录
        history = self.execution_history[task_key]
        history.append(execution_time)
        if len(history) > 10:
            history.pop(0)

    def get_historical_avg_time(self, task_key: str) -> float | None:
        """获取历史平均执行时间"""
        history = self.execution_history.get(task_key, [])
        if not history:
            return None
        return sum(history) / len(history)


# 全局智能超时控制器实例
smart_timeout_controller = SmartTimeoutController()


def calculate_smart_timeout(
    acceptance_criteria: list[dict[str, Any]],
    target_agent_name: str,
    task_type: str = "standard",
    goal: dict[str, Any] | None = None,
) -> float:
    """
    便捷函数：计算智能等待时间

    Args:
        acceptance_criteria: 验收标准列表
        target_agent_name: 目标 Agent 名称
        task_type: 任务类型
        goal: 任务目标

    Returns:
        等待时间（秒）
    """
    # 根据 Agent 名称推断类型
    agent_type = AgentType.STANDARD
    if "fast" in target_agent_name.lower() or "quick" in target_agent_name.lower():
        agent_type = AgentType.FAST
    elif "slow" in target_agent_name.lower() or "heavy" in target_agent_name.lower():
        agent_type = AgentType.SLOW

    # 根据目标内容推断任务类型
    inferred_task_type = task_type
    if goal and isinstance(goal, dict):
        title = goal.get("title", "").lower()
        description = goal.get("description", "").lower()
        content = f"{title} {description}"

        if any(word in content for word in ["文件", "创建", "删除", "file"]):
            inferred_task_type = "file_operation"
        elif any(word in content for word in ["代码", "编程", "code", "script"]):
            inferred_task_type = "code_generation"
        elif any(word in content for word in ["查询", "搜索", "query", "search"]):
            inferred_task_type = "simple_query"
        elif any(word in content for word in ["分析", "处理", "analysis", "process"]):
            inferred_task_type = "data_processing"

    # 构建任务画像
    task_profile = TaskProfile(
        acceptance_criteria_count=len(acceptance_criteria),
        target_agent_type=agent_type,
        task_type=inferred_task_type,
        estimated_complexity=None,  # 让系统自动估算
        historical_avg_time=smart_timeout_controller.get_historical_avg_time(
            f"{target_agent_name}:{inferred_task_type}"
        ),
    )

    return smart_timeout_controller.calculate_timeout(task_profile)
