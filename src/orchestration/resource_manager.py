"""资源管理器

管理资源配额和分配。
"""

import asyncio
import logging
import time
from typing import Any

from src.core.exceptions import ResourceExhaustedError
from src.orchestration.types import AgentLevel, ResourceAllocation, ResourceQuota

logger = logging.getLogger(__name__)


class ResourceManager:
    """资源管理器

    职责：
    1. 管理各层级资源配额
    2. 分配和回收资源
    3. 监控系统资源（CPU/内存）
    """

    def __init__(self, quota: ResourceQuota | None = None) -> None:
        """初始化资源管理器

        Args:
            quota: 资源配额配置，None 使用默认配置
        """
        self.quota = quota or ResourceQuota()

        # 资源分配记录
        self._allocations: dict[str, ResourceAllocation] = {}

        # 层级计数器
        self._level_counters: dict[AgentLevel, int] = {
            AgentLevel.L1: 0,
            AgentLevel.L2: 0,
            AgentLevel.L3: 0,
        }

        # 锁
        self._lock = asyncio.Lock()

        # 系统资源检查是否可用
        self._psutil_available = self._check_psutil_available()

        logger.info(
            "资源管理器已初始化 | L1=%s, L2=%s, L3=%s, total=%s",
            self.quota.max_l1_agents,
            self.quota.max_l2_agents,
            self.quota.max_l3_agents,
            self.quota.max_total_agents,
        )

    def _check_psutil_available(self) -> bool:
        """检查 psutil 是否可用"""
        try:
            import psutil  # noqa: F401
            return True
        except ImportError:
            logger.warning("psutil 未安装，系统资源监控功能不可用")
            return False

    async def can_allocate(self, level: AgentLevel) -> bool:
        """检查是否可以分配资源

        Args:
            level: Agent 层级

        Returns:
            是否可以分配
        """
        async with self._lock:
            return await self._can_allocate_internal(level)

    async def _can_allocate_internal(self, level: AgentLevel) -> bool:
        """内部检查资源分配（已加锁）"""
        # 检查层级限制
        current = self._level_counters[level]
        max_c = getattr(self.quota, f"max_l{level.value}_agents")
        if current >= max_c:
            return False

        # 检查总数限制
        total = sum(self._level_counters.values())
        if total >= self.quota.max_total_agents:
            return False

        # 检查系统资源
        return await self._check_system_resources()

    async def _check_system_resources(self) -> bool:
        """检查系统资源

        Returns:
            系统资源是否充足
        """
        if not self._psutil_available:
            return True

        try:
            import psutil

            # CPU 检查（快速模式）
            cpu = psutil.cpu_percent(interval=0.01)
            if cpu > self.quota.max_cpu_percent:
                logger.debug("CPU 使用率超过阈值 | cpu=%s%%, threshold=%s%%", cpu, self.quota.max_cpu_percent)
                return False

            # 内存检查
            mem = psutil.virtual_memory()
            if mem.percent > self.quota.max_memory_percent:
                logger.debug("内存使用率超过阈值 | mem=%s%%, threshold=%s%%", mem.percent, self.quota.max_memory_percent)
                return False

            return True
        except OSError as e:
            logger.error("资源检查失败: %s", e)
            return False

    async def allocate(
        self,
        task_id: str,
        level: AgentLevel,
        expected_duration: float = 60.0,
    ) -> ResourceAllocation:
        """分配资源

        Args:
            task_id: 任务 ID
            level: Agent 层级
            expected_duration: 预计执行时长（秒）

        Returns:
            资源分配记录

        Raises:
            ResourceExhaustedError: 资源不足无法分配
        """
        async with self._lock:
            if not await self._can_allocate_internal(level):
                raise ResourceExhaustedError(
                    f"资源不足，无法分配 | level={level.name}, "
                    f"current={self._level_counters[level]}, "
                    f"max={getattr(self.quota, f'max_l{level.value}_agents')}"
                )

            # 创建分配记录
            now = time.time()
            allocation = ResourceAllocation(
                task_id=task_id,
                agent_level=level,
                allocated_at=now,
                expected_release_at=now + expected_duration,
            )

            # 记录分配
            self._allocations[task_id] = allocation
            self._level_counters[level] += 1

            logger.info(
                "资源已分配 | task_id=%s, level=%s, current=%s/%s",
                task_id,
                level.name,
                self._level_counters[level],
                getattr(self.quota, f"max_l{level.value}_agents"),
            )

            return allocation

    async def release(self, task_id: str) -> None:
        """释放资源

        Args:
            task_id: 任务 ID
        """
        async with self._lock:
            if task_id not in self._allocations:
                logger.warning("释放资源失败，任务不存在 | task_id=%s", task_id)
                return

            allocation = self._allocations[task_id]
            level = allocation.agent_level

            # 更新计数
            if self._level_counters[level] > 0:
                self._level_counters[level] -= 1

            # 删除分配记录
            del self._allocations[task_id]

            logger.info(
                "资源已释放 | task_id=%s, level=%s, current=%s/%s",
                task_id,
                level.name,
                self._level_counters[level],
                getattr(self.quota, f"max_l{level.value}_agents"),
            )

    async def get_allocation(self, task_id: str) -> ResourceAllocation | None:
        """获取资源分配记录

        Args:
            task_id: 任务 ID

        Returns:
            资源分配记录，不存在返回 None
        """
        async with self._lock:
            return self._allocations.get(task_id)

    def get_usage(self) -> dict[str, Any]:
        """获取资源使用情况

        Returns:
            资源使用统计
        """
        return {
            "by_level": {
                level.name: {
                    "current": count,
                    "max": getattr(self.quota, f"max_l{level.value}_agents"),
                    "available": getattr(self.quota, f"max_l{level.value}_agents") - count,
                }
                for level, count in self._level_counters.items()
            },
            "total": {
                "current": sum(self._level_counters.values()),
                "max": self.quota.max_total_agents,
                "available": self.quota.max_total_agents - sum(self._level_counters.values()),
            },
            "active_allocations": len(self._allocations),
        }

    async def wait_for_resource(
        self,
        level: AgentLevel,
        timeout: float | None = None,
        check_interval: float = 0.1,
    ) -> bool:
        """等待资源可用

        Args:
            level: Agent 层级
            timeout: 超时时间（秒），None 表示无限等待
            check_interval: 检查间隔（秒）

        Returns:
            是否在超时前获得资源
        """
        start_time = time.time()

        while True:
            if await self.can_allocate(level):
                return True

            if timeout is not None and (time.time() - start_time) > timeout:
                return False

            await asyncio.sleep(check_interval)
