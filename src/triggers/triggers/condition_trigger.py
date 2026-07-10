"""
条件触发器

基于自定义条件表达式判断是否触发动作。
"""

import logging
from typing import Any

from simpleeval import InvalidExpression, simple_eval

from src.core.event_bus.types import ExecutionEvent
from src.triggers.models import ExecutionResult, TriggerConfig, TriggerType
from src.triggers.triggers.base import BaseTrigger

logger = logging.getLogger(__name__)


class ConditionTrigger(BaseTrigger):
    """
    条件触发器

    监听系统事件并根据复杂的条件表达式判断是否执行动作。
    支持复合条件、阈值判断、外部触发等场景。
    """

    def __init__(self, config: TriggerConfig):
        """
        初始化条件触发器

        Args:
            config: 触发器配置，必须包含 condition 字段
        """
        super().__init__(config)

        if config.trigger_type != TriggerType.CONDITION:
            raise ValueError(f"触发器类型必须是 CONDITION，实际是 {config.trigger_type}")

        if not config.condition:
            raise ValueError("条件触发器必须包含 condition 配置")

        self.condition_config = config.condition
        self.expression = self.condition_config.get("expression")
        self.watch_event_types = self.condition_config.get("watch_events", [])

        # 状态追踪
        self._event_history: list[ExecutionEvent] = []
        self._max_history = self.condition_config.get("max_history", 100)

    async def execute(self, event: ExecutionEvent) -> ExecutionResult:
        """
        事件处理函数

        Args:
            event: 触发的事件对象

        Returns:
            ExecutionResult: 执行结果
        """
        if not self.enabled:
            logger.debug(f"条件触发器 {self.name} 已禁用，跳过执行")
            return ExecutionResult(success=False, message="触发器已禁用", data={"trigger_id": self.id})

        # 检查是否需要监听此事件类型
        event_type_value = event.event_type.value
        if self.watch_event_types and event_type_value not in self.watch_event_types:
            logger.debug(f"条件触发器 {self.name} 不监听事件类型: {event_type_value}")
            return ExecutionResult(
                success=False,
                message="不监听此事件类型",
                data={
                    "trigger_id": self.id,
                    "event_type": event_type_value,
                    "watch_events": self.watch_event_types,
                },
            )

        # 添加到历史记录
        self._add_to_history(event)

        # 检查条件是否满足
        if not self._check_condition(event):
            logger.debug(f"条件触发器 {self.name} 条件不满足: {self.expression}")
            return ExecutionResult(
                success=False,
                message="条件不满足",
                data={
                    "trigger_id": self.id,
                    "condition": self.expression,
                    "event_type": event.event_type.value,
                    "event_data": event.data,
                },
            )

        logger.info(f"条件触发器 {self.name} 被触发: {event.event_type.value}, 条件: {self.expression}")

        # 执行配置的动作
        context = {
            **event.data,
            "_event_history": [e.model_dump() for e in self._event_history],
            "_trigger_id": self.id,
        }

        return await self.execute_actions(context=context)

    def _check_condition(self, event: ExecutionEvent) -> bool:
        """
        检查条件是否满足

        Args:
            event: 当前事件

        Returns:
            bool: 条件是否满足
        """
        if not self.expression:
            return True

        try:
            # 构建求值上下文
            context = {
                **event.data,
                "event": event.data,
                "event_type": event.event_type.value,
                "timestamp": event.timestamp.isoformat(),
                # 历史统计
                "history_count": len(self._event_history),
                "recent_events": [e.model_dump() for e in self._event_history[-10:]],
            }

            # 使用 simpleeval 安全评估表达式 - 防止代码注入
            logger.debug(f"安全评估条件表达式: {self.expression}")
            result = simple_eval(self.expression, names=context)

            return bool(result)

        except InvalidExpression as e:
            logger.error(f"条件表达式求值失败（无效表达式）: {e}, 表达式: {self.expression}")
            return False
        except Exception as e:
            logger.error(f"条件表达式求值失败: {e}, 表达式: {self.expression}")
            return False

    def _add_to_history(self, event: ExecutionEvent) -> None:
        """
        添加事件到历史记录

        Args:
            event: 事件对象
        """
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history.pop(0)

    def get_event_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        获取事件历史

        Args:
            limit: 返回数量限制

        Returns:
            List[Dict[str, Any]]: 事件列表
        """
        return [e.model_dump() for e in self._event_history[-limit:]]

    def clear_history(self) -> None:
        """清空事件历史"""
        self._event_history.clear()
        logger.debug(f"条件触发器 {self.name} 历史记录已清空")

    def matches_event(self, event_type: str) -> bool:
        """
        检查触发器是否监听指定类型的事件

        Args:
            event_type: 事件类型

        Returns:
            bool: 是否匹配
        """
        # 如果没有指定监听的事件类型，则监听所有事件
        if not self.watch_event_types:
            return True
        return event_type in self.watch_event_types

    def __repr__(self) -> str:
        watch_str = f" watch={self.watch_event_types}" if self.watch_event_types else ""
        return f"<ConditionTrigger id={self.id} name={self.name} condition='{self.expression}'{watch_str}>"
