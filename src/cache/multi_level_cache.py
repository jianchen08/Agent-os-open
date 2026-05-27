"""
多层缓存管理器

实现L1(内存) + L2(Redis)的多层缓存策略
"""

import asyncio
import os
import time
from datetime import timedelta
from typing import Any

from .redis_manager import get_redis_manager


class CacheManager:
    """简单内存缓存管理器，支持 TTL。"""

    def __init__(self, default_ttl: int | None = None):
        self._cache: dict[str, Any] = {}
        self._ttl: dict[str, float] = {}
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any:
        if key in self._cache:
            if key in self._ttl and time.time() > self._ttl[key]:
                del self._cache[key]
                del self._ttl[key]
                self._misses += 1
                return None
            self._hits += 1
            return self._cache[key]
        self._misses += 1
        return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._cache[key] = value
        effective_ttl = ttl if ttl is not None else self._default_ttl
        if effective_ttl is not None:
            self._ttl[key] = time.time() + effective_ttl

    def delete(self, key: str) -> bool:
        existed = key in self._cache
        self._cache.pop(key, None)
        self._ttl.pop(key, None)
        return existed

    def clear(self) -> None:
        self._cache.clear()
        self._ttl.clear()

    def stats(self) -> dict[str, Any]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}


class MultiLevelCache:
    """
    多层缓存管理器

    L1: 内存缓存（快速访问）
    L2: Redis缓存（分布式共享）
    """

    def __init__(
        self,
        l1_ttl: int = 300,  # L1缓存5分钟
        l2_ttl: int = 3600,  # L2缓存1小时
        enable_redis: bool = True,
    ):
        """
        初始化多层缓存

        Args:
            l1_ttl: L1缓存TTL（秒）
            l2_ttl: L2缓存TTL（秒）
            enable_redis: 是否启用Redis
        """
        self.l1_cache = CacheManager(default_ttl=l1_ttl)
        self.l2_ttl = l2_ttl
        self.enable_redis = enable_redis

        if enable_redis:
            self.redis_manager = get_redis_manager()
        else:
            self.redis_manager = None

    async def get(self, key: str) -> Any | None:
        """
        获取缓存值（L1 -> L2）

        Args:
            key: 缓存键

        Returns:
            缓存值或None
        """
        # 先尝试L1缓存
        value = self.l1_cache.get(key)
        if value is not None:
            return value

        # 尝试L2缓存
        if self.enable_redis and self.redis_manager:
            try:
                value = await self.redis_manager.get(key)
                if value is not None:
                    # 回填L1缓存
                    self.l1_cache.set(key, value)
                    return value
            except Exception:
                # Redis失败时继续使用L1缓存
                pass

        return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | timedelta | None = None,
    ) -> bool:
        """
        设置缓存值（同时写入L1和L2）

        Args:
            key: 缓存键
            value: 缓存值
            ttl: 过期时间

        Returns:
            是否成功
        """
        # 设置L1缓存
        l1_ttl = (
            ttl if isinstance(ttl, int) else (int(ttl.total_seconds()) if ttl else None)
        )
        self.l1_cache.set(key, value, l1_ttl)

        # 设置L2缓存
        if self.enable_redis and self.redis_manager:
            try:
                l2_ttl = ttl or self.l2_ttl
                await self.redis_manager.set(key, value, l2_ttl)
            except Exception:
                # Redis失败不影响L1缓存
                pass

        return True

    async def delete(self, key: str) -> bool:
        """
        删除缓存（同时删除L1和L2）

        Args:
            key: 缓存键

        Returns:
            是否成功
        """
        # 删除L1缓存
        l1_success = self.l1_cache.delete(key)

        # 删除L2缓存
        l2_success = True
        if self.enable_redis and self.redis_manager:
            try:
                l2_success = await self.redis_manager.delete(key)
            except Exception:
                l2_success = False

        return l1_success or l2_success

    async def clear_pattern(self, pattern: str) -> int:
        """
        清除匹配模式的缓存

        Args:
            pattern: 键模式

        Returns:
            删除的键数量
        """
        count = 0

        # 清除L2缓存
        if self.enable_redis and self.redis_manager:
            try:
                count = await self.redis_manager.clear_pattern(pattern)
            except Exception:
                pass

        # L1缓存没有模式匹配，只能全清
        if pattern == "*":
            self.l1_cache.clear()

        return count

    def get_stats(self) -> dict:
        """
        获取缓存统计信息

        Returns:
            统计信息
        """
        stats = {
            "l1": self.l1_cache.stats(),
            "l2": {"enabled": self.enable_redis},
        }

        return stats


# 全局缓存实例
_global_cache: MultiLevelCache | None = None


def get_global_cache() -> MultiLevelCache:
    """
    获取全局缓存实例

    Returns:
        MultiLevelCache实例
    """
    global _global_cache
    if _global_cache is None:
        # 根据环境变量决定是否启用 Redis
        # 在测试环境中禁用 Redis 以避免连接超时
        enable_redis = os.environ.get("ENABLE_REDIS_CACHE", "true").lower() == "true"
        _global_cache = MultiLevelCache(enable_redis=enable_redis)
    return _global_cache


async def cached(
    key: str,
    factory_func,
    ttl: int | None = None,
    cache_instance: MultiLevelCache | None = None,
):
    """
    缓存装饰器函数

    Args:
        key: 缓存键
        factory_func: 数据生成函数
        ttl: 缓存TTL
        cache_instance: 缓存实例

    Returns:
        缓存值或新生成的值
    """
    cache = cache_instance or get_global_cache()

    # 尝试从缓存获取
    value = await cache.get(key)
    if value is not None:
        return value

    # 生成新值
    if asyncio.iscoroutinefunction(factory_func):
        value = await factory_func()
    else:
        value = factory_func()

    # 存入缓存
    await cache.set(key, value, ttl)
    return value
