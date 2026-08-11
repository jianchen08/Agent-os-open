"""计时器管理器

管理任务超时计时器，支持创建、重置、取消和恢复。

暴露接口：
- get_timer_manager() -> TimerManager：获取单例
- TimerManager：计时器管理器类
  - create_timer(task_id, timeout, callback, root_task_id) -> TimerState
  - reset_timer(task_id, new_timeout) -> TimerState | None
  - cancel_timer(task_id) -> bool
  - restore_from_storage(task_service, callback) -> int
  - get_timer_status(task_id) -> TimerState | None
  - get_all_timers() -> list[TimerState]
  - get_active_timers() -> list[TimerState]
  - reload_config() -> None
- TimerStatus：计时器状态枚举
- TimerState：计时器状态数据类
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path  # noqa: F401
from typing import TYPE_CHECKING, Any

import yaml  # noqa: F401

if TYPE_CHECKING:
    from tasks.service import TaskService

logger = logging.getLogger(__name__)


class TimerStatus(str, Enum):
    """计时器状态枚举"""

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class TimerState:
    """计时器状态数据类。

    Attributes:
        task_id: 任务ID
        root_task_id: 根任务ID（用于长期任务的层级关系）
        created_at: 创建时间
        last_activity: 最后活动时间
        timeout_at: 超时时间点
        timeout_duration: 超时时长（秒）
        handle: asyncio.TimerHandle 对象
        status: 计时器状态
        callback: 超时回调函数
    """

    task_id: str
    root_task_id: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    timeout_at: datetime | None = None
    timeout_duration: float = 3600.0
    handle: asyncio.TimerHandle | None = field(default=None, repr=False)
    status: TimerStatus = TimerStatus.ACTIVE
    callback: Callable[[str], None] | None = field(default=None, repr=False)

    def is_active(self) -> bool:
        """检查计时器是否活跃"""
        return self.status == TimerStatus.ACTIVE and self.handle is not None

    def is_expired(self) -> bool:
        """检查计时器是否已过期"""
        return self.status == TimerStatus.EXPIRED

    def is_cancelled(self) -> bool:
        """检查计时器是否已取消"""
        return self.status == TimerStatus.CANCELLED

    def time_remaining(self) -> float | None:
        """获取剩余时间（秒）"""
        if self.timeout_at is None or self.status != TimerStatus.ACTIVE:
            return None

        remaining = (self.timeout_at - datetime.now(UTC)).total_seconds()
        return max(0.0, remaining)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于序列化）"""
        return {
            "task_id": self.task_id,
            "root_task_id": self.root_task_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_activity": (self.last_activity.isoformat() if self.last_activity else None),
            "timeout_at": self.timeout_at.isoformat() if self.timeout_at else None,
            "timeout_duration": self.timeout_duration,
            "status": self.status.value,
            "time_remaining": self.time_remaining(),
        }


class TimerManager:
    """计时器管理器（单例）

    核心职责:
      1. 创建和管理任务计时器
      2. 重置和取消计时器
      3. 服务重启时从存储恢复计时器
      4. 提供计时器状态查询

    使用方式:
        manager = TimerManager.get_instance()
        await manager.create_timer("task-123", 3600, callback)
        await manager.reset_timer("task-123")
        await manager.cancel_timer("task-123")
    """

    _instance: TimerManager | None = None
    _initialized: bool = False

    DEFAULT_CONFIG = {
        "timeout": {
            # 任务总执行时间硬墙（独立于 idle，活跃也强制 fail）。按 agent_level 分级：
            #   L1 (主 Agent)：不限制（None 表示不创建总超时计时器）
            #   L2 (子任务 Agent)：9000s = 2.5h
            #   L3 (原子工具 Agent)：3600s = 1h
            # 由 _arm_total_timer 在任务启动时按 task.agent_level 选取。
            "task_max_duration_by_level": {
                "L1": None,
                "L2": 9000,
                "L3": 3600,
            },
            # 兼容兜底：未知 agent_level 或未传时使用此值
            "task_max_duration": 3600,
            "idle_threshold": 600,
            "project_max_duration": 86400,
            "activity_threshold": 300,
        },
        "retry": {
            "max_retries": 3,
            "retry_interval": 60,
        },
        "auto_execute": {
            "enabled": True,
            "next_task_delay": 5,
            "fallback_check_interval": 300,
        },
        "heartbeat": {
            "interval": 60,
            "grace_period": 120,
        },
        "notification": {
            "notify_on_timeout": True,
            "notify_on_stuck": True,
            "notify_on_max_retries": True,
        },
        "recovery": {
            "auto_restore": True,
            "restore_lookback": 7200,
        },
    }

    def __new__(cls) -> TimerManager:
        """单例模式：确保全局唯一实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """初始化计时器管理器，只在首次创建时执行"""
        if TimerManager._initialized:
            return

        self._timers: dict[str, TimerState] = {}
        self._config: dict[str, Any] = {}
        self._load_config()

        TimerManager._initialized = True
        logger.info("TimerManager 初始化完成")

    def _load_config(self) -> None:
        """从 YAML 配置文件加载配置。

        直接读取 config/system/long_term_task.yaml，
        不再依赖 config.system_config 模块。
        """
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            config = get_config_center().get("system/long_term_task.yaml")
            if config and isinstance(config, dict):
                self._config = self._merge_config(self.DEFAULT_CONFIG, config)
                logger.info("从配置文件加载长期任务配置成功")
                return

            self._config = self.DEFAULT_CONFIG.copy()
            logger.info("使用默认长期任务配置")
        except Exception as e:
            logger.warning("加载配置文件失败，使用默认配置: %s", e)
            self._config = self.DEFAULT_CONFIG.copy()

    def _merge_config(self, default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """递归合并配置。

        Args:
            default: 默认配置
            override: 覆盖配置

        Returns:
            合并后的配置
        """
        result = default.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value

        return result

    @classmethod
    def get_instance(cls) -> TimerManager:
        """获取单例实例"""
        return cls()

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例实例（仅用于测试）"""
        if cls._instance is not None:
            for timer in cls._instance._timers.values():
                if timer.handle:
                    timer.handle.cancel()
            cls._instance._timers.clear()

        cls._instance = None
        cls._initialized = False

    @property
    def task_max_duration(self) -> int:
        """获取单个任务最大执行时间（秒），未分级时的兜底默认值。

        分级配置见 task_max_duration_for_level()。
        """
        return self._config["timeout"]["task_max_duration"]

    def task_max_duration_for_level(self, agent_level: str | None) -> int | None:
        """按 agent_level 取任务总超时（秒）。

        Args:
            agent_level: "L1" / "L2" / "L3" 或 None

        Returns:
            秒数；返回 None 表示该层级无总超时限制（如 L1 主 Agent）。
        """
        level_map = self._config["timeout"].get("task_max_duration_by_level", {})
        if agent_level and agent_level in level_map:
            return level_map[agent_level]
        return self._config["timeout"]["task_max_duration"]

    @property
    def idle_threshold(self) -> int:
        """获取无活动判定阈值（秒）"""
        return self._config["timeout"]["idle_threshold"]

    @property
    def project_max_duration(self) -> int:
        """获取总任务最大执行时间（秒）"""
        return self._config["timeout"]["project_max_duration"]

    @property
    def activity_threshold(self) -> int:
        """获取活动判定阈值（秒）"""
        return self._config["timeout"]["activity_threshold"]

    @property
    def max_retries(self) -> int:
        """获取最大重试次数"""
        return self._config["retry"]["max_retries"]

    @property
    def retry_interval(self) -> int:
        """获取重试间隔（秒）"""
        return self._config["retry"]["retry_interval"]

    @property
    def auto_restore(self) -> bool:
        """是否启用自动恢复"""
        return self._config["recovery"]["auto_restore"]

    @property
    def restore_lookback(self) -> int:
        """获取恢复时检查的时间范围（秒）"""
        return self._config["recovery"]["restore_lookback"]

    async def create_timer(
        self,
        task_id: str,
        timeout: float | None = None,
        callback: Callable[[str], None] | None = None,
        root_task_id: str | None = None,
    ) -> TimerState:
        """创建计时器。

        Args:
            task_id: 任务ID
            timeout: 超时时间（秒），None 时使用默认值
            callback: 超时回调函数
            root_task_id: 根任务ID

        Returns:
            创建的计时器状态

        Raises:
            ValueError: 计时器已存在
        """
        if task_id in self._timers:
            raise ValueError(f"计时器已存在: {task_id}")

        if timeout is None:
            timeout = float(self.task_max_duration)

        now = datetime.now(UTC)
        timeout_at = now + timedelta(seconds=timeout)

        timer = TimerState(
            task_id=task_id,
            root_task_id=root_task_id,
            created_at=now,
            last_activity=now,
            timeout_at=timeout_at,
            timeout_duration=timeout,
            status=TimerStatus.ACTIVE,
            callback=callback,
        )

        timer.handle = asyncio.get_event_loop().call_later(timeout, self._on_timeout, task_id)

        self._timers[task_id] = timer
        logger.debug("创建计时器: task_id=%s, timeout=%ss", task_id, timeout)

        return timer

    async def reset_timer(
        self,
        task_id: str,
        new_timeout: float | None = None,
    ) -> TimerState | None:
        """重置计时器。

        Args:
            task_id: 任务ID
            new_timeout: 新超时时间（秒），None 时保持原值

        Returns:
            重置后的计时器状态，不存在时返回 None
        """
        if task_id not in self._timers:
            logger.warning("计时器不存在: %s", task_id)
            return None

        old_timer = self._timers[task_id]

        if old_timer.handle:
            old_timer.handle.cancel()

        timeout = new_timeout if new_timeout is not None else old_timer.timeout_duration
        now = datetime.now(UTC)
        timeout_at = now + timedelta(seconds=timeout)

        new_timer = TimerState(
            task_id=task_id,
            root_task_id=old_timer.root_task_id,
            created_at=old_timer.created_at,
            last_activity=now,
            timeout_at=timeout_at,
            timeout_duration=timeout,
            status=TimerStatus.ACTIVE,
            callback=old_timer.callback,
        )

        new_timer.handle = asyncio.get_event_loop().call_later(timeout, self._on_timeout, task_id)

        self._timers[task_id] = new_timer
        logger.debug("重置计时器: task_id=%s, timeout=%ss", task_id, timeout)

        return new_timer

    async def cancel_timer(self, task_id: str) -> bool:
        """取消计时器。

        Args:
            task_id: 任务ID

        Returns:
            是否取消成功
        """
        if task_id not in self._timers:
            logger.debug("计时器不存在: %s", task_id)
            return False

        timer = self._timers[task_id]

        if timer.handle:
            timer.handle.cancel()
            timer.handle = None

        timer.status = TimerStatus.CANCELLED
        del self._timers[task_id]
        logger.debug("取消计时器: task_id=%s", task_id)

        return True

    def _on_timeout(self, task_id: str) -> None:
        """计时器超时回调。

        Args:
            task_id: 超时的任务ID
        """
        if task_id not in self._timers:
            return

        timer = self._timers[task_id]
        timer.status = TimerStatus.EXPIRED
        timer.handle = None

        logger.warning("计时器超时: task_id=%s", task_id)

        if timer.callback:
            try:
                timer.callback(task_id)
            except Exception as e:
                logger.error("执行超时回调失败: task_id=%s, error=%s", task_id, e)

    def get_timer_status(self, task_id: str) -> TimerState | None:
        """获取计时器状态。

        Args:
            task_id: 任务ID

        Returns:
            计时器状态，不存在时返回 None
        """
        return self._timers.get(task_id)

    def get_all_timers(self) -> list[TimerState]:
        """获取所有计时器状态"""
        return list(self._timers.values())

    def get_active_timers(self) -> list[TimerState]:
        """获取所有活跃的计时器"""
        return [t for t in self._timers.values() if t.is_active()]

    def get_timer_count(self) -> int:
        """获取计时器总数"""
        return len(self._timers)

    async def restore_from_storage(  # noqa: PLR0912
        self,
        task_service: TaskService,
        callback: Callable[[str], None] | None = None,
    ) -> int:
        """从 TaskService 存储恢复计时器。

        替代旧的 restore_from_db()，不再依赖 ORM 和数据库。

        Args:
            task_service: 任务服务实例
            callback: 超时回调函数

        Returns:
            恢复的计时器数量
        """
        if not self.auto_restore:
            logger.info("自动恢复已禁用，跳过计时器恢复")
            return 0

        restored_count = 0
        expired_count = 0
        lookback_time = datetime.now(UTC) - timedelta(seconds=self.restore_lookback)

        try:
            running_tasks = task_service.list_by_status(
                __import__("tasks.types", fromlist=["TaskStatus"]).TaskStatus.RUNNING
            )

            for task in running_tasks:
                if task.id in self._timers:
                    logger.debug("计时器已存在，跳过恢复: task_id=%s", task.id)
                    continue

                updated_at_str = task.updated_at
                if not updated_at_str:
                    continue

                try:
                    updated_at = datetime.fromisoformat(updated_at_str)
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    continue

                if updated_at < lookback_time:
                    continue

                elapsed = (datetime.now(UTC) - updated_at).total_seconds()
                remaining = self.task_max_duration - elapsed

                if remaining > 0:
                    try:
                        await self.create_timer(
                            task_id=task.id,
                            timeout=remaining,
                            callback=callback,
                            root_task_id=task.parent_task_id,
                        )
                        restored_count += 1
                        logger.info(
                            "恢复计时器成功: task_id=%s, remaining=%.1fs",
                            task.id,
                            remaining,
                        )
                    except Exception as e:
                        logger.error("恢复计时器失败: task_id=%s, error=%s", task.id, e)
                else:
                    expired_count += 1
                    logger.warning(
                        "任务已超时，立即触发回调: task_id=%s, elapsed=%.1fs",
                        task.id,
                        elapsed,
                    )
                    if callback:
                        try:
                            asyncio.create_task(self._async_callback(callback, task.id))
                        except Exception as e:
                            logger.error("触发超时回调失败: task_id=%s, error=%s", task.id, e)

            logger.info(
                "计时器恢复完成: restored=%d, expired=%d",
                restored_count,
                expired_count,
            )

        except Exception as e:
            logger.error("从存储恢复计时器失败: %s", e, exc_info=True)

        return restored_count

    async def _async_callback(self, callback: Callable[[str], None], task_id: str) -> None:
        """异步执行回调函数。

        Args:
            callback: 回调函数
            task_id: 任务ID
        """
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(task_id)
            else:
                callback(task_id)
        except Exception as e:
            logger.error("执行异步回调失败: task_id=%s, error=%s", task_id, e)

    async def cleanup_expired_timers(self) -> int:
        """清理已过期或已取消的计时器"""
        to_remove = [task_id for task_id, timer in self._timers.items() if timer.is_expired() or timer.is_cancelled()]

        for task_id in to_remove:
            del self._timers[task_id]

        if to_remove:
            logger.info("清理过期计时器: count=%d", len(to_remove))

        return len(to_remove)

    async def clear_all(self) -> None:
        """清除所有计时器（仅用于测试）"""
        for timer in self._timers.values():
            if timer.handle:
                timer.handle.cancel()

        self._timers.clear()
        logger.info("已清除所有计时器")

    def reload_config(self) -> None:
        """重新加载配置"""
        self._load_config()
        logger.info("配置重新加载完成")


def get_timer_manager() -> TimerManager:
    """获取 TimerManager 单例实例"""
    return TimerManager.get_instance()
