"""
缓存模块

提供多层缓存策略：内存缓存 + Redis分布式缓存
"""

from .decorators import cache_result, cached_function, invalidate_cache
from .multi_level_cache import MultiLevelCache, cached, get_global_cache
from .redis_manager import RedisManager, get_redis_client, get_redis_manager

__all__ = [
    "RedisManager",
    "get_redis_manager",
    "get_redis_client",
    "MultiLevelCache",
    "get_global_cache",
    "cached",
    "cached_function",
    "cache_result",
    "invalidate_cache",
]
