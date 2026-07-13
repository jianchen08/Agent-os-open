"""
时间触发器

基于时间调度执行动作的触发器，支持 Cron 表达式、间隔和单次触发。
"""

import logging
from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger as APSchedulerCronTrigger
from apscheduler.triggers.date import DateTrigger as APSchedulerDateTrigger
from apscheduler.triggers.interval import IntervalTrigger as APSchedulerIntervalTrigger

from src.triggers.models import ExecutionResult, TriggerConfig, TriggerType
from src.triggers.triggers.base import BaseTrigger

logger = logging.getLogger(__name__)


class TimeTrigger(BaseTrigger):
    """
    时间触发器

    基于 APScheduler 实现的时间调度触发器。

    支持的调度类型:
    - cron: Cron 表达式
    - interval: 固定间隔
    - date: 单次执行
    """

    def __init__(self, config: TriggerConfig):
        """
        初始化时间触发器

        Args:
            config: 触发器配置，必须包含 schedule 字段
        """
        super().__init__(config)

        if config.trigger_type != TriggerType.TIME:
            raise ValueError(f"触发器类型必须是 TIME，实际是 {config.trigger_type}")

        if not config.schedule:
            raise ValueError("时间触发器必须包含 schedule 配置")

        self.schedule_config = config.schedule
        self.schedule_type = self.schedule_config.get("type", "cron")

    async def execute(self, *args, **kwargs) -> ExecutionResult:
        """
        执行触发器动作

        Returns:
            ExecutionResult: 执行结果
        """
        if not self.enabled:
            logger.debug(f"时间触发器 {self.name} 已禁用，跳过执行")
            return ExecutionResult(success=False, message="触发器已禁用", data={"trigger_id": self.id})

        logger.info(f"时间触发器 {self.name} 被触发，执行动作...")

        # 执行配置的动作
        return await self.execute_actions(
            context={
                "trigger_type": "time",
                "triggered_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def get_apscheduler_trigger(self):
        """
        获取 APScheduler 触发器对象

        Returns:
            APScheduler trigger object
        """
        schedule_type = self.schedule_config.get("type")

        if schedule_type == "cron":
            return self._get_cron_trigger()
        if schedule_type == "interval":
            return self._get_interval_trigger()
        if schedule_type == "date":
            return self._get_date_trigger()
        raise ValueError(f"不支持的调度类型: {schedule_type}")

    def _get_cron_trigger(self) -> APSchedulerCronTrigger:
        """
        创建 Cron 触发器

        Returns:
            APSchedulerCronTrigger: Cron 触发器对象
        """
        expression = self.schedule_config.get("expression")

        if not expression:
            raise ValueError("Cron 触发器必须包含 expression 字段")

        # 支持标准 5 段或 6 段 Cron 表达式
        # 分 时 日 月 周 [年]
        try:
            return APSchedulerCronTrigger.from_crontab(expression, timezone=timezone.utc)
        except Exception as e:
            # 如果不是标准 crontab 格式，尝试手动解析
            logger.debug(f"Crontab 解析失败: {e}，尝试手动解析")

            return APSchedulerCronTrigger(
                minute=self.schedule_config.get("minute", "*"),
                hour=self.schedule_config.get("hour", "*"),
                day=self.schedule_config.get("day", "*"),
                month=self.schedule_config.get("month", "*"),
                day_of_week=self.schedule_config.get("day_of_week", "*"),
                timezone=timezone.utc,
            )

    def _get_interval_trigger(self) -> APSchedulerIntervalTrigger:
        """
        创建间隔触发器

        Returns:
            APSchedulerIntervalTrigger: 间隔触发器对象
        """
        weeks = self.schedule_config.get("weeks", 0)
        days = self.schedule_config.get("days", 0)
        hours = self.schedule_config.get("hours", 0)
        minutes = self.schedule_config.get("minutes", 0)
        seconds = self.schedule_config.get("seconds", 0)

        # 确保至少有一个时间单位
        if all([weeks == 0, days == 0, hours == 0, minutes == 0, seconds == 0]):
            raise ValueError("间隔触发器必须至少指定一个时间单位")

        return APSchedulerIntervalTrigger(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)

    def _get_date_trigger(self) -> APSchedulerDateTrigger:
        """
        创建单次触发器

        Returns:
            APSchedulerDateTrigger: 单次触发器对象
        """
        run_date_str = self.schedule_config.get("datetime")

        if not run_date_str:
            raise ValueError("单次触发器必须包含 datetime 字段")

        # 解析日期时间
        try:
            run_date = datetime.fromisoformat(run_date_str)

            # naive datetime（用户未带时区）按 UTC 解释，避免与下方 aware 的
            # datetime.now(timezone.utc) 比较时抛 TypeError（aware vs naive 非法比较）。
            if run_date.tzinfo is None:
                run_date = run_date.replace(tzinfo=timezone.utc)

            # 检查时间是否已过期
            now = datetime.now(timezone.utc)
            if run_date < now:
                logger.warning(f"时间触发器 {self.id} 的执行时间已过期: {run_date_str}")
                # 对于过期的单次触发器，返回一个永远不会触发的触发器
                # 设置为一个很远的未来时间
                run_date = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

        except ValueError as e:
            raise ValueError(f"无效的日期时间格式: {run_date_str}, {e}")  # noqa: B904

        return APSchedulerDateTrigger(run_date=run_date)

    def get_next_run_time(self) -> datetime | None:
        """
        获取下次运行时间

        Returns:
            Optional[datetime]: 下次运行时间，如果无法计算则返回 None
        """
        try:
            trigger = self.get_apscheduler_trigger()
            now = datetime.now(timezone.utc)
            next_time = trigger.get_next_fire_time(None, now)
            return next_time
        except Exception as e:
            logger.error(f"计算下次运行时间失败: {e}")
            return None

    def __repr__(self) -> str:
        schedule_info = f"{self.schedule_type}"
        if self.schedule_type == "cron":
            schedule_info = f"cron({self.schedule_config.get('expression', '* * * * *')})"
        elif self.schedule_type == "interval":
            schedule_info = f"interval({self.schedule_config})"
        elif self.schedule_type == "date":
            schedule_info = f"date({self.schedule_config.get('datetime', 'N/A')})"

        return f"<TimeTrigger id={self.id} name={self.name} schedule={schedule_info}>"
