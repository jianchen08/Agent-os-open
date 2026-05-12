"""
监控协调器 - 负责监控和用量管理

职责：
- 初始化和管理用量监控器
- 初始化和管理任务进度管理器
- 处理用量告警
- 收集性能指标
"""

import logging
from collections.abc import Callable
from typing import Any

from src.agents.interfaces import ITaskProgressManager, IUsageMonitor

logger = logging.getLogger(__name__)


class MonitoringCoordinator:
    """
    监控协调器

    负责监控和进度管理相关服务的初始化和管理
    """

    def __init__(
        self,
        session_id: str,
        user_id: str | None = None,
        quota_config: Any | None = None,
        usage_monitor: IUsageMonitor | None = None,
        task_progress_manager: ITaskProgressManager | None = None,
        alert_callback: Callable | None = None,
    ):
        """
        初始化监控协调器

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            quota_config: 配额配置
            usage_monitor: 用量监控器（可选，注入）
            task_progress_manager: 任务进度管理器（可选，注入）
            alert_callback: 告警回调函数
        """
        self.session_id = session_id
        self.user_id = user_id
        self._quota_config = quota_config
        self._alert_callback = alert_callback or self._default_alert_callback

        # 依赖注入的组件（优先使用注入的，否则由协调器创建）
        self._usage_monitor = usage_monitor
        self._task_progress_manager = task_progress_manager

    async def initialize(self) -> None:
        """初始化监控组件"""
        await self._initialize_usage_monitor()
        await self._initialize_task_progress_manager()

    async def _initialize_usage_monitor(self) -> None:
        """初始化用量监控器（如果未注入）"""
        if self._usage_monitor is not None:
            return

        try:
            from src.monitoring.usage_monitor import (
                QuotaConfig,
                UsageMonitor,
            )

            # 使用注入的配置或默认配置
            quota_config = self._quota_config or QuotaConfig()

            self._usage_monitor = UsageMonitor(
                config=quota_config,
                alert_callback=self._handle_usage_alert,
            )
            logger.debug("[MonitoringCoordinator] UsageMonitor 已创建")
        except Exception as e:
            logger.warning(f"[MonitoringCoordinator] UsageMonitor 创建失败: {e}")

    async def _initialize_task_progress_manager(self) -> None:
        """初始化任务进度管理器（如果未注入）"""
        if self._task_progress_manager is not None:
            return

        try:
            from src.monitoring.task_progress import TaskProgressManager

            self._task_progress_manager = TaskProgressManager(
                session_id=self.session_id,
                user_id=self.user_id,
                auto_save=True,
            )
            logger.debug("[MonitoringCoordinator] TaskProgressManager 已创建")
        except Exception as e:
            logger.warning(f"[MonitoringCoordinator] TaskProgressManager 创建失败: {e}")

    def _handle_usage_alert(self, alert: Any) -> None:
        """
        处理用量告警

        Args:
            alert: 告警对象
        """
        logger.warning(
            f"[MonitoringCoordinator] 用量告警: {alert.level.value.upper()} - {alert.message}"
        )

        # 调用外部回调（如果提供）
        if self._alert_callback:
            self._alert_callback(alert)

    def _default_alert_callback(self, alert: Any) -> None:
        """
        默认告警回调函数

        Args:
            alert: 告警对象
        """
        logger.debug(f"📊 用量告警: {alert.level.value.upper()} - {alert.message}")

    @property
    def usage_monitor(self) -> IUsageMonitor | None:
        """获取用量监控器"""
        return self._usage_monitor

    @property
    def task_progress_manager(self) -> ITaskProgressManager | None:
        """获取任务进度管理器"""
        return self._task_progress_manager

    def get_usage_statistics(self) -> dict[str, Any] | None:
        """
        获取用量统计

        Returns:
            用量统计字典，如果监控器未初始化则返回 None
        """
        if self._usage_monitor:
            stats = self._usage_monitor.get_statistics()
            return stats.model_dump()
        return None

    async def cleanup(self) -> None:
        """清理监控协调器资源"""
        if self._task_progress_manager:
            await self._task_progress_manager.cleanup()
            self._task_progress_manager = None

        logger.debug("[MonitoringCoordinator] 监控协调器资源已清理")
