"""
触发器注册表

管理触发器的注册、检索、加载和生命周期。
"""

import logging
from pathlib import Path
from typing import Any

import yaml

from src.core.event_bus import get_event_bus
from src.triggers.models import TriggerConfig, TriggerType
from src.triggers.triggers.base import BaseTrigger
from src.triggers.triggers.condition_trigger import ConditionTrigger
from src.triggers.triggers.event_trigger import EventTrigger
from src.triggers.triggers.time_trigger import TimeTrigger

logger = logging.getLogger(__name__)


class TriggerRegistry:
    """
    触发器注册表

    功能:
    - 从配置文件加载触发器
    - 管理触发器生命周期
    - 提供触发器查询接口
    - 自动订阅事件触发器到事件总线
    """

    def __init__(self, config_dir: str = "config/triggers"):
        """
        初始化触发器注册表

        Args:
            config_dir: 触发器配置文件目录
        """
        self._triggers: dict[str, BaseTrigger] = {}
        self._config_dir = Path(config_dir)
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._event_bus = get_event_bus()
        self._subscription_ids: dict[str, list[str]] = {}

        logger.info(f"触发器注册表初始化，配置目录: {self._config_dir}")

    async def load_from_config(self) -> None:
        """从配置文件加载所有触发器"""
        if not self._config_dir.exists():
            logger.warning(f"触发器配置目录不存在: {self._config_dir}")
            return

        config_files = list(self._config_dir.glob("*.yaml")) + list(self._config_dir.glob("*.yml"))

        if not config_files:
            logger.warning(f"配置目录中没有找到 YAML 文件: {self._config_dir}")
            return

        logger.info(f"发现 {len(config_files)} 个配置文件")

        for config_file in config_files:
            try:
                await self._load_from_file(config_file)
            except Exception as e:
                logger.error(f"加载配置文件失败 {config_file}: {e}", exc_info=True)

        logger.info(f"触发器加载完成，共加载 {len(self._triggers)} 个触发器")

    async def _load_from_file(self, config_file: Path) -> None:
        """
        从单个配置文件加载触发器

        Args:
            config_file: 配置文件路径
        """
        logger.info(f"加载触发器配置: {config_file}")

        with open(config_file, encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if not config_data:
            logger.warning(f"配置文件为空: {config_file}")
            return

        triggers_data = config_data.get("triggers", [])

        for trigger_data in triggers_data:
            try:
                await self.register_trigger(trigger_data)
            except Exception as e:
                logger.error(
                    f"注册触发器失败: {trigger_data.get('id', 'unknown')}, 错误: {e}",
                    exc_info=True,
                )

    async def register_trigger(self, config: dict[str, Any]) -> None:
        """
        注册触发器

        Args:
            config: 触发器配置字典
        """
        trigger_config = TriggerConfig.from_dict(config)

        # 如果触发器已存在，先注销旧的
        if trigger_config.id in self._triggers:
            await self.unregister_trigger(trigger_config.id)
            logger.info(f"更新触发器: {trigger_config.id}")

        # 创建触发器实例
        trigger = self._create_trigger(trigger_config)

        # 验证触发器
        if not trigger.validate():
            raise ValueError(f"触发器配置无效: {trigger_config.id}")

        # 对于时间触发器，检查是否已过期
        if isinstance(trigger, TimeTrigger) and trigger.schedule_config.get("type") == "date":
            from datetime import datetime  # noqa: PLC0415

            run_date_str = trigger.schedule_config.get("datetime")
            if run_date_str:
                try:
                    run_date = datetime.fromisoformat(run_date_str)
                    if run_date < datetime.utcnow():
                        logger.warning(f"时间触发器 {trigger.id} 已过期 ({run_date_str})，自动禁用")
                        trigger.enabled = False
                except ValueError:
                    logger.error(f"时间触发器 {trigger.id} 时间格式无效: {run_date_str}")

        # 注册到事件总线（如果是事件触发器）
        from src.core.event_bus.types import EventFilter, EventType  # noqa: PLC0415

        self._subscription_ids[trigger.id] = []
        if isinstance(trigger, (EventTrigger, ConditionTrigger)):
            if trigger.trigger_type == TriggerType.EVENT:
                sub_id = self._event_bus.subscribe(
                    trigger.execute, filter=EventFilter(event_types=[EventType(trigger.event_type)])
                )
                self._subscription_ids[trigger.id].append(sub_id)
                logger.info(f"订阅事件: {trigger.event_type} -> {trigger.name}")
            elif trigger.trigger_type == TriggerType.CONDITION:
                # 条件触发器可能监听多个事件类型
                for event_type in trigger.watch_event_types or []:
                    try:
                        et = EventType(event_type)
                    except ValueError:
                        et = EventType.CUSTOM
                    sub_id = self._event_bus.subscribe(trigger.execute, filter=EventFilter(event_types=[et]))
                    self._subscription_ids[trigger.id].append(sub_id)
                logger.info(f"订阅事件: {trigger.watch_event_types} -> {trigger.name}")

        # 保存到注册表
        self._triggers[trigger.id] = trigger
        logger.info(f"触发器已注册: {trigger.name} ({trigger.id})")

    def _create_trigger(self, config: TriggerConfig) -> BaseTrigger:
        """
        创建触发器实例

        Args:
            config: 触发器配置

        Returns:
            BaseTrigger: 触发器实例

        Raises:
            ValueError: 不支持的触发器类型
        """
        if config.trigger_type == TriggerType.TIME:
            return TimeTrigger(config)
        if config.trigger_type == TriggerType.EVENT:
            return EventTrigger(config)
        if config.trigger_type == TriggerType.CONDITION:
            return ConditionTrigger(config)
        raise ValueError(f"不支持的触发器类型: {config.trigger_type}")

    async def unregister_trigger(self, trigger_id: str) -> None:
        """
        注销触发器

        Args:
            trigger_id: 触发器 ID
        """
        trigger = self._triggers.get(trigger_id)

        if not trigger:
            logger.warning(f"触发器不存在: {trigger_id}")
            return

        # 从事件总线取消订阅
        sub_ids = self._subscription_ids.get(trigger_id, [])
        for sub_id in sub_ids:
            self._event_bus.unsubscribe(sub_id)
        if trigger_id in self._subscription_ids:
            del self._subscription_ids[trigger_id]
        if isinstance(trigger, EventTrigger):
            logger.info(f"取消订阅事件: {trigger.event_type}")
        elif isinstance(trigger, ConditionTrigger):
            logger.info(f"取消订阅事件: {trigger.watch_event_types}")

        # 从注册表移除
        del self._triggers[trigger_id]
        logger.info(f"触发器已注销: {trigger_id}")

    async def update_trigger(self, trigger_id: str, config: dict[str, Any]) -> None:
        """
        更新触发器

        Args:
            trigger_id: 触发器 ID
            config: 新的配置
        """
        if config.get("id") != trigger_id:
            raise ValueError("配置中的 ID 必须与 trigger_id 一致")

        await self.unregister_trigger(trigger_id)
        await self.register_trigger(config)

    async def get_trigger(self, trigger_id: str) -> BaseTrigger | None:
        """
        获取触发器

        Args:
            trigger_id: 触发器 ID

        Returns:
            Optional[BaseTrigger]: 触发器实例，不存在返回 None
        """
        return self._triggers.get(trigger_id)

    async def list_triggers(
        self, enabled_only: bool = False, trigger_type: TriggerType | None = None
    ) -> list[BaseTrigger]:
        """
        列出触发器

        Args:
            enabled_only: 只返回已启用的触发器
            trigger_type: 过滤触发器类型

        Returns:
            List[BaseTrigger]: 触发器列表
        """
        triggers = list(self._triggers.values())

        if enabled_only:
            triggers = [t for t in triggers if t.enabled]

        if trigger_type:
            triggers = [t for t in triggers if t.trigger_type == trigger_type]

        return triggers

    async def enable_trigger(self, trigger_id: str) -> bool:
        """
        启用触发器

        Args:
            trigger_id: 触发器 ID

        Returns:
            bool: 是否成功
        """
        trigger = self._triggers.get(trigger_id)
        if not trigger:
            return False

        trigger.enabled = True
        logger.info(f"触发器已启用: {trigger_id}")
        return True

    async def disable_trigger(self, trigger_id: str) -> bool:
        """
        禁用触发器

        Args:
            trigger_id: 触发器 ID

        Returns:
            bool: 是否成功
        """
        trigger = self._triggers.get(trigger_id)
        if not trigger:
            return False

        trigger.enabled = False
        logger.info(f"触发器已禁用: {trigger_id}")
        return True

    def get_stats(self) -> dict[str, Any]:
        """
        获取统计信息

        Returns:
            Dict[str, Any]: 统计信息
        """
        triggers = list(self._triggers.values())

        type_counts = {}
        for trigger in triggers:
            ttype = trigger.trigger_type.value
            type_counts[ttype] = type_counts.get(ttype, 0) + 1

        return {
            "total_triggers": len(triggers),
            "enabled_triggers": sum(1 for t in triggers if t.enabled),
            "disabled_triggers": sum(1 for t in triggers if not t.enabled),
            "type_counts": type_counts,
            "trigger_ids": list(self._triggers.keys()),
        }
