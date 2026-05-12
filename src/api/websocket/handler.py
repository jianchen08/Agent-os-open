"""
WebSocket 连接处理器

管理 WebSocket 连接的生命周期和消息分发
"""

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket 连接管理器（线程安全）"""

    def __init__(self, enable_redis: bool = False):
        """
        初始化连接管理器

        Args:
            enable_redis: 是否启用 Redis 支持（预留参数，当前版本暂未实现 Redis 功能）
        """
        # 并发控制锁
        self._lock = asyncio.Lock()
        # 所有活跃连接
        self._active_connections: set[WebSocket] = set()
        # 按线程 ID 分组的连接
        self._thread_connections: dict[str, set[WebSocket]] = {}
        # 按用户 ID 分组的连接
        self._user_connections: dict[str, set[WebSocket]] = {}
        # WebSocket 到元数据的映射
        self._connection_metadata: dict[WebSocket, dict[str, Any]] = {}
        # Redis 支持标志（预留）
        self._enable_redis = enable_redis
        # Redis 监听器任务（预留）
        self._redis_listener_task = None

    async def connect(self, websocket: WebSocket, thread_id: str, user_id: str) -> bool:
        """
        接受新的 WebSocket 连接（线程安全）

        Args:
            websocket: WebSocket 连接
            thread_id: 线程 ID
            user_id: 用户 ID

        Returns:
            是否连接成功
        """
        try:
            await websocket.accept()
            logger.info(f"[WS] 连接建立 | thread_id={thread_id} | user_id={user_id}")
        except Exception as e:
            logger.error(f"[WS] 连接建立失败 | thread_id={thread_id} | error={e}")
            return False

        # 使用锁保护连接操作
        async with self._lock:
            # 添加到活跃连接
            self._active_connections.add(websocket)

            # 添加到线程分组
            if thread_id not in self._thread_connections:
                self._thread_connections[thread_id] = set()
            self._thread_connections[thread_id].add(websocket)

            # 添加到用户分组
            if user_id not in self._user_connections:
                self._user_connections[user_id] = set()
            self._user_connections[user_id].add(websocket)

            # 保存元数据
            self._connection_metadata[websocket] = {
                "thread_id": thread_id,
                "user_id": user_id,
            }

        logger.info(f"WebSocket 连接建立 | thread_id={thread_id} | user_id={user_id}")
        return True

    async def disconnect(self, websocket: WebSocket, thread_id: str) -> None:
        """
        断开 WebSocket 连接（线程安全）

        Args:
            websocket: WebSocket 连接
            thread_id: 线程 ID
        """
        # 使用锁保护断开操作
        async with self._lock:
            # 从活跃连接移除
            self._active_connections.discard(websocket)

            # 从线程分组移除
            if thread_id in self._thread_connections:
                self._thread_connections[thread_id].discard(websocket)
                if not self._thread_connections[thread_id]:
                    del self._thread_connections[thread_id]

            # 获取元数据
            metadata = self._connection_metadata.get(websocket, {})
            user_id = metadata.get("user_id")

            # 从用户分组移除
            if user_id and user_id in self._user_connections:
                self._user_connections[user_id].discard(websocket)
                if not self._user_connections[user_id]:
                    del self._user_connections[user_id]

            # 移除元数据
            self._connection_metadata.pop(websocket, None)

        logger.info(f"[WS] 连接断开 | thread_id={thread_id}")

    async def send_to_connection(
        self, websocket: WebSocket, message: dict[str, Any], max_retries: int = 3
    ) -> bool:
        """
        向单个连接发送消息（带重试机制）

        Args:
            websocket: WebSocket 连接
            message: 消息内容
            max_retries: 最大重试次数，默认 3 次

        Returns:
            是否发送成功
        """
        from starlette.websockets import WebSocketState

        from src.core.tokenizer import get_token_counter

        msg_type = message.get("type", "unknown")
        token_counter = get_token_counter()
        msg_size = token_counter.count_tokens(str(message))

        logger.debug(
            f"[ConnectionManager] 发送消息 | "
            f"type={msg_type} | "
            f"size={msg_size} | "
            f"max_retries={max_retries}"
        )

        for attempt in range(max_retries):
            try:
                # 检查 WebSocket 连接状态
                # WebSocket.client_state 可能的值：CONNECTING, CONNECTED, DISCONNECTED
                if websocket.client_state != WebSocketState.CONNECTED:
                    logger.debug(
                        f"[ConnectionManager] 连接已关闭，跳过发送 | "
                        f"state={websocket.client_state} | "
                        f"msg_type={msg_type}"
                    )
                    return False

                # 实际发送消息
                await websocket.send_json(message)

                logger.debug(
                    f"[ConnectionManager] 消息发送成功 | "
                    f"type={msg_type} | "
                    f"attempt={attempt + 1}"
                )
                return True

            except RuntimeError as e:
                # 捕获 "Cannot call 'send' once a close message has been sent" 错误
                if "close" in str(e).lower():
                    logger.debug(
                        f"[ConnectionManager] 连接已关闭，无法发送 | "
                        f"msg_type={msg_type} | "
                        f"error={e}"
                    )
                    return False
                # 其他 RuntimeError，如果是最后一次重试则记录警告
                if attempt == max_retries - 1:
                    logger.warning(
                        f"[ConnectionManager] RuntimeError 发送失败 | "
                        f"type={msg_type} | "
                        f"error={e} | "
                        f"retry={attempt + 1}/{max_retries}"
                    )
                    return False
                # 等待后重试
                logger.debug(
                    f"[ConnectionManager] RuntimeError 重试 | "
                    f"attempt={attempt + 1}/{max_retries}"
                )
                await asyncio.sleep(0.1 * (attempt + 1))

            except Exception as e:
                # 其他异常
                if attempt == max_retries - 1:
                    logger.error(
                        f"[ConnectionManager] 发送失败 | "
                        f"type={msg_type} | "
                        f"error={e} | "
                        f"retry={attempt + 1}/{max_retries}",
                        exc_info=True,
                    )
                    return False
                # 等待后重试
                logger.debug(
                    f"[ConnectionManager] 异常重试 | "
                    f"attempt={attempt + 1}/{max_retries} | "
                    f"error={str(e)[:100]}"
                )
                await asyncio.sleep(0.1 * (attempt + 1))

        logger.warning(
            f"[ConnectionManager] 发送失败，已达最大重试次数 | "
            f"type={msg_type} | "
            f"max_retries={max_retries}"
        )
        return False

    async def send_to_thread(self, thread_id: str, message: dict[str, Any]) -> int:
        """
        向线程的所有连接发送消息（线程安全）

        Args:
            thread_id: 线程 ID
            message: 消息内容

        Returns:
            成功发送的连接数
        """
        # 使用锁保护连接读取
        async with self._lock:
            connections = self._thread_connections.get(thread_id, set()).copy()

        success_count = 0
        failed_connections = []

        for websocket in connections:
            if await self.send_to_connection(websocket, message):
                success_count += 1
            else:
                # 记录发送失败的连接，稍后清理
                failed_connections.append(websocket)

        # 清理断开的连接
        for websocket in failed_connections:
            await self._cleanup_connection(websocket, thread_id)

        return success_count

    async def _cleanup_connection(self, websocket: WebSocket, thread_id: str) -> None:
        """
        清理断开的连接（内部方法，线程安全）

        Args:
            websocket: WebSocket 连接
            thread_id: 线程 ID
        """
        # 使用锁保护清理操作
        async with self._lock:
            # 从活跃连接移除
            self._active_connections.discard(websocket)

            # 从线程分组移除
            if thread_id in self._thread_connections:
                self._thread_connections[thread_id].discard(websocket)
                if not self._thread_connections[thread_id]:
                    del self._thread_connections[thread_id]

            # 获取元数据
            metadata = self._connection_metadata.get(websocket, {})
            user_id = metadata.get("user_id")

            # 从用户分组移除
            if user_id and user_id in self._user_connections:
                self._user_connections[user_id].discard(websocket)
                if not self._user_connections[user_id]:
                    del self._user_connections[user_id]

            # 移除元数据
            self._connection_metadata.pop(websocket, None)

        logger.debug(f"已清理断开的连接 | thread_id={thread_id}")

    async def send_to_user(self, user_id: str, message: dict[str, Any]) -> int:
        """
        向用户的所有连接发送消息（线程安全）

        Args:
            user_id: 用户 ID
            message: 消息内容

        Returns:
            成功发送的连接数
        """
        # 使用锁保护连接读取
        async with self._lock:
            connections = self._user_connections.get(user_id, set()).copy()

        success_count = 0

        for websocket in connections:
            if await self.send_to_connection(websocket, message):
                success_count += 1

        return success_count

    async def broadcast(self, message: dict[str, Any]) -> int:
        """
        向所有连接广播消息（线程安全）

        Args:
            message: 消息内容

        Returns:
            成功发送的连接数
        """
        # 使用锁保护连接读取
        async with self._lock:
            connections = self._active_connections.copy()

        success_count = 0

        for websocket in connections:
            if await self.send_to_connection(websocket, message):
                success_count += 1

        return success_count

    async def get_connection_count(self) -> int:
        """获取活跃连接数（线程安全）"""
        async with self._lock:
            return len(self._active_connections)

    async def get_thread_connection_count(self, thread_id: str) -> int:
        """获取线程的连接数（线程安全）"""
        async with self._lock:
            return len(self._thread_connections.get(thread_id, set()))

    async def get_user_connection_count(self, user_id: str) -> int:
        """获取用户的连接数（线程安全）"""
        async with self._lock:
            return len(self._user_connections.get(user_id, set()))

    async def get_connection_metadata(
        self, websocket: WebSocket
    ) -> dict[str, Any] | None:
        """获取连接的元数据（线程安全）"""
        async with self._lock:
            return self._connection_metadata.get(websocket)

    # 工具调用相关消息发送方法

    async def send_tool_call_start(
        self,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        ai_message_id: str,
    ) -> int:
        """发送工具调用开始消息"""
        from src.api.websocket.message_types import create_tool_call_start_message

        logger.info(
            f"[ConnectionManager] 发送工具调用开始 | "
            f"tool_name={tool_name} | "
            f"tool_call_id={tool_call_id} | "
            f"thread_id={thread_id}"
        )
        logger.debug(
            f"[ConnectionManager] 工具参数 | "
            f"tool_name={tool_name} | "
            f"parameters={str(parameters)[:200]}..."
        )

        message = create_tool_call_start_message(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            parameters=parameters,
            ai_message_id=ai_message_id,
        )

        sent_count = await self.send_to_thread(thread_id, message)
        logger.debug(
            f"[ConnectionManager] 工具调用开始消息已发送 | "
            f"tool_name={tool_name} | "
            f"sent_count={sent_count}"
        )
        return sent_count

    async def send_tool_call_end(
        self,
        thread_id: str,
        tool_call_id: str,
        status: str,
        result: Any | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        ai_message_id: str | None = None,
    ) -> int:
        """发送工具调用结束消息"""
        from src.api.websocket.message_types import create_tool_call_end_message

        logger.info(
            f"[ConnectionManager] 发送工具调用结束 | "
            f"tool_call_id={tool_call_id} | "
            f"status={status} | "
            f"duration_ms={duration_ms} | "
            f"thread_id={thread_id}"
        )

        if result:
            logger.debug(
                f"[ConnectionManager] 工具执行结果 | "
                f"tool_call_id={tool_call_id} | "
                f"result={str(result)[:300]}..."
            )
        if error:
            logger.warning(
                f"[ConnectionManager] 工具执行错误 | "
                f"tool_call_id={tool_call_id} | "
                f"error={error}"
            )

        message = create_tool_call_end_message(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            status=status,
            result=result,
            error=error,
            duration_ms=duration_ms,
            ai_message_id=ai_message_id,
        )

        sent_count = await self.send_to_thread(thread_id, message)
        logger.debug(
            f"[ConnectionManager] 工具调用结束消息已发送 | "
            f"tool_call_id={tool_call_id} | "
            f"sent_count={sent_count}"
        )
        return sent_count

    async def cleanup_redis(self) -> None:
        """
        清理 Redis 资源（预留方法，当前版本暂未实现 Redis 功能）

        用于测试兼容性，实际 Redis 功能将在后续版本实现。
        """
        if self._redis_listener_task is not None:
            self._redis_listener_task.cancel()
            try:
                await self._redis_listener_task
            except (asyncio.CancelledError, TypeError, RuntimeError):
                # CancelledError: 任务被取消（正常情况）
                # TypeError: Mock 对象无法等待（测试场景）
                # RuntimeError: 任务已完成或无法等待
                pass
            self._redis_listener_task = None
        logger.debug("[ConnectionManager] Redis 资源已清理")

    def cancel_thread(self, thread_id: str) -> None:
        """
        标记线程为已取消（预留方法，用于测试兼容性）

        Args:
            thread_id: 线程 ID
        """
        # 预留方法，实际取消逻辑将在后续版本实现
        logger.debug(f"[ConnectionManager] 线程已标记为取消 | thread_id={thread_id}")

    def is_thread_cancelled(self, thread_id: str) -> bool:
        """
        检查线程是否已被取消（预留方法，用于测试兼容性）

        Args:
            thread_id: 线程 ID

        Returns:
            线程是否已取消（当前始终返回 False）
        """
        # 预留方法，实际取消逻辑将在后续版本实现
        return False


# 全局连接管理器实例
connection_manager = ConnectionManager()
