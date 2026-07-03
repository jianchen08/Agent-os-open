"""
多层缓存管理器

实现纯内存缓存策略（原 L2 Redis 层已移除）
"""

import asyncio
import time
from datetime import timedelta
from typing import Any


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
    多层缓存管理器（纯内存实现）

    L1: 内存缓存（快速访问）
    """

    def __init__(
        self,
        l1_ttl: int = 300,  # L1缓存5分钟
    ):
        """
        初始化内存缓存

        Args:
            l1_ttl: L1缓存TTL（秒）
        """
        self.l1_cache = CacheManager(default_ttl=l1_ttl)

    async def get(self, key: str) -> Any | None:
        """
        获取缓存值

        Args:
            key: 缓存键

        Returns:
            缓存值或None
        """
        return self.l1_cache.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | timedelta | None = None,
    ) -> bool:
        """
        设置缓存值

        Args:
            key: 缓存键
            value: 缓存值
            ttl: 过期时间

        Returns:
            是否成功
        """
        l1_ttl = ttl if isinstance(ttl, int) else (int(ttl.total_seconds()) if ttl else None)
        self.l1_cache.set(key, value, l1_ttl)
        return True

    async def delete(self, key: str) -> bool:
        """
        删除缓存

        Args:
            key: 缓存键

        Returns:
            是否成功
        """
        return self.l1_cache.delete(key)

    async def clear_pattern(self, pattern: str) -> int:
        """
        清除匹配模式的缓存

        Args:
            pattern: 键模式

        Returns:
            删除的键数量
        """
        # L1缓存没有模式匹配，只能全清
        if pattern == "*":
            self.l1_cache.clear()
            return 1

        return 0

    def get_stats(self) -> dict:
        """
        获取缓存统计信息

        Returns:
            统计信息
        """
        return {"l1": self.l1_cache.stats()}


# 全局缓存实例
_global_cache: MultiLevelCache | None = None


def get_global_cache() -> MultiLevelCache:
    """
    获取全局缓存实例

    Returns:
        MultiLevelCache实例
    """
    global _global_cache  # noqa: PLW0603
    if _global_cache is None:
        _global_cache = MultiLevelCache()
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
