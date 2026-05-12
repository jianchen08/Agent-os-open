"""
分布式锁管理器

基于 Redis 的分布式锁实现
"""

import asyncio
import logging
import uuid

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class DistributedLock:
    """分布式锁"""

    def __init__(
        self,
        redis_client: Redis,
        lock_name: str,
        expire_time: float = 30.0,
        auto_renewal: bool = True,
    ):
        """
        初始化分布式锁

        Args:
            redis_client: Redis 客户端
            lock_name: 锁名称
            expire_time: 锁过期时间（秒）
            auto_renewal: 是否自动续期
        """
        self.redis = redis_client
        self.lock_name = f"lock:{lock_name}"
        self.expire_time = expire_time
        self.auto_renewal = auto_renewal
        self._lock_value: str | None = None
        self._locked = False
        self._renewal_task: asyncio.Task | None = None

    async def acquire(self, timeout: float | None = None) -> bool:
        """
        获取锁

        Args:
            timeout: 超时时间（秒），None 表示无限等待

        Returns:
            是否成功获取锁
        """
        self._lock_value = str(uuid.uuid4())
        start_time = asyncio.get_event_loop().time()

        while True:
            # 使用 SET NX EX 命令获取锁
            acquired = await self.redis.set(
                self.lock_name, self._lock_value, nx=True, ex=self.expire_time
            )

            if acquired:
                self._locked = True

                # 启动自动续期任务
                if self.auto_renewal:
                    self._renewal_task = asyncio.create_task(self._renew_loop())

                logger.debug(f"获取分布式锁成功: {self.lock_name}")
                return True

            # 检查超时
            if timeout is not None:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout:
                    logger.warning(f"获取分布式锁超时: {self.lock_name}")
                    return False

            # 等待一小段时间后重试
            await asyncio.sleep(0.1)

    async def release(self) -> None:
        """释放锁"""
        if not self._locked:
            return

        # 停止续期任务
        if self._renewal_task:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass
            self._renewal_task = None

        # 使用 Lua 脚本确保只释放自己持有的锁
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        try:
            await self.redis.eval(lua_script, 1, self.lock_name, self._lock_value)
            self._locked = False
            logger.debug(f"释放分布式锁成功: {self.lock_name}")
        except Exception as e:
            logger.error(f"释放分布式锁失败: {e}")

    async def _renew_loop(self):
        """自动续期循环"""
        renewal_interval = self.expire_time / 3  # 每1/3过期时间续期一次

        while self._locked:
            try:
                await asyncio.sleep(renewal_interval)

                # 续期
                lua_script = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("expire", KEYS[1], ARGV[2])
                else
                    return 0
                end
                """

                result = await self.redis.eval(
                    lua_script, 1, self.lock_name, self._lock_value, self.expire_time
                )

                if result == 0:
                    logger.warning(f"分布式锁已丢失: {self.lock_name}")
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"分布式锁续期失败: {e}")

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.release()

    @property
    def locked(self) -> bool:
        """是否持有锁"""
        return self._locked


class DistributedLockManager:
    """分布式锁管理器"""

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        """
        初始化管理器

        Args:
            redis_url: Redis 连接 URL
        """
        self.redis_url = redis_url
        self._redis: Redis | None = None
        self._locks: dict[str, DistributedLock] = {}

    async def _get_redis(self) -> Redis:
        """获取 Redis 连接"""
        if self._redis is None:
            self._redis = Redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def create_lock(
        self,
        lock_name: str,
        expire_time: float = 30.0,
        auto_renewal: bool = True,
    ) -> DistributedLock:
        """
        创建分布式锁

        Args:
            lock_name: 锁名称
            expire_time: 过期时间（秒）
            auto_renewal: 是否自动续期

        Returns:
            分布式锁实例
        """
        redis = await self._get_redis()

        if lock_name not in self._locks:
            self._locks[lock_name] = DistributedLock(
                redis_client=redis,
                lock_name=lock_name,
                expire_time=expire_time,
                auto_renewal=auto_renewal,
            )

        return self._locks[lock_name]

    async def acquire_lock(
        self,
        lock_name: str,
        timeout: float | None = None,
        expire_time: float = 30.0,
    ) -> bool:
        """
        获取锁

        Args:
            lock_name: 锁名称
            timeout: 超时时间
            expire_time: 过期时间

        Returns:
            是否成功
        """
        lock = await self.create_lock(lock_name, expire_time=expire_time)
        return await lock.acquire(timeout=timeout)

    async def release_lock(self, lock_name: str) -> None:
        """
        释放锁

        Args:
            lock_name: 锁名称
        """
        if lock_name in self._locks:
            await self._locks[lock_name].release()

    async def close(self) -> None:
        """关闭管理器，释放所有资源"""
        # 释放所有锁
        for lock in self._locks.values():
            await lock.release()

        # 关闭 Redis 连接
        if self._redis:
            await self._redis.close()
            self._redis = None

        self._locks.clear()

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._get_redis()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.close()


# 全局锁管理器实例
_lock_manager: DistributedLockManager | None = None


def get_lock_manager() -> DistributedLockManager:
    """获取全局锁管理器"""
    global _lock_manager
    if _lock_manager is None:
        from src.config.settings import settings

        redis_url = getattr(settings, "redis_url", "redis://localhost:6379/0")
        _lock_manager = DistributedLockManager(redis_url=redis_url)
    return _lock_manager
