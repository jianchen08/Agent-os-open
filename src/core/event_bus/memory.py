"""
内存事件总线实现

用于开发/测试环境，或不需要跨进程通信的场景
保持与 Redis Streams 版本相同的 API
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any

from src.core.event_bus.base import EventBusBase
from src.core.event_bus.types import (
    EventFilter,
    EventHandler,
    EventPriority,
    EventType,
    ExecutionEvent,
    Subscription,
)

logger = logging.getLogger(__name__)

_DEBUG_LOG_FILE = "debug_event_delivery.log"


def _debug_log(msg: str) -> None:
    """写入调试日志到专用文件，避免被控制台输出截断。"""
    import datetime  # noqa: PLC0415

    try:
        with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n")
    except Exception:
        pass


class InMemoryEventBus(EventBusBase):
    """
    内存事件总线

    特性：
    - 单进程内事件通信
    - 无持久化
    - 用于开发/测试
    - API 与 Redis Streams 版本兼容
    """

    def __init__(self, history_size: int = 100, batch_size: int = 100):
        """
        初始化内存事件总线

        Args:
            history_size: 历史事件保留数量
            batch_size: 批处理大小
        """
        super().__init__()
        self._subscriptions: dict[str, Subscription] = {}
        self._history: deque = deque(maxlen=history_size)
        self._history_size = history_size
        self._message_counter = 0
        self._batch_size = batch_size
        self._batch_queue: list = []
        self._batch_timer: asyncio.TimerHandle | None = None

    async def connect(self) -> None:
        """建立连接（内存模式无需实际连接）"""
        await super().connect()
        logger.info("内存事件总线已启动")

    async def disconnect(self) -> None:
        """断开连接"""
        # 处理批处理队列中的剩余事件
        if self._batch_queue:
            try:
                await self._process_batch()
                logger.info(f"处理了批处理队列中的 {len(self._batch_queue)} 个事件")
            except Exception as e:
                logger.error(f"处理批处理队列失败: {e}")

        # 取消批处理定时器
        if self._batch_timer:
            self._batch_timer.cancel()
            self._batch_timer = None

        # 清除订阅
        self._subscriptions.clear()

        # 调用父类的 disconnect 方法
        await super().disconnect()
        logger.info("内存事件总线已停止")

    async def publish(self, event: ExecutionEvent, retry_count: int = 3) -> str:
        """
        发布事件

        Args:
            event: 执行事件
            retry_count: 重试次数

        Returns:
            事件 ID
        """
        _debug_log(
            f"publish | event_type={event.event_type.value} | priority={event.priority.value} | HIGH_threshold={EventPriority.HIGH.value}"
        )
        if event.priority.value >= EventPriority.HIGH.value:
            _debug_log("publish -> _publish_direct (HIGH priority)")
            return await self._publish_direct(event)

        _debug_log("publish -> _publish_batch (normal priority)")
        return await self._publish_batch(event)

    async def _publish_direct(self, event: ExecutionEvent) -> str:
        """
        直接发布事件（用于高优先级事件）

        Args:
            event: 执行事件

        Returns:
            事件 ID
        """
        _debug_log(f"_publish_direct | event_type={event.event_type.value} | event_id={event.event_id}")
        start_time = time.time()
        success = False

        try:
            self._message_counter += 1
            message_id = f"{int(event.timestamp.timestamp() * 1000)}-{self._message_counter}"

            self._history.append(event)

            await self._notify_subscribers(event)

            logger.debug(f"事件已发布 | type={event.event_type.value} | session={event.session_id} | id={message_id}")

            success = True
            return message_id
        finally:
            # 记录指标
            self._record_publish_metrics(start_time, success)

    async def _publish_batch(self, event: ExecutionEvent) -> str:
        """
        批量发布事件（用于普通优先级事件）

        Args:
            event: 执行事件

        Returns:
            事件 ID
        """
        # 添加到批处理队列
        self._batch_queue.append(event)

        # 检查是否需要立即处理批次
        if len(self._batch_queue) >= self._batch_size:
            await self._process_batch()
        else:
            # 检查是否在测试环境中，如果是，立即处理批次
            import os  # noqa: PLC0415

            if os.environ.get("PYTEST_CURRENT_TEST"):
                await self._process_batch()
            else:
                # 设置定时器，确保批次会被处理
                self._ensure_batch_timer()

        return event.event_id

    def _ensure_batch_timer(self) -> None:
        """
        确保批处理定时器已设置
        """
        if self._batch_timer is None or self._batch_timer.cancelled():
            loop = asyncio.get_event_loop()
            self._batch_timer = loop.call_later(0.1, lambda: asyncio.create_task(self._process_batch()))

    async def _process_batch(self) -> None:
        """
        处理批处理队列中的事件
        """
        if not self._batch_queue:
            return

        # 复制并清空队列
        batch = self._batch_queue.copy()
        self._batch_queue.clear()

        # 重置定时器
        if self._batch_timer:
            self._batch_timer.cancel()
            self._batch_timer = None

        # 处理批次
        start_time = time.time()
        success_count = 0

        try:
            # 保存到历史
            for event in batch:
                self._history.append(event)

            # 批量通知订阅者
            for event in batch:
                try:
                    await self._notify_subscribers(event)
                    success_count += 1
                except Exception as e:
                    logger.error(f"处理批处理事件失败: {e}")

            # 记录指标
            for event in batch:
                event_success = event in batch[:success_count]
                self._record_publish_metrics(start_time, event_success)

            logger.debug(f"批量处理完成 | 事件数量: {len(batch)} | 成功: {success_count}")
        except Exception as e:
            logger.error(f"处理批处理队列失败: {e}")
            # 记录失败指标
            for event in batch:  # noqa: B007
                self._record_publish_metrics(start_time, False)

    async def _notify_subscribers(self, event: ExecutionEvent) -> None:
        """
        通知所有匹配的订阅者

        Args:
            event: 执行事件
        """
        _debug_log(
            f"_notify_subscribers | event_type={event.event_type.value} | subs={len(self._subscriptions)} | event_id={event.event_id}"
        )
        matched_count = 0
        total_count = len(self._subscriptions)
        for sub_id, subscription in self._subscriptions.items():
            if subscription.filter and not subscription.filter.matches(event):
                _debug_log(
                    f"  SKIP sub={sub_id} | filter_types={[t.value for t in (subscription.filter.event_types or [])]} | event_type={event.event_type.value}"
                )
                continue

            matched_count += 1
            handler_name = getattr(subscription.handler, "__qualname__", None) or getattr(
                subscription.handler, "__name__", str(subscription.handler)
            )
            _debug_log(f"  MATCH sub={sub_id} | handler={handler_name} | event_type={event.event_type.value}")
            try:
                asyncio.create_task(self._safe_call(subscription.handler, event))
            except Exception as e:
                logger.error(f"创建事件处理任务失败: {e}")

        logger.info(
            f"[InMemoryEventBus] 事件分发 | type={event.event_type.value} "
            f"| matched={matched_count}/{total_count} | event_id={event.event_id}"
        )

    async def _safe_call(
        self,
        handler: EventHandler,
        event: ExecutionEvent,
    ) -> None:
        """安全调用处理器"""
        handler_name = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", str(handler))
        logger.info(
            f"[InMemoryEventBus] 调用处理器 | handler={handler_name} "
            f"| event_type={event.event_type.value} | event_id={event.event_id}"
        )
        try:
            await handler(event)
        except Exception as e:
            data_summary = str(event.data)[:200] if event.data else ""
            logger.error(
                f"[InMemoryEventBus] 处理器异常 | handler={handler_name} "
                f"| event_type={event.event_type.value} | event_id={event.event_id} "
                f"| data_summary={data_summary} | error={e}"
            )

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
            consumer_group: 消费者组名称

        Returns:
            订阅 ID
        """
        subscription_id = f"sub_{uuid.uuid4().hex[:8]}"

        self._subscriptions[subscription_id] = Subscription(
            id=subscription_id,
            handler=handler,
            filter=filter,
            consumer_group=consumer_group,
        )

        logger.debug(f"新订阅: {subscription_id}")
        return subscription_id

    def unsubscribe(
        self,
        subscription_id: str,
    ) -> bool:
        """
        取消订阅

        Args:
            subscription_id: 订阅 ID

        Returns:
            是否成功取消
        """
        if subscription_id in self._subscriptions:
            del self._subscriptions[subscription_id]
            logger.debug(f"取消订阅: {subscription_id}")
            return True
        return False

    async def acknowledge(self, event_id: str, consumer_group: str) -> bool:
        """确认消息（内存模式无需确认）"""
        return True

    async def get_history(
        self,
        session_id: str | None = None,
        event_type: EventType | None = None,
        limit: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[ExecutionEvent]:
        """获取事件历史"""
        events = list(self._history)

        # 按会话过滤
        if session_id:
            events = [e for e in events if e.session_id == session_id]

        # 按事件类型过滤
        if event_type:
            events = [e for e in events if e.event_type == event_type]

        # 限制数量
        return events[-limit:]

    async def get_pending_events(
        self,
        consumer_group: str,
        consumer_name: str | None = None,
    ) -> list[ExecutionEvent]:
        """获取待处理事件（内存模式无待处理概念）"""
        return []

    async def get_dead_letter_events(self, limit: int = 100) -> list[ExecutionEvent]:
        """获取死信队列中的事件（内存模式无死信队列）"""
        return []

    async def retry_dead_letter_event(self, event_id: str) -> bool:
        """重试死信队列中的事件（内存模式无死信队列）"""
        return False

    async def clear_dead_letter_queue(self) -> int:
        """清空死信队列（内存模式无死信队列）"""
        return 0

    def clear_history(self) -> None:
        """清除历史"""
        self._history.clear()

    def has_subscribers(self, event_type: str) -> bool:
        """检查指定事件类型是否有订阅者。

        通过归一化事件类型字符串（点号→下划线）与订阅过滤器的 event_types 匹配，
        与 subscribe_simple / emit 的归一化逻辑保持一致。

        Args:
            event_type: 事件类型字符串（如 "task.submitted", "task_submitted"）

        Returns:
            是否存在匹配的订阅者
        """
        normalized = event_type.replace(".", "_")
        for subscription in self._subscriptions.values():
            if subscription.filter is None:
                return True
            if subscription.filter.event_types:
                for et in subscription.filter.event_types:
                    if et.value in (normalized, event_type):
                        return True
            if subscription.filter.custom_event_types:  # noqa: SIM102
                if (
                    event_type in subscription.filter.custom_event_types
                    or normalized in subscription.filter.custom_event_types
                ):
                    return True
        return False

    @property
    def subscriber_count(self) -> int:
        """订阅者总数"""
        return len(self._subscriptions)

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            "events_published": self._metrics["published_events"],
            "events_processed": self._metrics["subscribed_events"],
            "events_failed": self._metrics["failed_publishes"],
            "subscriber_count": len(self._subscriptions),
            "event_types": list(
                {
                    sub.filter.event_types[0].value
                    for sub in self._subscriptions.values()
                    if sub.filter and sub.filter.event_types
                }
            )
            if self._subscriptions
            else [],
            "history_size": len(self._history),
        }
