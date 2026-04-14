"""
隔离策略决策器

基于 IsolationPolicyLoader 的策略配置，决策工具的隔离级别。
支持优雅降级：当配置的隔离级别不可用时，根据策略的 fallback 字段决定降级或报错。

暴露接口：
- IsolationDecider：隔离策略决策器类
"""

import logging
from dataclasses import replace

from src.isolation.policy import IsolationPolicyLoader, ToolIsolationPolicy
from src.isolation.types import IsolationLevel

logger = logging.getLogger(__name__)

FALLBACK_ORDER = [IsolationLevel.CONTAINER, IsolationLevel.HOST]


class IsolationDecider:
    """隔离策略决策器

    基于 IsolationPolicyLoader 加载的策略配置，决策工具的隔离级别。
    支持优雅降级：当配置的隔离级别不可用时，根据策略的 fallback 字段决定降级或报错。

    核心原则：
    - 默认 CONTAINER（容器隔离），无需审批
    - HOST（宿主机执行）需要明确指定 + 人工审批
    - fallback="allow" 的工具允许降级，fallback="deny" 的工具禁止降级
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

        从策略加载器获取工具对应的隔离策略，并检查配置的隔离级别是否可用。
        如果不可用，根据策略的 fallback 字段决定降级或报错。

        Args:
            tool_name: 工具名称
            tool_category: 工具分类（可选）
            available_providers: 各隔离级别的可用性，为 None 时不做可用性检查

        Returns:
            匹配到的隔离策略（可能经过降级调整）

        Raises:
            IsolationError: 隔离级别不可用且策略禁止降级时抛出
        """
        policy = self._policy_loader.resolve(tool_name, tool_category)

        if available_providers is None:
            return policy

        # 检查配置的隔离级别是否可用
        if not available_providers.get(policy.isolation, False):
            policy = self._apply_fallback(policy, tool_name, available_providers)

        return policy

    def _apply_fallback(
        self,
        policy: ToolIsolationPolicy,
        tool_name: str,
        available_providers: dict[IsolationLevel, bool],
    ) -> ToolIsolationPolicy:
        """应用降级策略

        当配置的隔离级别不可用时，根据策略的 fallback 字段决定降级或报错。

        Args:
            policy: 原始策略
            tool_name: 工具名称（用于日志）
            available_providers: 各隔离级别的可用性

        Returns:
            调整后的策略

        Raises:
            IsolationError: 隔离级别不可用且策略禁止降级时抛出
        """
        # 尝试在降级顺序中查找可用级别
        try:
            current_index = FALLBACK_ORDER.index(policy.isolation)
        except ValueError:
            current_index = 0

        for level in FALLBACK_ORDER[current_index + 1 :]:
            if available_providers.get(level, False):
                if policy.fallback == "allow":
                    logger.warning(
                        f"工具 {tool_name} 的隔离级别 {policy.isolation.value} 不可用，"
                        f"降级到 {level.value} 执行"
                    )
                    return replace(
                        policy,
                        isolation=level,
                        execution="host_direct",
                    )
                else:
                    raise IsolationError(
                        f"工具 {tool_name} 的隔离级别 {policy.isolation.value} 不可用，"
                        f"且策略禁止降级（fallback={policy.fallback}）"
                    )

        # 没有可用的降级目标
        if policy.fallback == "allow":
            logger.warning(
                f"工具 {tool_name} 无可用隔离级别，强制使用 HOST 执行"
            )
            return replace(policy, isolation=IsolationLevel.HOST, execution="host_direct")

        raise IsolationError(
            f"工具 {tool_name} 的隔离级别 {policy.isolation.value} 不可用，"
            f"且无可用降级目标"
        )

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
