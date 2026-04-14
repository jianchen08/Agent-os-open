"""
计时器管理器

暴露接口：
- get_timer_manager() -> TimerManager：get_timer_manager功能
- is_active(self) -> bool：is_active功能
- is_expired(self) -> bool：is_expired功能
- is_cancelled(self) -> bool：is_cancelled功能
- time_remaining(self) -> float | None：time_remaining功能
- to_dict(self) -> dict[str, Any]：to_dict功能
- get_instance(cls) -> 'TimerManager'：get_instance功能
- reset_instance(cls) -> None：reset_instance功能
- task_max_duration(self) -> int：task_max_duration功能
- idle_threshold(self) -> int：idle_threshold功能
- project_max_duration(self) -> int：project_max_duration功能
- activity_threshold(self) -> int：activity_threshold功能
- max_retries(self) -> int：max_retries功能
- retry_interval(self) -> int：retry_interval功能
- auto_restore(self) -> bool：auto_restore功能
- restore_lookback(self) -> int：restore_lookback功能
- get_timer_status(self, task_id: str) -> TimerState | None：get_timer_status功能
- get_all_timers(self) -> list[TimerState]：get_all_timers功能
- get_active_timers(self) -> list[TimerState]：get_active_timers功能
- get_timer_count(self) -> int：get_timer_count功能
- reload_config(self) -> None：reload_config功能
- TimerStatus：TimerStatus类
- TimerState：TimerState类
- TimerManager：TimerManager类
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import select

from config.system_config import get_system_config_manager
from core.states import ExecutionStatus
from db.models import Task
from db.session_manager import managed_session

logger = logging.getLogger(__name__)


class TimerStatus(str, Enum):
    """计时器状态枚举"""

    ACTIVE = "active"  # 活跃状态
    EXPIRED = "expired"  # 已过期
    CANCELLED = "cancelled"  # 已取消


@dataclass
class TimerState:
    """
    计时器状态数据类

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
    timeout_duration: float = 3600.0  # 默认1小时
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
            "last_activity": (
                self.last_activity.isoformat() if self.last_activity else None
            ),
            "timeout_at": self.timeout_at.isoformat() if self.timeout_at else None,
            "timeout_duration": self.timeout_duration,
            "status": self.status.value,
            "time_remaining": self.time_remaining(),
        }


class TimerManager:
    """
    计时器管理器（单例）

    核心职责:
      1. 创建和管理任务计时器
      2. 重置和取消计时器
      3. 服务重启时从数据库恢复计时器
      4. 提供计时器状态查询

    使用方式:
        manager = TimerManager.get_instance()
        await manager.create_timer("task-123", 3600, callback)
        await manager.reset_timer("task-123")
        await manager.cancel_timer("task-123")
    """

    _instance: "TimerManager | None" = None
    _initialized: bool = False

    # 默认配置值
    DEFAULT_CONFIG = {
        "timeout": {
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

    def __new__(cls) -> "TimerManager":
        """单例模式：确保全局唯一实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """
        初始化计时器管理器

        只在首次创建时执行初始化
        """
        if TimerManager._initialized:
            return

        self._timers: dict[str, TimerState] = {}
        self._config: dict[str, Any] = {}
        self._load_config()

        TimerManager._initialized = True
        logger.info("TimerManager 初始化完成")

    def _load_config(self) -> None:
        """
        从配置文件加载配置

        如果配置文件不存在或加载失败，使用默认配置
        """
        try:
            config_manager = get_system_config_manager()
            config = config_manager.load("long_term_task")

            if config:
                # 合并配置，优先使用文件配置
                self._config = self._merge_config(self.DEFAULT_CONFIG, config)
                logger.info("从配置文件加载长期任务配置成功")
            else:
                self._config = self.DEFAULT_CONFIG.copy()
                logger.info("使用默认长期任务配置")

        except Exception as e:
            logger.warning(f"加载配置文件失败，使用默认配置: {e}")
            self._config = self.DEFAULT_CONFIG.copy()

    def _merge_config(
        self, default: dict[str, Any], override: dict[str, Any]
    ) -> dict[str, Any]:
        """递归合并配置"""
        result = default.copy()

        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value

        return result

    @classmethod
    def get_instance(cls) -> "TimerManager":
        """获取单例实例"""
        return cls()

    @classmethod
    def reset_instance(cls) -> None:
        """
        重置单例实例（仅用于测试）
        """
        if cls._instance is not None:
            # 取消所有计时器
            for timer in cls._instance._timers.values():
                if timer.handle:
                    timer.handle.cancel()
            cls._instance._timers.clear()

        cls._instance = None
        cls._initialized = False

    # ==================== 配置访问接口 ====================

    @property
    def task_max_duration(self) -> int:
        """获取单个任务最大执行时间（秒）"""
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

    # ==================== 计时器管理接口 ====================

    async def create_timer(
        self,
        task_id: str,
        timeout: float | None = None,
        callback: Callable[[str], None] | None = None,
        root_task_id: str | None = None,
    ) -> TimerState:
        """创建计时器"""
        if task_id in self._timers:
            raise ValueError(f"计时器已存在: {task_id}")

        # 使用默认超时时间
        if timeout is None:
            timeout = float(self.task_max_duration)

        now = datetime.now(UTC)
        timeout_at = now + timedelta(seconds=timeout)

        # 创建计时器状态
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

        # 设置定时器回调
        timer.handle = asyncio.get_event_loop().call_later(
            timeout, self._on_timeout, task_id
        )

        self._timers[task_id] = timer
        logger.info(f"创建计时器: task_id={task_id}, timeout={timeout}s")

        return timer

    async def reset_timer(
        self,
        task_id: str,
        new_timeout: float | None = None,
    ) -> TimerState | None:
        """重置计时器"""
        if task_id not in self._timers:
            logger.warning(f"计时器不存在: {task_id}")
            return None

        old_timer = self._timers[task_id]

        # 取消旧计时器
        if old_timer.handle:
            old_timer.handle.cancel()

        # 使用新超时时间或原超时时间
        timeout = new_timeout if new_timeout is not None else old_timer.timeout_duration
        now = datetime.now(UTC)
        timeout_at = now + timedelta(seconds=timeout)

        # 创建新计时器
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

        new_timer.handle = asyncio.get_event_loop().call_later(
            timeout, self._on_timeout, task_id
        )

        self._timers[task_id] = new_timer
        logger.info(f"重置计时器: task_id={task_id}, timeout={timeout}s")

        return new_timer

    async def cancel_timer(self, task_id: str) -> bool:
        """取消计时器"""
        if task_id not in self._timers:
            logger.warning(f"计时器不存在: {task_id}")
            return False

        timer = self._timers[task_id]

        # 取消定时器句柄
        if timer.handle:
            timer.handle.cancel()
            timer.handle = None

        timer.status = TimerStatus.CANCELLED
        logger.info(f"取消计时器: task_id={task_id}")

        return True

    def _on_timeout(self, task_id: str) -> None:
        """计时器超时回调"""
        if task_id not in self._timers:
            return

        timer = self._timers[task_id]
        timer.status = TimerStatus.EXPIRED
        timer.handle = None

        logger.warning(f"计时器超时: task_id={task_id}")

        # 执行回调
        if timer.callback:
            try:
                timer.callback(task_id)
            except Exception as e:
                logger.error(f"执行超时回调失败: task_id={task_id}, error={e}")

    # ==================== 状态查询接口 ====================

    def get_timer_status(self, task_id: str) -> TimerState | None:
        """获取计时器状态"""
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

    # ==================== 恢复接口 ====================

    async def restore_from_db(
        self,
        callback: Callable[[str], None] | None = None,
    ) -> int:
        """从数据库恢复计时器"""
        if not self.auto_restore:
            logger.info("自动恢复已禁用，跳过计时器恢复")
            return 0

        restored_count = 0
        expired_count = 0
        lookback_time = datetime.now(UTC) - timedelta(seconds=self.restore_lookback)

        try:
            async with managed_session() as session:
                # 查找所有进行中的任务
                result = await session.execute(
                    select(Task).where(
                        Task.status == ExecutionStatus.RUNNING.value,
                        Task.updated_at >= lookback_time,
                    )
                )
                tasks = result.scalars().all()

                for task in tasks:
                    # 检查 task_metadata 中的 auto_execute 标志
                    metadata = task.task_metadata or {}
                    if not metadata.get("auto_execute", False):
                        logger.debug(f"任务未启用自动执行，跳过恢复: task_id={task.id}")
                        continue

                    # 检查是否已有计时器
                    if task.id in self._timers:
                        logger.debug(f"计时器已存在，跳过恢复: task_id={task.id}")
                        continue

                    # 计算剩余时间
                    updated_at = task.updated_at or task.created_at
                    if not updated_at:
                        continue

                    elapsed = (datetime.now(UTC) - updated_at).total_seconds()
                    remaining = self.task_max_duration - elapsed

                    if remaining > 0:
                        # 剩余时间 > 0: 创建计时器
                        try:
                            await self.create_timer(
                                task_id=task.id,
                                timeout=remaining,
                                callback=callback,
                                root_task_id=task.parent_task_id,
                            )
                            restored_count += 1
                            logger.info(
                                f"恢复计时器成功: task_id={task.id}, remaining={remaining:.1f}s"
                            )
                        except Exception as e:
                            logger.error(f"恢复计时器失败: task_id={task.id}, error={e}")
                    else:
                        # 剩余时间 <= 0: 已超时，立即触发回调
                        expired_count += 1
                        logger.warning(
                            f"任务已超时，立即触发回调: task_id={task.id}, elapsed={elapsed:.1f}s"
                        )
                        if callback:
                            try:
                                # 使用 asyncio.create_task 异步触发回调
                                import asyncio

                                asyncio.create_task(self._async_callback(callback, task.id))
                            except Exception as e:
                                logger.error(
                                    f"触发超时回调失败: task_id={task.id}, error={e}"
                                )

            logger.info(
                f"计时器恢复完成: restored={restored_count}, expired={expired_count}"
            )

        except Exception as e:
            logger.error(f"从数据库恢复计时器失败: {e}", exc_info=True)

        return restored_count

    async def _async_callback(
        self, callback: Callable[[str], None], task_id: str
    ) -> None:
        """异步执行回调函数"""
        try:
            import asyncio

            if asyncio.iscoroutinefunction(callback):
                await callback(task_id)
            else:
                callback(task_id)
        except Exception as e:
            logger.error(f"执行异步回调失败: task_id={task_id}, error={e}")

    # ==================== 清理接口 ====================

    async def cleanup_expired_timers(self) -> int:
        """清理已过期或已取消的计时器"""
        to_remove = [
            task_id
            for task_id, timer in self._timers.items()
            if timer.is_expired() or timer.is_cancelled()
        ]

        for task_id in to_remove:
            del self._timers[task_id]

        if to_remove:
            logger.info(f"清理过期计时器: count={len(to_remove)}")

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
