"""触发器管理器。

管理触发器的注册、评估和执行，支持事件触发、条件触发和定时触发。

公共 API:
    TriggerManager: 触发器管理器类
"""

import datetime
import logging
from typing import Any

from .types import TriggerConfig, TriggerStatus, TriggerType

logger = logging.getLogger(__name__)


class TriggerManager:
    """触发器管理器。

    支持：
    - 注册/注销触发器
    - 评估事件触发器
    - 评估条件触发器
    - 检查定时触发器
    - 按类型/状态查询触发器
    """

    def __init__(self) -> None:
        """初始化管理器。"""
        self._triggers: dict[str, TriggerConfig] = {}

    def register(self, config: TriggerConfig) -> None:
        """注册触发器。

        注册后自动将状态设为 ACTIVE。

        Args:
            config: 触发器配置。
        """
        config.status = TriggerStatus.ACTIVE
        self._triggers[config.trigger_id] = config
        logger.info(f"注册触发器: {config.trigger_id} - {config.name}")

    def unregister(self, trigger_id: str) -> bool:
        """注销触发器。

        Args:
            trigger_id: 触发器 ID。

        Returns:
            是否成功注销（False 表示触发器不存在）。
        """
        if trigger_id in self._triggers:
            del self._triggers[trigger_id]
            logger.info(f"注销触发器: {trigger_id}")
            return True
        return False

    def evaluate_event(
        self, event_name: str, event_data: dict[str, Any]
    ) -> list[str]:
        """评估事件触发器。

        遍历所有 EVENT 类型的触发器，检查事件名称和数据是否匹配。
        匹配的触发器将 fire_count +1，达到 max_fires 时状态变为 FIRED。

        Args:
            event_name: 事件名称。
            event_data: 事件数据字典。

        Returns:
            被触发的 trigger_id 列表。
        """
        fired: list[str] = []

        for trigger in self._triggers.values():
            if trigger.trigger_type != TriggerType.EVENT:
                continue
            if trigger.status != TriggerStatus.ACTIVE:
                continue
            if trigger.event_name != event_name:
                continue
            if not self._match_event_filter(trigger, event_data):
                continue

            # 触发
            trigger.fire_count += 1
            fired.append(trigger.trigger_id)

            # 检查是否达到最大触发次数
            if (
                trigger.max_fires > 0
                and trigger.fire_count >= trigger.max_fires
            ):
                trigger.status = TriggerStatus.FIRED

            logger.info(
                f"事件触发器触发: {trigger.trigger_id} "
                f"(事件: {event_name}, 第 {trigger.fire_count} 次)"
            )

        return fired

    def evaluate_condition(
        self, context: dict[str, Any]
    ) -> list[str]:
        """评估条件触发器。

        在 context 命名空间中执行条件表达式，求值为 True 时触发。

        Args:
            context: 上下文变量字典，作为条件表达式的求值环境。

        Returns:
            被触发的 trigger_id 列表。
        """
        fired: list[str] = []

        for trigger in self._triggers.values():
            if trigger.trigger_type != TriggerType.CONDITION:
                continue
            if trigger.status != TriggerStatus.ACTIVE:
                continue
            if not trigger.condition_expression:
                continue

            try:
                result = self._eval_condition(
                    trigger.condition_expression, context
                )
                if result:
                    trigger.fire_count += 1
                    fired.append(trigger.trigger_id)

                    if (
                        trigger.max_fires > 0
                        and trigger.fire_count >= trigger.max_fires
                    ):
                        trigger.status = TriggerStatus.FIRED

                    logger.info(
                        f"条件触发器触发: {trigger.trigger_id} "
                        f"(表达式: {trigger.condition_expression})"
                    )
            except Exception as e:
                logger.warning(
                    f"条件评估失败: {trigger.trigger_id}, "
                    f"表达式: {trigger.condition_expression}, 错误: {e}"
                )

        return fired

    def check_scheduled(
        self, now: datetime.datetime
    ) -> list[str]:
        """检查定时/延迟触发器。

        对于 DELAY 类型，检查从注册时刻起是否已过 delay_seconds。
        对于 SCHEDULED 类型，检查 scheduled_at 是否已到。
        不处理 cron 表达式（需要外部调度器驱动）。

        Args:
            now: 当前时间。

        Returns:
            被触发的 trigger_id 列表。
        """
        fired: list[str] = []

        for trigger in self._triggers.values():
            if trigger.status != TriggerStatus.ACTIVE:
                continue

            should_fire = False

            if trigger.trigger_type == TriggerType.DELAY:
                should_fire = self._check_delay(trigger, now)
            elif trigger.trigger_type == TriggerType.SCHEDULED:
                should_fire = self._check_scheduled_time(trigger, now)

            if should_fire:
                trigger.fire_count += 1
                fired.append(trigger.trigger_id)

                if (
                    trigger.max_fires > 0
                    and trigger.fire_count >= trigger.max_fires
                ):
                    trigger.status = TriggerStatus.FIRED

                logger.info(f"定时触发器触发: {trigger.trigger_id}")

        return fired

    def get(self, trigger_id: str) -> TriggerConfig | None:
        """按 ID 获取触发器。

        Args:
            trigger_id: 触发器 ID。

        Returns:
            触发器配置，不存在时返回 None。
        """
        return self._triggers.get(trigger_id)

    def list_by_type(
        self, trigger_type: TriggerType
    ) -> list[TriggerConfig]:
        """按类型列出触发器。

        Args:
            trigger_type: 触发器类型。

        Returns:
            匹配的触发器列表。
        """
        return [
            t
            for t in self._triggers.values()
            if t.trigger_type == trigger_type
        ]

    def list_active(self) -> list[TriggerConfig]:
        """列出所有活跃触发器。

        Returns:
            状态为 ACTIVE 的触发器列表。
        """
        return [
            t
            for t in self._triggers.values()
            if t.status == TriggerStatus.ACTIVE
        ]

    def cancel(self, trigger_id: str) -> bool:
        """取消触发器。

        将状态设为 CANCELLED。

        Args:
            trigger_id: 触发器 ID。

        Returns:
            是否成功取消。
        """
        trigger = self._triggers.get(trigger_id)
        if trigger is None:
            return False
        if trigger.status in (TriggerStatus.FIRED, TriggerStatus.CANCELLED):
            return False
        trigger.status = TriggerStatus.CANCELLED
        return True

    def _match_event_filter(
        self, trigger: TriggerConfig, event_data: dict[str, Any]
    ) -> bool:
        """检查事件数据是否匹配过滤条件。

        Args:
            trigger: 触发器配置。
            event_data: 事件数据。

        Returns:
            是否匹配。
        """
        if not trigger.event_filter:
            return True

        for key, expected in trigger.event_filter.items():
            actual = event_data.get(key)
            if isinstance(expected, dict):
                op = expected.get("op", "eq")
                value = expected.get("value")
                if not self._compare(actual, op, value):
                    return False
            else:
                if actual != expected:
                    return False

        return True

    def _compare(
        self, actual: Any, op: str, value: Any
    ) -> bool:
        """比较操作。

        支持 eq, ne, gt, lt, gte, lte, contains 操作符。

        Args:
            actual: 实际值。
            op: 操作符。
            value: 期望值。

        Returns:
            比较结果。
        """
        if op == "eq":
            return actual == value
        elif op == "ne":
            return actual != value
        elif op == "gt":
            return actual > value
        elif op == "lt":
            return actual < value
        elif op == "gte":
            return actual >= value
        elif op == "lte":
            return actual <= value
        elif op == "contains":
            return value in str(actual)
        return False

    def _eval_condition(
        self, expression: str, context: dict[str, Any]
    ) -> bool:
        """安全地评估条件表达式。

        使用 condition_parser 替代 eval()，杜绝代码注入风险。

        Args:
            expression: 条件表达式字符串。
            context: 上下文变量字典。

        Returns:
            表达式求值结果。
        """
        from pipeline.condition_parser import parse_condition

        return parse_condition(expression, context)

    def _check_delay(
        self, trigger: TriggerConfig, now: datetime.datetime
    ) -> bool:
        """检查延迟触发器是否到期。

        通过 metadata 中的 register_time 计算是否已过 delay_seconds。

        Args:
            trigger: 触发器配置。
            now: 当前时间。

        Returns:
            是否到期。
        """
        if trigger.trigger_type != TriggerType.DELAY:
            return False
        if trigger.delay_seconds <= 0:
            return False

        register_time_str = trigger.metadata.get("register_time")
        if not register_time_str:
            return False

        try:
            register_time = datetime.datetime.fromisoformat(
                register_time_str
            )
            elapsed = (now - register_time).total_seconds()
            return elapsed >= trigger.delay_seconds
        except (ValueError, TypeError):
            return False

    def _check_scheduled_time(
        self, trigger: TriggerConfig, now: datetime.datetime
    ) -> bool:
        """检查定时触发器是否到期。

        比较 scheduled_at 与当前时间。

        Args:
            trigger: 触发器配置。
            now: 当前时间。

        Returns:
            是否到期。
        """
        if trigger.trigger_type != TriggerType.SCHEDULED:
            return False
        if trigger.scheduled_at is None:
            return False

        return now >= trigger.scheduled_at
