"""
预算管理器

管理 Token 预算、检查配额、触发告警
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.core.exceptions import BudgetExceededException, QuotaExhaustedException
from src.cost_control.config import CostControlConfig, get_cost_control_config

logger = logging.getLogger(__name__)


class BudgetAlertLevel(str, Enum):
    """预算告警级别"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


class BudgetAlertAction(str, Enum):
    """预算告警动作"""

    LOG_ONLY = "log_only"
    SAVE_CHECKPOINT = "save_checkpoint"
    PAUSE_EXECUTION = "pause_execution"
    STOP_EXECUTION = "stop_execution"


@dataclass
class BudgetAlert:
    """预算告警"""

    level: BudgetAlertLevel
    usage_percent: float
    message: str
    scope: str  # global, user, task, session
    scope_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    action_taken: BudgetAlertAction | None = None


@dataclass
class UsageRecord:
    """使用记录"""

    tokens: int
    model: str
    scope: str  # global, user, task, session
    scope_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    cost: float = 0.0


@dataclass
class BudgetStatus:
    """预算状态"""

    scope: str
    scope_id: str | None
    limit: int
    used: int
    remaining: int
    usage_percent: float
    alert_level: BudgetAlertLevel
    estimated_cost: float


class BudgetManager:
    """
    预算管理器

    功能:
    - 跟踪 Token 使用量
    - 检查预算限制
    - 触发告警和保护策略
    - 提供成本统计
    """

    def __init__(
        self,
        config: CostControlConfig | None = None,
        alert_callback: Callable[[BudgetAlert], None] | None = None,
    ):
        """
        初始化预算管理器

        Args:
            config: 成本控制配置
            alert_callback: 告警回调函数
        """
        self.config = config or get_cost_control_config()
        self.alert_callback = alert_callback

        # 使用量跟踪
        self._daily_usage: dict[str, int] = {}  # user_id -> tokens
        self._monthly_usage: dict[str, int] = {}  # user_id -> tokens
        self._task_usage: dict[str, int] = {}  # task_id -> tokens
        self._session_usage: dict[str, int] = {}  # session_id -> tokens
        self._global_daily_usage: int = 0
        self._global_monthly_usage: int = 0

        # 使用记录
        self._usage_records: list[UsageRecord] = []

        # 时间跟踪
        self._day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self._month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # 告警状态（防止重复告警）
        self._last_alerts: dict[str, BudgetAlertLevel] = {}

        # 锁
        self._lock = asyncio.Lock()

    async def check_budget(
        self,
        estimated_tokens: int,
        user_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """
        检查预算是否足够

        Args:
            estimated_tokens: 预估 Token 数
            user_id: 用户 ID
            task_id: 任务 ID
            session_id: 会话 ID

        Returns:
            是否允许执行

        Raises:
            BudgetExceededException: 预算超限
            QuotaExhaustedException: 配额耗尽
        """
        async with self._lock:
            await self._check_and_reset_periods()

            # 检查全局每日限制
            global_daily_after = self._global_daily_usage + estimated_tokens
            if global_daily_after > self.config.global_budget.daily_token_limit:
                raise QuotaExhaustedException(
                    message=f"全局每日配额已耗尽，当前: {self._global_daily_usage}, 限制: {self.config.global_budget.daily_token_limit}",
                    usage_percent=self._global_daily_usage / self.config.global_budget.daily_token_limit * 100,
                    quota_type="daily",
                )

            # 检查全局每月限制
            global_monthly_after = self._global_monthly_usage + estimated_tokens
            if global_monthly_after > self.config.global_budget.monthly_token_limit:
                raise QuotaExhaustedException(
                    message=f"全局每月配额已耗尽，当前: {self._global_monthly_usage}, 限制: {self.config.global_budget.monthly_token_limit}",
                    usage_percent=self._global_monthly_usage / self.config.global_budget.monthly_token_limit * 100,
                    quota_type="monthly",
                )

            # 检查任务限制
            if task_id:
                task_usage = self._task_usage.get(task_id, 0)
                task_after = task_usage + estimated_tokens
                if task_after > self.config.global_budget.per_task_token_limit:
                    raise BudgetExceededException(
                        message=f"任务 {task_id} 预算超限，当前: {task_usage}, 限制: {self.config.global_budget.per_task_token_limit}",
                        current_usage=task_usage,
                        limit=self.config.global_budget.per_task_token_limit,
                        limit_type="task",
                    )

            # 检查会话限制
            if session_id:
                session_usage = self._session_usage.get(session_id, 0)
                session_after = session_usage + estimated_tokens
                if session_after > self.config.global_budget.per_session_token_limit:
                    raise BudgetExceededException(
                        message=f"会话 {session_id} 预算超限，当前: {session_usage}, 限制: {self.config.global_budget.per_session_token_limit}",
                        current_usage=session_usage,
                        limit=self.config.global_budget.per_session_token_limit,
                        limit_type="session",
                    )

            return True

    async def record_usage(
        self,
        tokens: int,
        model: str,
        user_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> BudgetAlert | None:
        """
        记录 Token 使用量

        Args:
            tokens: 使用的 Token 数
            model: 模型名称
            user_id: 用户 ID
            task_id: 任务 ID
            session_id: 会话 ID

        Returns:
            如果触发告警，返回告警对象
        """
        async with self._lock:
            await self._check_and_reset_periods()

            # 计算成本
            cost_rate = self.config.get_model_cost_rate(model)
            cost = (tokens / 1000) * cost_rate

            # 更新全局使用量
            self._global_daily_usage += tokens
            self._global_monthly_usage += tokens

            # 更新用户使用量
            if user_id:
                self._daily_usage[user_id] = self._daily_usage.get(user_id, 0) + tokens
                self._monthly_usage[user_id] = self._monthly_usage.get(user_id, 0) + tokens

            # 更新任务使用量
            if task_id:
                self._task_usage[task_id] = self._task_usage.get(task_id, 0) + tokens

            # 更新会话使用量
            if session_id:
                self._session_usage[session_id] = self._session_usage.get(session_id, 0) + tokens

            # 记录使用
            record = UsageRecord(
                tokens=tokens,
                model=model,
                scope="global",
                scope_id=user_id or task_id or session_id,
                cost=cost,
            )
            self._usage_records.append(record)

            # 检查告警
            return await self._check_alerts()

    async def _check_and_reset_periods(self) -> None:
        """检查并重置周期统计"""
        now = datetime.now()

        # 跨天重置
        if now.date() > self._day_start.date():
            self._day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self._global_daily_usage = 0
            self._daily_usage.clear()
            self._last_alerts.clear()

        # 跨月重置
        if now.month != self._month_start.month or now.year != self._month_start.year:
            self._month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            self._global_monthly_usage = 0
            self._monthly_usage.clear()

    async def _check_alerts(self) -> BudgetAlert | None:
        """检查是否触发告警"""
        # 计算使用率
        daily_percent = self._global_daily_usage / self.config.global_budget.daily_token_limit
        monthly_percent = self._global_monthly_usage / self.config.global_budget.monthly_token_limit
        usage_percent = max(daily_percent, monthly_percent)

        # 确定告警级别
        alert_level: BudgetAlertLevel
        action: BudgetAlertAction | None = None

        if usage_percent >= self.config.alerts.exhausted_threshold:
            alert_level = BudgetAlertLevel.EXHAUSTED
            if self.config.protection.auto_stop_at_exhausted:
                action = BudgetAlertAction.STOP_EXECUTION
        elif usage_percent >= self.config.alerts.critical_threshold:
            alert_level = BudgetAlertLevel.CRITICAL
            if self.config.protection.auto_pause_at_critical:
                action = BudgetAlertAction.PAUSE_EXECUTION
        elif usage_percent >= self.config.alerts.warning_threshold:
            alert_level = BudgetAlertLevel.WARNING
            if self.config.protection.auto_save_at_warning:
                action = BudgetAlertAction.SAVE_CHECKPOINT
        else:
            alert_level = BudgetAlertLevel.INFO
            action = None

        # 防止重复告警
        alert_key = "global"
        if alert_level == self._last_alerts.get(alert_key):
            return None

        self._last_alerts[alert_key] = alert_level

        # 构建告警
        alert = BudgetAlert(
            level=alert_level,
            usage_percent=usage_percent * 100,
            message=self._build_alert_message(alert_level, usage_percent),
            scope="global",
            action_taken=action,
        )

        # 调用回调
        if self.alert_callback and alert_level != BudgetAlertLevel.INFO:
            try:
                if asyncio.iscoroutinefunction(self.alert_callback):
                    await self.alert_callback(alert)
                else:
                    self.alert_callback(alert)
            except Exception as e:
                logger.debug(f"告警回调执行失败: {e}")

        return alert if alert_level != BudgetAlertLevel.INFO else None

    def _build_alert_message(self, level: BudgetAlertLevel, usage_percent: float) -> str:
        """构建告警消息"""
        if level == BudgetAlertLevel.EXHAUSTED:
            return (
                f"⛔ Token 配额已耗尽！\n"
                f"今日用量: {self._global_daily_usage:,} tokens\n"
                f"本月用量: {self._global_monthly_usage:,} tokens\n"
                f"已自动停止执行，请等待配额重置。"
            )
        if level == BudgetAlertLevel.CRITICAL:
            return (
                f"🚨 Token 配额即将耗尽！\n"
                f"当前用量: {usage_percent * 100:.1f}%\n"
                f"今日: {self._global_daily_usage:,} tokens\n"
                f"建议立即暂停任务并保存进度。"
            )
        if level == BudgetAlertLevel.WARNING:
            return (
                f"⚠️ Token 配额使用警告\n"
                f"当前用量: {usage_percent * 100:.1f}%\n"
                f"今日: {self._global_daily_usage:,} tokens\n"
                f"已自动创建检查点。"
            )
        return f"✅ 用量正常: {self._global_daily_usage:,} tokens"

    def get_budget_status(
        self,
        user_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> BudgetStatus:
        """获取预算状态"""
        # 默认返回全局状态
        if task_id:
            used = self._task_usage.get(task_id, 0)
            limit = self.config.global_budget.per_task_token_limit
            scope = "task"
            scope_id = task_id
        elif session_id:
            used = self._session_usage.get(session_id, 0)
            limit = self.config.global_budget.per_session_token_limit
            scope = "session"
            scope_id = session_id
        else:
            used = self._global_daily_usage
            limit = self.config.global_budget.daily_token_limit
            scope = "global"
            scope_id = None

        remaining = max(0, limit - used)
        usage_percent = used / limit if limit > 0 else 0

        # 确定告警级别
        if usage_percent >= self.config.alerts.exhausted_threshold:
            alert_level = BudgetAlertLevel.EXHAUSTED
        elif usage_percent >= self.config.alerts.critical_threshold:
            alert_level = BudgetAlertLevel.CRITICAL
        elif usage_percent >= self.config.alerts.warning_threshold:
            alert_level = BudgetAlertLevel.WARNING
        else:
            alert_level = BudgetAlertLevel.INFO

        # 估算成本
        cost_rate = self.config.cost_rates.default
        estimated_cost = (used / 1000) * cost_rate

        return BudgetStatus(
            scope=scope,
            scope_id=scope_id,
            limit=limit,
            used=used,
            remaining=remaining,
            usage_percent=usage_percent * 100,
            alert_level=alert_level,
            estimated_cost=estimated_cost,
        )

    def get_usage_statistics(self) -> dict[str, Any]:
        """获取使用统计"""
        cost_rate = self.config.cost_rates.default

        return {
            "global": {
                "daily_tokens": self._global_daily_usage,
                "monthly_tokens": self._global_monthly_usage,
                "daily_limit": self.config.global_budget.daily_token_limit,
                "monthly_limit": self.config.global_budget.monthly_token_limit,
                "daily_usage_percent": self._global_daily_usage / self.config.global_budget.daily_token_limit * 100,
                "monthly_usage_percent": self._global_monthly_usage
                / self.config.global_budget.monthly_token_limit
                * 100,
                "estimated_daily_cost": (self._global_daily_usage / 1000) * cost_rate,
                "estimated_monthly_cost": (self._global_monthly_usage / 1000) * cost_rate,
            },
            "tasks": {
                task_id: {
                    "tokens": tokens,
                    "limit": self.config.global_budget.per_task_token_limit,
                    "usage_percent": tokens / self.config.global_budget.per_task_token_limit * 100,
                }
                for task_id, tokens in self._task_usage.items()
            },
            "sessions": {
                session_id: {
                    "tokens": tokens,
                    "limit": self.config.global_budget.per_session_token_limit,
                    "usage_percent": tokens / self.config.global_budget.per_session_token_limit * 100,
                }
                for session_id, tokens in self._session_usage.items()
            },
            "recent_records": [
                {
                    "tokens": r.tokens,
                    "model": r.model,
                    "cost": r.cost,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in self._usage_records[-50:]
            ],
        }

    async def reset_task_budget(self, task_id: str) -> None:
        """重置任务预算"""
        async with self._lock:
            self._task_usage.pop(task_id, None)

    async def reset_session_budget(self, session_id: str) -> None:
        """重置会话预算"""
        async with self._lock:
            self._session_usage.pop(session_id, None)


# 全局单例
_budget_manager: BudgetManager | None = None


def get_budget_manager() -> BudgetManager:
    """获取预算管理器单例"""
    global _budget_manager  # noqa: PLW0603
    if _budget_manager is None:
        _budget_manager = BudgetManager()
    return _budget_manager


def reset_budget_manager() -> None:
    """重置预算管理器（用于测试）"""
    global _budget_manager  # noqa: PLW0603
    _budget_manager = None
