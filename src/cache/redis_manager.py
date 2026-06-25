"""
Redis缓存管理器

提供高性能的分布式缓存功能
"""

import json
import logging
import pickle
from datetime import timedelta
from typing import Any

import redis.asyncio as redis
from redis.asyncio import Redis

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


class RedisManager:
    """Redis缓存管理器"""

    def __init__(self, redis_url: str | None = None):
        """
        初始化Redis管理器

        Args:
            redis_url: Redis连接URL
        """
        settings = get_settings()
        self.redis_url = redis_url or settings.redis_url
        self._redis: Redis | None = None
        self._connected = False

    async def connect(self) -> None:
        """连接Redis"""
        if not self._connected:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=False,  # 保持二进制模式以支持pickle
                max_connections=20,
                retry_on_timeout=True,
                socket_keepalive=True,
                socket_keepalive_options={},
            )
            # 测试连接
            await self._redis.ping()
            self._connected = True

    async def disconnect(self) -> None:
        """断开Redis连接"""
        if self._redis:
            await self._redis.close()
            self._connected = False

    async def get(self, key: str, use_json: bool = True) -> Any | None:
        """
        获取缓存值

        Args:
            key: 缓存键
            use_json: 是否使用JSON序列化（False则使用pickle）

        Returns:
            缓存值或None
        """
        if not self._connected:
            await self.connect()

        try:
            value = await self._redis.get(key)
            if value is None:
                return None

            if use_json:
                return json.loads(value.decode("utf-8"))
            else:
                # 安全警告：pickle反序列化不受信任的数据存在安全风险
                logger.warning("使用pickle反序列化，请确保数据来源可信")
                return pickle.loads(value)
        except Exception as exc:
            # 缓存读取失败，返回 None
            logger.debug(f"Redis 读取失败 (key={key}): {exc}")
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | timedelta | None = None,
        use_json: bool = True,
    ) -> bool:
        """
        设置缓存值

        Args:
            key: 缓存键
            value: 缓存值
            ttl: 过期时间（秒或timedelta对象）
            use_json: 是否使用JSON序列化

        Returns:
            是否成功
        """
        if not self._connected:
            await self.connect()

        try:
            if use_json:
                serialized_value = json.dumps(value, ensure_ascii=False)
            else:
                serialized_value = pickle.dumps(value)

            if ttl:
                if isinstance(ttl, timedelta):
                    ttl = int(ttl.total_seconds())
                return await self._redis.setex(key, ttl, serialized_value)
            else:
                return await self._redis.set(key, serialized_value)
        except Exception as exc:
            # 缓存设置失败，返回 False
            logger.debug(f"Redis 设置失败 (key={key}): {exc}")
            return False

    async def delete(self, key: str) -> bool:
        """
        删除缓存

        Args:
            key: 缓存键

        Returns:
            是否成功
        """
        if not self._connected:
            await self.connect()

        try:
            result = await self._redis.delete(key)
            return result > 0
        except Exception as exc:
            # 删除失败，返回 False
            logger.debug(f"Redis 删除失败 (key={key}): {exc}")
            return False

    async def exists(self, key: str) -> bool:
        """
        检查键是否存在

        Args:
            key: 缓存键

        Returns:
            是否存在
        """
        if not self._connected:
            await self.connect()

        try:
            return await self._redis.exists(key) > 0
        except Exception as exc:
            # 检查存在失败，返回 False
            logger.debug(f"Redis 检查存在失败 (key={key}): {exc}")
            return False

    async def expire(self, key: str, ttl: int | timedelta) -> bool:
        """
        设置键的过期时间

        Args:
            key: 缓存键
            ttl: 过期时间

        Returns:
            是否成功
        """
        if not self._connected:
            await self.connect()

        try:
            if isinstance(ttl, timedelta):
                ttl = int(ttl.total_seconds())
            return await self._redis.expire(key, ttl)
        except Exception as exc:
            # 设置过期时间失败，返回 False
            logger.debug(f"Redis 设置过期时间失败 (key={key}): {exc}")
            return False

    async def clear_pattern(self, pattern: str) -> int:
        """
        清除匹配模式的所有键

        Args:
            pattern: 键模式（支持通配符）

        Returns:
            删除的键数量
        """
        if not self._connected:
            await self.connect()

        try:
            keys = await self._redis.keys(pattern)
            if keys:
                return await self._redis.delete(*keys)
            return 0
        except Exception as exc:
            # 清除模式失败，返回 0
            logger.debug(f"Redis 清除模式失败 (pattern={pattern}): {exc}")
            return 0

    async def get_info(self) -> dict:
        """
        获取Redis信息

        Returns:
            Redis信息字典
        """
        if not self._connected:
            await self.connect()

        try:
            return await self._redis.info()
        except Exception as exc:
            # 获取信息失败，返回空字典
            logger.debug(f"Redis 获取信息失败: {exc}")
            return {}


# 全局Redis管理器实例
_redis_manager: RedisManager | None = None


def get_redis_manager() -> RedisManager:
    """
    获取Redis管理器单例

    Returns:
        RedisManager实例
    """
    global _redis_manager
    if _redis_manager is None:
        _redis_manager = RedisManager()
    return _redis_manager


async def get_redis_client() -> Redis:
    """
    获取Redis客户端

    Returns:
        Redis客户端实例
    """
    manager = get_redis_manager()
    if not manager._connected:
        await manager.connect()
    return manager._redis
