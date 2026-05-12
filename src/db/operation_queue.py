"""
数据库操作队列

用于序列化数据库操作，避免并发事务冲突
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session_context

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DatabaseOperation:
    """数据库操作封装"""

    def __init__(self, operation: Callable, args: tuple, kwargs: dict):
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self.future = asyncio.get_event_loop().create_future()

    async def execute(self, session: AsyncSession):
        """执行操作"""
        try:
            result = await self.operation(session, *self.args, **self.kwargs)
            self.future.set_result(result)
        except Exception as e:
            self.future.set_exception(e)


class DatabaseOperationQueue:
    """
    数据库操作队列

    用于序列化数据库操作，确保同一时间只有一个数据库事务在执行
    """

    def __init__(self):
        self._queue: asyncio.Queue[DatabaseOperation] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """启动队列处理器"""
        if not self._running:
            self._running = True
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("[DBQueue] 数据库操作队列已启动")

    async def stop(self):
        """停止队列处理器"""
        if self._running:
            self._running = False
            # 发送停止信号
            await self._queue.put(None)
            if self._worker_task:
                await self._worker_task
            logger.info("[DBQueue] 数据库操作队列已停止")

    async def _worker(self):
        """队列处理器 - 串行执行数据库操作"""
        while self._running:
            try:
                # 获取操作
                operation = await self._queue.get()

                # 检查停止信号
                if operation is None:
                    break

                # 执行操作
                async with get_session_context() as session:
                    try:
                        await operation.execute(session)
                    except Exception as e:
                        logger.error(f"[DBQueue] 操作执行失败 | error={e}")
                        if not operation.future.done():
                            operation.future.set_exception(e)

            except Exception as e:
                logger.error(f"[DBQueue] 工作线程异常 | error={e}")

    async def execute(self, operation: Callable[..., T], *args, **kwargs) -> T:
        """
        提交操作到队列执行

        Args:
            operation: 数据库操作函数，第一个参数必须是 session
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            操作结果
        """
        if not self._running:
            await self.start()

        db_operation = DatabaseOperation(operation, args, kwargs)
        await self._queue.put(db_operation)

        # 等待操作完成
        return await db_operation.future


# 全局队列实例
_global_db_queue: DatabaseOperationQueue | None = None


def get_db_operation_queue() -> DatabaseOperationQueue:
    """获取全局数据库操作队列"""
    global _global_db_queue
    if _global_db_queue is None:
        _global_db_queue = DatabaseOperationQueue()
    return _global_db_queue


@asynccontextmanager
async def db_queue_context():
    """数据库操作队列上下文管理器"""
    queue = get_db_operation_queue()
    await queue.start()
    try:
        yield queue
    finally:
        await queue.stop()
