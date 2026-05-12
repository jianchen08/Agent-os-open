"""
用量监控与告警模块

实时监控 LLM API 使用量,在达到配额阈值时触发告警并执行保护策略。
"""

import asyncio
import logging

logger = logging.getLogger(__name__)
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.llm.base import TokenUsage


class AlertLevel(str, Enum):
    """告警级别"""

    INFO = "info"  # 信息: 用量正常
    WARNING = "warning"  # 警告: 达到 80%
    CRITICAL = "critical"  # 严重: 达到 90%
    EXHAUSTED = "exhausted"  # 耗尽: 达到 100%


class AlertAction(str, Enum):
    """告警动作"""

    LOG_ONLY = "log_only"  # 仅记录日志
    SAVE_CHECKPOINT = "save_checkpoint"  # 保存检查点
    PAUSE_EXECUTION = "pause_execution"  # 暂停执行
    STOP_EXECUTION = "stop_execution"  # 停止执行


@dataclass
class UsageAlert:
    """用量告警"""

    level: AlertLevel
    usage_percent: float
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    action_taken: AlertAction | None = None


class QuotaConfig(BaseModel):
    """配额配置"""

    # 配额限制
    daily_token_limit: int | None = Field(None, description="每日 token 限制")
    daily_request_limit: int | None = Field(None, description="每日请求次数限制")
    monthly_token_limit: int | None = Field(None, description="每月 token 限制")

    # 告警阈值
    warning_threshold: float = Field(0.8, description="警告阈值 (0-1)")
    critical_threshold: float = Field(0.9, description="严重阈值 (0-1)")

    # 自动保护策略
    auto_save_at_warning: bool = Field(True, description="达到警告阈值自动保存")
    auto_pause_at_critical: bool = Field(True, description="达到严重阈值自动暂停")
    auto_stop_at_exhausted: bool = Field(True, description="达到配额自动停止")


class UsageRecord(BaseModel):
    """用量记录"""

    timestamp: datetime = Field(default_factory=datetime.now)
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "request_id": self.request_id,
        }


class UsageStatistics(BaseModel):
    """用量统计"""

    # 今日统计
    today_tokens: int = 0
    today_requests: int = 0

    # 本月统计
    month_tokens: int = 0
    month_requests: int = 0

    # 总计
    total_tokens: int = 0
    total_requests: int = 0

    # 使用率
    daily_token_usage_percent: float = 0.0
    monthly_token_usage_percent: float = 0.0

    def update(self, usage: TokenUsage) -> None:
        """更新统计"""
        self.today_tokens += usage.total_tokens
        self.today_requests += 1
        self.month_tokens += usage.total_tokens
        self.month_requests += 1
        self.total_tokens += usage.total_tokens
        self.total_requests += 1


class UsageMonitor:
    """
    用量监控器

    功能:
    - 实时统计 token 使用量
    - 计算配额使用率
    - 触发阈值告警
    - 执行保护策略
    """

    def __init__(
        self,
        config: QuotaConfig,
        alert_callback: Callable[[UsageAlert], None] | None = None,
    ):
        """
        初始化用量监控器

        Args:
            config: 配额配置
            alert_callback: 告警回调函数
        """
        self.config = config
        self.alert_callback = alert_callback

        # 用量记录
        self._usage_records: list[UsageRecord] = []
        self._statistics = UsageStatistics()

        # 今日/本月开始时间
        self._day_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self._month_start = datetime.now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )

        # 锁
        self._lock = asyncio.Lock()

        # 告警状态 (防止重复告警)
        self._last_alert_level: AlertLevel | None = None

    async def record_usage(
        self,
        usage: TokenUsage,
        model: str,
        request_id: str | None = None,
    ) -> UsageAlert | None:
        """
        记录一次 API 使用

        Args:
            usage: token 使用量
            model: 模型名称
            request_id: 请求 ID

        Returns:
            如果触发告警,返回告警对象
        """
        async with self._lock:
            # 检查是否需要重置统计 (跨天/跨月)
            await self._check_and_reset_statistics()

            # 记录使用
            record = UsageRecord(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                model=model,
                request_id=request_id,
            )
            self._usage_records.append(record)

            # 更新统计
            self._statistics.update(usage)

            # 计算使用率
            await self._calculate_usage_percent()

            # 检查告警
            return await self._check_alerts()

    async def _check_and_reset_statistics(self) -> None:
        """检查并重置统计 (跨天/跨月)"""
        now = datetime.now()

        # 跨天重置
        if now.date() > self._day_start.date():
            self._day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self._statistics.today_tokens = 0
            self._statistics.today_requests = 0

        # 跨月重置
        if now.month != self._month_start.month or now.year != self._month_start.year:
            self._month_start = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            self._statistics.month_tokens = 0
            self._statistics.month_requests = 0

    async def _calculate_usage_percent(self) -> None:
        """计算配额使用率"""
        stats = self._statistics

        # 每日 token 使用率
        if self.config.daily_token_limit:
            stats.daily_token_usage_percent = (
                stats.today_tokens / self.config.daily_token_limit
            )
        else:
            stats.daily_token_usage_percent = 0.0

        # 每月 token 使用率
        if self.config.monthly_token_limit:
            stats.monthly_token_usage_percent = (
                stats.month_tokens / self.config.monthly_token_limit
            )
        else:
            stats.monthly_token_usage_percent = 0.0

    async def _check_alerts(self) -> UsageAlert | None:
        """检查是否触发告警"""
        # 使用两个限制中较高的使用率
        usage_percent = max(
            self._statistics.daily_token_usage_percent,
            self._statistics.monthly_token_usage_percent,
        )

        # 确定告警级别
        alert_level: AlertLevel | None = None
        action: AlertAction | None = None

        if usage_percent >= 1.0:
            alert_level = AlertLevel.EXHAUSTED
            action = (
                AlertAction.STOP_EXECUTION
                if self.config.auto_stop_at_exhausted
                else None
            )
        elif usage_percent >= self.config.critical_threshold:
            alert_level = AlertLevel.CRITICAL
            action = (
                AlertAction.PAUSE_EXECUTION
                if self.config.auto_pause_at_critical
                else None
            )
        elif usage_percent >= self.config.warning_threshold:
            alert_level = AlertLevel.WARNING
            action = (
                AlertAction.SAVE_CHECKPOINT
                if self.config.auto_save_at_warning
                else None
            )
        else:
            alert_level = AlertLevel.INFO
            action = None

        # 防止重复告警 (相同级别不重复告警)
        if alert_level == self._last_alert_level:
            return None

        self._last_alert_level = alert_level

        # 构建告警消息
        alert = UsageAlert(
            level=alert_level,
            usage_percent=usage_percent * 100,
            message=self._build_alert_message(alert_level, usage_percent),
            action_taken=action,
        )

        # 调用回调
        if self.alert_callback:
            try:
                await self.alert_callback(alert)
            except Exception as e:
                print(f"告警回调执行失败: {e}")

        return alert

    def _build_alert_message(self, level: AlertLevel, usage_percent: float) -> str:
        """构建告警消息"""
        stats = self._statistics

        if level == AlertLevel.EXHAUSTED:
            return (
                f"⛔ API 配额已耗尽!\n"
                f"今日用量: {stats.today_tokens:,} tokens ({stats.daily_token_usage_percent * 100:.1f}%)\n"
                f"本月用量: {stats.month_tokens:,} tokens ({stats.monthly_token_usage_percent * 100:.1f}%)\n"
                f"已自动停止执行,请等待配额重置或升级套餐。"
            )
        elif level == AlertLevel.CRITICAL:
            return (
                f"🚨 API 配额即将耗尽!\n"
                f"当前用量: {usage_percent * 100:.1f}%\n"
                f"今日: {stats.today_tokens:,} tokens | "
                f"本月: {stats.month_tokens:,} tokens\n"
                f"建议立即暂停任务并保存进度。"
            )
        elif level == AlertLevel.WARNING:
            return (
                f"[警告] API 配额使用警告\n"
                f"当前用量: {usage_percent * 100:.1f}%\n"
                f"今日: {stats.today_tokens:,} tokens | "
                f"本月: {stats.month_tokens:,} tokens\n"
                f"已自动创建检查点,任务可随时恢复。"
            )
        else:
            return (
                f"[正常] 用量正常\n"
                f"今日: {stats.today_tokens:,} tokens | "
                f"本月: {stats.month_tokens:,} tokens"
            )

    def get_statistics(self) -> UsageStatistics:
        """获取当前统计"""
        return self._statistics

    def get_recent_records(self, limit: int = 100) -> list[UsageRecord]:
        """获取最近的用量记录"""
        return self._usage_records[-limit:]

    def export_usage_report(self) -> dict[str, Any]:
        """导出用量报告"""
        return {
            "generated_at": datetime.now().isoformat(),
            "statistics": self._statistics.model_dump(),
            "config": self.config.model_dump(),
            "recent_records": [r.to_dict() for r in self.get_recent_records(50)],
        }

    async def reset_statistics(self) -> None:
        """重置统计"""
        self._statistics = UsageStatistics()
        self._usage_records.clear()
        self._last_alert_level = None
