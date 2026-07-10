"""
触发器基类

定义所有触发器的抽象基类和通用接口。
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from src.triggers.actions.executor import ActionExecutor
from src.triggers.models import (
    ExecutionResult,
    TriggerConfig,
)

logger = logging.getLogger(__name__)


class BaseTrigger(ABC):
    """
    触发器基类

    所有触发器都必须继承此类并实现 execute 方法。
    """

    def __init__(self, config: TriggerConfig):
        """
        初始化触发器

        Args:
            config: 触发器配置
        """
        self.config = config
        self.id = config.id
        self.name = config.name
        self.enabled = config.enabled
        self.trigger_type = config.trigger_type

        # 动作执行器
        self.action_executor = ActionExecutor()

        # 状态追踪
        self.execution_count = 0
        self.last_execution: datetime | None = None
        self.last_result: ExecutionResult | None = None

        logger.info(f"触发器初始化: {self.name} ({self.id})")

    @abstractmethod
    async def execute(self, *args, **kwargs) -> ExecutionResult:
        """
        执行触发器动作

        子类必须实现此方法。

        Returns:
            ExecutionResult: 执行结果
        """

    async def execute_actions(self, context: dict[str, Any] | None = None) -> ExecutionResult:
        """
        执行所有配置的动作

        Args:
            context: 执行上下文（事件数据等）

        Returns:
            ExecutionResult: 执行结果
        """
        if not self.config.actions:
            logger.warning(f"触发器 {self.name} 没有配置动作")
            return ExecutionResult(success=True, message="没有配置动作", data={"trigger_id": self.id})

        results = []
        errors = []

        # 按顺序执行动作
        sorted_actions = sorted(self.config.actions, key=lambda a: a.order)

        for action_config in sorted_actions:
            try:
                result = await self.action_executor.execute(action_config, context or {})
                results.append(result)

                if not result.success:
                    errors.append(f"{action_config.type}: {result.error}")

            except Exception as e:
                logger.error(f"执行动作失败: {e}", exc_info=True)
                errors.append(f"{action_config.type}: {str(e)}")

        # 更新执行统计
        self.execution_count += 1
        self.last_execution = datetime.utcnow()

        # 构建最终结果
        success = len(errors) == 0
        self.last_result = ExecutionResult(
            success=success,
            message=(f"执行 {len(results)} 个动作, {len(errors)} 个失败" if errors else "所有动作执行成功"),
            data={
                "trigger_id": self.id,
                "action_results": [r.to_dict() for r in results],
            },
            error="; ".join(errors) if errors else None,
        )

        return self.last_result

    def validate(self) -> bool:
        """
        验证触发器配置

        Returns:
            bool: 配置是否有效
        """
        if not self.id:
            logger.error("触发器 ID 不能为空")
            return False

        if not self.name:
            logger.error("触发器名称不能为空")
            return False

        if not self.config.actions:
            logger.warning(f"触发器 {self.name} 没有配置动作")

        return True

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典

        Returns:
            Dict[str, Any]: 触发器信息字典
        """
        return {
            "id": self.id,
            "name": self.name,
            "trigger_type": self.trigger_type.value,
            "enabled": self.enabled,
            "execution_count": self.execution_count,
            "last_execution": (self.last_execution.isoformat() if self.last_execution else None),
            "last_result": self.last_result.to_dict() if self.last_result else None,
            "config": self.config.to_dict(),
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id} name={self.name} enabled={self.enabled}>"
