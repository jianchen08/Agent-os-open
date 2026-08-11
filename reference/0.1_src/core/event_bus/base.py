"""
事件总线抽象基类

定义事件总线的统一接口
"""

import abc
import logging
import time
from typing import Any

from src.core.event_bus.types import (
    EventFilter,
    EventHandler,
    EventPriority,
    EventType,
    ExecutionEvent,
)

logger = logging.getLogger(__name__)


class EventBusBase(abc.ABC):
    """
    事件总线抽象基类

    定义所有事件总线实现必须遵循的接口
    """

    def __init__(self):
        """
        初始化事件总线
        """
        # 指标收集
        self._metrics = {
            "published_events": 0,
            "subscribed_events": 0,
            "failed_publishes": 0,
            "failed_subscriptions": 0,
            "publish_latency": [],  # 发布延迟（毫秒）
            "subscribe_latency": [],  # 订阅处理延迟（毫秒）
            "dead_letter_count": 0,
            "retry_count": 0,
        }
        # 连接状态
        self._connected = False
        # 启动时间
        self._start_time = 0

    @abc.abstractmethod
    async def connect(self) -> None:
        """
        建立连接
        """
        self._connected = True
        self._start_time = time.time()
        logger.info("事件总线已连接")

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """
        断开连接
        """
        self._connected = False
        logger.info("事件总线已断开")

    @abc.abstractmethod
    async def publish(self, event: ExecutionEvent, retry_count: int = 3) -> str:
        """
        发布事件

        Args:
            event: 执行事件
            retry_count: 重试次数

        Returns:
            事件 ID（在 Redis Streams 中是消息 ID）
        """

    @abc.abstractmethod
    def subscribe(
        self,
        handler: EventHandler,
        filter: EventFilter | None = None,
        consumer_group: str | None = None,
    ) -> str:
        """
        订阅事件

        Args:
            handler: 事件处理器
            filter: 事件过滤器
            consumer_group: 消费者组名称（用于负载均衡）

        Returns:
            订阅 ID
        """

    @abc.abstractmethod
    def unsubscribe(self, subscription_id: str) -> bool:
        """
        取消订阅

        Args:
            subscription_id: 订阅 ID

        Returns:
            是否成功取消
        """

    @abc.abstractmethod
    async def acknowledge(self, event_id: str, consumer_group: str) -> bool:
        """
        确认消息已处理（用于消费者组）

        Args:
            event_id: 事件/消息 ID
            consumer_group: 消费者组名称

        Returns:
            是否成功确认
        """

    @abc.abstractmethod
    async def get_history(
        self,
        session_id: str | None = None,
        event_type: EventType | None = None,
        limit: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[ExecutionEvent]:
        """
        获取事件历史

        Args:
            session_id: 按会话过滤
            event_type: 按事件类型过滤
            limit: 返回数量限制
            start_time: 开始时间（Redis Stream ID 或时间戳）
            end_time: 结束时间

        Returns:
            事件列表
        """

    @abc.abstractmethod
    async def get_pending_events(
        self,
        consumer_group: str,
        consumer_name: str | None = None,
    ) -> list[ExecutionEvent]:
        """
        获取待处理的事件（已读取但未确认）

        Args:
            consumer_group: 消费者组名称
            consumer_name: 消费者名称（可选）

        Returns:
            待处理事件列表
        """

    @abc.abstractmethod
    async def get_dead_letter_events(self, limit: int = 100) -> list[ExecutionEvent]:
        """
        获取死信队列中的事件

        Args:
            limit: 返回数量限制

        Returns:
            死信队列事件列表
        """

    @abc.abstractmethod
    async def retry_dead_letter_event(self, event_id: str) -> bool:
        """
        重试死信队列中的事件

        Args:
            event_id: 死信队列中的事件ID

        Returns:
            是否重试成功
        """

    @abc.abstractmethod
    async def clear_dead_letter_queue(self) -> int:
        """
        清空死信队列

        Returns:
            清空的事件数量
        """

    async def _send_to_dead_letter_queue(self, event: ExecutionEvent, error: str) -> None:
        """
        将失败的事件发送到死信队列

        Args:
            event: 失败的事件
            error: 错误信息
        """
        self._metrics["dead_letter_count"] += 1

    # ==================== 可观测性方法 ====================

    def get_metrics(self) -> dict[str, Any]:
        """
        获取事件总线指标

        Returns:
            指标字典
        """
        # 计算平均延迟
        avg_publish_latency = (
            sum(self._metrics["publish_latency"]) / len(self._metrics["publish_latency"])
            if self._metrics["publish_latency"]
            else 0
        )
        avg_subscribe_latency = (
            sum(self._metrics["subscribe_latency"]) / len(self._metrics["subscribe_latency"])
            if self._metrics["subscribe_latency"]
            else 0
        )

        return {
            "published_events": self._metrics["published_events"],
            "subscribed_events": self._metrics["subscribed_events"],
            "failed_publishes": self._metrics["failed_publishes"],
            "failed_subscriptions": self._metrics["failed_subscriptions"],
            "avg_publish_latency_ms": avg_publish_latency,
            "avg_subscribe_latency_ms": avg_subscribe_latency,
            "dead_letter_count": self._metrics["dead_letter_count"],
            "retry_count": self._metrics["retry_count"],
            "uptime_seconds": int(time.time() - self._start_time) if self._start_time else 0,
            "connected": self._connected,
        }

    def reset_metrics(self) -> None:
        """
        重置事件总线指标
        """
        self._metrics = {
            "published_events": 0,
            "subscribed_events": 0,
            "failed_publishes": 0,
            "failed_subscriptions": 0,
            "publish_latency": [],
            "subscribe_latency": [],
            "dead_letter_count": 0,
            "retry_count": 0,
        }
        logger.info("事件总线指标已重置")

    async def health_check(self) -> dict[str, Any]:
        """
        健康检查

        Returns:
            健康状态字典
        """
        return {
            "status": "healthy" if self._connected else "unhealthy",
            "uptime_seconds": int(time.time() - self._start_time) if self._start_time else 0,
            "metrics": self.get_metrics(),
        }

    # ==================== 内部辅助方法 ====================

    def _record_publish_metrics(self, start_time: float, success: bool) -> None:
        """
        记录发布指标

        Args:
            start_time: 开始时间
            success: 是否成功
        """
        latency = (time.time() - start_time) * 1000  # 转换为毫秒
        self._metrics["publish_latency"].append(latency)
        if len(self._metrics["publish_latency"]) > 1000:
            # 只保留最近的 1000 个数据点
            self._metrics["publish_latency"] = self._metrics["publish_latency"][-1000:]

        if success:
            self._metrics["published_events"] += 1
        else:
            self._metrics["failed_publishes"] += 1

    def _record_subscribe_metrics(self, start_time: float, success: bool) -> None:
        """
        记录订阅指标

        Args:
            start_time: 开始时间
            success: 是否成功
        """
        latency = (time.time() - start_time) * 1000  # 转换为毫秒
        self._metrics["subscribe_latency"].append(latency)
        if len(self._metrics["subscribe_latency"]) > 1000:
            # 只保留最近的 1000 个数据点
            self._metrics["subscribe_latency"] = self._metrics["subscribe_latency"][-1000:]

        if success:
            self._metrics["subscribed_events"] += 1
        else:
            self._metrics["failed_subscriptions"] += 1

    def _record_retry(self) -> None:
        """
        记录重试
        """
        self._metrics["retry_count"] += 1

    # ==================== 便捷方法 ====================

    async def emit_state_change(
        self,
        session_id: str,
        old_state: str,
        new_state: str,
        retry_count: int = 3,
    ) -> str:
        """发送状态变更事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.STATE_CHANGE,
                session_id=session_id,
                data={
                    "old_state": old_state,
                    "new_state": new_state,
                },
            ),
            retry_count=retry_count,
        )

    async def emit_step_start(
        self,
        session_id: str,
        step_id: str,
        step_name: str,
        retry_count: int = 3,
    ) -> str:
        """发送步骤开始事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.STEP_START,
                session_id=session_id,
                data={
                    "step_id": step_id,
                    "step_name": step_name,
                },
            ),
            retry_count=retry_count,
        )

    async def emit_step_complete(
        self,
        session_id: str,
        step_id: str,
        result: dict[str, Any] | None = None,
        retry_count: int = 3,
    ) -> str:
        """发送步骤完成事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.STEP_COMPLETE,
                session_id=session_id,
                data={
                    "step_id": step_id,
                    "result": result or {},
                },
            ),
            retry_count=retry_count,
        )

    async def emit_step_error(
        self,
        session_id: str,
        step_id: str,
        error: str,
        error_type: str | None = None,
        retry_count: int = 3,
    ) -> str:
        """发送步骤错误事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.STEP_ERROR,
                session_id=session_id,
                data={
                    "step_id": step_id,
                    "error": error,
                    "error_type": error_type,
                },
            ),
            retry_count=retry_count,
        )

    async def emit_approval_request(
        self,
        session_id: str,
        request_id: str,
        operation: str,
        description: str,
        retry_count: int = 3,
    ) -> str:
        """发送审批请求事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.APPROVAL_REQUEST,
                session_id=session_id,
                data={
                    "request_id": request_id,
                    "operation": operation,
                    "description": description,
                },
            ),
            retry_count=retry_count,
        )

    async def emit_checkpoint_saved(
        self,
        session_id: str,
        checkpoint_id: str,
        step_id: str,
        retry_count: int = 3,
    ) -> str:
        """发送检查点保存事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.CHECKPOINT_SAVED,
                session_id=session_id,
                data={
                    "checkpoint_id": checkpoint_id,
                    "step_id": step_id,
                },
            ),
            retry_count=retry_count,
        )

    async def emit_execution_complete(
        self,
        session_id: str,
        result: dict[str, Any] | None = None,
        retry_count: int = 3,
    ) -> str:
        """发送执行完成事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.EXECUTION_COMPLETE,
                session_id=session_id,
                data={
                    "result": result or {},
                },
            ),
            retry_count=retry_count,
        )

    async def emit_tool_call_start(
        self,
        session_id: str,
        call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        retry_count: int = 3,
    ) -> str:
        """发送工具调用开始事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.TOOL_CALL_START,
                session_id=session_id,
                data={
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                },
            ),
            retry_count=retry_count,
        )

    async def emit_tool_call_end(
        self,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: Any,
        success: bool = True,
        error: str | None = None,
        duration_ms: int | None = None,
        retry_count: int = 3,
    ) -> str:
        """发送工具调用结束事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.TOOL_CALL_END,
                session_id=session_id,
                data={
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "result": result,
                    "success": success,
                    "error": error,
                    "duration_ms": duration_ms,
                },
            ),
            retry_count=retry_count,
        )

    async def emit_stream_chunk(
        self,
        session_id: str,
        message_id: str,
        chunk: str,
        retry_count: int = 3,
    ) -> str:
        """发送流式输出片段事件"""
        return await self.publish(
            ExecutionEvent(
                event_type=EventType.STREAM_CHUNK,
                session_id=session_id,
                data={
                    "message_id": message_id,
                    "chunk": chunk,
                },
            ),
            retry_count=retry_count,
        )

    async def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        session_id: str = "default",
        retry_count: int = 3,
    ) -> str:
        """
        通用事件发送方法

        Args:
            event_type: 事件类型字符串
            data: 事件数据
            session_id: 会话ID，默认为 "default"
            retry_count: 重试次数

        Returns:
            事件ID
        """
        try:
            event_enum = EventType(event_type)
        except ValueError:
            normalized = event_type.replace(".", "_")
            try:
                event_enum = EventType(normalized)
            except ValueError:
                event_enum = EventType.CUSTOM
                data["custom_event_type"] = event_type

        data_summary = str(data)[:200] if data else ""
        logger.info(
            "[EventBus] emit | raw_type=%s | resolved_enum=%s | session_id=%s | data_summary=%s",
            event_type,
            event_enum.value,
            session_id,
            data_summary,
        )

        _normalized_type = event_type.replace(".", "_")
        _priority = EventPriority.NORMAL
        if _normalized_type.startswith("task"):
            _priority = EventPriority.HIGH

        return await self.publish(
            ExecutionEvent(
                event_type=event_enum,
                session_id=session_id,
                data=data,
                priority=_priority,
            ),
            retry_count=retry_count,
        )

    def subscribe_simple(
        self,
        event_type: str,
        handler: "EventHandler",
        consumer_group: str | None = None,
    ) -> str:
        """
        简化的事件订阅方法

        通过事件类型字符串快速订阅事件，无需手动创建 EventFilter。
        支持多种格式：点号格式（task.submitted）和下划线格式（task_submitted）。
        对于未定义的事件类型，自动作为自定义事件类型处理。

        Args:
            event_type: 事件类型字符串（如 "task.submitted", "task_submitted"）
            handler: 事件处理函数
            consumer_group: 消费者组名称（可选）

        Returns:
            订阅 ID

        Example:
            event_bus.subscribe_simple("task.submitted", self._on_task_submitted)
        """
        try:
            event_enum = EventType(event_type)
            event_filter = EventFilter(event_types=[event_enum])
        except ValueError:
            normalized = event_type.replace(".", "_")
            try:
                event_enum = EventType(normalized)
                event_filter = EventFilter(event_types=[event_enum])
            except ValueError:
                event_filter = EventFilter(
                    event_types=[EventType.CUSTOM],
                    custom_event_types=[event_type, normalized],
                )

        return self.subscribe(handler, filter=event_filter, consumer_group=consumer_group)
