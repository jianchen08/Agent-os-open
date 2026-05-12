"""
PostgreSQL LISTEN/NOTIFY监听器
用于实时接收数据库变更通知并广播到WebSocket客户端
"""

import asyncio
import json
import logging
from collections.abc import Callable

import asyncpg

logger = logging.getLogger(__name__)


class PostgresNotifier:
    """PostgreSQL LISTEN/NOTIFY监听器"""

    def __init__(self, database_url: str):
        # 移除+asyncpg前缀以获取标准PostgreSQL URL
        if database_url.startswith("postgresql+asyncpg://"):
            database_url = database_url.replace(
                "postgresql+asyncpg://", "postgresql://"
            )

        self.database_url = database_url
        self.connection: asyncpg.Connection | None = None
        self.listeners: list[Callable] = []
        self.listening_task: asyncio.Task | None = None

    async def connect(self):
        """连接到PostgreSQL"""
        try:
            self.connection = await asyncpg.connect(self.database_url)
            logger.info("[PostgresNotifier] 已连接到PostgreSQL")
        except Exception as e:
            logger.error(f"[PostgresNotifier] 连接失败: {e}")
            raise

    async def disconnect(self):
        """断开连接"""
        if self.listening_task:
            self.listening_task.cancel()
            try:
                await self.listening_task
            except asyncio.CancelledError:
                pass

        if self.connection:
            await self.connection.close()
            logger.info("[PostgresNotifier] 已断开PostgreSQL连接")

    def add_listener(self, callback: Callable):
        """添加事件监听器回调函数"""
        self.listeners.append(callback)

    async def listen_to_channel(self, channel: str):
        """监听指定PostgreSQL通道"""
        if not self.connection:
            await self.connect()

        await self.connection.add_listener(channel, self._handle_notification)
        logger.info(f"[PostgresNotifier] 正在监听通道: {channel}")

    async def _handle_notification(
        self, connection: asyncpg.Connection, pid: int, channel: str, payload: str
    ):
        """处理PostgreSQL NOTIFY事件"""
        try:
            data = json.loads(payload)
            logger.info(
                f"[PostgresNotifier] 收到通知: {data.get('event')} | "
                f"message_id={data.get('message_id')}"
            )

            # 通知所有注册的监听器
            for listener in self.listeners:
                if asyncio.iscoroutinefunction(listener):
                    await listener(data)
                else:
                    listener(data)
        except Exception as e:
            logger.error(f"[PostgresNotifier] 处理通知失败: {e}")

    async def start_listening(self):
        """启动监听循环(保持连接活跃)"""
        try:
            while True:
                await asyncio.sleep(0.1)  # 保持连接活跃
        except asyncio.CancelledError:
            logger.info("[PostgresNotifier] 监听已取消")
        except Exception as e:
            logger.error(f"[PostgresNotifier] 监听循环错误: {e}")
        finally:
            await self.disconnect()
