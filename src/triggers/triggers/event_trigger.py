"""
事件触发器

监听系统事件并在满足条件时执行动作。
"""

import logging
from typing import Any

from simpleeval import InvalidExpression, simple_eval

from src.core.event_bus.types import ExecutionEvent
from src.triggers.models import ExecutionResult, TriggerConfig, TriggerType
from src.triggers.triggers.base import BaseTrigger

logger = logging.getLogger(__name__)


class EventTrigger(BaseTrigger):
    """
    事件触发器

    监听特定类型的系统事件，在事件发生时执行配置的动作。
    支持基于事件数据的过滤条件。
    """

    def __init__(self, config: TriggerConfig):
        """
        初始化事件触发器

        Args:
            config: 触发器配置，必须包含 event 字段
        """
        super().__init__(config)

        if config.trigger_type != TriggerType.EVENT:
            raise ValueError(f"触发器类型必须是 EVENT，实际是 {config.trigger_type}")

        if not config.event:
            raise ValueError("事件触发器必须包含 event 配置")

        self.event_config = config.event
        self.event_type = self.event_config.get("type")
        self.filter_expression = self.event_config.get("filter")

        if not self.event_type:
            raise ValueError("事件触发器必须包含 event.type 字段")

    async def execute(self, event: ExecutionEvent) -> ExecutionResult:
        """
        事件处理函数

        Args:
            event: 触发的事件对象

        Returns:
            ExecutionResult: 执行结果
        """
        if not self.enabled:
            logger.debug(f"事件触发器 {self.name} 已禁用，跳过执行")
            return ExecutionResult(success=False, message="触发器已禁用", data={"trigger_id": self.id})

        # 检查事件类型是否匹配
        if event.event_type.value != self.event_type:
            logger.debug(f"事件类型不匹配: 期望 {self.event_type}, 实际 {event.event_type.value}")
            return ExecutionResult(
                success=False,
                message="事件类型不匹配",
                data={
                    "trigger_id": self.id,
                    "expected_type": self.event_type,
                    "actual_type": event.event_type.value,
                },
            )

        # 检查过滤条件
        if not self._check_filter(event.data):
            logger.debug(f"事件 {event.event_type.value} 不满足过滤条件: {self.filter_expression}")
            return ExecutionResult(
                success=False,
                message="事件不满足过滤条件",
                data={
                    "trigger_id": self.id,
                    "filter": self.filter_expression,
                    "event_data": event.data,
                },
            )

        logger.info(f"事件触发器 {self.name} 被触发: {event.event_type.value}, 数据: {event.data}")

        # 执行配置的动作
        return await self.execute_actions(context=event.data)

    def _check_filter(self, event_data: dict[str, Any]) -> bool:
        """
        检查事件数据是否满足过滤条件

        Args:
            event_data: 事件数据

        Returns:
            bool: 是否满足过滤条件
        """
        if not self.filter_expression:
            # 没有过滤条件，所有事件都通过
            return True

        try:
            # 使用 simpleeval 安全评估表达式 - 防止代码注入
            allowed_names = {**event_data}
            logger.debug(f"安全评估过滤表达式: {self.filter_expression}")
            result = simple_eval(self.filter_expression, names=allowed_names)
            return bool(result)

        except InvalidExpression as e:
            logger.error(f"过滤条件求值失败（无效表达式）: {e}, 表达式: {self.filter_expression}")
            # 出错时默认不通过，避免误触发
            return False
        except Exception as e:
            logger.error(f"过滤条件求值失败: {e}, 表达式: {self.filter_expression}")
            # 出错时默认不通过，避免误触发
            return False

    def matches_event(self, event_type: str) -> bool:
        """
        检查触发器是否监听指定类型的事件

        Args:
            event_type: 事件类型

        Returns:
            bool: 是否匹配
        """
        return self.event_type == event_type

    def __repr__(self) -> str:
        filter_str = f" filter={self.filter_expression}" if self.filter_expression else ""
        return f"<EventTrigger id={self.id} name={self.name} event={self.event_type}{filter_str}>"
