"""
WebSocket 消息总线（多层次路由版本）

统一管理所有 WebSocket 消息的发送，确保：
1. 每个 thread_id 只有一个活跃连接
2. 支持多层次消息路由（thread:source_type:source_id:channel）
3. 消息按顺序发送，不会丢失
4. 避免重复发送
5. 提供统一的消息发送接口

架构决策: ADR-001
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from fastapi import WebSocket
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SourceType(str, Enum):
    """消息源类型"""

    MAIN = "main"  # 主控 Agent
    AGENT = "agent"  # 子 Agent
    WORKFLOW = "workflow"  # 工作流
    SYSTEM = "system"  # 系统消息


@dataclass
class RoutingKey:
    """
    路由键：用于层次化消息路由

    格式: {thread_id}:{source_type}:{source_id}:{channel}

    示例:
        thread-00001:main:default:output
        thread-00001:agent:data_analyzer:progress
        thread-00001:workflow:etl_pipeline:step
        thread-00001:system:health:status
    """

    thread_id: str
    source_type: SourceType
    source_id: str
    channel: str = "default"

    def to_string(self) -> str:
        """转换为字符串格式"""
        return (
            f"{self.thread_id}:{self.source_type.value}:{self.source_id}:{self.channel}"
        )

    @classmethod
    def from_string(cls, routing_key_str: str) -> "RoutingKey":
        """从字符串解析"""
        parts = routing_key_str.split(":")
        if len(parts) != 4:
            raise ValueError(f"Invalid routing key format: {routing_key_str}")

        return cls(
            thread_id=parts[0],
            source_type=SourceType(parts[1]),
            source_id=parts[2],
            channel=parts[3],
        )

    @classmethod
    def create_main(cls, thread_id: str, channel: str = "output") -> "RoutingKey":
        """创建主控 Agent 的路由键"""
        return cls(thread_id, SourceType.MAIN, "default", channel)

    @classmethod
    def create_agent(
        cls, thread_id: str, agent_id: str, channel: str = "output"
    ) -> "RoutingKey":
        """创建子 Agent 的路由键"""
        return cls(thread_id, SourceType.AGENT, agent_id, channel)

    @classmethod
    def create_workflow(
        cls, thread_id: str, workflow_id: str, channel: str = "step"
    ) -> "RoutingKey":
        """创建工作流的路由键"""
        return cls(thread_id, SourceType.WORKFLOW, workflow_id, channel)

    @classmethod
    def create_system(cls, thread_id: str, channel: str) -> "RoutingKey":
        """创建系统消息的路由键"""
        return cls(thread_id, SourceType.SYSTEM, "system", channel)


class MessageMetadata(BaseModel):
    """消息元数据"""

    routing_key: str
    source_type: str
    source_id: str
    channel: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    correlation_id: str = Field(default_factory=lambda: f"msg-{uuid.uuid4().hex[:16]}")


class EnrichedMessage(BaseModel):
    """增强的消息（带路由信息）"""

    type: str
    routing_key: str
    message: dict[str, Any]
    metadata: MessageMetadata


class QueuedMessage(BaseModel):
    """队列中的消息"""

    thread_id: str
    enriched_message: EnrichedMessage
    priority: int = 0
    timestamp: float = Field(default_factory=time.time)
    retry_count: int = 0


class ThreadConnection(BaseModel):
    """线程连接信息"""

    model_config = {"arbitrary_types_allowed": True}

    thread_id: str
    websocket: WebSocket
    connected_at: float = Field(default_factory=time.time)
    last_heartbeat: float = Field(default_factory=time.time)


@dataclass
class RegisteredSource:
    """已注册的消息源"""

    thread_id: str
    source_type: SourceType
    source_id: str
    registered_at: float = field(default_factory=time.time)


class MessageBus:
    """
    WebSocket 消息总线（多层次路由版本）

    核心职责：
    1. 维护每个 thread_id 的唯一活跃连接
    2. 统一管理所有消息的发送（带路由键）
    3. 提供消息队列和顺序保证
    4. 自动处理连接/断开事件
    5. 支持消息源注册和管理
    """

    _instance: Optional["MessageBus"] = None

    def __new__(cls) -> "MessageBus":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # 每个 thread_id 的活跃连接（保证 1:1）
        self._active_connections: dict[str, ThreadConnection] = {}

        # 每个 thread_id 的消息队列
        self._message_queues: dict[str, asyncio.Queue] = {}

        # 发送器任务（每个线程一个）
        self._sender_tasks: dict[str, asyncio.Task] = {}

        # 已注册的消息源（用于验证）
        self._registered_sources: dict[str, RegisteredSource] = {}

        # 锁
        self._lock = asyncio.Lock()

        # 统计
        self._stats = {
            "total_messages_sent": 0,
            "total_connections": 0,
            "total_disconnections": 0,
            "connection_replacements": 0,
            "registered_sources": 0,
        }

        self._initialized = True
        logger.info("[MessageBus] 消息总线已初始化（多层次路由版本）")

    async def register_source(
        self, thread_id: str, source_type: SourceType, source_id: str
    ) -> str:
        """
        注册消息源

        Args:
            thread_id: 线程 ID
            source_type: 源类型
            source_id: 源 ID（如 agent_id、workflow_id）

        Returns:
            路由键字符串
        """
        routing_key = RoutingKey(thread_id, source_type, source_id)
        routing_key_str = routing_key.to_string()

        async with self._lock:
            self._registered_sources[routing_key_str] = RegisteredSource(
                thread_id=thread_id, source_type=source_type, source_id=source_id
            )
            self._stats["registered_sources"] = len(self._registered_sources)

        logger.debug(f"[MessageBus] 消息源已注册 | routing_key={routing_key_str}")

        return routing_key_str

    async def emit(
        self,
        thread_id: str,
        message: dict[str, Any],
        source_type: SourceType | None = None,
        source_id: str | None = None,
        channel: str = "default",
    ) -> bool:
        """
        发送消息到指定线程（带路由信息）

        这是唯一的外部接口，所有消息发送都应该通过它

        Args:
            thread_id: 线程 ID
            message: 消息内容
            source_type: 源类型（可选，默认为 MAIN）
            source_id: 源 ID（可选）
            channel: 通道（可选，默认为 default）

        Returns:
            是否成功加入队列
        """
        # 默认为主控 Agent
        if source_type is None:
            source_type = SourceType.MAIN
        if source_id is None:
            source_id = "default"

        # 创建路由键
        routing_key = RoutingKey(thread_id, source_type, source_id, channel)
        routing_key_str = routing_key.to_string()

        # 创建增强消息
        metadata = MessageMetadata(
            routing_key=routing_key_str,
            source_type=source_type.value,
            source_id=source_id,
            channel=channel,
        )

        enriched_message = EnrichedMessage(
            type=message.get("type", "unknown"),
            routing_key=routing_key_str,
            message=message,
            metadata=metadata,
        )

        # 转换为发送格式
        {
            **message,
            "routing_key": routing_key_str,
            "metadata": metadata.dict(),
        }

        async with self._lock:
            # 检查是否有活跃连接
            if thread_id not in self._active_connections:
                logger.warning(
                    f"[MessageBus] 无法发送消息 | "
                    f"thread_id={thread_id} | "
                    f"routing_key={routing_key_str} | "
                    f"原因：无活跃连接"
                )
                return False

            # 确保队列存在
            if thread_id not in self._message_queues:
                self._message_queues[thread_id] = asyncio.Queue()

            # 确保发送器在运行（每次都检查，因为发送器可能意外停止）
            if thread_id not in self._sender_tasks:
                await self._start_sender(thread_id)

            # 加入队列
            try:
                queued_message = QueuedMessage(
                    thread_id=thread_id, enriched_message=enriched_message, priority=0
                )
                await self._message_queues[thread_id].put(queued_message)
                logger.debug(
                    f"[MessageBus] 消息已加入队列 | "
                    f"thread_id={thread_id} | "
                    f"routing_key={routing_key_str} | "
                    f"type={message.get('type', 'unknown')}"
                )
                return True
            except asyncio.QueueFull:
                logger.error(f"[MessageBus] 队列已满 | thread_id={thread_id}")
                return False

    async def register_connection(
        self,
        thread_id: str,
        websocket: WebSocket,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        注册新的 WebSocket 连接

        策略：每个 thread_id 只保留一个活跃连接
        - 如果已有连接，关闭旧连接
        - 将新连接设置为活跃连接

        Args:
            thread_id: 线程 ID
            websocket: WebSocket 连接
            metadata: 元数据

        Returns:
            注册结果
        """
        old_websocket = None

        # 先 accept WebSocket 连接
        try:
            await websocket.accept()
        except Exception as e:
            logger.error(
                f"[MessageBus] WebSocket accept 失败 | "
                f"thread_id={thread_id} | error={e}"
            )
            return {"success": False, "error": str(e)}

        async with self._lock:
            old_connection = self._active_connections.get(thread_id)

            if old_connection:
                logger.warning(
                    f"[MessageBus] 检测到旧连接 | "
                    f"thread_id={thread_id} | "
                    f"将被新连接替换"
                )
                old_websocket = old_connection.websocket
                self._stats["connection_replacements"] += 1

            # 创建新连接记录
            connection = ThreadConnection(thread_id=thread_id, websocket=websocket)

            self._active_connections[thread_id] = connection
            self._stats["total_connections"] += 1

            logger.info(
                f"[MessageBus] 连接已注册 | "
                f"thread_id={thread_id} | "
                f"当前连接数={len(self._active_connections)}"
            )

            return {
                "success": True,
                "thread_id": thread_id,
                "replaced": old_connection is not None,
                "total_connections": self._stats["total_connections"],
            }

        # 在锁外关闭旧连接（避免阻塞）
        if old_websocket:
            try:
                await old_websocket.close(code=1000, reason="Connection replaced")
            except Exception as e:
                logger.warning(f"[MessageBus] 关闭旧连接失败: {e}")

    async def unregister_connection(self, thread_id: str, websocket: WebSocket) -> None:
        """
        注销 WebSocket 连接

        Args:
            thread_id: 线程 ID
            websocket: WebSocket 连接
        """
        async with self._lock:
            connection = self._active_connections.get(thread_id)

            # 只有匹配的连接才注销（防止误关闭）
            if connection and connection.websocket == websocket:
                del self._active_connections[thread_id]
                self._stats["total_disconnections"] += 1

                logger.info(f"[MessageBus] 连接已注销 | thread_id={thread_id}")

    async def get_active_connection(self, thread_id: str) -> WebSocket | None:
        """
        获取线程的活跃连接

        Args:
            thread_id: 线程 ID

        Returns:
            WebSocket 连接或 None
        """
        async with self._lock:
            connection = self._active_connections.get(thread_id)
            if connection:
                return connection.websocket
            return None

    def get_stats(self) -> dict[str, Any]:
        """获取总线统计信息"""
        return {
            **self._stats,
            "active_connections": len(self._active_connections),
            "thread_ids": list(self._active_connections.keys()),
            "registered_sources": list(self._registered_sources.keys()),
        }

    async def _start_sender(self, thread_id: str) -> None:
        """
        启动消息发送器任务（每个线程一个）

        Args:
            thread_id: 线程 ID
        """
        if thread_id in self._sender_tasks:
            return  # 已经在运行

        async def sender():
            """消息发送器协程"""
            logger.debug(f"[MessageBus] 发送器已启动 | thread_id={thread_id}")

            while thread_id in self._active_connections:
                try:
                    queue = self._message_queues.get(thread_id)
                    if not queue:
                        logger.warning(
                            f"[MessageBus] 队列不存在 | thread_id={thread_id}"
                        )
                        break

                    # 获取消息（带超时）
                    try:
                        queued_msg: QueuedMessage = await asyncio.wait_for(
                            queue.get(), timeout=1.0
                        )
                    except TimeoutError:
                        # 没有新消息，继续循环
                        continue

                    # 检查连接是否还活跃
                    connection = self._active_connections.get(thread_id)
                    if not connection:
                        logger.warning(
                            f"[MessageBus] 连接已断开 | thread_id={thread_id}"
                        )
                        break

                    # 发送消息（使用增强消息格式）
                    try:
                        # 检查 WebSocket 连接状态，避免向已关闭连接发送消息
                        from starlette.websockets import WebSocketState, WebSocketDisconnect

                        websocket = connection.websocket
                        if websocket.client_state == WebSocketState.DISCONNECTED:
                            logger.warning(
                                f"[MessageBus] WebSocket 已断开，跳过消息发送 | "
                                f"thread_id={thread_id}"
                            )
                            await self.unregister_connection(thread_id, websocket)
                            break

                        # 发送带 routing_key 的消息
                        # 将 message 字段展开，避免嵌套
                        base_message = queued_msg.enriched_message.message
                        message_to_send = {
                            **base_message,
                            "routing_key": queued_msg.enriched_message.routing_key,
                            "metadata": queued_msg.enriched_message.metadata.dict(),
                        }
                        await websocket.send_json(message_to_send)
                        self._stats["total_messages_sent"] += 1
                        logger.debug(
                            f"[MessageBus] 消息已发送 | "
                            f"thread_id={thread_id} | "
                            f"routing_key={queued_msg.enriched_message.routing_key}"
                        )
                    except WebSocketDisconnect:
                        # 客户端正常断开连接，记录为警告级别（非错误）
                        logger.warning(
                            f"[MessageBus] 客户端断开连接，停止发送 | "
                            f"thread_id={thread_id}"
                        )
                        await self.unregister_connection(thread_id, connection.websocket)
                        break
                    except Exception as send_error:
                        logger.error(
                            f"[MessageBus] 发送失败 | "
                            f"thread_id={thread_id} | "
                            f"error_type={type(send_error).__name__} | "
                            f"error={str(send_error)} | "
                            f"repr={repr(send_error)}",
                            exc_info=True,
                        )
                        # 发送失败，标记连接为无效
                        await self.unregister_connection(
                            thread_id, connection.websocket
                        )
                        break

                except Exception as e:
                    logger.error(
                        f"[MessageBus] 发送器异常 | thread_id={thread_id} | error={e}"
                    )
                    break

            # 发送器结束，清理
            logger.debug(f"[MessageBus] 发送器已停止 | thread_id={thread_id}")
            if thread_id in self._sender_tasks:
                del self._sender_tasks[thread_id]

        # 启动发送器任务
        task = asyncio.create_task(sender())
        self._sender_tasks[thread_id] = task
        logger.debug(f"[MessageBus] 发送器任务已创建 | thread_id={thread_id}")


# 全局单例
_message_bus: MessageBus | None = None


def get_message_bus() -> MessageBus:
    """获取消息总线单例"""
    global _message_bus
    if _message_bus is None:
        _message_bus = MessageBus()
    return _message_bus


# 便捷函数
async def emit_to_thread(
    thread_id: str,
    message: dict[str, Any],
    source_type: SourceType | None = None,
    source_id: str | None = None,
) -> bool:
    """
    发送消息到线程（便捷函数）

    Args:
        thread_id: 线程 ID
        message: 消息内容
        source_type: 源类型（可选）
        source_id: 源 ID（可选）

    Returns:
        是否成功
    """
    bus = get_message_bus()
    return await bus.emit(thread_id, message, source_type, source_id)


async def emit_from_agent(
    thread_id: str, agent_id: str, message: dict[str, Any]
) -> bool:
    """从子 Agent 发送消息"""
    return await emit_to_thread(thread_id, message, SourceType.AGENT, agent_id)


async def emit_from_workflow(
    thread_id: str, workflow_id: str, message: dict[str, Any]
) -> bool:
    """从工作流发送消息"""
    return await emit_to_thread(thread_id, message, SourceType.WORKFLOW, workflow_id)


def get_bus_stats() -> dict[str, Any]:
    """获取总线统计"""
    bus = get_message_bus()
    return bus.get_stats()
