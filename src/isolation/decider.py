"""
隔离策略决策器

基于 IsolationPolicyLoader 的策略配置，决策工具的隔离级别。
隔离级别不可用时直接报错，不降级——降级会让属于其它容器/工作区的任务
静默落到本编排进程执行，造成跨容器污染（见 .checkpoints 串台事故）。

暴露接口：
- IsolationDecider：隔离策略决策器类
"""

import logging

from isolation.policy import IsolationPolicyLoader, ToolIsolationPolicy
from isolation.types import IsolationLevel

logger = logging.getLogger(__name__)


class IsolationDecider:
    """隔离策略决策器

    基于 IsolationPolicyLoader 加载的策略配置，决策工具的隔离级别。
    隔离级别不可用时直接报错，不降级。

    核心原则：
    - 默认 CONTAINER（容器隔离），无需审批
    - HOST（宿主机执行）需要明确指定 + 人工审批
    - 配置的隔离级别不可用即报错，不自动降级到其它级别
    """

    def __init__(
        self,
        policy_loader: IsolationPolicyLoader | None = None,
    ):
        """初始化决策器

        Args:
            policy_loader: 策略加载器实例，为 None 时使用默认配置创建
        """
        self._policy_loader = policy_loader or IsolationPolicyLoader()

    async def decide(
        self,
        tool_name: str,
        tool_category: str | None = None,
        available_providers: dict[IsolationLevel, bool] | None = None,
    ) -> ToolIsolationPolicy:
        """决策工具的隔离策略

        从策略加载器获取工具对应的隔离策略。若指定了 available_providers 且
        配置的隔离级别不可用，直接抛 IsolationError——不支持降级。

        Args:
            tool_name: 工具名称
            tool_category: 工具分类（可选）
            available_providers: 各隔离级别的可用性，为 None 时不做可用性检查

        Returns:
            匹配到的隔离策略

        Raises:
            IsolationError: 配置的隔离级别不可用时抛出
        """
        policy = self._policy_loader.resolve(tool_name, tool_category)

        if available_providers is None:
            return policy

        # 隔离级别不可用即报错，不降级（降级会导致跨容器/工作区污染）
        if not available_providers.get(policy.isolation, False):
            raise IsolationError(f"工具 {tool_name} 的隔离级别 {policy.isolation.value} 不可用，且不支持降级")

        return policy

    def resolve(self, tool_name: str, tool_category: str | None = None) -> ToolIsolationPolicy:
        """直接获取工具的隔离策略（不做可用性检查）

        Args:
            tool_name: 工具名称
            tool_category: 工具分类（可选）

        Returns:
            匹配到的隔离策略
        """
        return self._policy_loader.resolve(tool_name, tool_category)

    @property
    def policy_loader(self) -> IsolationPolicyLoader:
        """获取策略加载器实例"""
        return self._policy_loader


class IsolationError(Exception):
    """隔离策略错误"""

    pass
