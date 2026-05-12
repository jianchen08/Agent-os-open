"""
Redis Pub/Sub 消息广播模块

支持多实例部署的 WebSocket 消息广播
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import redis.asyncio as redis

from src.config import get_settings

logger = logging.getLogger(__name__)


class RedisPublisher:
    """Redis 消息发布器"""

    def __init__(self):
        """初始化 Redis 发布器"""
        self._redis: redis.Redis | None = None
        self._settings = get_settings()

    async def _get_redis(self) -> redis.Redis:
        """获取 Redis 连接（懒加载）"""
        if self._redis is None:
            # 解析 Redis URL
            redis_url_parts = self._settings.redis_url.split("://")[1]
            if ":" in redis_url_parts and "/" in redis_url_parts:
                host_port, db_part = redis_url_parts.split("/", 1)
                host, port = host_port.split(":", 1)
                # 使用配置中的 redis_db，而不是 URL 中的
                db = self._settings.redis_db
            else:
                host = redis_url_parts
                port = 6379
                db = self._settings.redis_db

            self._redis = redis.Redis(
                host=host,
                port=int(port),
                db=db,
                decode_responses=self._settings.redis_decode_responses,
                socket_connect_timeout=5,
                socket_keepalive=True,
                socket_keepalive_options={},
                health_check_interval=30,
            )

            # 测试连接
            try:
                await self._redis.ping()
                logger.info("[RedisPublisher] Redis 连接成功")
            except Exception as e:
                logger.error(f"[RedisPublisher] Redis 连接失败: {e}")
                raise

        return self._redis

    async def publish_to_thread(self, thread_id: str, message: dict[str, Any]) -> int:
        """
        发布消息到指定线程频道

        Args:
            thread_id: 线程 ID
            message: 消息内容

        Returns:
            接收消息的订阅者数量
        """
        try:
            redis_client = await self._get_redis()
            channel = f"websocket:thread:{thread_id}"

            # 序列化消息
            message_json = json.dumps(message, ensure_ascii=False)

            # 发布消息
            subscriber_count = await redis_client.publish(channel, message_json)

            logger.debug(
                f"[RedisPublisher] 发布消息到线程 {thread_id}, "
                f"订阅者数量: {subscriber_count}"
            )

            return subscriber_count

        except Exception as e:
            logger.error(f"[RedisPublisher] 发布消息失败: {e}")
            return 0

    async def broadcast_message(self, message: dict[str, Any]) -> int:
        """
        广播消息到所有实例

        Args:
            message: 消息内容

        Returns:
            接收消息的订阅者数量
        """
        try:
            redis_client = await self._get_redis()
            channel = "websocket:broadcast"

            # 序列化消息
            message_json = json.dumps(message, ensure_ascii=False)

            # 发布消息
            subscriber_count = await redis_client.publish(channel, message_json)

            logger.debug(f"[RedisPublisher] 广播消息, 订阅者数量: {subscriber_count}")

            return subscriber_count

        except Exception as e:
            logger.error(f"[RedisPublisher] 广播消息失败: {e}")
            return 0

    async def close(self):
        """关闭 Redis 连接"""
        if self._redis:
            await self._redis.close()
            self._redis = None
            logger.info("[RedisPublisher] Redis 连接已关闭")


class RedisSubscriber:
    """Redis 消息订阅器"""

    def __init__(self, on_message: Callable[[str, dict[str, Any]], None]):
        """
        初始化 Redis 订阅器

        Args:
            on_message: 消息处理回调函数，参数为 (channel, message)
        """
        self._redis: redis.Redis | None = None
        self._pubsub: redis.client.PubSub | None = None
        self._on_message = on_message
        self._settings = get_settings()
        self._listener_task: asyncio.Task | None = None
        self._subscribed_channels: set = set()

    async def _get_redis(self) -> redis.Redis:
        """获取 Redis 连接（懒加载）"""
        if self._redis is None:
            # 解析 Redis URL
            redis_url_parts = self._settings.redis_url.split("://")[1]
            if ":" in redis_url_parts and "/" in redis_url_parts:
                host_port, db_part = redis_url_parts.split("/", 1)
                host, port = host_port.split(":", 1)
                # 使用配置中的 redis_db，而不是 URL 中的
                db = self._settings.redis_db
            else:
                host = redis_url_parts
                port = 6379
                db = self._settings.redis_db

            self._redis = redis.Redis(
                host=host,
                port=int(port),
                db=db,
                decode_responses=self._settings.redis_decode_responses,
                socket_connect_timeout=5,
                socket_keepalive=True,
                socket_keepalive_options={},
                health_check_interval=30,
            )

            # 创建 PubSub 对象
            self._pubsub = self._redis.pubsub()

            # 测试连接
            try:
                await self._redis.ping()
                logger.info("[RedisSubscriber] Redis 连接成功")
            except Exception as e:
                logger.error(f"[RedisSubscriber] Redis 连接失败: {e}")
                raise

        return self._redis

    async def subscribe_thread(self, thread_id: str):
        """
        订阅线程消息

        Args:
            thread_id: 线程 ID
        """
        try:
            await self._get_redis()
            channel = f"websocket:thread:{thread_id}"

            if channel not in self._subscribed_channels:
                await self._pubsub.subscribe(channel)
                self._subscribed_channels.add(channel)

                logger.info(f"[RedisSubscriber] 订阅线程频道: {channel}")

                # 启动监听器（如果还未启动）
                if self._listener_task is None:
                    self._listener_task = asyncio.create_task(self._listen())

        except Exception as e:
            logger.error(f"[RedisSubscriber] 订阅线程失败: {e}")

    async def subscribe_broadcast(self):
        """订阅广播消息"""
        try:
            await self._get_redis()
            channel = "websocket:broadcast"

            if channel not in self._subscribed_channels:
                await self._pubsub.subscribe(channel)
                self._subscribed_channels.add(channel)

                logger.info(f"[RedisSubscriber] 订阅广播频道: {channel}")

                # 启动监听器（如果还未启动）
                if self._listener_task is None:
                    self._listener_task = asyncio.create_task(self._listen())

        except Exception as e:
            logger.error(f"[RedisSubscriber] 订阅广播失败: {e}")

    async def unsubscribe_thread(self, thread_id: str):
        """
        取消订阅线程消息

        Args:
            thread_id: 线程 ID
        """
        try:
            if self._pubsub:
                channel = f"websocket:thread:{thread_id}"
                await self._pubsub.unsubscribe(channel)
                self._subscribed_channels.discard(channel)

                logger.info(f"[RedisSubscriber] 取消订阅线程频道: {channel}")

        except Exception as e:
            logger.error(f"[RedisSubscriber] 取消订阅线程失败: {e}")

    async def _listen(self):
        """监听 Redis 消息"""
        try:
            logger.info("[RedisSubscriber] 开始监听 Redis 消息")

            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    try:
                        # 解析消息
                        channel = message["channel"]
                        data = json.loads(message["data"])

                        # 调用回调函数
                        await self._on_message(channel, data)

                    except json.JSONDecodeError as e:
                        logger.error(f"[RedisSubscriber] 消息解析失败: {e}")
                    except Exception as e:
                        logger.error(f"[RedisSubscriber] 消息处理失败: {e}")

        except asyncio.CancelledError:
            logger.info("[RedisSubscriber] 监听器已取消")
        except Exception as e:
            logger.error(f"[RedisSubscriber] 监听器异常: {e}")

    async def close(self):
        """关闭订阅器"""
        try:
            # 取消监听器任务
            if self._listener_task:
                self._listener_task.cancel()
                try:
                    await self._listener_task
                except asyncio.CancelledError:
                    pass
                self._listener_task = None

            # 关闭 PubSub
            if self._pubsub:
                await self._pubsub.close()
                self._pubsub = None

            # 关闭 Redis 连接
            if self._redis:
                await self._redis.close()
                self._redis = None

            self._subscribed_channels.clear()
            logger.info("[RedisSubscriber] 订阅器已关闭")

        except Exception as e:
            logger.error(f"[RedisSubscriber] 关闭订阅器失败: {e}")


# 全局实例
_redis_publisher: RedisPublisher | None = None
_redis_subscriber: RedisSubscriber | None = None


async def get_redis_publisher() -> RedisPublisher:
    """获取 Redis 发布器单例"""
    global _redis_publisher
    if _redis_publisher is None:
        _redis_publisher = RedisPublisher()
    return _redis_publisher


async def get_redis_subscriber(
    on_message: Callable[[str, dict[str, Any]], None],
) -> RedisSubscriber:
    """获取 Redis 订阅器单例"""
    global _redis_subscriber
    if _redis_subscriber is None:
        _redis_subscriber = RedisSubscriber(on_message)
    return _redis_subscriber


async def cleanup_redis():
    """清理 Redis 连接"""
    global _redis_publisher, _redis_subscriber

    if _redis_publisher:
        await _redis_publisher.close()
        _redis_publisher = None

    if _redis_subscriber:
        await _redis_subscriber.close()
        _redis_subscriber = None

    logger.info("[Redis] 清理完成")
